"""输入框命令的会话控制接口；不写入用户消息，不触发模型或重新启动任务。"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import VersionConflict
from spiritscout.core.models import BlackboardLayer
from spiritscout.gateway.manual_context import REQUEST_KEY, RESULT_KIND
from spiritscout.web.sessions import SessionManager


class CompactRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z0-9_-]+$")


def mount_command_routes(app: FastAPI, manager: SessionManager) -> None:
    def require(session_id: str):
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return session

    @app.post("/api/sessions/{session_id}/compact")
    async def compact(session_id: str, payload: CompactRequest):
        session = require(session_id)
        if session.request.get("side_agent"):
            raise HTTPException(409, "请在主任务会话中压缩执行上下文")
        board = Blackboard(session.bb_path)
        try:
            # 重试沿用身份，即使后来已有新命令也不能重新激活旧请求。
            while True:
                saved = board.read_versioned(BlackboardLayer.TOPOLOGY, REQUEST_KEY)
                previous, version = saved if saved else ({}, 0)
                requests = dict(previous.get("requests", {}))
                if payload.request_id in requests:
                    request = {"request_id": payload.request_id, "requested_at": requests[payload.request_id]}
                    break
                request = {"request_id": payload.request_id, "requested_at": datetime.now(UTC).isoformat()}
                requests[payload.request_id] = request["requested_at"]
                try:
                    board.write(BlackboardLayer.TOPOLOGY, REQUEST_KEY, {**request, "requests": requests},
                                updated_by="user-command", expected_version=version)
                except VersionConflict:
                    continue
                AuditLogger(session.audit_path).log("context.compaction_requested", "user", request)
                break
            return {**request, "status": "queued"}
        finally:
            board.close()

    @app.get("/api/sessions/{session_id}/context")
    def context_status(session_id: str):
        session = require(session_id)
        board = Blackboard(session.bb_path)
        try:
            request = board.read(BlackboardLayer.TOPOLOGY, REQUEST_KEY)
            results = [row["data"] for row in board.query(BlackboardLayer.TOPOLOGY,
                data_filter=("kind", RESULT_KIND), limit=200)
                if request and row["data"].get("request_id") == request["request_id"]]
            return {"session": session.brief(include_summary=False),
                    "request": {k: request[k] for k in ("request_id", "requested_at") if k in request} if request else None,
                    "results": results}
        finally:
            board.close()
