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

import base64
import json
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

from spiritscout import __version__
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import GatewayConfig, ModelEntry, SystemConfig
from spiritscout.core.audit import AuditLogger, tool_preview
from spiritscout.core.exceptions import (
    ConfigurationError,
    RuleConflictError,
    SpiritScoutError,
)
from spiritscout.core.models import BlackboardLayer, RunMode
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.web import agentcfg, modelcfg, vault
from spiritscout.web.events import stream_audit
from spiritscout.web.sessions import (
    DIALOGUE_HISTORY_MAX,
    TERMINAL_STATES,
    Session,
    SessionManager,
)

_STATIC = Path(__file__).parent / "static" / "index.html"
_STATIC_DIR = Path(__file__).parent / "static"

# 读 API 只回规范公开字段（不回吐 YAML 原始键——历史污染字段不再扩散，
# 密钥字段永不出现）。模型条目的公开字段 = 注册表声明的全部字段去掉 api_key。
_MODEL_PUBLIC_FIELDS = tuple(
    k for k in ModelEntry.model_fields if k != "api_key")
_PROVIDER_PUBLIC_FIELDS = ("name", "provider", "base_url", "api_format")


class ApprovalDecision(BaseModel):
    """审批裁决体：allow 必须是真布尔——字符串 "false"/"0" 等真值文本
    一律 422（安全布尔不做隐式转换）。"""

    allow: StrictBool


class QuestionAnswerPayload(BaseModel):
    """回答及显式扩围目标；拒绝把空答复或错误类型吞成已作答。"""

    model_config = ConfigDict(extra="forbid")
    choice: StrictInt | None = None
    text: str = Field(default="", max_length=1000)
    add_targets: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def require_answer(self) -> QuestionAnswerPayload:
        if self.choice is None and not self.text.strip():
            raise ValueError("请选择选项或填写答复")
        return self


class SessionCreatePayload(BaseModel):
    """完整校验创建流程消费的字段，拒绝在搬附件或建会话后才发现类型错误。"""

    model_config = ConfigDict(extra="forbid")

    mode: str | None = None
    mission: str | None = None
    target: str | None = None
    scope: list[str] = Field(default_factory=list)
    upload_ids: list[str] = Field(default_factory=list, max_length=20)
    platform: str | None = None
    flag_format: str | None = None
    challenges_yaml: str | None = None
    rules_yaml: str | None = None
    title: str = Field(default="", max_length=80)
    no_llm: StrictBool = False
    authorized: StrictBool = True
    budget_tokens: StrictInt | None = None
    rps: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    allow_sensitive: StrictBool = False
    accept_rule_conflicts: StrictBool = False


