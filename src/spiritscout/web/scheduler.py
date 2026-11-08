"""持久化独立自动任务与逐轮结果，兼容旧会话计划，不重放后台命令。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from spiritscout.core.audit import AuditLogger
from spiritscout.web.sessions import TERMINAL_STATES


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(
        default="", max_length=100
    )  # 仅保留旧计划兼容，新建界面不再绑定会话。
    name: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)
    run_at: AwareDatetime
    interval_seconds: int | None = Field(default=None, ge=60)
    mode: Literal["src", "ctf", "competition"] = "src"
    model: str = Field(default="", max_length=512)
    project_id: str = Field(default="", max_length=64)
    target: str = Field(default="", max_length=2000)
    scope: list[str] = Field(default_factory=list, max_length=100)


class ScheduleSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class Scheduler:
    def __init__(self, root: Path, manager: Any, *, launch=None):
        self.folder = root / "scheduled-tasks"
        self.manager = manager
        self.launch = launch
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()

    @contextlib.contextmanager
    def connect(self):
        if self.folder.is_symlink():
            raise ValueError("定时任务目录不能是符号链接")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.folder / "schedules.db"
        if path.is_symlink():
            raise ValueError("定时任务索引不能是符号链接")
        with contextlib.closing(sqlite3.connect(path, timeout=2)) as db:
            path.chmod(0o600)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, name TEXT NOT NULL,
                message TEXT NOT NULL, next_run REAL NOT NULL, interval_seconds INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'ready',
                error TEXT NOT NULL DEFAULT '', last_run REAL, retry_at REAL NOT NULL DEFAULT 0)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(schedules)")}
            if "task_json" not in columns:
                try:
                    db.execute(
                        "ALTER TABLE schedules ADD COLUMN task_json TEXT NOT NULL DEFAULT '{}'"
                    )
                except sqlite3.OperationalError:
                    if "task_json" not in {
                        row[1] for row in db.execute("PRAGMA table_info(schedules)")
                    }:
                        raise
            db.execute("""CREATE TABLE IF NOT EXISTS schedule_runs (
                run_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, due REAL NOT NULL,
                created_at REAL NOT NULL, session_id TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '')""")
            with db:
                yield db

    def rows(self, *, due: float | None = None, include_runs: bool = True):
        with self.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM schedules "
                    + (
                        "WHERE enabled=1 AND next_run<=? AND retry_at<=? "
                        if due is not None
                        else ""
                    )
                    + "ORDER BY next_run,id",
                    (due, due) if due is not None else (),
                )
            ]
        for row in rows:
            session = self.manager.get(row["session_id"])
            row["session_title"] = (
                (session.display_title() if session else "会话已删除")
                if row["session_id"]
                else "独立自动任务"
            )
            row["enabled"] = bool(row["enabled"])
            row["task"] = json.loads(row.pop("task_json"))
            row["kind"] = "existing_session" if row["session_id"] else "standalone"
            row["runs"] = self.history(row["id"], limit=5) if include_runs else []
        return rows

    def history(self, key: str, *, limit: int = 20):
        with self.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY created_at DESC LIMIT ?",
                    (key, limit),
                )
            ]
        for row in rows:
            session = self.manager.get(row["session_id"]) if row["session_id"] else None
            row["status"] = session.status if session else row["state"]
        return rows

    def create(self, payload: ScheduleInput):
        if payload.session_id and not self.manager.get(payload.session_id):
            raise ValueError("会话不存在")
        if not payload.name.strip() or not payload.message.strip():
            raise ValueError("名称和任务消息不能为空")
        key = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO schedules(id,session_id,name,message,next_run,interval_seconds,task_json) VALUES(?,?,?,?,?,?,?)",
                (
                    key,
                    payload.session_id,
                    payload.name.strip(),
                    payload.message.strip(),
                    payload.run_at.timestamp(),
                    payload.interval_seconds,
                    json.dumps(
                        {
                            "mission": payload.message.strip(),
                            "mode": payload.mode,
                            "title": payload.name.strip(),
                            "model": payload.model,
                            "project_id": payload.project_id,
                            "target": payload.target,
                            "scope": payload.scope,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        return next(row for row in self.rows() if row["id"] == key)

    def change(self, key: str, enabled: bool | None):
        with self.connect() as db:
            if enabled is None:
                found = db.execute("DELETE FROM schedules WHERE id=?", (key,)).rowcount
            else:
                found = db.execute(
                    "UPDATE schedules SET enabled=?,error='',state='ready',retry_at=0, "
                    "next_run=CASE WHEN ? AND enabled=0 AND interval_seconds IS NULL AND last_run IS NOT NULL "
                    "THEN ? ELSE next_run END WHERE id=?",
                    (enabled, enabled, time.time(), key),
                ).rowcount
        if not found:
            raise ValueError("定时任务不存在")

    def patch(self, key, **values):
        allowed = {"state", "error", "retry_at", "next_run", "last_run", "enabled"}
        if not values or not set(values) <= allowed:
            raise ValueError("无效的调度状态")
        with self.connect() as db:
            db.execute(
                "UPDATE schedules SET "
                + ",".join(name + "=?" for name in values)
                + " WHERE id=?",
                (*values.values(), key),
            )

    async def tick(self, now: float | None = None):
        if getattr(self.manager, "_update_maintenance", False):
            return
        now = time.time() if now is None else now
        rows = await asyncio.to_thread(self.rows, due=now, include_runs=False)
        for row in rows:
            if not row["enabled"] or row["next_run"] > now or row["retry_at"] > now:
                continue
            if not row["session_id"]:
                await self.fire_standalone(row, now)
                continue
            session = self.manager.get(row["session_id"])
            if session is None:
                await asyncio.to_thread(
                    self.patch,
                    row["id"],
                    enabled=False,
                    state="error",
                    error="目标会话已删除",
                )
                continue
            if session.status not in TERMINAL_STATES or session.pause_gate.paused:
                await asyncio.to_thread(
                    self.patch,
                    row["id"],
                    state="waiting",
                    error="等待目标会话结束并解除暂停",
                    retry_at=now + 5,
                )
                continue
            with self.connect() as db:
                claimed = db.execute(
                    "UPDATE schedules SET state='dispatching' WHERE id=? AND enabled=1 AND state!='dispatching' AND next_run=?",
                    (row["id"], row["next_run"]),
                ).rowcount
            if not claimed:
                continue
            message_id = f"schedule-{row['id']}-{int(row['next_run'] * 1000000)}"
            try:
                # 消息 ID 持久化，失败重试不会重复投递同一调度消息。
                existing = session.user_statement(message_id)
                if existing is None:
                    session.remember_user_message(
                        row["message"], message_id=message_id, enqueue=True
                    )
                    self.manager._persist(session)
                    AuditLogger(session.audit_path).log(
                        "user.message",
                        "scheduler",
                        {
                            "content": row["message"],
                            "message_id": message_id,
                            "schedule_id": row["id"],
                        },
                    )
                if message_id not in session.handled_user_messages:
                    await self.manager.resume(session.session_id)
                interval = row["interval_seconds"]
                next_run = (
                    row["next_run"]
                    + (math.floor(max(0, now - row["next_run"]) / interval) + 1)
                    * interval
                    if interval
                    else row["next_run"]
                )
                await asyncio.to_thread(
                    self.patch,
                    row["id"],
                    enabled=bool(interval),
                    state="ready" if interval else "completed",
                    error="",
                    last_run=now,
                    next_run=next_run,
                    retry_at=0,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 单个计划失败不能终止调度器
                await asyncio.to_thread(
                    self.patch,
                    row["id"],
                    state="error",
                    error=f"调度失败：{type(exc).__name__}",
                    retry_at=now + 30,
                )

    async def fire_standalone(self, row, now):
        latest = self.history(row["id"], limit=1)
        if latest and latest[0]["session_id"]:
            previous = self.manager.get(latest[0]["session_id"])
            if (
                previous
                and previous.status not in TERMINAL_STATES
                and latest[0]["due"] != row["next_run"]
            ):
                await asyncio.to_thread(
                    self.patch,
                    row["id"],
                    state="waiting",
                    error="上一轮自动任务仍在运行",
                    retry_at=now + 5,
                )
                return
        with self.connect() as db:
            claimed = db.execute(
                "UPDATE schedules SET state='dispatching' WHERE id=? AND enabled=1 AND state!='dispatching' AND next_run=?",
                (row["id"], row["next_run"]),
            ).rowcount
        if not claimed:
            return
        run_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"spiritscout:automation:{row['id']}:{row['next_run']!r}",
        ).hex
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO schedule_runs(run_id,schedule_id,due,created_at) VALUES(?,?,?,?)",
                (run_id, row["id"], row["next_run"], now),
            )
            existing = dict(
                db.execute(
                    "SELECT * FROM schedule_runs WHERE run_id=?", (run_id,)
                ).fetchone()
            )
        try:
            if not existing["session_id"]:
                if self.launch is None:
                    raise ValueError("独立自动任务启动器尚未装配")
                result = await self.launch(row["task"], run_id)
                with self.connect() as db:
                    db.execute(
                        "UPDATE schedule_runs SET session_id=?,state='dispatched',error='' WHERE run_id=?",
                        (result["session_id"], run_id),
                    )
            interval = row["interval_seconds"]
            next_run = (
                row["next_run"]
                + (math.floor(max(0, now - row["next_run"]) / interval) + 1) * interval
                if interval
                else row["next_run"]
            )
            await asyncio.to_thread(
                self.patch,
                row["id"],
                enabled=bool(interval),
                state="ready" if interval else "completed",
                error="",
                last_run=now,
                next_run=next_run,
                retry_at=0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 一次启动失败不停止其他自动任务
            reason = str(getattr(exc, "detail", ""))[:300] or type(exc).__name__
            with self.connect() as db:
                db.execute(
                    "UPDATE schedule_runs SET state='error',error=? WHERE run_id=?",
                    (reason, run_id),
                )
            await asyncio.to_thread(
                self.patch,
                row["id"],
                state="error",
                error=f"启动失败：{reason}",
                retry_at=now + 30,
            )

    async def run(self):
        with self.connect() as db:
            db.execute("UPDATE schedules SET state='ready' WHERE state='dispatching'")
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, sqlite3.Error):
                pass  # 页面读取仍会报告索引故障，调度器稍后重试。
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except TimeoutError:
                pass

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="session-scheduler")

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def mount_scheduler(
    app: FastAPI, root: Path, manager: Any, *, launch=None
) -> Scheduler:
    scheduler = Scheduler(root, manager, launch=launch)

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/api/schedules")
    async def schedules():
        try:
            return response({"items": await asyncio.to_thread(scheduler.rows)})
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(503, "定时任务索引暂时不可用") from exc

    @app.post("/api/schedules")
    async def create_schedule(payload: ScheduleInput):
        try:
            value = await asyncio.to_thread(scheduler.create, payload)
            scheduler.wake.set()
            return response(value)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/schedules/{schedule_id}/runs")
    async def schedule_runs(schedule_id: str):
        return response(
            {"items": await asyncio.to_thread(scheduler.history, schedule_id)}
        )

    @app.patch("/api/schedules/{schedule_id}")
    async def switch_schedule(schedule_id: str, payload: ScheduleSwitch):
        try:
            await asyncio.to_thread(scheduler.change, schedule_id, payload.enabled)
            scheduler.wake.set()
            return response({"ok": True})
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str):
        try:
            await asyncio.to_thread(scheduler.change, schedule_id, None)
            scheduler.wake.set()
            return response({"ok": True})
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    return scheduler
