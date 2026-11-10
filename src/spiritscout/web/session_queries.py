"""会话只读查询与审计事件流；复用主应用的会话查找及认证边界。"""
from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer
from spiritscout.web.events import (
    read_activity_page,
    read_audit_page,
    read_typed_page,
    stream_audit,
)
from spiritscout.web.session_state import _read_state
from spiritscout.web.sessions import TERMINAL_STATES, Session, SessionManager


def mount_session_read_routes(app: FastAPI, manager: SessionManager,
                              _require: Callable[[str], Session]) -> None:
    """挂载状态、历史、产物索引与 SSE；不变更会话状态。"""
    @app.get("/api/sessions")
    def list_sessions() -> dict[str, Any]:
        return {"sessions": [s.brief(include_summary=False)
                             for s in manager.list()]}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        session = _require(session_id)
        detail = session.brief()
        detail["request"] = session.request
        # 本题附件常驻可见：滑窗滚出首屏后，界面仍要有附件存在的凭据
        try:
            detail["attachments"] = [
                {"name": p.name, "size": p.stat().st_size}
                for p in sorted((session.workspace / "attachments").iterdir())
                if p.is_file()]
        except OSError:
            detail["attachments"] = []
        return detail

    @app.get("/api/sessions/{session_id}/conversation")
    def agent_conversation(session_id: str) -> dict[str, Any]:
        """旁路栏只读取对话与待办，不重建主会话黑板快照。"""
        session = _require(session_id)
        if not session.request.get("side_agent"):
            raise HTTPException(422, "该会话不是子 Agent 旁路会话")
        return {"session": session.brief(include_summary=False),
                "messages": copy.deepcopy(session.dialogue_history),
                "pending_approvals": session.approvals.view(),
                "pending_questions": session.questions.view()}

    @app.get("/api/sessions/{session_id}/state")
    def session_state(session_id: str) -> dict[str, Any]:
        return _read_state(_require(session_id))

    @app.get("/api/sessions/{session_id}/outcome-costs")
    def session_outcome_costs(session_id: str) -> dict[str, Any]:
        from spiritscout.gateway.outcome_costs import (
            session_outcome_costs as read_outcomes,
        )

        session = _require(session_id)
        return read_outcomes(session.workspace, session.model_usage())

    @app.get("/api/sessions/{session_id}/topology/rules")
    def topology_rules(session_id: str, record_id: str = Query(..., max_length=300),
                       offset: int = Query(0, ge=0), limit: int = Query(6000, ge=1, le=12000)) -> dict[str, Any]:
        from spiritscout.web.process_graph import rules_text

        session = _require(session_id)
        if not session.bb_path.exists():
            raise HTTPException(404, "规则尚未保存")
        bb = Blackboard(session.bb_path, bus=None, readonly=True)
        try:
            row = bb.read_versioned(BlackboardLayer.TARGET_AUTH, record_id)
        finally:
            bb.close()
        if row is None:
            raise HTTPException(404, "规则记录不存在")
        text = rules_text(row[0])
        return {"text": text[offset:offset + limit], "version": row[1],
                "next_offset": offset + limit if offset + limit < len(text) else None}

    @app.get("/api/sessions/{session_id}/deliverables")
    def session_deliverables(session_id: str, kind: str = "all",
                             q: str = Query("", max_length=200),
                             offset: int = Query(0, ge=0),
                             limit: int = Query(60, ge=1, le=200)) -> dict[str, Any]:
        from spiritscout.web.deliverables import read_deliverables

        try:
            return read_deliverables(_require(session_id).workspace, kind=kind,
                                     query=q, offset=offset, limit=limit)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/sessions/{session_id}/coverage')
    def session_coverage(session_id: str, q: str = Query('', max_length=200),
                         status: str = Query('', max_length=30), identity: str = Query('', max_length=100),
                         test_type: str = Query('', max_length=100), offset: int = Query(0, ge=0),
                         asset_ref: str = Query('', max_length=500), include_unattempted: bool = True,
                         limit: int = Query(50, ge=1, le=100)) -> JSONResponse:
        from spiritscout.web.coverage import read_coverage

        try:
            result = read_coverage(_require(session_id).workspace, query=q, status=status,
                                   identity=identity, test_type=test_type, offset=offset, limit=limit,
                                   asset_ref=asset_ref, include_unattempted=include_unattempted)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.get('/api/sessions/{session_id}/coverage/evidence')
    def coverage_evidence(session_id: str, ref: str = Query(..., min_length=1, max_length=500)) -> JSONResponse:
        from spiritscout.web.coverage import read_evidence

        return JSONResponse(read_evidence(_require(session_id).workspace, ref), headers={'Cache-Control': 'no-store'})

    @app.get("/api/sessions/{session_id}/vulnerabilities")
    def session_vulnerabilities(session_id: str, offset: int = Query(0, ge=0),
                                limit: int = Query(50, ge=1, le=200)) -> JSONResponse:
        from spiritscout.web.deliverables import read_vulnerability_page

        return JSONResponse(read_vulnerability_page(_require(session_id).workspace, offset, limit),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{session_id}/record-history")
    def session_record_history(session_id: str, layer: str = Query(..., max_length=40),
                               record_id: str = Query(..., max_length=300),
                               before: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)) -> JSONResponse:
        from spiritscout.web.deliverables import read_record_history

        page = read_record_history(_require(session_id).workspace, layer, record_id, before, limit)
        return JSONResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{session_id}/executions")
    async def session_executions(session_id: str, after: int = Query(0, ge=0),
                                  limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        """执行元数据分页；不返回参数、凭据或原始工具输出。"""
        from spiritscout.tools.journal import read_execution_page

        return read_execution_page(_require(session_id).bb_path, after=after, limit=limit)

    @app.get("/api/sessions/{session_id}/tool-parameters")
    def session_tool_parameters(session_id: str, tool: str = Query(..., max_length=300),
                               digest: str = Query(..., pattern=r"^[0-9a-f]{64}$")) -> JSONResponse:
        from spiritscout.web.tool_parameters import read_tool_parameters

        result = read_tool_parameters(_require(session_id).workspace, tool, digest)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{session_id}/recovery")
    async def session_recovery(session_id: str) -> dict[str, Any]:
        from spiritscout.tools.journal import read_recovery_view

        session = _require(session_id)
        return read_recovery_view(session.bb_path, include_inflight=session.status != "running")

    @app.get("/api/sessions/{session_id}/execution-events")
    async def session_execution_events(session_id: str, after: int = Query(0, ge=0),
                                        limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        """数据库事件游标独立于原 JSONL SSE 的行号，断线后可补读。"""
        from spiritscout.tools.journal import read_execution_page

        page = read_execution_page(_require(session_id).bb_path, events=True,
                                   after=after, limit=limit)
        return {"cursor_version": "execution-v1", **page}

    @app.get("/api/sessions/{session_id}/audit-page")
    def session_audit_page(session_id: str, before: int | None = Query(None, ge=0),
                           limit: int = Query(160, ge=1, le=250),
                           activity: bool = False,
                           types: str | None = Query(None)) -> dict[str, Any]:
        if types:
            wanted = {name for name in types.split(",") if name}
            return read_typed_page(_require(session_id).audit_path, wanted,
                                   before=before, limit=limit)
        reader = read_activity_page if activity else read_audit_page
        return reader(_require(session_id).audit_path, before=before, limit=limit)

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request) -> StreamingResponse:
        session = _require(session_id)
        try:
            # ?last=N 供前端看门狗显式断点续传（EventSource 自动重连走头部）
            last = int(request.query_params.get("last")
                       or request.headers.get("last-event-id", "0") or 0)
        except ValueError:
            last = 0
        byte_mode = request.query_params.get("cursor") == "bytes"
        try:
            offset = int(request.headers.get("last-event-id")
                         or request.query_params.get("offset", "0")) if byte_mode else None
        except ValueError:
            offset = 0
        return StreamingResponse(
            stream_audit(session.audit_path, last,
                         byte_offset=offset,
                         is_terminal=lambda: session.status in TERMINAL_STATES),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