def _read_state(session: Session) -> dict[str, Any]:
    """数据面快照：短连接只读打开会话黑板（WAL 下与写入侧不互堵）。

    查询配方复用 monitor.panel 的只读组合（层计数 / TASK_STATE / VULN /
    flags.json），输出 JSON 而非文本面板。
    """
    from spiritscout.tools.journal import read_recovery_view

    run_summary: dict[str, Any] = session.summary.get("run", {})
    # 实时用量：运行中由网关逐笔持久化（usage.db，兼容旧 usage.json），终态回退运行汇总
    live_usage = session.model_usage()
    state: dict[str, Any] = {
        "session": session.brief(include_summary=False),
        "recovery": read_recovery_view(session.bb_path, include_inflight=session.status != "running"),
        "layers": {}, "tasks": [],
        "vulns": {"by_status": {}, "items": []},
        "flags": None,
        "pending_approvals": session.approvals.view(),
        "pending_questions": session.questions.view(),
        "ring": session.ring.snapshot(),
        "user_opinions": session.opinions[-8:],
        "round_views": run_summary.get("round_views", []),
        "model_usage": live_usage or run_summary.get("model_usage"),
        "verify": session.summary.get("verify"),
        "business_context": None,
        "topology": {"dag_nodes": [], "tasks": []},
    }
    if not session.bb_path.exists():
        # 黑板未建（排队中）：终态摘要兜底（重开历史会话）
        state["topology"]["dag_nodes"] = run_summary.get("dag_nodes", [])
        return state
    bb = Blackboard(session.bb_path, bus=None, readonly=True)
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
        state["business_context"] = bb.read(
            BlackboardLayer.TOPOLOGY, "business-context")
        state["observed_evidence"] = [
            {"url": tool_preview({"url": rec["data"].get("value", "")}),
             "record_id": rec["record_id"],
             "method": (rec["data"].get("attrs") or {}).get("method", "GET"),
             "status_code": (rec["data"].get("attrs") or {}).get("status_code"),
             "note": "已观测登记，待业务分析整理"}
            for rec in bb.query(BlackboardLayer.ASSET, limit=100)
            if rec["data"].get("asset_type") in {"endpoint", "app"}]
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
                "verification": d.get("verification", {}),
                "false_positive_reason": d.get("false_positive_reason"),
                "endpoint": d.get("endpoint"),
            })
        state["vulns"]["by_status"] = bb.status_counts(BlackboardLayer.VULN)
        state["vulns"]["total"] = bb.count(BlackboardLayer.VULN)
        state["vulns"]["has_more"] = state["vulns"]["total"] > len(state["vulns"]["items"])
        # 灵枢应答流：拓扑「你 → 灵枢」节点的回执展示
        replies = []
        topology_records = bb.query(BlackboardLayer.TOPOLOGY, limit=400)
        for rec in topology_records:
            d = rec["data"]
            if isinstance(d, dict) and d.get("kind") == "reply":
                replies.append(d)
        replies.sort(key=lambda d: float(d.get("ts") or 0))
        state["master_replies"] = replies[-8:]
        # 发现台账：拓扑「发现」组数据源（共享笔记/入口/Flag 落黑板，跨轮持久）
        findings: list[dict[str, Any]] = []
        for rec in topology_records:
            d = rec["data"]
            if isinstance(d, dict) and d.get("kind") == "finding":
                findings.append(d)
        findings.sort(key=lambda d: float(d.get("ts") or 0))
        state["findings"] = findings[-80:]
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
    # 黑板保留历次续传记录；状态页以当前 DAG 为准，历史任务仍可在审计中查。
    current_task_ids = {
        str(task_id)
        for node in state["topology"]["dag_nodes"]
        for task_id in node.get("task_ids", [])
    }
    if current_task_ids:
        state["tasks"] = [
            task for task in state["tasks"]
            if str(task.get("task_id")) in current_task_ids
        ]
        state["topology"]["tasks"] = [
            task for task in state["topology"]["tasks"]
            if str(task.get("task_id")) in current_task_ids
        ]
    return state


