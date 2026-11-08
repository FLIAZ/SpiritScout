"""更新 API 沿用控制台认证；健康回执另用一次性更新令牌。"""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from spiritscout.updates.controller import (
    ACTIVE_PHASES,
    Updater,
    release_claim,
    write_json,
)
from spiritscout.updates.source import containerized


class UpdateApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag: str = Field(min_length=1, max_length=64, pattern=r"^v?\d+\.\d+\.\d+$")


class BridgeHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bridge_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    ready: bool
    reason: str = Field(default="", max_length=300)


class BridgeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    phase: str = Field(
        pattern=r"^(preparing|backup|switching|restarting|completed|error|recovery_required)$"
    )
    message: str = Field(max_length=300)


def mount_updates(app: FastAPI, cfg, manager) -> Updater:
    updater = Updater(cfg, manager)
    app.state.updater = updater

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/api/updates")
    async def status(details: bool = False):
        return response(await updater.snapshot(details=details))

    @app.post("/api/updates/check")
    async def check():
        await updater.check(force=True)
        return response(await updater.snapshot(details=True))

    @app.post("/api/updates/apply")
    async def apply(payload: UpdateApply):
        try:
            return response(await updater.apply(payload.tag))
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/updates/health", include_in_schema=False)
    async def health(request: Request):
        value = updater.health(request.headers.get("X-SpiritScout-Update", ""))
        if value is None:
            raise HTTPException(404, "不存在")
        return response(value)

    def bridge_auth(request: Request):
        value = updater.bridge()
        if (
            not containerized()
            or request.headers.get("X-SpiritScout-Bridge") != value.get("bridge_id")
            or not value
        ):
            raise HTTPException(409, "宿主更新器未连接或身份已变化")
        return value

    @app.post("/api/updates/host/heartbeat", include_in_schema=False)
    async def heartbeat(payload: BridgeHeartbeat):
        if not containerized():
            raise HTTPException(409, "此实例不是容器部署")
        job = updater.job()
        if (
            job.get("phase") in ACTIVE_PHASES
            and job.get("bridge_id") != payload.bridge_id
        ):
            raise HTTPException(409, "已有宿主更新器正在执行更新")
        write_json(
            updater.root / "bridge.json",
            {**payload.model_dump(), "seen_at": time.time()},
        )
        return response({"ok": True})

    @app.get("/api/updates/host/job", include_in_schema=False)
    async def host_job(request: Request):
        bridge = bridge_auth(request)
        job = updater.job()
        return response(
            job
            if job.get("adapter") == "container"
            and job.get("bridge_id") == bridge["bridge_id"]
            and job.get("phase") in ACTIVE_PHASES
            else {}
        )

    @app.post("/api/updates/host/report", include_in_schema=False)
    async def report(payload: BridgeReport, request: Request):
        bridge = bridge_auth(request)
        job = updater.job()
        if payload.id != job.get("id") or job.get("bridge_id") != bridge["bridge_id"]:
            raise HTTPException(409, "更新任务已变化")
        if job.get("phase") not in ACTIVE_PHASES and payload.phase != job.get("phase"):
            raise HTTPException(409, "该更新已经结束，不能重新执行")
        job.update(**payload.model_dump(), updated_at=time.time())
        write_json(updater.root / "job.json", job)
        if payload.phase in {"completed", "error", "recovery_required"}:
            release_claim(updater.root, job["id"])
        return response({"ok": True})

    return updater
