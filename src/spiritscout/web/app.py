"""FastAPI 应用工厂（Web 控制台）。

路由面：会话 CRUD（创建=启动）、数据面快照（黑板八层/任务路线/漏洞/Flag/
拓扑）、SSE 事件流、报告读取与下载、审计导出、无构建单页前端。
授权模型：Web 交互式在场即声明——打开控制台创建会话本身即构成
授权确认（G1），Authorization.human_confirmed 照常落审计；对话框保留
「仅限已授权目标」显著提示，不设勾选摩擦。
模式参数/赛制规则冲突由 launch.prepare_launch 校验 → 422（单一事实源）。
空范围的唯一权威门在 runtime（ScopeViolation → 会话落 failed），
Web 不另设前置副本，避免两套门槛漂移。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from spiritscout import __version__
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import GatewayConfig, ModelEntry, SystemConfig
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import RuleConflictError, SpiritScoutError
from spiritscout.core.models import BlackboardLayer, RunMode
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.web import agentcfg, modelcfg, vault
from spiritscout.web.events import stream_audit
from spiritscout.web.sessions import TERMINAL_STATES, Session, SessionManager

_STATIC = Path(__file__).parent / "static" / "index.html"


def _read_state(session: Session) -> dict[str, Any]:
    """数据面快照：短连接只读打开会话黑板（WAL 下与写入侧不互堵）。

    查询配方复用 monitor.panel 的只读组合（层计数 / TASK_STATE / VULN /
    flags.json），输出 JSON 而非文本面板。
    """
    run_summary: dict[str, Any] = session.summary.get("run", {})
    state: dict[str, Any] = {
        "session": session.brief(include_summary=False),
        "layers": {}, "tasks": [],
        "vulns": {"by_status": {}, "items": []},
        "flags": None,
        "pending_approvals": session.approvals.view(),
        "ring": session.ring.snapshot(),
        "round_views": run_summary.get("round_views", []),
        "model_usage": run_summary.get("model_usage"),
        "verify": session.summary.get("verify"),
        "topology": {"dag_nodes": [], "tasks": []},
    }
    if not session.bb_path.exists():
        # 黑板未建（排队中）：终态摘要兜底（重开历史会话）
        state["topology"]["dag_nodes"] = run_summary.get("dag_nodes", [])
        return state
    bb = Blackboard(session.bb_path, bus=None)
    try:
        for layer in BlackboardLayer:
            state["layers"][layer.value] = bb.count(layer)
        for rec in bb.query(BlackboardLayer.TASK_STATE, limit=100):
            data = rec["data"]
            if "dag_snapshot" in data:
                # 主 Agent 每轮落盘的 DAG 快照（拓扑数据源，非任务记录）
                state["topology"]["dag_nodes"] = data["dag_snapshot"]
                continue
            t = data.get("task", {})
            state["tasks"].append({
                "task_id": t.get("task_id"), "task_type": t.get("task_type"),
                "agent_type": t.get("agent_type"), "status": t.get("status"),
                "round_no": t.get("round_no"),
                "result_summary": str(t.get("result_summary") or "")[:200],
                "parent_dag_node": t.get("parent_dag_node"),
                "routes": [
                    {"name": r.get("name"), "status": r.get("status"),
                     "health": r.get("health"), "progress": r.get("progress"),
                     "value_score": r.get("value_score"),
                     "risk_score": r.get("risk_score"),
                     "steps_executed": r.get("steps_executed"),
                     "note": (r.get("notes") or [""])[-1][:200]}
                    for r in data.get("routes", [])
                ],
            })
            state["topology"]["tasks"].append({
                "task_id": t.get("task_id"), "task_type": t.get("task_type"),
                "agent_type": t.get("agent_type"), "status": t.get("status"),
                "round_no": t.get("round_no"),
                "parent_dag_node": t.get("parent_dag_node"),
                "routes_done": sum(1 for r in data.get("routes", [])
                                   if r.get("status") == "done"),
                "routes_total": len(data.get("routes", [])),
                "steps": sum(int(r.get("steps_executed") or 0)
                             for r in data.get("routes", [])),
                "routes": [
                    {"name": r.get("name"), "status": r.get("status"),
                     "progress": r.get("progress"),
                     "note": (r.get("notes") or [""])[-1][:200]}
                    for r in data.get("routes", [])
                ],
            })
        if not state["topology"]["dag_nodes"]:
            # 终态会话兜底：摘要中的最终 DAG（如快照记录被清理）
            state["topology"]["dag_nodes"] = run_summary.get("dag_nodes", [])
        by_status: dict[str, int] = {}
        for rec in bb.query(BlackboardLayer.VULN, limit=50):
            d = rec["data"]
            status = str(d.get("status", "candidate"))
            by_status[status] = by_status.get(status, 0) + 1
            state["vulns"]["items"].append({
                "vuln_id": d.get("vuln_id"), "title": d.get("title"),
                "vuln_type": d.get("vuln_type"), "severity": d.get("severity"),
                "status": status, "cvss_score": d.get("cvss_score"),
                "poc_verified": d.get("poc_verified"),
                "endpoint": d.get("endpoint"),
            })
        state["vulns"]["by_status"] = by_status
        if session.flags_path.exists():
            try:
                flags = json.loads(
                    session.flags_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                flags = []
            state["flags"] = {
                "total": len(flags),
                "submitted": sum(1 for f in flags if f.get("submitted")),
                "accepted": sum(1 for f in flags if f.get("accepted")),
                "items": [
                    {"value": f.get("value"), "challenge_id": f.get("challenge_id"),
                     "submitted": f.get("submitted"), "accepted": f.get("accepted")}
                    for f in flags[:50]
                ],
            }
    finally:
        bb.close()
    return state


def create_app(cfg: SystemConfig, models: list[ModelEntry], *,
               max_concurrent: int = 4) -> FastAPI:
    vault.configure(cfg.workspace.root)   # 密钥库落在工作区（vault.db/vault.key）
    manager = SessionManager(cfg, models, max_concurrent=max_concurrent)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 监督协程在 lifespan 内启动（不隶属任何请求任务组），
        # 关停时停掉所有未终态会话，避免孤儿任务
        manager.start()
        yield
        await manager.aclose()

    app = FastAPI(title="灵探 SpiritScout 控制台", version=__version__,
                  lifespan=lifespan)
    app.state.manager = manager

    # ------------------------------------------------------------ 前端

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_STATIC.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ 会话 CRUD

    @app.post("/api/sessions", status_code=201)
    async def create_session(payload: dict[str, Any]) -> dict[str, Any]:
        # 授权模型：Web 在场即声明（G1 语义不变、勾选摩擦去除）——
        # 创建行为本身即授权确认，human_confirmed 照旧落审计
        payload = {**payload, "authorized": True}
        # 智能模式：不传 mode / 传 auto → None，由 LLM 路由按任务描述裁决
        raw_mode = str(payload.get("mode") or "").strip().lower()
        if raw_mode in ("", "auto"):
            mode = None
        else:
            try:
                mode = RunMode(raw_mode)
            except ValueError:
                raise HTTPException(422, f"未知模式: {payload.get('mode')}") from None
        try:
            plan = prepare_launch(LaunchRequest(
                mode=mode,
                target=payload.get("target") or None,
                extra_scope=list(payload.get("scope") or []),
                platform=payload.get("platform") or None,
                allow_sensitive=bool(payload.get("allow_sensitive", False)),
                rps=float(payload.get("rps", 10.0)),
                flag_format=payload.get("flag_format") or None,
                challenges_yaml=payload.get("challenges_yaml") or None,
                rules_yaml=payload.get("rules_yaml") or None,
                accept_rule_conflicts=bool(
                    payload.get("accept_rule_conflicts", False)),
                mission=payload.get("mission") or None,
            ))
        except RuleConflictError as exc:
            # 结构化冲突清单：前端据此弹出即时确认对话，确认后带
            # accept_rule_conflicts 重发（合规门从表单前移到触发时刻）
            raise HTTPException(422, {"code": "rule_conflict",
                                      "message": str(exc),
                                      "conflicts": exc.conflicts}) from exc
        except SpiritScoutError as exc:
            raise HTTPException(422, str(exc)) from exc
        # 智能模式：会话先按装配兜底模式登记，LLM 路由改判后由执行体更新
        normalized = dict(payload, mode=plan.authorization.mode.value,
                          mode_auto=plan.mode_auto)
        try:
            session = await manager.create(normalized, plan)
        except SpiritScoutError as exc:   # 模型注册表为空/损坏（热加载）
            raise HTTPException(422, str(exc)) from exc
        return {**session.brief(), "request": session.request}

    # ------------------------------------------------------------ Agent 编队

    def _agents_view() -> dict[str, Any]:
        from spiritscout.agents.roster import roster_view
        from spiritscout.config import load_model_config
        aliases = [m.alias for m in load_model_config(cfg.models_config_path)]
        return {"roster": roster_view(),
                "assignments": agentcfg.read_assignments(cfg.agent_models_path),
                "models": aliases,
                "path": cfg.agent_models_path}

    @app.get("/api/agents")
    async def list_agents() -> dict[str, Any]:
        return _agents_view()

    @app.put("/api/agents/{agent_key}")
    async def assign_agent_model(agent_key: str,
                                 payload: dict[str, Any]) -> dict[str, Any]:
        """给 Agent 分配模型（空串 = 清除分配，回到档位路由）。"""
        from spiritscout.config import load_model_config
        alias = str(payload.get("model") or "").strip()
        valid = {m.alias for m in load_model_config(cfg.models_config_path)}
        try:
            agentcfg.set_assignment(cfg.agent_models_path, agent_key, alias, valid)
        except SpiritScoutError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _agents_view()

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": [s.brief(include_summary=False)
                             for s in manager.list()]}

    def _require(session_id: str) -> Session:
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return session

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        session = _require(session_id)
        detail = session.brief()
        detail["request"] = session.request
        return detail

    @app.patch("/api/sessions/{session_id}")
    async def rename_session(session_id: str,
                             payload: dict[str, Any]) -> dict[str, Any]:
        """重命名会话（空标题 = 恢复自动摘要名）。"""
        session = _require(session_id)
        title = str(payload.get("title") or "")
        if len(title.strip()) > 80:
            raise HTTPException(422, "会话名过长（≤80 字）")
        manager.rename(session_id, title)
        return session.brief(include_summary=False)

    @app.get("/api/sessions/{session_id}/state")
    async def session_state(session_id: str) -> dict[str, Any]:
        return _read_state(_require(session_id))

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request) -> StreamingResponse:
        session = _require(session_id)
        try:
            last = int(request.headers.get("last-event-id", "0") or 0)
        except ValueError:
            last = 0
        return StreamingResponse(
            stream_audit(session.audit_path, last,
                         is_terminal=lambda: session.status in TERMINAL_STATES),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str) -> dict[str, Any]:
        session = await manager.stop(session_id)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return session.brief(include_summary=False)

    @app.post("/api/sessions/{session_id}/approvals/{approval_id}")
    async def decide_approval(session_id: str, approval_id: str,
                              payload: dict[str, Any]) -> dict[str, Any]:
        """高风险动作即时确认裁决：{"allow": true|false}。

        允许 = 本次会话内该动作放行（后续同动作不再询问）；
        拒绝 = 本次调用按策略违规拒绝（分支留痕后继续）。
        """
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        allow = bool(payload.get("allow", False))
        if not session.approvals.decide(approval_id, allow):
            raise HTTPException(409, f"待确认项不存在或已裁决: {approval_id}")
        return {"approval_id": approval_id, "allow": allow}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str,
                             confirm: bool = Query(False)) -> dict[str, Any]:
        try:
            session = await manager.delete(session_id, confirm=confirm)
        except SpiritScoutError as exc:
            raise HTTPException(409, str(exc)) from exc
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return {"deleted": session_id}

    @app.get("/api/sessions/{session_id}/report")
    async def session_report(session_id: str) -> dict[str, Any]:
        session = _require(session_id)
        report = session.report_path()
        if report is None:
            raise HTTPException(404, "报告尚未生成（任务完成后可用）")
        return {"path": str(report),
                "markdown": report.read_text(encoding="utf-8")}

    @app.get("/api/sessions/{session_id}/report/download")
    async def session_report_download(session_id: str) -> FileResponse:
        """报告文件下载（Web 功能补全）。"""
        report = _require(session_id).report_path()
        if report is None:
            raise HTTPException(404, "报告尚未生成（任务完成后可用）")
        return FileResponse(report, media_type="text/markdown",
                            filename=report.name)

    @app.get("/api/sessions/{session_id}/audit/download")
    async def session_audit_download(session_id: str) -> FileResponse:
        """审计日志 JSONL 导出（Web 功能补全）。"""
        audit = _require(session_id).audit_path
        if not audit.exists():
            raise HTTPException(404, "审计日志不存在（会话未产生事件）")
        return FileResponse(audit, media_type="application/x-ndjson",
                            filename=f"{session_id}-audit.jsonl")

    # ------------------------------------------------------------ 模型管理

    def _models_view(entries: list[dict[str, Any]]) -> dict[str, Any]:
        return {"path": cfg.models_config_path,
                "models": [{**e, **vault.key_status(e)} for e in entries]}

    @app.get("/api/models")
    async def list_models() -> dict[str, Any]:
        return _models_view(modelcfg.read_entries(cfg.models_config_path))

    def _model_error(exc: SpiritScoutError) -> HTTPException:
        msg = str(exc)
        status = 409 if "已存在" in msg else 404 if "不存在" in msg else 422
        return HTTPException(status, msg)

    def _stash_key(alias: str, payload: dict[str, Any]) -> None:
        """摘出 api_key_value 加密存入密钥库（G8：绝不进 YAML、不回传、留痕不含值）。

        调用方必须先经本函数处理再把 payload 交给 upsert_entry——
        否则直填密钥会被当作普通字段写盘。
        """
        if "api_key_value" not in payload:
            return
        value = str(payload.pop("api_key_value") or "").strip()
        vault.set_key(alias, value)                # 空串 = 清除
        AuditLogger(cfg.audit_log_path).log(
            "system.lifecycle", "modelcfg",
            {"action": "model_key_set" if value else "model_key_cleared",
             "alias": alias, "source": "vault"})

    @app.post("/api/models", status_code=201)
    async def add_model(payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        alias = str(payload.get("alias") or "")
        _stash_key(alias, payload)
        try:
            entries = modelcfg.upsert_entry(cfg.models_config_path, payload)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        return _models_view(entries)

    @app.put("/api/models/{alias}")
    async def update_model(alias: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        new_alias = str(payload.get("alias") or alias)
        _stash_key(new_alias, payload)
        try:
            entries = modelcfg.upsert_entry(
                cfg.models_config_path, payload, original_alias=alias)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        vault.rename_key(alias, new_alias)
        return _models_view(entries)

    @app.delete("/api/models/{alias}")
    async def delete_model(alias: str) -> dict[str, Any]:
        try:
            entries = modelcfg.delete_entry(cfg.models_config_path, alias)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        vault.drop_key(alias)
        return _models_view(entries)

    @app.post("/api/models/test")
    async def test_model(payload: dict[str, Any]) -> dict[str, Any]:
        """连通性测试（用户显式触发的真实外呼；审计留痕 model.call）。"""
        from spiritscout.config import load_model_config
        from spiritscout.gateway import ModelGateway, ModelRegistry

        alias = str(payload.get("alias") or "")
        entries = vault.apply_keys(
            load_model_config(cfg.models_config_path))
        entry = next((e for e in entries if e.alias == alias), None)
        if entry is None:
            raise HTTPException(404, f"模型不存在: {alias}")
        if not entry.api_key:
            return {"ok": False,
                    "error": "密钥未配置——在「模型」页编辑填入密钥"
                             "（加密存入本机密钥库）或配置环境变量"}
        gateway = ModelGateway(
            ModelRegistry(entries),
            GatewayConfig(request_timeout_seconds=25),
            audit=AuditLogger(cfg.audit_log_path))
        try:
            resp = await gateway.chat(
                [{"role": "user", "content": "连通性测试：请只回复 ok"}],
                model_alias=alias, max_tokens=16)
            return {"ok": True, "latency_ms": round(resp.latency_ms),
                    "sample": resp.content[:80],
                    "has_reasoning": bool(resp.reasoning)}
        except Exception as exc:  # noqa: BLE001 —— 测试结果原样上报告知用户
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    return app