def create_app(cfg: SystemConfig, models: list[ModelEntry], *,
               max_concurrent: int = 4) -> FastAPI:
    password = os.environ.get(cfg.web.password_env, "")
    loopback = cfg.web.host in ("127.0.0.1", "localhost", "::1")
    if cfg.web.remote_auth_required and not loopback and not password:
        raise ConfigurationError(
            f"Web 控制台绑定 {cfg.web.host} 时必须通过环境变量 "
            f"{cfg.web.password_env} 设置管理密码")
    vault.configure(cfg.workspace.root)   # 密钥库落在工作区（vault.db/vault.key）
    manager = SessionManager(cfg, models, max_concurrent=max_concurrent)
    from spiritscout.resources.assistant import ResourceAssistant
    from spiritscout.resources.store import ResourceStore, resource_root
    from spiritscout.web.resources import mount_resources

    resource_store = ResourceStore(resource_root(cfg),cfg.resources.freshness_hours)
    resource_assistant = ResourceAssistant(cfg,resource_store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 监督协程在 lifespan 内启动（不隶属任何请求任务组），
        # 关停时停掉所有未终态会话，避免孤儿任务
        manager.start()
        resource_assistant.start()
        # 读取已存在的资产，不启动旧会话或发起网络探测。
        for path in (Path(cfg.workspace.root)/"sessions").glob("sess-*"):
            try:
                resource_store.import_session(path)
            except (OSError,ValueError,sqlite3.Error):
                pass
        yield
        await resource_assistant.close()
        await manager.aclose()

    app = FastAPI(title="灵枢 SpiritScout 控制台", version=__version__,
                  lifespan=lifespan)
    app.state.manager = manager
    mount_resources(app,cfg,resource_store,resource_assistant)
    app.state.resources = resource_store

    @app.middleware("http")
    async def console_security(request: Request, call_next):
        """统一的控制台安全边界。

        Basic Auth 只在配置密码时启用；跨站写防护始终启用，
        因为恶意网页同样可以向本机回环地址提交普通表单。
        """
        if password:
            supplied_user = supplied_password = ""
            value = request.headers.get("authorization", "")
            if value.lower().startswith("basic "):
                try:
                    decoded = base64.b64decode(value.split(None, 1)[1],
                                               validate=True).decode("utf-8")
                    supplied_user, supplied_password = decoded.split(":", 1)
                except (ValueError, UnicodeDecodeError):
                    pass
            if not (secrets.compare_digest(supplied_user, "spiritscout")
                    and secrets.compare_digest(supplied_password, password)):
                return PlainTextResponse(
                    "需要管理认证", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="SpiritScout"'})
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            fetch_site = request.headers.get("sec-fetch-site", "").lower()
            origin = request.headers.get("origin", "").rstrip("/")
            expected = str(request.base_url).rstrip("/")
            if fetch_site == "cross-site" or (origin and origin != expected):
                return PlainTextResponse("拒绝跨站写请求", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # ------------------------------------------------------------ 前端

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_STATIC.read_text(encoding="utf-8"))

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    # ------------------------------------------------------------ 会话 CRUD

    @app.post("/api/sessions", status_code=201)
    async def create_session(payload: SessionCreatePayload) -> dict[str, Any]:
        # 安全相关布尔/数值在请求模型处强类型校验；其余字段透传。
        # 授权模型：Web 在场即声明（G1 语义不变、勾选摩擦去除）——
        # 创建行为本身即授权确认，human_confirmed 照旧落审计
        payload = {**payload.model_dump(), "authorized": True}
        # 智能模式：不传 mode / 传 auto → None，由 LLM 路由按任务描述裁决
        raw_mode = str(payload.get("mode") or "").strip().lower()
        if raw_mode in ("", "auto"):
            mode = None
        else:
            try:
                mode = RunMode(raw_mode)
            except ValueError:
                raise HTTPException(422, f"未知模式: {payload.get('mode')}") from None
        staged = _resolve_staged_uploads(payload)
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
                has_uploads=bool(staged),
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
            session = await manager.create(normalized, plan, staged_files=staged)
        except SpiritScoutError as exc:   # 模型注册表为空/损坏（热加载）
            raise HTTPException(422, str(exc)) from exc
        return {**session.brief(), "request": session.request}

    # ------------------------------------------------------------ Agent 编队

    def _agents_view() -> dict[str, Any]:
        from spiritscout.agents.roster import roster_view
        from spiritscout.config import load_model_config
        models = [{"model": m.model, "provider": m.provider_ref,
                   "capabilities": list(m.capabilities),
                   "context_window": m.context_window}
                  for m in load_model_config(cfg.models_config_path)]
        return {"roster": roster_view(),
                "assignments": agentcfg.read_assignments(cfg.agent_models_path),
                "models": models,
                "path": cfg.agent_models_path}

    @app.get("/api/agents")
    async def list_agents() -> dict[str, Any]:
        return _agents_view()

    @app.put("/api/agents/{agent_key}")
    async def assign_agent_model(agent_key: str,
                                 payload: dict[str, Any]) -> dict[str, Any]:
        """给 Agent 分配模型（空串 = 清除分配，回到档位路由）。"""
        from spiritscout.agents.profiles import PROFILE_BY_KEY
        from spiritscout.config import load_model_config
        model = str(payload.get("model") or "").strip()
        profile = PROFILE_BY_KEY.get(agent_key)
        if profile is None or not profile.configurable:
            raise HTTPException(422, f"Agent 不支持模型指派: {agent_key}")
        valid = {m.model for m in load_model_config(cfg.models_config_path)}
        try:
            agentcfg.set_assignment(cfg.agent_models_path, agent_key, model, valid)
        except SpiritScoutError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _agents_view()

    @app.get("/api/sessions")
    def list_sessions() -> dict[str, Any]:
        return {"sessions": [s.brief(include_summary=False)
                             for s in manager.list()]}

    def _require(session_id: str) -> Session:
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return session

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
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
    def session_state(session_id: str) -> dict[str, Any]:
        return _read_state(_require(session_id))

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

    @app.get("/api/sessions/{session_id}/artifacts/download")
    def session_artifact(session_id: str, path: str = Query(..., max_length=1000)) -> StreamingResponse:
        from spiritscout.web.deliverables import open_artifact

        try:
            file = open_artifact(_require(session_id).workspace, path)
        except OSError as exc:
            raise HTTPException(404, "产出文件不存在或不在本会话产出目录中") from exc

        def chunks() -> Iterator[bytes]:
            with file:
                while chunk := file.read(65536):
                    yield chunk

        return StreamingResponse(chunks(), media_type="application/octet-stream", headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(Path(path).name)}"})

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

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, request: Request) -> StreamingResponse:
        session = _require(session_id)
        try:
            # ?last=N 供前端看门狗显式断点续传（EventSource 自动重连走头部）
            last = int(request.query_params.get("last")
                       or request.headers.get("last-event-id", "0") or 0)
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

    @app.post("/api/sessions/{session_id}/pause")
    async def pause_session(session_id: str) -> dict[str, Any]:
        """协作式暂停：灵枢图节点与子 Agent 步骤在检查点挂起（在途动作跑完）。"""
        session = await manager.set_paused(session_id, True)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return session.brief(include_summary=False)

    @app.post("/api/sessions/{session_id}/resume")
    async def resume_paused_session(session_id: str) -> dict[str, Any]:
        """从暂停继续（与「续传」不同：会话从未停止，只是检查点放行）。"""
        session = await manager.set_paused(session_id, False)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return session.brief(include_summary=False)

    @app.post("/api/sessions/{session_id}/approvals/{approval_id}")
    async def decide_approval(session_id: str, approval_id: str,
                              payload: ApprovalDecision) -> dict[str, Any]:
        """高风险动作即时确认裁决：{"allow": true|false}（严格布尔）。

        允许 = 本次会话内该动作放行（后续同动作不再询问）；
        拒绝 = 本次调用按策略违规拒绝（分支留痕后继续）。
        """
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        allow = payload.allow
        if not session.approvals.decide(approval_id, allow):
            raise HTTPException(409, f"待确认项不存在或已裁决: {approval_id}")
        return {"approval_id": approval_id, "allow": allow}

    @app.get("/api/dashboard")
    def dashboard_data() -> dict[str, Any]:
        """仪表盘聚合数据：跨会话 token 消耗、资产总览、关键发现。"""
        sessions = manager.list()
        total_tokens = 0
        total_cost = 0.0
        running = 0
        per_model = {}
        daily = {}
        for sess in sessions:
            mu = sess.model_usage()
            t = mu.get("used_tokens", 0)
            c = mu.get("used_usd", 0.0)
            total_tokens += t
            total_cost += c
            if sess.status == "running":
                running += 1
            for m, mt in (mu.get("per_model_tokens") or {}).items():
                per_model[m] = per_model.get(m, 0) + mt
            for ev in mu.get("call_series", []):
                day = time.strftime("%Y-%m-%d", time.gmtime(ev.get("ts", 0)))
                daily[day] = daily.get(day, 0) + ev.get("tokens", 0)
        from spiritscout.knowledge.experience import experience_db_path, read_recent

        return {
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 4),
            "total_sessions": len(sessions),
            "running": running,
            "per_model": per_model,
            "daily": dict(sorted(daily.items())),
            "experience": read_recent(
                experience_db_path(cfg.experience.root, cfg.workspace.root), limit=8),
        }

    @app.post("/api/sessions/{session_id}/questions/{question_id}")
    async def answer_question(session_id: str, question_id: str,
                              payload: QuestionAnswerPayload) -> dict[str, Any]:
        """回答模型发起的提问：{"choice": 选项下标} 或 {"text": 自由输入}，
        {"add_targets": [...]} 为用户勾选的扩围目标（写入会话批准集）。"""
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        try:
            ok = session.questions.answer(
                question_id, choice=payload.choice, text=payload.text,
                add_targets=payload.add_targets)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not ok:
            raise HTTPException(409, f"待答问题不存在或已作答: {question_id}")
        return {"question_id": question_id, "answered": True}

    # ------------------------------------------------------------ 会话协作（用户 → 灵枢）

    _MAX_UPLOAD_BYTES = 50 * 1024 * 1024

    def _clean_upload_name(name: str) -> str:
        """附件名安全化：只留 basename，拒绝路径分隔符/控制字符/超长。"""
        clean = Path(name.replace("\\", "/")).name.strip()
        if (not clean or clean in (".", "..") or len(clean) > 200
                or any(ord(c) < 32 for c in clean)):
            raise HTTPException(422, "附件名不合法")
        return clean

    def _gc_staging(root: Path, *, max_age_s: float = 86400.0) -> None:
        """暂存区惰性回收：超过 24h 未被会话创建带走的上传整目录清除。"""
        now = time.time()
        for d in root.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime > max_age_s:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue

    @app.post("/api/uploads")
    async def stage_upload(request: Request, name: str = Query(...)) -> dict[str, Any]:
        """任务下达前暂存附件（初始会话的附件上传）。

        请求体为文件原始字节，?name= 携带文件名。返回 upload_id，创建会话
        时随 upload_ids 一并材料化进会话工作区；未被带走的暂存 24h 后惰性回收。
        """
        clean = _clean_upload_name(name)
        body = await request.body()
        if not body:
            raise HTTPException(422, "附件内容为空")
        if len(body) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"附件过大（上限 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）")
        staging = Path(cfg.workspace.root) / "uploads-staging"
        staging.mkdir(parents=True, exist_ok=True)
        _gc_staging(staging)
        updir = staging / uuid.uuid4().hex[:12]
        updir.mkdir()
        dest = updir / clean
        if dest.exists():                      # 单目录内同名（理论不可达）不覆盖
            stem, suffix, n = dest.stem, dest.suffix, 2
            while dest.exists():
                dest = updir / f"{stem}-{n}{suffix}"
                n += 1
        dest.write_bytes(body)
        return {"upload_id": updir.name, "name": dest.name, "size": len(body)}

    def _resolve_staged_uploads(payload: dict[str, Any]) -> list[Path]:
        """校验并收集任务下达携带的暂存附件（路径全部收窄在暂存区内）。"""
        ids = [str(u).strip() for u in (payload.get("upload_ids") or [])
               if str(u).strip()]
        if not ids:
            return []
        if len(ids) > 20:
            raise HTTPException(422, "一次最多携带 20 个上传附件")
        staging = (Path(cfg.workspace.root) / "uploads-staging").resolve()
        out: list[Path] = []
        for uid in ids:
            if "/" in uid or "\\" in uid or uid in (".", ".."):
                raise HTTPException(422, "上传标识不合法")
            updir = staging / uid
            if not updir.is_dir():
                raise HTTPException(422, "上传不存在或已过期，请重新选择文件")
            out.extend(sorted(p for p in updir.iterdir() if p.is_file()))
        return out

    @app.post("/api/sessions/{session_id}/messages")
    async def post_message(session_id: str,
                           payload: dict[str, Any]) -> dict[str, Any]:
        """会话任意时刻给灵枢发消息：入会话收件箱 + 落对话史 + 审计留痕。

        灵枢在最近的决策点取走消费：派发期督导（~2s 节拍，化为对子 Agent
        的方向建议）或轮末复盘；消费动作有审计事件，意见不重不漏。
        会话已终态时消息即这一轮对话的输入（对话史末尾接着聊）——灵枢带着
        同一工作区、审计与 Flag 库和全部上下文继续，提问即回答，
        布置新方向即派工，与网页版大模型对话同一形态。
        """
        session = _require(session_id)
        content = str(payload.get("content") or "").strip()
        if not content:
            raise HTTPException(422, "意见内容为空")
        if len(content) > 4000:
            raise HTTPException(422, "意见过长（≤4000 字）")
        session.user_inbox.append({"kind": "message", "text": content})
        # 拓扑图/状态面展示用：历史意见（收件箱是消费语义，另一份不可变留档）
        session.opinions.append({"text": content, "ts": time.time()})
        del session.opinions[:-8]
        # 对话史随发随落：运行中被直达通道共读，终态后续传以史末尾为
        # 这一轮的输入——灵枢应答始终带着此前全部往来
        session.dialogue_history.append({"role": "user", "content": content})
        del session.dialogue_history[:-DIALOGUE_HISTORY_MAX]
        AuditLogger(session.audit_path).log(
            "user.message", "user", {"content": content})
        resumed = False
        if session.status in TERMINAL_STATES:
            try:
                await manager.resume(session_id)
                resumed = True
            except SpiritScoutError as exc:
                raise HTTPException(409, str(exc)) from exc
            except Exception as exc:
                # 续传失败必须可读，不能以 internal error 蒙用户
                # （会话状态由 resume 自身保证不翻转）
                raise HTTPException(
                    503, f"续传未完成：{type(exc).__name__}: {exc}") from exc
        return {"queued": True, "resumed": resumed}

    @app.post("/api/sessions/{session_id}/attachments")
    async def upload_attachment(session_id: str, request: Request,
                                name: str = Query(...)) -> dict[str, Any]:
        """会话区上传附件（请求体为文件原始字节，?name= 携带文件名）。

        落到会话工作区 attachments/——file_analyze 的允许目录、沙箱工作区
        的子目录，两类工具通道都能直接读取；审计留痕后由灵枢在下一决策点
        知会各 Agent（督导建议里带文件路径）。终态会话也可上传：
        附件随下一次续传（发消息触发）进入决策环。
        """
        session = _require(session_id)
        clean = _clean_upload_name(name)
        body = await request.body()
        if not body:
            raise HTTPException(422, "附件内容为空")
        if len(body) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"附件过大（上限 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）")
        dest_dir = session.workspace / "attachments"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / clean
        if dest.exists():                      # 同名不覆盖：追加序号
            stem, suffix, n = dest.stem, dest.suffix, 2
            while dest.exists():
                dest = dest_dir / f"{stem}-{n}{suffix}"
                n += 1
        dest.write_bytes(body)
        AuditLogger(session.audit_path).log("user.attachment", "user", {
            "name": dest.name, "size": len(body), "path": str(dest)})
        return {"name": dest.name, "size": len(body), "path": str(dest)}

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
    async def session_audit_download(session_id: str) -> StreamingResponse:
        """审计日志 JSONL 导出（Web 功能补全）。"""
        audit = _require(session_id).audit_path
        if not audit.exists():
            raise HTTPException(404, "审计日志不存在（会话未产生事件）")
        from spiritscout.core.audit import sanitize_record

        def records() -> Iterator[str]:
            # 同步生成器由响应在线程池消费，不一次性物化整个历史文件。
            with audit.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        record = sanitize_record(json.loads(line))
                    except (ValueError, TypeError, AttributeError):
                        record = {"type": "audit.invalid", "detail": {"error": "日志行无效"}}
                    yield json.dumps(record, ensure_ascii=False) + "\n"

        return StreamingResponse(records(), media_type="application/x-ndjson",
                                 headers={"Content-Disposition":
                                          f'attachment; filename="{session_id}-audit.jsonl"'})

    # ------------------------------------------------------------ 模型管理

    def _providers_view() -> dict[str, Any]:
        providers = modelcfg.read_providers(cfg.models_config_path)
        # 读 API 只回规范公开字段（+密钥状态）：不回吐 YAML 原始键
        return {"path": cfg.models_config_path,
                "providers": [{**{k: p.get(k) for k in _PROVIDER_PUBLIC_FIELDS},
                               **vault.key_status(p, str(p.get("name") or ""))}
                              for p in providers]}

    def _models_view(entries: list[dict[str, Any]]) -> dict[str, Any]:
        # 读 API 只回规范公开字段：密钥字段与历史污染字段不再扩散
        return {"path": cfg.models_config_path,
                "models": [{k: e.get(k) for k in _MODEL_PUBLIC_FIELDS}
                           for e in entries]}

    @app.get("/api/providers")
    async def list_providers() -> dict[str, Any]:
        return _providers_view()

    @app.get("/api/provider-presets")
    async def provider_presets() -> dict[str, Any]:
        """厂商预设目录（添加供应商对话框：选中带入协议与地址）。"""
        from spiritscout.web.provider_api import PROVIDER_PRESETS
        return {"presets": PROVIDER_PRESETS}

    def _model_error(exc: SpiritScoutError) -> HTTPException:
        msg = str(exc)
        status = 409 if "已存在" in msg else 404 if "不存在" in msg else 422
        return HTTPException(status, msg)

    def _pop_key_value(payload: dict[str, Any]) -> str | None:
        """摘出 api_key_value（G8：绝不进 YAML、不回传、留痕不含值）。

        返回 None=请求未提供密钥字段（保持既有密钥不动）；空串=清除语义。
        密钥属于供应商：同一端点下的模型共用一条记录。
        """
        if "api_key_value" not in payload:
            return None
        return str(payload.pop("api_key_value") or "").strip()

    def _store_key(provider: str, value: str) -> None:
        vault.set_key(provider, value)             # 空串 = 清除
        AuditLogger(cfg.audit_log_path).log(
            "system.lifecycle", "modelcfg",
            {"action": "model_key_set" if value else "model_key_cleared",
             "provider": provider, "source": "vault"})

    @app.post("/api/providers", status_code=201)
    async def add_provider(payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        key_value = _pop_key_value(payload)
        try:
            payload = modelcfg.canonical_provider(payload)
            modelcfg.upsert_provider(cfg.models_config_path, payload)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        if key_value is not None:                  # 校验通过才动密钥库
            _store_key(str(payload.get("name") or ""), key_value)
        return _providers_view()

    @app.put("/api/providers/{name}")
    async def update_provider(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """供应商更新：先完整校验落盘（失败不动密钥库——已有可用密钥不因
        失败请求改变），再做密钥操作。语义：仅改名=密钥随名迁移；
        改名+新密钥=迁移后按显式输入覆写；api_key_value 空串=清除。"""
        payload = dict(payload)
        key_value = _pop_key_value(payload)
        try:
            payload = modelcfg.canonical_provider({**payload, "name": payload.get("name", name)})
            new_name = payload["name"]
            modelcfg.upsert_provider(cfg.models_config_path, payload,
                                     original_name=name)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        if new_name != name:
            vault.rename_key(name, new_name)       # 先迁移
        if key_value is not None:
            _store_key(new_name, key_value)        # 再按显式输入覆写/清除
        return _providers_view()

    @app.delete("/api/providers/{name}")
    async def delete_provider(name: str) -> dict[str, Any]:
        try:
            modelcfg.delete_provider(cfg.models_config_path, name)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        vault.drop_key(name)
        return _providers_view()

    @app.post("/api/providers/{name}/models")
    async def fetch_provider_models(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """从供应商端点拉取可用模型列表（用户点击触发；未保存的编辑也可先拉）。

        端点没有实现模型列表接口是常态——失败时如实回报试过的地址与错误，
        用户改用手动填模型 ID（同一入口）。
        """
        from spiritscout.web.provider_api import fetch_model_list

        providers = {str(p.get("name")): p for p in
                     modelcfg.read_providers(cfg.models_config_path)}
        stored = providers.get(name, {})
        payload = dict(payload or {})
        key_value = str(payload.pop("api_key_value", "") or "").strip()
        spec = {
            "name": name,
            "provider": str(payload.get("provider") or stored.get("provider")
                            or "openai_compatible"),
            "base_url": str(payload.get("base_url") or stored.get("base_url") or ""),
            # 密钥只认本次直填或密钥库——YAML 不落密钥，stored 里没有可用值
            "api_key": key_value or vault.get_key(name),
        }
        result = await fetch_model_list(spec)
        AuditLogger(cfg.audit_log_path).log("system.lifecycle", "modelcfg", {
            "action": "provider_model_list", "provider": name,
            "ok": result.get("ok"), "count": len(result.get("models") or []),
            "error": str(result.get("error", ""))[:200]})
        return result

    @app.post("/api/models/batch", status_code=201)
    async def add_models_batch(payload: dict[str, Any]) -> dict[str, Any]:
        """批量启用供应商下的模型（从拉取的列表里勾选）。"""
        provider_ref = str(payload.get("provider_ref") or "")
        ids = [str(x) for x in (payload.get("models") or []) if str(x).strip()]
        try:
            entries, added = modelcfg.add_entries(
                cfg.models_config_path, provider_ref, ids)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        return {**_models_view(entries), "added": added}

    @app.get("/api/models")
    async def list_models() -> dict[str, Any]:
        return _models_view(modelcfg.read_entries(cfg.models_config_path))

    @app.post("/api/models", status_code=201)
    async def add_model(payload: dict[str, Any]) -> dict[str, Any]:
        """手动添加单个模型（自动拉取失败或列表里没有时的入口）。"""
        payload = dict(payload)
        try:
            entries = modelcfg.upsert_entry(cfg.models_config_path, payload)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        return _models_view(entries)

    @app.put("/api/models/{model:path}")
    async def update_model(model: str, payload: dict[str, Any]) -> dict[str, Any]:
        """更新模型画像。模型 ID 含 /（org/model）——path 转换器整体捕获，
        %2F 与裸 / 同样命中；/api/models/test 等静态路由不受影响。"""
        payload = dict(payload)
        try:
            entries = modelcfg.upsert_entry(
                cfg.models_config_path, payload, original_model=model, assignments_path=cfg.agent_models_path)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        return _models_view(entries)

    @app.delete("/api/models/{model:path}")
    async def delete_model(model: str) -> dict[str, Any]:
        try:
            entries = modelcfg.delete_entry(cfg.models_config_path, model, assignments_path=cfg.agent_models_path)
        except SpiritScoutError as exc:
            raise _model_error(exc) from exc
        return _models_view(entries)

    @app.post("/api/models/test")
    async def test_model(payload: dict[str, Any]) -> dict[str, Any]:
        """连通性测试（用户显式触发的真实外呼；审计留痕 model.call）。"""
        from spiritscout.config import load_model_config
        from spiritscout.gateway import ModelGateway, ModelRegistry

        model = str(payload.get("model") or "")
        entries = vault.apply_keys(load_model_config(cfg.models_config_path))
        entry = next((e for e in entries if e.model == model), None)
        if entry is None:
            raise HTTPException(404, f"模型不存在: {model}")
        if not entry.api_key:
            return {"ok": False,
                    "error": "密钥未配置——在该模型所属供应商里填入密钥"
                             "（加密存入本机密钥库）"}
        gateway = ModelGateway(
            ModelRegistry(entries),
            GatewayConfig(request_timeout_seconds=25),
            audit=AuditLogger(cfg.audit_log_path))
        try:
            resp = await gateway.chat(
                [{"role": "user", "content": "连通性测试：请只回复 ok"}],
                model=model, max_tokens=16,
                reasoning_effort=str(payload.get("reasoning_effort") or ""))
            return {"ok": True, "latency_ms": round(resp.latency_ms),
                    "sample": resp.content[:80],
                    "has_reasoning": bool(resp.reasoning)}
        except Exception as exc:  # noqa: BLE001 —— 测试结果原样上报告知用户
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    return app
