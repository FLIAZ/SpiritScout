"""定时任务持久化、幂等投递、忙碌/暂停等待与接口校验；不调用真实模型。"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.core.pause import PauseGate
from spiritscout.web.scheduler import ScheduleInput, Scheduler, mount_scheduler


class Session:
    def __init__(self, root):
        self.session_id = "fixture"
        self.status = "completed"
        self.pause_gate = PauseGate()
        self.handled_user_messages = set()
        self.statements = {}
        self.audit_path = root / "audit.jsonl"

    def display_title(self):
        return "本机验证会话"

    def user_statement(self, key):
        return self.statements.get(key)

    def remember_user_message(self, text, *, message_id, enqueue):
        self.statements[message_id] = {"content": text}


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = Session(self.root)
        self.manager = SimpleNamespace(
            get=lambda sid: self.session if sid == "fixture" else None,
            _persist=lambda session: None,
            resume=AsyncMock(),
        )
        self.scheduler = Scheduler(self.root, self.manager)
        self.now = time.time()

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.temp.cleanup()

    def create(self, interval=None):
        return self.scheduler.create(
            ScheduleInput(
                session_id="fixture",
                name="检查",
                message="检查已授权的服务",
                run_at=datetime.fromtimestamp(self.now - 100, tz=timezone.utc),
                interval_seconds=interval,
            )
        )

    async def test_once_is_durable_and_only_fires_once(self):
        row = self.create()
        await self.scheduler.tick(self.now)
        await self.scheduler.tick(self.now + 10)
        self.assertEqual(self.manager.resume.await_count, 1)
        self.assertEqual(len(self.session.statements), 1)
        stored = Scheduler(self.root, self.manager).rows()[0]
        self.assertFalse(stored["enabled"])
        self.assertEqual(stored["state"], "completed")
        self.scheduler.change(row["id"], True)
        await self.scheduler.tick(time.time() + 1)
        self.assertEqual(
            self.manager.resume.await_count, 2, "显式重新启用单次计划可再次执行"
        )

    async def test_interval_skips_missed_ticks_and_no_concurrent_duplicates(self):
        self.create(60)

        async def resume(sid):
            await asyncio.sleep(0.02)

        self.manager.resume.side_effect = resume
        await asyncio.gather(
            self.scheduler.tick(self.now), self.scheduler.tick(self.now)
        )
        self.assertEqual(self.manager.resume.await_count, 1)
        stored = self.scheduler.rows()[0]
        self.assertTrue(stored["enabled"])
        self.assertGreater(stored["next_run"], self.now)
        self.assertLessEqual(stored["next_run"], self.now + 60)

    async def test_running_and_manually_paused_sessions_do_not_start(self):
        self.create(60)
        self.session.status = "running"
        await self.scheduler.tick(self.now)
        self.manager.resume.assert_not_awaited()
        self.session.status = "completed"
        self.session.pause_gate.pause()
        await self.scheduler.tick(self.now + 6)
        self.assertEqual(self.scheduler.rows()[0]["state"], "waiting")
        self.manager.resume.assert_not_awaited()
        self.session.pause_gate.resume()
        await self.scheduler.tick(self.now + 12)
        self.manager.resume.assert_awaited_once()

    async def test_retry_reuses_message_and_deleted_session_is_disabled(self):
        self.create()
        self.manager.resume.side_effect = ValueError("fixture unavailable")
        await self.scheduler.tick(self.now)
        self.assertEqual(self.scheduler.rows()[0]["state"], "error")
        self.manager.resume.side_effect = None
        await self.scheduler.tick(self.now + 31)
        self.assertEqual(len(self.session.statements), 1)
        row = self.create(60)
        self.manager.get = lambda sid: None
        await self.scheduler.tick(self.now + 40)
        stored = next(r for r in self.scheduler.rows() if r["id"] == row["id"])
        self.assertFalse(stored["enabled"])

    async def test_pending_handled_message_is_not_reexecuted_after_recovery(self):
        row = self.create()
        key = f"schedule-{row['id']}-{int(row['next_run'] * 1000000)}"
        self.session.statements[key] = {"content": "done"}
        self.session.handled_user_messages.add(key)
        await self.scheduler.tick(self.now)
        self.manager.resume.assert_not_awaited()


class SchedulerAPITests(unittest.TestCase):
    def test_create_switch_delete_and_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = Session(root)
            manager = SimpleNamespace(
                get=lambda sid: session if sid == "fixture" else None
            )
            app = FastAPI()
            mount_scheduler(app, root, manager)
            with TestClient(app) as client:
                body = {
                    "session_id": "fixture",
                    "name": "每日检查",
                    "message": "检查服务",
                    "run_at": "2099-01-01T08:00:00+08:00",
                    "interval_seconds": 86400,
                }
                response = client.post("/api/schedules", json=body)
                self.assertEqual(response.status_code, 200, response.text)
                key = response.json()["id"]
                self.assertEqual(
                    client.get("/api/schedules").headers["cache-control"], "no-store"
                )
                self.assertEqual(
                    client.patch(
                        "/api/schedules/" + key, json={"enabled": False}
                    ).status_code,
                    200,
                )
                self.assertFalse(
                    client.get("/api/schedules").json()["items"][0]["enabled"]
                )
                self.assertEqual(
                    client.post(
                        "/api/schedules", json={**body, "run_at": "2099-01-01T08:00:00"}
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.post(
                        "/api/schedules", json={**body, "interval_seconds": 0}
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.post(
                        "/api/schedules", json={**body, "session_id": "foreign"}
                    ).status_code,
                    422,
                )
                self.assertEqual(
                    client.delete("/api/schedules/" + key).status_code, 200
                )
                self.assertEqual(client.get("/api/schedules").json()["items"], [])


class StandaloneAutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = {}
        self.created = []
        self.attempts = []
        self.fail_after_create = False

        async def launch(payload, run_id):
            self.attempts.append(run_id)
            sid = "sess-auto-" + run_id
            if sid not in self.sessions:
                self.created.append(dict(payload))
                self.sessions[sid] = SimpleNamespace(
                    session_id=sid,
                    status="running",
                    display_title=lambda: payload["title"],
                )
                if self.fail_after_create:
                    self.fail_after_create = False
                    raise RuntimeError("fixture interrupted after creation")
            return {"session_id": sid}

        self.scheduler = Scheduler(
            self.root, SimpleNamespace(get=self.sessions.get), launch=launch
        )
        self.now = time.time()

    async def asyncTearDown(self):
        await self.scheduler.close()
        self.temp.cleanup()

    def create(self, interval=60):
        return self.scheduler.create(
            ScheduleInput(
                name="独立工作",
                message="总结指定项目资料",
                mode="ctf",
                model="fixture-model",
                project_id="fixture-project",
                run_at=datetime.fromtimestamp(self.now - 1, tz=timezone.utc),
                interval_seconds=interval,
            )
        )

    async def test_no_existing_session_required_and_each_run_gets_new_context(self):
        row = self.create()
        self.sessions["unrelated"] = SimpleNamespace(status="running")
        await self.scheduler.tick(self.now)
        self.assertEqual(len(self.created), 1)
        first = self.scheduler.history(row["id"])[0]["session_id"]
        self.assertEqual(self.created[0]["mission"], "总结指定项目资料")
        self.assertEqual(self.created[0]["mode"], "ctf")
        self.assertEqual(self.created[0]["model"], "fixture-model")
        self.assertEqual(self.scheduler.rows()[0]["kind"], "standalone")
        self.sessions[first].status = "completed"
        await self.scheduler.tick(self.now + 61)
        history = self.scheduler.history(row["id"])
        self.assertEqual(len(history), 2)
        self.assertNotEqual(history[0]["session_id"], history[1]["session_id"])
        self.assertEqual(len(self.created), 2)

    async def test_only_same_automation_overlap_is_deferred(self):
        row = self.create()
        await self.scheduler.tick(self.now)
        await self.scheduler.tick(self.now + 61)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.scheduler.rows()[0]["state"], "waiting")
        first = self.scheduler.history(row["id"])[0]["session_id"]
        self.sessions[first].status = "completed"
        await self.scheduler.tick(self.now + 67)
        self.assertEqual(len(self.created), 2)

    async def test_startup_retry_reuses_occurrence_identity_without_duplicate_execution(
        self,
    ):
        self.create(None)
        self.fail_after_create = True
        await self.scheduler.tick(self.now)
        self.assertEqual(self.scheduler.rows()[0]["state"], "error")
        await self.scheduler.tick(self.now + 31)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.attempts[0], self.attempts[1])
        self.assertFalse(self.scheduler.rows()[0]["enabled"])

    async def test_api_creates_standalone_without_session_id(self):
        app = FastAPI()
        manager = SimpleNamespace(get=lambda sid: None)
        mount_scheduler(app, self.root, manager)
        with TestClient(app) as client:
            response = client.post(
                "/api/schedules",
                json={
                    "name": "每日工作",
                    "message": "自主完成这项工作",
                    "mode": "ctf",
                    "run_at": "2099-01-01T08:00:00+08:00",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["kind"], "standalone")
            self.assertEqual(response.json()["session_id"], "")
            self.assertEqual(response.json()["task"]["mission"], "自主完成这项工作")
            self.assertEqual(
                client.get("/api/schedules/" + response.json()["id"] + "/runs").json()[
                    "items"
                ],
                [],
            )


class SchedulerMigrationTests(unittest.TestCase):
    def test_legacy_session_plan_is_preserved_when_adding_standalone_templates(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "scheduled-tasks"
            folder.mkdir()
            with contextlib.closing(sqlite3.connect(folder / "schedules.db")) as db:
                db.execute(
                    "CREATE TABLE schedules(id TEXT PRIMARY KEY,session_id TEXT,name TEXT,message TEXT,next_run REAL,interval_seconds INTEGER,enabled INTEGER,state TEXT,error TEXT,last_run REAL,retry_at REAL)"
                )
                db.execute(
                    "INSERT INTO schedules VALUES('old','fixture','旧计划','原始消息',9999999999,3600,1,'ready','',NULL,0)"
                )
                db.commit()
            session = SimpleNamespace(display_title=lambda: "原会话")
            scheduler = Scheduler(root, SimpleNamespace(get=lambda sid: session))
            row = scheduler.rows()[0]
            self.assertEqual(row["message"], "原始消息")
            self.assertEqual(row["session_id"], "fixture")
            self.assertEqual(row["kind"], "existing_session")
            self.assertEqual(row["task"], {})
