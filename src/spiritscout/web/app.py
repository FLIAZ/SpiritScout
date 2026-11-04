"""FastAPI 控制台装配：认证、生命周期、会话控制、SSE 和静态页面。

请求模型、状态投影、模型配置及文件路由按职责分离；本模块保留原请求模型
和 _read_state 的导入路径。启动请求仍由 launch.prepare_launch 校验。
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from spiritscout import __version__
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry, SystemConfig
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import (
    ConfigurationError,
    RuleConflictError,
    SpiritScoutError,
)
from spiritscout.core.models import BlackboardLayer, RunMode
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.web import auth as web_auth
from spiritscout.web import vault
from spiritscout.web.api_schemas import (
    AgentSideSessionPayload,
    ApprovalDecision,
    LoginPayload,
    QuestionAnswerPayload,
    SessionCreatePayload,
    SessionModelPayload,
    SessionPatchPayload,
    StorageCleanupPayload,
)
from spiritscout.web.events import (
    read_activity_page,
    read_audit_page,
    read_typed_page,
    stream_audit,
)
from spiritscout.web.file_routes import (
    mount_artifact_routes,
    mount_collaboration_routes,
    mount_report_routes,
    resolve_staged_uploads,
)
from spiritscout.web.model_routes import mount_agent_routes, mount_model_routes
from spiritscout.web.session_state import _read_state as _read_state
from spiritscout.web.sessions import (
    TERMINAL_STATES,
    Session,
    SessionManager,
)

_STATIC = Path(__file__).parent / "static" / "index.html"
_STATIC_DIR = Path(__file__).parent / "static"
_LOGIN = _STATIC_DIR / "login.html"

# 无管理员本机模式允许的 Host（DNS 重绑定防护）：正规本机访问只会用回环名
_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# 未登录也可访问：登录页、登录/登出接口、前端静态资源
_PUBLIC_PATHS = frozenset({"/login", "/api/auth/login", "/api/auth/logout"})


def _is_public_path(path: str) -> bool:
    return (path in _PUBLIC_PATHS or path == "/static"
            or path.startswith("/static/"))


def create_app(cfg: SystemConfig, models: list[ModelEntry], *,
               max_concurrent: int = 4) -> FastAPI:
    loopback = cfg.web.host in ("127.0.0.1", "localhost", "::1")
    # 先把环境变量凭据导入管理员库，再判断是否启用登录——首次带 .env
    # 启动的非回环部署才不会误报“没有管理员”。
    admin_store = web_auth.AdminStore(cfg.workspace.root)
    admin_store.sync_from_env(cfg.web.password_env,
                              cfg.web.admin_username_env,
                              cfg.web.admin_password_env)
    auth_enabled = admin_store.has_admins()
    if cfg.web.remote_auth_required and not loopback and not auth_enabled:
        raise ConfigurationError(
            f"Web 控制台绑定 {cfg.web.host} 时必须配置管理员凭据：在 .env 中"
            f"设置 {cfg.web.admin_username_env} 与 "
            f"{cfg.web.admin_password_env}（旧版 {cfg.web.password_env} 也兼容）"
            "后重启")
    limiter = web_auth.LoginRateLimiter()
    vault.configure(cfg.workspace.root)   # 密钥库落在工作区（vault.db/vault.key）
    from spiritscout.resources.assistant import ResourceAssistant
    from spiritscout.resources.store import ResourceStore, resource_root
    from spiritscout.web.projects import mount_projects
    from spiritscout.web.resources import mount_resources

    # 先建 store 再建 manager：两者共用同一迁移实例，避免并发跑一次性迁移
    resource_store = ResourceStore(resource_root(cfg), cfg.resources.freshness_hours)
    manager = SessionManager(cfg, models, max_concurrent=max_concurrent,
                             resources=resource_store)
    resource_assistant = ResourceAssistant(cfg, resource_store)
    from spiritscout.web.storage import StorageCleaner
    storage = StorageCleaner(manager)

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
        await close_manual_distills(manager)
        await storage.close()
        await resource_assistant.close()
        await manager.aclose()

    app = FastAPI(title="灵枢 SpiritScout 控制台", version=__version__,
                  lifespan=lifespan)
    app.state.manager = manager
    app.state.storage = storage
    app.state.auth = admin_store
    app.state.auth_enabled = auth_enabled
    mount_resources(app, cfg, resource_store, resource_assistant)
    mount_projects(app, resource_store, manager)
    from spiritscout.web.console_insights import mount_console_insights
    from spiritscout.web.session_experience import (
        close_manual_distills,
        mount_experience_routes,
    )

    mount_console_insights(app, cfg, manager)
    mount_experience_routes(app, cfg, manager)
    app.state.resources = resource_store

    @app.get('/api/mcp/health')
    def mcp_health() -> dict[str, Any]:
        pool = manager._mcp_preflight
        return {'servers': pool.snapshot() if pool is not None else []}

    def _secure(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.middleware("http")
    async def console_security(request: Request, call_next):
        """统一的控制台安全边界。

        管理员登录启用后，浏览器走签名 Cookie，API 客户端仍可用 Basic；
        服务端永不下发 WWW-Authenticate，避免浏览器弹原生认证框。
        跨站写防护始终启用——恶意网页同样能向本机回环地址提交表单。
        """
        path = request.url.path
        # DNS 重绑定防护：无管理员的本机模式只接受回环 Host——恶意网页把
        # 自有域名解析到 127.0.0.1 后，Host 与 Origin 都是攻击者域名，
        # 下面的同源自洽检查分辨不出；远程访问应配置管理员凭据。
        if loopback and not auth_enabled:
            if (request.url.hostname or "").lower() not in _LOCAL_HOSTNAMES:
                return _secure(PlainTextResponse(
                    "拒绝非同源主机访问（本机控制台仅响应回环地址）",
                    status_code=403))
        username = ""
        if auth_enabled:
            username = admin_store.verify_session(
                request.cookies.get(web_auth.SESSION_COOKIE, "")) or ""
            if not username:
                supplied = web_auth.parse_basic_header(
                    request.headers.get("authorization", ""))
                if supplied and admin_store.verify(*supplied):
                    username = supplied[0]
            if not username and not _is_public_path(path):
                if path == "/api" or path.startswith("/api/"):
                    return _secure(JSONResponse(
                        {"detail": "需要管理员登录"}, status_code=401))
                return _secure(RedirectResponse("/login", status_code=302))
        request.state.authenticated = bool(username)
        request.state.username = username
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            fetch_site = request.headers.get("sec-fetch-site", "").lower()
            origin = request.headers.get("origin", "").rstrip("/")
            expected = str(request.base_url).rstrip("/")
            if fetch_site == "cross-site" or (origin and origin != expected):
                return _secure(PlainTextResponse(
                    "拒绝跨站写请求", status_code=403))
        return _secure(await call_next(request))

    # ------------------------------------------------------------ 登录

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        if not auth_enabled or request.state.authenticated:
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(
            _LOGIN.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/login")
    def login(payload: LoginPayload, request: Request) -> Response:
        if not auth_enabled:
            return JSONResponse({"detail": "未配置管理员凭据"},
                                status_code=400)
        client = request.client.host if request.client else "unknown"
        retry = limiter.retry_after(client)
        if retry is not None:
            return JSONResponse(
                {"detail": "登录尝试过于频繁，请稍后再试"},
                status_code=429, headers={"Retry-After": str(retry)})
        username = payload.username.strip()
        if (not username or len(username) > web_auth.MAX_USERNAME_LENGTH
                or len(payload.password) > web_auth.MAX_PASSWORD_LENGTH
                or not admin_store.verify(username, payload.password)):
            limiter.failure(client)
            return JSONResponse({"detail": "用户名或密码错误"},
                                status_code=401)
        limiter.reset(client)
        forwarded = request.headers.get("x-forwarded-proto", "")
        secure = (request.url.scheme == "https"
                  or forwarded.split(",")[0].strip().lower() == "https")
        response = JSONResponse({"ok": True, "username": username})
        response.set_cookie(
            web_auth.SESSION_COOKIE, admin_store.issue_session(username),
            max_age=web_auth.SESSION_TTL_SECONDS, httponly=True,
            samesite="lax", secure=secure, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/logout", status_code=204)
    async def logout() -> Response:
        response = Response(status_code=204)
        response.delete_cookie(web_auth.SESSION_COOKIE, path="/")
        return response

    # ------------------------------------------------------------ 前端

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = _STATIC.read_text(encoding="utf-8")
        if cfg.gateway.lightweight_local:
            # 轻量部署标记：前端据此收窄模式入口（只留 CTF/AWDP）
            html = html.replace(
                "<head>", "<head><script>window.__SPIRITSCOUT_LIGHT__=true</script>", 1)
        return HTMLResponse(html)

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
        if cfg.gateway.lightweight_local and (
                mode in (RunMode.SRC, RunMode.COMPETITION) or mode is None):
            # 轻量部署（Ollama 离线赛台）只承载 CTF 解题与 AWDP 修补；
            # auto 路由可能落到 SRC，一并拒绝，前端已按部署收窄选项
            raise HTTPException(
                422, "轻量本地部署仅支持 CTF 解题与 AWDP 修补模式；"
                     "SRC/智能体模式请使用正常部署")
        staged = resolve_staged_uploads(cfg, payload)
        try:
            plan = prepare_launch(LaunchRequest(
                mode=mode,
                target=payload.get("target") or None,
                extra_scope=list(payload.get("scope") or []),
                platform=payload.get("platform") or None,
                allow_sensitive=bool(payload.get("allow_sensitive", False)),
                rps=float(payload.get("rps", 10.0)),
                flag_format=payload.get("flag_format") or None,
                multiple_answers=bool(payload.get("multiple_answers", False)),
                ctf_type_hints=list(payload.get("ctf_type_hints") or []),
                ctf_type_note=str(payload.get("ctf_type_note") or ""),
                challenges_yaml=payload.get("challenges_yaml") or None,
                platform_yaml=payload.get("platform_yaml") or None,
                rules_yaml=payload.get("rules_yaml") or None,
                contest_type=payload.get("contest_type"),
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

    @app.post("/api/sessions/{session_id}/model")
    async def set_session_model(session_id: str, payload: SessionModelPayload) -> dict[str, Any]:
        _require(session_id)
        try:
            session = manager.set_model(session_id, payload.model)
        except SpiritScoutError as exc:
            raise HTTPException(422, str(exc)) from exc
        return session.brief()

    @app.post("/api/sessions/{session_id}/agent-sessions", status_code=201)
    async def create_agent_session(session_id: str, payload: AgentSideSessionPayload) -> dict[str, Any]:
        from spiritscout.web.workbench import side_session_source

        parent = _require(session_id)
        try:
            context = side_session_source(parent, payload.actor, payload.task_id)
            mission = payload.mission.strip()
            if not mission:
                raise SpiritScoutError("请填写旁路任务")
            plan = copy.deepcopy(parent.plan or manager._rebuild_plan(parent))
            if plan is None:
                raise SpiritScoutError("无法恢复原会话授权，请先恢复原会话")
            if plan.authorization.mode.value != parent.mode:
                raise SpiritScoutError("原会话模式与授权不一致，无法创建旁路")
            plan.mission, plan.mode_auto, plan.needs_mode_routing = mission, False, False
            request = {**copy.deepcopy(parent.launch_request), "mode": parent.mode,
                       "mode_auto": False, "mission": mission, "target": parent.launch_request.get("target") or parent.target,
                       "project_id": parent.project_id,
                       "title": "旁路 · " + mission[:60], "side_agent": context["agent"],
                       "side_context": context, "standing_rules": parent.standing_rules, "no_llm": False}
            session = await manager.create(request, plan)
        except (SpiritScoutError, ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**session.brief(), "request": session.request}

    mount_agent_routes(app, cfg)

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

    @app.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: str,
                            payload: SessionPatchPayload) -> dict[str, Any]:
        """局部更新会话：只处理请求里显式出现的字段。

        空标题 = 恢复自动摘要名；空 project_id = 移入未分类。两个字段
        互不牵连——只移动项目不会顺手清掉用户自定义标题。
        """
        session = _require(session_id)
        fields = payload.model_fields_set
        if not fields:
            raise HTTPException(422, "未提供要修改的字段")
        if "title" in fields:
            manager.rename(session_id, str(payload.title or ""))
        if "project_id" in fields:
            try:
                manager.set_project(session_id, str(payload.project_id or ""))
            except SpiritScoutError as exc:
                raise HTTPException(422, str(exc)) from exc
        return session.brief(include_summary=False)

    @app.get("/api/sessions/{session_id}/state")
    def session_state(session_id: str) -> dict[str, Any]:
        return _read_state(_require(session_id))

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

    mount_artifact_routes(app, _require)
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

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_session(session_id: str) -> dict[str, Any]:
        session = await manager.stop(session_id)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return session.brief(include_summary=False)

    @app.post("/api/sessions/{session_id}/pause")
    async def pause_session(session_id: str) -> dict[str, Any]:
        """协作式暂停：灵枢图节点与子 Agent 步骤在检查点挂起（在途动作跑完）。"""
        try:
            session = await manager.set_paused(session_id, True)
        except SpiritScoutError as exc:
            raise HTTPException(409, str(exc)) from exc
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        return session.brief(include_summary=False)

    @app.post("/api/sessions/{session_id}/resume")
    async def resume_paused_session(session_id: str) -> dict[str, Any]:
        """从暂停继续（与「续传」不同：会话从未停止，只是检查点放行）。"""
        try:
            session = await manager.set_paused(session_id, False)
        except SpiritScoutError as exc:
            raise HTTPException(409, str(exc)) from exc
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

    from spiritscout.web.dashboard import DashboardReader

    dashboard_reader = DashboardReader()

    @app.get("/api/dashboard")
    def dashboard_data(period: str = Query("today", pattern="^(hour|today|7d|30d|all)$")) -> dict[str, Any]:
        """北京时间的执行、成果、费用与缓存总览；只读，不干预调度。"""
        from spiritscout.knowledge.experience import experience_db_path, read_recent

        data = dashboard_reader.build(manager.list(), period=period)
        data["experience"] = read_recent(
            experience_db_path(cfg.experience.root, cfg.workspace.root), limit=8)
        return data

    @app.get("/api/experience")
    def experience_page(q: str = "", tag: str = "",
                        limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0)) -> dict[str, Any]:
        """经验提炼页：自我进化的全部产出（可检索/按标签过滤）+ 提炼过程记录。

        history 从各会话审计尾段提取 experience.distill 事件——含失败与
        跳过，让「学没学到、为什么没学到」对用户完全可见。
        """
        from spiritscout.knowledge.experience import experience_db_path, read_page

        data = read_page(
            experience_db_path(cfg.experience.root, cfg.workspace.root),
            q=q, tag=tag, limit=limit, offset=offset)
        titles = {s.session_id: s.brief(include_summary=False)["title"]
                  for s in manager.list()}
        for item in data["items"]:
            item["source_title"] = titles.get(
                str(item.get("source_session") or ""), "")
        data["history"] = _distill_history(cfg)
        return data

    @app.delete("/api/experience/{insight_id}")
    def delete_experience(insight_id: str) -> dict[str, Any]:
        """人工审核：删除一条提炼跑偏的经验（如把单题步骤当成可复用事实）。

        经验库是模型产出、用户可审可清；删除在系统审计留痕（actor=user），
        被删内容不参与后续会话召回。
        """
        from spiritscout.knowledge.experience import delete_insight, experience_db_path

        try:
            item = delete_insight(
                experience_db_path(cfg.experience.root, cfg.workspace.root),
                insight_id, audit=AuditLogger(cfg.audit_log_path))
        except SpiritScoutError as exc:
            raise HTTPException(500, str(exc)) from exc
        if item is None:
            raise HTTPException(404, f"经验不存在: {insight_id}")
        return {"deleted": insight_id}

    def _distill_history(cfg: SystemConfig, *, limit: int = 12) -> list[dict[str, Any]]:
        """最近提炼记录（成功/空/失败/跳过）：会话审计尾段反向扫描。"""
        sessions_dir = Path(cfg.workspace.root) / "sessions"
        events: list[dict[str, Any]] = []
        if not sessions_dir.is_dir():
            return events
        for audit_path in sorted(sessions_dir.glob("*/audit.jsonl")):
            sid = audit_path.parent.name
            try:
                with audit_path.open("rb") as fh:
                    fh.seek(0, 2)
                    size = fh.tell()
                    fh.seek(max(0, size - 131072))     # 提炼在收尾：只看尾段 128KB
                    tail = fh.read().decode("utf-8", "ignore")
            except OSError:
                continue
            for line in tail.splitlines():
                if '"experience.distill"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "experience.distill":
                    continue
                events.append({"session_id": sid, "ts": rec.get("ts", ""),
                               **(rec.get("detail") or {})})
        events.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
        return events[:limit]

    @app.post("/api/sessions/{session_id}/questions/{question_id}")
    async def answer_question(session_id: str, question_id: str,
                              payload: QuestionAnswerPayload) -> dict[str, Any]:
        """回答模型发起的提问：{"choice": 选项下标} 或 {"choices": [多选下标]}
        或 {"text": 自由输入}，{"add_targets": [...]} 为用户勾选的扩围目标
        （写入会话批准集）。"""
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        try:
            ok = session.questions.answer(
                question_id, choice=payload.choice, choices=payload.choices,
                text=payload.text, add_targets=payload.add_targets)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not ok:
            raise HTTPException(409, f"待答问题不存在或已作答: {question_id}")
        return {"question_id": question_id, "answered": True}

    mount_collaboration_routes(app, cfg, manager, _require)

    @app.get("/api/storage/sessions")
    async def storage_sessions() -> dict[str, Any]:
        rows = [{"session_id": s.session_id, "title": s.display_title(),
                 "status": s.status, "blocked_reason": storage.blocked_reason(s)}
                for s in manager.list()]
        return await asyncio.to_thread(storage.overview, rows)

    @app.get("/api/sessions/{session_id}/storage")
    async def storage_preview(session_id: str) -> dict[str, Any]:
        _require(session_id)
        try:
            return await asyncio.to_thread(storage.preview, session_id)
        except (SpiritScoutError, OSError, sqlite3.Error) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/sessions/{session_id}/storage/cleanup")
    async def storage_cleanup(session_id: str, payload: StorageCleanupPayload) -> dict[str, Any]:
        _require(session_id)
        if not payload.confirm:
            raise HTTPException(422, "请先查看清理范围，再确认执行")
        try:
            return storage.start(session_id, database=payload.database, caches=payload.caches)
        except SpiritScoutError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/storage/job")
    async def storage_job(session_id: str) -> dict[str, Any]:
        _require(session_id)
        return dict(storage.jobs.get(session_id, {"status": "idle", "session_id": session_id}))

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

    mount_report_routes(app, _require)

    mount_model_routes(app, cfg)

    return app
