"""会话注册、持久化和生命周期管理。

各会话使用独立工作区和配置副本；meta.json 用于重启恢复。
重启时将残留的 running 会话标记为 interrupted。"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from spiritscout.agents.ring import RingLedger
from spiritscout.config.model_identity import resolve_model
from spiritscout.config.settings import ModelEntry, SystemConfig
from spiritscout.core.audit import AuditLogger
from spiritscout.core.cleanup import finish_cleanup
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import Authorization, BlackboardLayer, RunMode
from spiritscout.core.pause import PauseGate
from spiritscout.launch import (
    LaunchPlan,
    LaunchRequest,
    extract_targets,
    parse_challenges_yaml,
    prepare_launch,
)
from spiritscout.policy.approvals import ApprovalBroker
from spiritscout.policy.questions import QuestionBroker
from spiritscout.policy.scope import ScopeGuard
from spiritscout.web import agentcfg, vault
from spiritscout.web.attachment_uploads import materialize_uploads
from spiritscout.web.session_assistant import (
    _NAMING_MAX_TOKENS,
    _NAMING_SYSTEM,
    _bound_model,
    _conversation_gateway,
    _llm_discover_platform,
    _llm_extract_platform,
    _llm_name,
    _llm_route_intent,
    _llm_route_mode,
    _llm_scope_question,
    _master_reply,
    _naming_model_candidates,
    _preflight_models,
    _routed_competition_rules_yaml,
    _self_view,
    _session_run_facts,
)
from spiritscout.web.session_execution import SessionExecution
from spiritscout.web.session_models import (
    DIALOGUE_HISTORY_MAX,
    TERMINAL_STATES,
    Session,
    _remember_reply,
)
from spiritscout.web.session_scope import (
    _clean_scope_question,
    _is_scope_domain,
    _project_target_key,
    _project_target_value,
)

if TYPE_CHECKING:
    from spiritscout.resources.store import ResourceStore


# 原入口继续可用，模块之间直接依赖各自所属的数据或能力。
__all__ = [
    "_preflight_models",
    "Session",
    "TERMINAL_STATES",
    "DIALOGUE_HISTORY_MAX",
    "_remember_reply",
    "_bound_model",
    "_conversation_gateway",
    "_llm_route_mode",
    "_llm_route_intent",
    "_routed_competition_rules_yaml",
    "_llm_extract_platform",
    "_llm_discover_platform",
    "_NAMING_SYSTEM",
    "_NAMING_MAX_TOKENS",
    "_naming_model_candidates",
    "_llm_name",
    "_self_view",
    "_session_run_facts",
    "_master_reply",
    "_llm_scope_question",
    "_is_scope_domain",
    "_project_target_value",
    "_project_target_key",
    "_clean_scope_question",
    "SessionManager",
]


def _coerce_meta(data: dict[str, Any]) -> dict[str, Any]:
    """元数据字段类型收敛：旧版本/手改文件里类型漂移的字段拉回正型。"""
    def _dict(v: Any) -> dict[str, Any]:
        return v if isinstance(v, dict) else {}

    def _str(v: Any) -> str:
        return v if isinstance(v, str) else ("" if v is None else str(v))

    def _float(v: Any, default: float = 0.0) -> float:
        try:
            result = float(v)
            return result if math.isfinite(result) else default
        except (TypeError, ValueError, OverflowError):
            return default

    def _opt_float(v: Any) -> float | None:
        try:
            return None if v is None or not math.isfinite(float(v)) else float(v)
        except (TypeError, ValueError, OverflowError):
            return None

    def _list(v: Any) -> list:
        return v if isinstance(v, list) else []

    status = data.get("status", "interrupted")
    if not isinstance(status, str) or status not in TERMINAL_STATES | {"created", "running"}:
        status = "interrupted"
    summary = dict(_dict(data.get("summary")))
    if "run" in summary:
        summary["run"] = _dict(summary["run"])
    return {
        **data,
        "session_id": _str(data.get("session_id")),
        "mode": _str(data.get("mode")),
        "target": _str(data.get("target")),
        "title": _str(data.get("title")),
        "auto_title": _str(data.get("auto_title")),
        "project_id": _str(data.get("project_id"))[:64],
        "model": _str(data.get("model") or _dict(data.get("request")).get("model"))[:512],
        "request": _dict(data.get("request")),
        "launch_request": _dict(data.get("launch_request")),
        "execution_plan": _dict(data.get("execution_plan")),
        "standing_rules": _str(data.get("standing_rules")),
        "opinions": [e for e in _list(data.get("opinions"))
                     if isinstance(e, dict)],
        "user_inbox": [item for item in _list(data.get("user_inbox")) if isinstance(item, dict)],
        "handled_user_messages": [item for item in _list(data.get("handled_user_messages")) if isinstance(item, str)],
        "dialogue_history": [
            e for e in _list(data.get("dialogue_history"))
            if isinstance(e, dict) and e.get("role") in {"user", "assistant"}
            and isinstance(e.get("content"), str) and (e["content"] or e.get("attachments"))],
        "created_at": _float(data.get("created_at")),
        "started_at": _opt_float(data.get("started_at")),
        "finished_at": _opt_float(data.get("finished_at")),
        "summary": summary,
        "error": _str(data.get("error")),
        "status": status,
        "phase": _str(data.get("phase")) or status,
        "paused": data.get("paused") is True,
        "excluded_seconds": max(0.0, _float(data.get("excluded_seconds"))),
    }


class SessionManager:
    """会话注册表与生命周期（创建/启动/停止/删除/重启重建）。

    任务创建走「创建队列 + 监督协程」：HTTP 端点只入队，监督协程（在应用
    生命周期内启动、不隶属任何请求作用域）负责 spawn 会话任务。原因：
    starlette TestClient/ASGI 栈会在请求返回时取消请求任务组内的 asyncio
    任务——若会话任务在端点内直接 create_task，响应一发出就会被误取消
    （实测 t≈响应完成时刻，CancelledError 沿主循环 await 链传播）。
    """

    def __init__(self, cfg: SystemConfig, models: list[ModelEntry],
                 *, max_concurrent: int | None = None,
                 resources: ResourceStore | None = None):
        self._cfg = cfg
        self._models = models
        # 兼容旧调用方传入 max_concurrent；会话执行不再等待全局名额。
        self._sessions: dict[str, Session] = {}
        self.storage_busy: set[str] = set()
        self._queue: asyncio.Queue[
            tuple[Session, SystemConfig, LaunchPlan, bool, list[ModelEntry], bool]] = \
            asyncio.Queue()
        self._supervisor: asyncio.Task | None = None
        self._distill_resume_task: asyncio.Task | None = None
        self.resource_sync_task: asyncio.Task[None] | None = None
        self._naming_tasks: set[asyncio.Task] = set()   # 会话自动命名（后台旁路）
        self._mcp_preflight = None
        # 项目目录由 app 注入（与项目 API 共用同一实例，避免并发迁移）；
        # 独立使用时惰性创建，保持 SessionManager(cfg, []) 的测试兼容
        self._store = resources
        self.root = Path(cfg.workspace.root) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._scan()
        if resources is not None:
            self._sync_projects()

    def _resources(self) -> ResourceStore:
        if self._store is None:
            from spiritscout.resources.store import ResourceStore, resource_root

            self._store = ResourceStore(
                resource_root(self._cfg), self._cfg.resources.freshness_hours)
        return self._store

    async def wait_for_resources(self) -> None:
        """首页不等待历史同步；任务仍使用同步完成后的相同共享事实。"""
        if self.resource_sync_task is not None:
            await asyncio.shield(self.resource_sync_task)

    def _sync_projects(self) -> None:
        """启动对账：catalog 成员行是事实源，meta 跟随（含清空）。

        无成员行的旧会话才按 meta 补登记；已显式未分类的会话绝不因 meta
        残留旧项目而被重新归类。逐个 best-effort，不阻断控制台启动。
        """
        store = self._store
        if store is None:
            return
        for session in list(self._sessions.values()):
            try:
                current = store.project_for_session(session.session_id)
                if current is None:
                    project_id = session.project_id
                    if project_id and store.project(project_id) is not None:
                        store.set_session_project(session.session_id, project_id)
                    else:
                        project_id = ""
                        store.set_session_project(session.session_id, "")
                else:
                    project_id = current
                if session.project_id != project_id:
                    session.project_id = project_id
                    self._persist(session)
            except (OSError, sqlite3.Error, SpiritScoutError, ValueError) as exc:
                print(f"[spiritscout] 会话项目对账失败（{session.session_id}）:"
                      f" {exc}", file=sys.stderr)

    # ------------------------------------------------------------ 注册表

    def _scan(self) -> None:
        """启动重建：加载历史会话；running 残留 → interrupted（进程已死）。

        损坏或缺关键元的 meta.json 隔离改名（meta.corrupt-<时刻>.json）并
        告警——不静默吞掉也不自动删，用户数据误判可人工找回；类型漂移的
        字段（旧版本/手改文件）按正型收敛，不让一个坏字段炸掉整个注册表。
        """
        for meta in sorted(self.root.glob("*/meta.json")):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._quarantine_meta(meta, f"解析失败: {exc}")
                continue
            if not isinstance(data, dict) or not str(data.get("session_id") or ""):
                self._quarantine_meta(meta, "不是键值映射或缺 session_id")
                continue
            try:
                data = _coerce_meta(data)
                session = Session(
                    session_id=data["session_id"], mode=data["mode"],
                    target=data["target"], workspace=meta.parent,
                    title=data["title"],
                    auto_title=data["auto_title"],
                    project_id=data["project_id"],
                    model=data["model"],
                    request=data["request"],
                    launch_request=data["launch_request"],
                    execution_plan=data["execution_plan"],
                    opinions=data["opinions"],
                    dialogue_history=data["dialogue_history"],
                    user_inbox=data["user_inbox"],
                    handled_user_messages=data["handled_user_messages"],
                    status=data.get("status", "interrupted"),
                    phase=data.get("phase", "interrupted"),
                    created_at=data["created_at"],
                    started_at=data["started_at"],
                    finished_at=data["finished_at"],
                    summary=data["summary"], error=data["error"],
                    standing_rules=data["standing_rules"],
                )
                session.pause_gate.restore(data["paused"], data["excluded_seconds"])
                session.restore_user_feed()
            except sqlite3.Error as exc:
                print(f"[spiritscout] 会话数据库读取失败，已跳过 {meta.parent.name}"
                      f"（{exc}）；元数据与数据库文件保持原样", file=sys.stderr)
                continue
            except (TypeError, ValueError) as exc:
                self._quarantine_meta(meta, f"字段类型异常: {exc}")
                continue
            # 暂停态与扣账跨重启恢复：静默置门（不补记审计）
            if session.status not in TERMINAL_STATES:
                session.status = "interrupted"
                session.phase = "interrupted"
                session.finished_at = session.finished_at or time.time()
                self._persist(session)
            self._sessions[session.session_id] = session

    def _quarantine_meta(self, meta: Path, reason: str) -> None:
        """损坏元数据隔离：改名留证 + 标准错误告警（不静默、不删除）。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = meta.with_name(f"meta.corrupt-{stamp}.json")
        serial = 0
        while target.exists():                  # 同秒多份损坏不互相覆盖
            serial += 1
            target = meta.with_name(f"meta.corrupt-{stamp}-{serial}.json")
        try:
            meta.rename(target)
        except OSError as exc:
            print(f"[spiritscout] 会话元数据损坏且无法隔离（{reason}）: {exc}",
                  file=sys.stderr)
            return
        print(f"[spiritscout] 会话元数据损坏已隔离：{meta.parent.name}"
              f"（{reason}）→ {target.name}", file=sys.stderr)

    @staticmethod
    def _snapshot_plan(plan: LaunchPlan) -> dict[str, Any]:
        source = dict(plan.source_request.__dict__) if plan.source_request else None
        if source and source.get("mode") is not None:
            source["mode"] = source["mode"].value
        rules = asdict(plan.rules) if plan.rules is not None else None
        if rules is not None:
            rules["contest_type"] = plan.rules.contest_type.value
        return {
            "source_request": source,
            "rules_yaml": yaml.safe_dump(rules, allow_unicode=True),
            "authorization": plan.authorization.model_dump(mode="json"),
            "target": plan.target, "mission": plan.mission,
            "challenges_yaml": yaml.safe_dump(plan.challenges, allow_unicode=True),
            "platform_yaml": yaml.safe_dump(plan.platform_profile, allow_unicode=True),
            "flag_patterns": plan.flag_patterns,
            "generate_wp": plan.generate_wp,
            "mode_auto": plan.mode_auto, "needs_mode_routing": plan.needs_mode_routing,
        }

    def _persist(self, session: Session) -> None:
        if session.execution_plan and isinstance(session.plan, LaunchPlan):
            session.execution_plan = self._snapshot_plan(session.plan)
        if session.bb_path.exists():
            from spiritscout.blackboard import Blackboard

            blackboard = Blackboard(session.bb_path)
            try:
                mailbox = {"handled_ids": session.handled_user_messages}
                if blackboard.read(BlackboardLayer.TARGET_AUTH, "user-mailbox") != mailbox:
                    blackboard.write(BlackboardLayer.TARGET_AUTH, "user-mailbox", mailbox,
                                     updated_by="session-mailbox")
            finally:
                blackboard.close()
        payload = json.dumps({
            "session_id": session.session_id, "mode": session.mode,
            "target": session.target, "title": session.title,
            "auto_title": session.auto_title,
            "project_id": session.project_id,
            "model": session.model,
            "request": session.request,
            "launch_request": session.launch_request,
            "execution_plan": session.execution_plan,
            "standing_rules": session.standing_rules,
            "opinions": session.opinions[-8:],
            "dialogue_history": session.dialogue_history[-DIALOGUE_HISTORY_MAX:],
            "user_inbox": session.user_inbox,
            "handled_user_messages": session.handled_user_messages,
            "status": session.status, "phase": session.phase,
            "created_at": session.created_at,
            "started_at": session.started_at, "finished_at": session.finished_at,
            "summary": session.summary, "error": session.error,
            "paused": session.pause_gate.paused,
            "excluded_seconds": round(session.pause_gate.excluded_seconds, 1),
        }, ensure_ascii=False, indent=2)
        # 原子替换：写半截崩进程不留损坏的 meta.json（损坏文件扫描时会被隔离）
        tmp = session.meta_path.with_name("meta.json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(session.meta_path)

    def list(self) -> list[Session]:
        # 先复制再遍历：/api/sessions 等同步端点在线程池执行，与本事件
        # 循环的创建/删除并发时会撞「dictionary changed size」
        return sorted(self._sessions.copy().values(),
                      key=lambda s: s.created_at, reverse=True)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def set_model(self, session_id: str, ref: str) -> Session:
        from spiritscout.config import load_model_config

        session = self.get(session_id)
        if session is None:
            raise SpiritScoutError(f"会话不存在: {session_id}")
        ref = ref.strip()
        entries = vault.apply_keys(load_model_config(self._cfg.models_config_path)) if ref else None
        selected = resolve_model(entries, ref).key if ref else ""
        previous, previous_entries = session.model, session._model_entries
        session.model = selected
        if entries is not None:
            session._model_entries = entries
        try:
            self._persist(session)
        except Exception:
            session.model, session._model_entries = previous, previous_entries
            raise
        AuditLogger(session.audit_path).log("session.model_changed", "user", {
            "previous": previous, "model": selected})
        return session

    def _set_phase(self, session: Session, phase: str,
                   audit: AuditLogger | None = None) -> None:
        """持久化用户可见阶段；审计存在时同步留痕。"""
        if session.phase == phase:
            return
        session.phase = phase
        self._persist(session)
        if audit is not None:
            audit.log("system.lifecycle", "runtime", {
                "action": "phase_changed", "phase": phase})

    # ------------------------------------------------------------ 生命周期

    def _session_cfg(self, session: Session, budget_tokens: int | None) -> SystemConfig:
        """会话级配置副本：全部状态路径重定向到会话工作区（隔离核心）。"""
        cfg = self._cfg.model_copy(deep=True)
        ws = session.workspace.resolve()
        # 控制面根目录：会话工作区在基础工作区内部，沙箱遮蔽需连基础根
        # 一起（vault 密钥库/兄弟会话/审计都在里面），只遮会话目录不够
        cfg.workspace.guard_root = str(self._cfg.workspace.root)
        try:
            cfg.mode = RunMode(session.mode)
        except ValueError:
            # 对话/规则展示态会话（session.mode=dialogue/rules）：配置副本
            # 只服务回复/审计路径，不派工——借用主配置模式，别让 RunMode
            # 构造崩掉续传（续传是否派工由语义路由重新裁决）
            cfg.mode = self._cfg.mode
        from spiritscout.resources.store import ResourceStore, resource_root

        cfg.resources.root = str(resource_root(self._cfg))
        resources = ResourceStore(cfg.resources.root, cfg.resources.freshness_hours)
        cfg.mcp_servers = resources.effective_mcp(cfg.mcp_servers)
        cfg.workspace.root = str(ws)
        # 经验库跨会话共享：不随会话工作区重定向（显式配置优先）
        cfg.experience.root = self._cfg.experience.root or str(self._cfg.workspace.root)
        cfg.blackboard.sqlite_path = str(session.bb_path)
        cfg.knowledge.sqlite_path = str(ws / "kb.db")
        cfg.knowledge.chroma_path = str(ws / "chroma")
        cfg.audit_log_path = str(session.audit_path)
        cfg.report.output_dir = str(ws / "reports")
        if budget_tokens:
            cfg.gateway.budget_tokens = budget_tokens
        return cfg

    # ------------------------------------------------------------ 监督协程

    def start(self) -> None:
        """启动监督协程（幂等；应用 lifespan startup 或直接使用时懒启动）。"""
        if self._mcp_preflight is None:
            from spiritscout.tools.mcp_preflight import McpPreflight

            self._mcp_preflight = McpPreflight()
            try:
                self._mcp_preflight.start(self._resources().effective_mcp(self._cfg.mcp_servers))
            except Exception as exc:  # noqa: BLE001 —— 目录读取故障不阻塞服务启动
                print(f'[spiritscout] MCP 启动预检配置读取失败：{type(exc).__name__}', file=sys.stderr)
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(
                self._supervise(), name="spiritscout-supervisor")
        # 每进程只扫描一次遗留快照。start() 也会在每次新建会话时调用；
        # 若任务结束后重扫，会把当前进程仍在跑的 snapshot 重复提炼。
        if self._distill_resume_task is None:
            self._distill_resume_task = asyncio.create_task(
                self._resume_pending_distills(),
                name="spiritscout-distill-resume")

    async def _resume_pending_distills(self) -> None:
        """恢复进程中止时遗留的提炼快照；失败只留痕，不影响会话控制台。"""
        if not self._models:
            return
        await self.wait_for_resources()

        sessions = list(self._sessions.values())

        def discover():
            from spiritscout.config import load_model_config
            from spiritscout.ctf.report_jobs import status as report_status
            from spiritscout.knowledge.distillation import pending_distill_snapshots

            models = vault.apply_keys(load_model_config(self._cfg.models_config_path))
            pending = [session for session in sessions
                       if pending_distill_snapshots(session.workspace)
                       or report_status(session.workspace).get("status") in {"queued", "running"}]
            return models, pending

        try:
            models, pending = await finish_cleanup(asyncio.to_thread(discover))
        except SpiritScoutError as exc:
            print(f"[spiritscout] 提炼快照恢复跳过（模型注册表不可用）: {exc}",
                  file=sys.stderr)
            return
        from spiritscout.ctf.report_jobs import resume as resume_report
        from spiritscout.knowledge.distillation import resume_pending_distills

        for session in pending:
            if self.get(session.session_id) is not session:
                continue
            try:
                cfg = self._session_cfg(session, None)
                assignments = agentcfg.read_assignments(cfg.agent_models_path)
                from spiritscout.gateway import ModelGateway, ModelRegistry
                from spiritscout.gateway.gateway import AgentBoundGateway
                report_gateway = ModelGateway(ModelRegistry(models), cfg.gateway, audit=AuditLogger(session.audit_path))
                from spiritscout.gateway.usage import bind_usage
                bind_usage(report_gateway, session.workspace / "usage.json")
                resume_report(session.workspace, AgentBoundGateway(report_gateway, assignments.get("master", "")),
                              AuditLogger(session.audit_path))
                await resume_pending_distills(
                    workspace=session.workspace, cfg=cfg,
                    models=models, agent_models=assignments)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 后台恢复不拖垮控制台
                print(f"[spiritscout] 提炼快照恢复失败（{session.session_id}）:"
                      f" {type(exc).__name__}: {exc}", file=sys.stderr)

    async def _supervise(self) -> None:
        """从创建队列取会话并 spawn 执行任务（不隶属任何请求作用域）。"""
        while True:
            session, cfg, plan, no_llm, models, resume = await self._queue.get()
            try:
                if resume:
                    if session.status != "running":
                        continue        # 续传排队期间状态被并发改动（如再次停止）
                elif session.status != "created":
                    continue            # 排队期间已被停止/删除
                session.status = "running"
                session.phase = "starting"
                session.started_at = session.started_at or time.time()
                self._persist(session)
                from spiritscout.tools.mcp_preflight import current_preflight

                token = current_preflight.set(self._mcp_preflight)
                try:
                    session.task = asyncio.create_task(
                        self._run(session, cfg, plan, no_llm, models, resume),
                        name=f"spiritscout-{session.session_id}",
                    )
                finally:
                    current_preflight.reset(token)
                session.task.add_done_callback(
                    lambda task, current=session: self._settle_task(current, task))
                if session.status != "running":
                    session.task.cancel()   # spawn 瞬间被并发 stop：立即取消
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 监督器不能因单会话退出
                session.status = "failed"
                session.phase = "failed"
                session.error = f"启动监督失败：{type(exc).__name__}: {exc}"[:500]
                session.finished_at = time.time()
                self._persist(session)
            finally:
                self._queue.task_done()

    def _settle_task(self, session: Session, task: asyncio.Task) -> None:
        """兜住执行协程 try 块之外的异常，禁止会话永久留在 running。"""
        if session.task is not None and session.task is not task:
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            exc = None
        if session.status in TERMINAL_STATES:
            return
        session.status = "failed" if exc is not None else "stopped"
        session.phase = session.status
        if exc is not None:
            session.error = f"执行任务异常退出：{type(exc).__name__}: {exc}"[:500]
        session.finished_at = time.time()
        self._persist(session)

    async def aclose(self) -> None:
        """应用关停：停监督协程，再停所有未终态会话（幂等）。"""
        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
        if (self._distill_resume_task is not None
                and not self._distill_resume_task.done()):
            self._distill_resume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._distill_resume_task
        for session in list(self._sessions.values()):
            if session.status not in TERMINAL_STATES:
                await self.stop(session.session_id, resume_pending=False)
        if self._mcp_preflight is not None:
            await self._mcp_preflight.close()

    async def create(self, payload: dict[str, Any], plan: LaunchPlan, *,
                     staged_files: list[Path] | None = None, automation_id: str | None = None) -> Session:
        """创建会话并入队启动（payload 已经 app 层合规校验；plan 由 launch 装配）。

        staged_files：用户任务下达前暂存的附件——在此移入会话工作区并注入
        赛题载荷（必须在入队前完成，会话任务可能即刻开工）。
        """
        if getattr(self, "_update_maintenance", False):
            raise SpiritScoutError("系统正在更新，暂不启动新会话")
        await self.wait_for_resources()
        if automation_id is not None:
            if len(automation_id) != 32 or any(c not in "0123456789abcdef" for c in automation_id):
                raise SpiritScoutError("自动任务运行标识不合法")
            existing = self.get(f"sess-auto-{automation_id}")
            if existing is not None:
                return existing
        # 热生效：Web 端改模型注册表后，新会话立即用新配置；
        # 加载失败显式报错（不静默退回旧配置）。先于建目录，失败不留痕。
        from spiritscout.config import load_model_config
        models = vault.apply_keys(
            load_model_config(self._cfg.models_config_path))
        if not models:
            raise SpiritScoutError(
                f"模型注册表为空: {self._cfg.models_config_path}（"
                "请在 Web「模型」面板或配置文件中至少注册一个云端模型）")
        selected = str(payload.get("model") or "").strip()
        if selected:
            selected = resolve_model(models, selected).key
        project_id = str(payload.get("project_id") or "").strip()[:64]
        store = self._resources()
        if project_id and store.project(project_id) is None:
            # 建工作区之前拒绝：不存在半成品目录
            raise SpiritScoutError(f"项目不存在: {project_id}")
        session_id = f"sess-auto-{automation_id}" if automation_id else f"sess-{uuid.uuid4().hex[:8]}"
        proxy_plan_id = str(payload.get('proxy_plan_id') or 'direct')
        egress_source = ''
        if payload.get('side_agent'):
            parent_id = str((payload.get('side_context') or {}).get('parent_session_id') or '')
            parent = self.get(parent_id)
            if parent and (parent.request.get('proxy_binding') or {}).get('id') == proxy_plan_id:
                egress_source = parent_id
        if proxy_plan_id != 'direct':
            from spiritscout.resources.proxy_pool import ProxyPool

            if plan.authorization.mode != RunMode.SRC:
                raise SpiritScoutError('目标代理方案目前用于 SRC 会话')
            try:
                if not egress_source:
                    ProxyPool(store).snapshot(proxy_plan_id)
            except ValueError as exc:
                raise SpiritScoutError(str(exc)) from None
        session = Session(
            session_id=session_id,
            mode=str(payload["mode"]), target=plan.target,
            workspace=self.root / session_id,
            title=str(payload.get("title") or "").strip()[:80],
            project_id=project_id,
            model=selected,
            _model_entries=models,
            request={k: v for k, v in payload.items()
                     if k not in ("challenges_yaml", "rules_yaml",
                                  "upload_ids", "project_id", "_asset_snapshot")},
            created_at=time.time(),
            standing_rules=str(payload.get("standing_rules") or ""),
        )
        session.workspace.mkdir(parents=True, exist_ok=False)
        session.plan = plan
        session.launch_request = {k: v for k, v in payload.items()
                                  if k not in ("upload_ids", "project_id", "_asset_snapshot")}
        initial_message_id = f"message-{uuid.uuid4().hex}"
        if plan.mission:
            session.remember_user_message(plan.mission, message_id=initial_message_id)
        self._sessions[session.session_id] = session
        self._persist(session)

        try:
            # 成员行无论是否加入项目都写：空串 = 明确未分类（启动对账事实源）
            if proxy_plan_id != 'direct':
                from spiritscout.traffic.egress import bind

                session.request['proxy_binding'] = bind(store, session_id, proxy_plan_id, source_session=egress_source)
                self._persist(session)
            store.set_session_project(session.session_id, session.project_id)
            if payload.get('browser_account_ids'):
                from spiritscout.resources.browser_accounts import BrowserAccounts

                BrowserAccounts(store).bind(session.session_id, payload['browser_account_ids'])
            cfg = self._session_cfg(session, payload.get("budget_tokens"))
            session.approvals = ApprovalBroker(AuditLogger(cfg.audit_log_path))
            session.questions = QuestionBroker(
                AuditLogger(cfg.audit_log_path), context_path=session.bb_path,
                interactive=session.mode != RunMode.COMPETITION.value)
            session.ring = RingLedger(AuditLogger(cfg.audit_log_path))
            session.pause_gate = PauseGate(AuditLogger(cfg.audit_log_path))
            no_llm = bool(payload.get("no_llm", False))
            # 暂存附件材料化：移入工作区 attachments/ + 注入无附件的赛题载荷
            # （方向判类/附件分析工具/题面视图直接可用）+ 种入灵枢收件箱
            records: list[dict[str, Any]] = []
            if payload.get("_asset_snapshot"):
                from spiritscout.web.asset_handoff import materialize_handoff

                records.extend(await materialize_handoff(payload["_asset_snapshot"], session.workspace, session.bb_path))
            if staged_files:
                dest_dir = session.workspace / "attachments"
                dest_dir.mkdir(parents=True, exist_ok=True)
                audit = AuditLogger(session.audit_path)
                records.extend(materialize_uploads(staged_files, dest_dir, move=True))
                for record in records:
                    audit.log("user.attachment", "user", {
                        **record, "message_id": initial_message_id})
                for ch in plan.challenges:
                    if not ch.get("attachments"):
                        ch["attachments"] = [r["path"] for r in records]
                for updir in {p.parent for p in staged_files}:
                    shutil.rmtree(updir, ignore_errors=True)
            if payload.get("side_agent"):
                context = session.request.get("side_context") or {}
                parent = self.get(str(context.get("parent_session_id") or ""))
                challenge = ((context.get("task") or {}).get("payload") or {}).get("challenge") or {}
                if parent is not None and challenge.get("attachments"):
                    rewritten = []
                    for index, name in enumerate(challenge["attachments"]):
                        source = Path(str(name))
                        if source.is_file() and source.resolve().is_relative_to((parent.workspace / "attachments").resolve()):
                            dest = session.workspace / "attachments" / f"{index}-{source.name}"
                            dest.parent.mkdir(exist_ok=True)
                            shutil.copy2(source, dest)
                            rewritten.append(str(dest.resolve()))
                        else:
                            rewritten.append(str(name))
                    challenge["attachments"] = rewritten
                    session.launch_request["side_context"] = copy.deepcopy(context)
                self._persist(session)
            # 生成的 CSV 是开场任务材料，不再作为执行中的新消息重复处理。
            if plan.mission or records:
                session.remember_user_message(plan.mission or "", message_id=initial_message_id,
                                              attachments=records, enqueue=bool(staged_files))
            self._persist(session)
            self.start()        # 直接使用（非 Web lifespan）时懒启动监督协程
            await self._queue.put((session, cfg, plan, no_llm, models, False))
        except Exception as exc:
            # 登记后中途失败（配置构建/附件搬运/入队）：落终态再原样抛出——
            # 「created」既非终态也无法续传，留下就成了删不掉也开不了的僵尸
            session.status = "failed"
            session.phase = "failed"
            session.error = f"会话创建中途失败（{type(exc).__name__}）"[:500]
            session.finished_at = time.time()
            try:
                self._persist(session)
            except Exception:  # noqa: BLE001 —— 落盘失败不遮蔽原始异常
                pass
            raise
        if not no_llm and self._cfg.web.auto_name_sessions:
            # 命名是旁路增强：不阻塞入队，失败静默（展示回退 mission 首行）
            self._schedule_naming(session, models, plan)
        return session

    def _schedule_naming(self, session: Session,
                         models: list[ModelEntry], plan: LaunchPlan) -> None:
        """创建后立刻后台命名：小模型给会话起短名，不阻塞开工。"""
        if not (plan.mission or plan.target):
            return
        if not models:
            return              # 没有模型可调（创建链路已保证注册表非空，兜底）
        task = asyncio.create_task(
            self._name_session(session, models, plan),
            name=f"spiritscout-naming-{session.session_id}")
        self._naming_tasks.add(task)
        task.add_done_callback(self._naming_tasks.discard)

    async def _name_session(self, session: Session,
                            models: list[ModelEntry], plan: LaunchPlan) -> None:
        # 命名是平台旁路调用：计量与审计都走全局台账（与开工前路由预检
        # 同一口径），不混入该会话——否则会话页的「模型消耗」背上命名
        # 开销（一句问候也被记几百 token），时间线还多出一条莫名的
        # classification 调用
        audit = AuditLogger(self._cfg.audit_log_path)
        try:
            result = await _llm_name(
                self._cfg, models, audit, mission=plan.mission or "",
                target=plan.target, mode=session.mode)
            if not result or self._sessions.get(session.session_id) is not session:
                return          # 调用失败，或会话已被删除/替换：不落库不回写
            if result.get("name"):
                session.auto_title = result["name"]
                self._persist(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 命名旁路绝不影响会话主流程
            audit.log("session.naming", "namer", {
                "action": "error",
                "error": f"{type(exc).__name__}: {exc}"[:300]})

    # ------------------------------------------------------------ 项目记忆与范围确认

    def _project_memory(self, session: Session) -> dict[str, Any] | None:
        """同项目历史记忆（未分类/读取失败返回 None）。

        记忆只作参考，不是授权：范围仍由本次会话启动计划决定，候选
        资产必须经用户勾选确认才会写进重建的计划。
        """
        if not session.project_id:
            return None
        try:
            if session.mode == RunMode.SRC.value:
                from spiritscout.resources.store import project_context

                memory = self._resources().project_memory(session.project_id, asset_limit=0, finding_limit=0)
                return project_context(memory) if memory else None
            return self._resources().project_memory(session.project_id)
        except (OSError, sqlite3.Error, SpiritScoutError, ValueError) as exc:
            print(f"[spiritscout] 读取项目记忆失败（{session.project_id}）:"
                  f" {exc}", file=sys.stderr)
            return None

    @staticmethod
    def _project_target_candidates(
            memory: dict[str, Any], plan: LaunchPlan,
            limit: int = 8) -> list[dict[str, str]]:
        """项目说明与历史资产 → 可勾选目标（按 origin/域名去重）。

        只规范化 URL/域名形态，不按排除目标筛选；是否测试由模型结合
        项目规则向用户确认，历史观测本身不构成授权。
        """
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        description = str(memory.get("description") or "")
        description_urls, description_domains = extract_targets(description)
        for value in [*description_urls, *description_domains]:
            target = _project_target_value(
                value, "url" if "://" in value else "domain")
            key = _project_target_key(target)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"target": target,
                        "why": "项目说明中的测试范围，需本次确认"})
            if len(out) >= limit:
                return out
        assets = memory.get("assets")
        for item in assets if isinstance(assets, list) else []:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            target = _project_target_value(value, str(item.get("kind") or ""))
            if not target:
                continue
            key = _project_target_key(target)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({"target": target,
                        "why": f"同项目历史观测：{value[:120]}"})
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _has_network_scope(plan: LaunchPlan) -> bool:
        scope = plan.authorization.scope
        return bool(scope.allowed_domains or scope.allowed_cidrs
                    or scope.allowed_urls)

    @staticmethod
    def _is_local_attachment_task(session: Session, mission: str) -> bool:
        """识别明确的纯本地材料任务；附件存在不等于网络任务自动免确认。"""
        attachment_dir = session.workspace / "attachments"
        if not attachment_dir.is_dir() or not any(attachment_dir.iterdir()):
            return False
        urls, domains = extract_targets(mission)
        if urls or domains:
            return False
        text = str(mission or "").lower()
        return any(word in text for word in (
            "附件", "本地", "文件", "样本", "源码", "代码审计",
            "attachment", "local file", "source code"))

    async def _confirm_project_scope(
            self, session: Session, cfg: SystemConfig,
            models: list[ModelEntry], mission: str, plan: LaunchPlan,
            audit: AuditLogger, project_memory: dict[str, Any] | None,
            *, no_llm: bool = False,
    ) -> tuple[bool, list[str], str]:
        """把项目记忆里的候选资产交用户一次答复确认。

        返回 (是否真的问过, 确认的目标, 用户答复)。记忆不是授权：只有
        用户明确确认的目标才返回，调用方据此重建启动计划；取消/
        「先不测」/会话停止返回空目标。问题与选项由模型按记忆撰写
        （LLM 不可用时用确定性兜底），目标声明原样回灌模型上下文。
        """
        if session.mode == RunMode.COMPETITION.value or not project_memory:
            return False, [], ""
        candidates = self._project_target_candidates(project_memory, plan)
        if not candidates:
            return False, [], ""
        name = str(project_memory.get("name") or "当前项目")
        question = (
            f"这个会话属于项目「{name}」。项目记忆里有这些资产："
            + "、".join(c["target"] for c in candidates[:6])
            + "。本次会话还没有任何授权目标——请确认是否同意测试以上目标，"
              "或直接输入新的目标；也可以选择这次先不测。")
        options = ["同意测试以上目标", "这次先不测"]
        targets = [{**item, "approved_by": [options[0]]} for item in candidates[:6]]
        if models and not no_llm:
            data = await _llm_scope_question(
                cfg, models, mission, project_memory, candidates, audit)
            cleaned = _clean_scope_question(data, candidates) if data else None
            if cleaned:
                question, options, targets = cleaned
        guard = ScopeGuard(plan.authorization.scope)
        session.questions.bind_guard(guard)     # 明确答复写入临时批准集取证
        self._set_phase(session, "scope_confirmation", audit)
        audit.log("master.ask_user", "master", {
            "source": "project_memory", "question": question[:500],
            "options": options, "targets": targets})
        result = await session.questions.ask(
            "master", question, options, targets, multiple=False, allow_new_targets=True)
        answer = str(result.get("answer") or "").strip()
        added = [str(t).strip() for t in (result.get("scope_added") or [])
                 if str(t).strip()]
        if answer and not added and not result.get("scope_resolved"):
            # 非 Web 的旧调用同样由模型解释，不能从否定句里直接提取域名并授权。
            from spiritscout.web.session_assistant import _llm_answer_scope
            rec = {"question": question, "options": options, "targets": targets,
                   "allow_new_targets": True}
            added = await _llm_answer_scope(cfg, session, rec, answer)
        if answer:
            # 让后续轮次与灵枢回复看得到这次范围确认的答复（不是新任务）
            session.dialogue_history.append(
                {"role": "user", "content": f"（范围确认答复）{answer}"})
            del session.dialogue_history[:-DIALOGUE_HISTORY_MAX]
        audit.log("master.ask_answered", "master", {
            "source": "project_memory", "answer": answer[:300],
            "added": added})
        return True, added[:6], answer

    def _rebuild_plan(self, session: Session, *, mission: str | None = None,
                      extra_scope: list[str] | None = None,
                      preserve_challenges: bool = False) -> LaunchPlan | None:
        """优先恢复最近执行计划；旧会话从原始启动参数重建。

        mission/extra_scope 供续传真任务的计划重建覆盖：新任务消息替换
        原任务原文，既有授权范围并入 extra_scope（用户追加目标时不缩
        既有目标事实；测试边界及变更由模型结合用户上下文判断）。
        """
        snapshot = session.execution_plan if mission is None and extra_scope is None else {}
        if snapshot:
            try:
                authorization = Authorization.model_validate(snapshot["authorization"])
                source = snapshot["source_request"]
                if source is not None and not isinstance(source, dict):
                    raise ValueError("启动参数必须为映射")
                source_request = LaunchRequest(**{**source, "mode": (
                    RunMode(source["mode"]) if source.get("mode") is not None else None)}) if source is not None else None
                rules = yaml.safe_load(snapshot["rules_yaml"])
                if rules is not None:
                    from spiritscout.competition.rules import (
                        CompetitionRules,
                        ContestType,
                        SubmitInterface,
                    )

                    rules = CompetitionRules(**{**rules, "contest_type": ContestType(rules["contest_type"]),
                                              "submit": SubmitInterface(**rules["submit"])})
                plan = LaunchPlan(authorization=authorization, target=snapshot["target"],
                                  mission=snapshot["mission"], challenges=parse_challenges_yaml(snapshot["challenges_yaml"]),
                                  rules=rules, flag_patterns=snapshot["flag_patterns"],
                                  source_request=source_request,
                                  generate_wp=snapshot.get("generate_wp", False) is True)
                if not isinstance(plan.target, str) or not isinstance(plan.mission, str):
                    raise ValueError("目标和任务意图必须为文本")
                plan.platform_profile = yaml.safe_load(snapshot["platform_yaml"])
                if plan.platform_profile is not None and not isinstance(plan.platform_profile, dict):
                    raise ValueError("平台档案必须为映射")
                patterns = snapshot["flag_patterns"]
                if patterns is not None and (not isinstance(patterns, list)
                        or not all(isinstance(item, str) for item in patterns)):
                    raise ValueError("Flag 格式必须为文本列表")
                plan.flag_patterns = patterns
                plan.mode_auto = snapshot.get("mode_auto") is True
                plan.needs_mode_routing = snapshot.get("needs_mode_routing") is True
                self._remount_attachments(session, plan)
                return plan
            except (KeyError, SpiritScoutError, TypeError, ValueError, yaml.YAMLError) as exc:
                raise SpiritScoutError(f"当前执行计划无法恢复：{exc}") from exc
        req = session.launch_request or {}
        if not req:
            return None
        try:
            # 自动模式且黑板没有开工记录，说明旧进程在预检期间中断；应按原始
            # 用户意图重新判定，而不是把当时的临时兜底模式永久钉死。
            untouched_auto = bool(req.get("mode_auto")) and not session.has_runtime_state()
            try:
                # 对话/规则是展示态；用户显式选定的业务模式从启动参数恢复。
                selected = req.get("mode") if not req.get("mode_auto") else session.mode
                mode_override = RunMode(selected or session.mode)
            except ValueError:
                # 对话/规则等展示态不是业务分支：交回语义路由重新裁决
                # （对话会话重启后续传此前会在此摔成「缺少启动信息」）
                mode_override = None
            challenges_yaml = req.get("challenges_yaml") or None
            # 指代性续作没有新题面 URL 时沿用已绑定赛题，不能丢题后落回 SRC。
            from spiritscout.launch import extract_targets

            if (mode_override in (RunMode.CTF, RunMode.AWDP) and mission is not None
                    and (preserve_challenges or not extract_targets(mission)[0]) and not challenges_yaml
                    and session.plan is not None and session.plan.challenges):
                challenges_yaml = yaml.safe_dump(
                    session.plan.challenges, allow_unicode=True)
            plan = prepare_launch(LaunchRequest(
                mode=None if untouched_auto else mode_override,
                target=req.get("target") or None,
                extra_scope=[*(req.get("scope") or []), *(extra_scope or [])],
                excluded_assets=list(req.get("excluded_assets") or []),
                platform=req.get("platform") or None,
                allow_sensitive=bool(req.get("allow_sensitive", False)),
                rps=float(req.get("rps", 10.0)),
                flag_format=req.get("flag_format") or None,
                generate_wp=req.get("generate_wp", False) is True,
                multiple_answers=bool(req.get("multiple_answers", False)),
                ctf_type_hints=list(req.get("ctf_type_hints") or []),
                ctf_type_note=str(req.get("ctf_type_note") or ""),
                challenges_yaml=challenges_yaml,
                platform_yaml=req.get("platform_yaml") or None,
                rules_yaml=req.get("rules_yaml") or None,
                contest_type=req.get("contest_type"),
                accept_rule_conflicts=bool(req.get("accept_rule_conflicts", False)),
                mission=mission if mission is not None
                        else (req.get("mission") or None),
                has_uploads=(session.workspace / "attachments").is_dir(),
            ))
        except (SpiritScoutError, TypeError, ValueError):
            return None
        self._remount_attachments(session, plan)
        return plan

    def _remount_attachments(self, session: Session, plan: LaunchPlan) -> None:
        """工作区附件回挂到赛题载荷（与创建时同规则，合并去重）。

        会话进行中/终态后上传的附件都落在 attachments/ 目录；重建计划时
        并入赛题（已有列表不重复），题级隔离的旁路会话照常按自身工作区
        回挂。仅补空赛题会丢中途上传的文件——用户勾了附件发送后赛题里
        却看不到。
        """
        att = session.workspace / "attachments"
        if not att.is_dir() or not plan.challenges:
            return
        try:
            files = sorted(str(p.resolve()) for p in att.rglob("*") if p.is_file())
        except OSError:
            return
        if not files:
            return
        for ch in plan.challenges:
            existing = [str(a) for a in (ch.get("attachments") or [])]
            merged = existing + [f for f in files if f not in existing]
            if merged != existing:
                ch["attachments"] = merged

    def _cancel_stale_tasks(self, session: Session) -> int:
        """续传前归档上一次被中断的黑板任务。

        进程停止时子 Agent 已不存在，黑板中仍为 pending/running 的任务不可能
        继续执行。将其原位更新为 cancelled，避免状态页和后续报告把历史残留
        当成当前活跃任务。
        """
        if not session.bb_path.exists():
            return 0
        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import AgentType, BlackboardLayer

        active = {"pending", "dispatched", "running"}
        changed = 0
        bb = Blackboard(session.bb_path, bus=None)
        try:
            for rec in bb.query(BlackboardLayer.TASK_STATE, limit=10000):
                data = rec["data"]
                if "dag_snapshot" in data:
                    dirty = False
                    for node in data.get("dag_snapshot", []):
                        if node.get("status") in active:
                            node["status"] = "cancelled"
                            dirty = True
                    if dirty:
                        bb.write(
                            BlackboardLayer.TASK_STATE, rec["record_id"], data,
                            agent_type=AgentType.MASTER,
                            updated_by="session-resume",
                            expected_version=rec["version"],
                        )
                    continue
                task = data.get("task") or {}
                if task.get("status") not in active:
                    continue
                task["status"] = "cancelled"
                for route in data.get("routes", []):
                    if route.get("status") in active:
                        route["status"] = "cancelled"
                summary = data.setdefault("summary", {})
                summary["interrupted"] = True
                summary["reason"] = "上一次会话在任务终态写入前停止"
                bb.write(
                    BlackboardLayer.TASK_STATE, rec["record_id"], data,
                    agent_type=AgentType.MASTER,
                    updated_by="session-resume",
                    dedup_key=f"task|{rec['record_id']}",
                    expected_version=rec["version"],
                )
                changed += 1
        finally:
            bb.close()
        if changed:
            AuditLogger(session.audit_path).log(
                "system.lifecycle", "session-resume", {
                    "action": "stale_tasks_cancelled", "count": changed})
        return changed

    async def resume(self, session_id: str) -> Session:
        """续传：终态会话从对话末尾接着进行（同一工作区/审计/Flag 库）。

        复用启动计划——进程内持有则直接用；控制台重启过则恢复最近执行
        快照，旧会话回退到原始启动参数。这一轮的输入是对话史末尾的用户消息，
        是否开工、进哪个分支由 _run 的语义路由结合上下文裁决；
        CTF 已破题的赛题不再重解，未破题的与用户意见一起进入新一轮决策环。
        """
        if getattr(self, "_update_maintenance", False):
            raise SpiritScoutError("系统正在更新，暂不恢复会话")
        await self.wait_for_resources()
        from spiritscout.config import load_model_config
        session = self._sessions.get(session_id)
        if session is None:
            raise SpiritScoutError(f"会话不存在: {session_id}")
        self.ensure_storage_idle(session_id)
        if session.status not in TERMINAL_STATES:
            raise SpiritScoutError("会话仍在运行中，意见将直接进入当前决策环")
        if session.task is not None and not session.task.done():
            if not session.task.cancelling():
                session.status, session.phase = "running", "queued"
                return session
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(session.task)
        if session.plan is None:
            session.plan = self._rebuild_plan(session)
        if session.plan is None:
            raise SpiritScoutError(
                "该会话缺少启动信息（由不支持续传的旧版本创建），无法续传——"
                "请基于现有报告与审计新开任务")
        # 开工前中断的自动模式会在 _rebuild_plan 中重新确定装配模式；
        # 对话/规则会话没有开工记录，展示态模式保持不动——新消息是否
        # 开工、进哪个分支由 _run 的语义路由重新裁决
        if not session.has_runtime_state() \
                and str((session.summary or {}).get("intent") or "") \
                not in ("dialogue", "rules"):
            session.mode = session.plan.authorization.mode.value
            session.target = session.plan.target
        self._cancel_stale_tasks(session)
        models = vault.apply_keys(load_model_config(self._cfg.models_config_path))
        if not models:
            raise SpiritScoutError(
                f"模型注册表为空: {self._cfg.models_config_path}（请在「模型」页检查）")
        session.resume_prev_status = session.status
        # 配置副本与通道先于状态翻转构建——构建失败会话保持原终态，
        # 不会卡出「运行中/等待调度」的僵尸态
        cfg = self._session_cfg(session, session.request.get("budget_tokens"))
        session.approvals = ApprovalBroker(AuditLogger(cfg.audit_log_path))
        session.questions = QuestionBroker(
            AuditLogger(cfg.audit_log_path), context_path=session.bb_path,
            interactive=session.mode != RunMode.COMPETITION.value)
        session.ring = RingLedger(AuditLogger(cfg.audit_log_path))
        # 暂停门换新（带本轮审计器），暂停态与已扣账的暂停/间隙时长跨续传
        # 保留：用户没手动继续前重开的任务在检查点挂起等待；时长丢失会让
        # 用时展示把此前暂停的时段重新计入
        was_paused = session.pause_gate.paused
        was_excluded = session.pause_gate.excluded_seconds
        session.pause_gate = PauseGate(AuditLogger(cfg.audit_log_path))
        session.pause_gate.restore(was_paused, was_excluded)
        no_llm = bool(session.request.get("no_llm", False))
        # 新一轮同步最新端点与密钥；在入队前更新，排队期间的手动选模仍可覆盖。
        session._model_entries = models
        session.status = "running"
        session.phase = "queued"
        session.finished_at = None
        session.error = ""
        self._persist(session)
        self.start()
        await self._queue.put((session, cfg, session.plan, no_llm, models, True))
        return session

    async def _run(self, session: Session, cfg: SystemConfig,
                   plan: LaunchPlan, no_llm: bool,
                   models: list[ModelEntry], resume: bool = False) -> None:
        from spiritscout.gateway.session_model import bind_session_model

        if session._model_entries is None:
            session._model_entries = models
        from spiritscout.traffic.egress import session_egress

        try:
            with bind_session_model(lambda: session.model, lambda: session._model_entries):
                async with session_egress(self._resources(), session):
                    await self._run_turns(session, cfg, plan, no_llm, models, resume)
        except Exception as exc:
            session.status = session.phase = 'interrupted' if resume and session.resume_prev_status else 'failed'
            prefix = '续传未完成' if resume and session.resume_prev_status else '会话出口或运行失败'
            session.error = f'{prefix}：{type(exc).__name__}: {exc}'[:500]
            session.finished_at = time.time()
            self._persist(session)

    async def _run_turns(self, session: Session, cfg: SystemConfig,
                         plan: LaunchPlan, no_llm: bool,
                         models: list[ModelEntry], resume: bool = False) -> None:
        while True:
            await self._run_once(session, cfg, plan, no_llm, models, resume)
            if (not session.user_inbox or session.status in {"failed", "stopped"}
                    or session.error or no_llm or not models):
                return
            session.resume_prev_status = session.status
            session.status, session.phase = "running", "queued"
            session.finished_at = None
            plan = session.plan or plan
            resume = True
            self._persist(session)

    async def _run_once(self, session: Session, cfg: SystemConfig,
                        plan: LaunchPlan, no_llm: bool,
                        models: list[ModelEntry], resume: bool = False) -> None:
        """会话执行体：按模式分发 runtime 入口，落终态与摘要。

        resume=True 为续传运行：同一工作区/审计/Flag 库重开一轮，
        模式不再改判（会话模式已定），失败时标记本轮中断（原成果与摘要不丢）。
        """
        try:
            await SessionExecution(self, session, cfg, plan, no_llm, models, resume).run()
        except asyncio.CancelledError:
            # stop() 主动取消：runtime finally 已关闭 infra；落 stopped 终态后
            # 正常结束（不向上 re-raise——任务目的即被终止）
            session.status = "stopped"
            session.phase = "stopped"
        except Exception as exc:  # noqa: BLE001 —— 会话故障隔离：单会话失败不影响其他会话
            if session.request.get("side_agent"):
                AuditLogger(session.audit_path).log("agent.reply_failed", session.request["side_agent"],
                    {"error": f"子 Agent 本轮回复未完成（{type(exc).__name__}），可继续发送消息重试。"})
            if resume and session.resume_prev_status:
                # 续传失败保留历史成果，但当前执行不得恢复成 completed
                session.status = "interrupted"
                session.phase = session.status
                session.error = f"续传未完成：{type(exc).__name__}: {exc}"[:500]
            else:
                session.status = "failed"
                session.phase = "failed"
                session.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            await session.pause_gate.stop_gap_watch()
            session.finished_at = time.time()
            self._persist(session)

    async def stop(self, session_id: str, *, resume_pending: bool = True) -> Session | None:
        """幂等停止：取消执行任务（数据保留）；已终态直接返回。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        pending_before_stop = {item.get("statement_id") for item in session.user_inbox}
        session.approvals.cancel_all()
        session.questions.cancel_all()
        task = session.task
        active = task is not None and not task.done()
        if active:
            session.phase = "stopping"
            self._persist(session)
            if not task.cancelling():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if active and (session.task is not task or (session.status == "running" and session.phase != "stopping")):
            return session
        if session.status not in TERMINAL_STATES:
            session.status = "stopped"
            session.phase = "stopped"
            session.finished_at = time.time()
            self._persist(session)
        if resume_pending and not getattr(self, "_update_maintenance", False) and any(
                item.get("statement_id") not in pending_before_stop for item in session.user_inbox):
            # 用户在停止回收期间发送的新输入是后续轮次；等资源真正释放后再处理。
            # 停止前未消费的旧输入仍保留，但不能因点击停止自行重启执行。
            await self.resume(session_id)
        return session

    async def set_paused(self, session_id: str, paused: bool) -> Session | None:
        """暂停/继续切换（幂等）：仅运行中生效并即时持久化。

        暂停是协作式的——灵枢图节点与子 Agent 步骤在各自检查点挂起，
        在途动作跑完才停；直达对话与审批不经此门，照常可用。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        self.ensure_storage_idle(session_id)
        if not paused and session.status in TERMINAL_STATES and session.pause_gate.paused:
            session = await self.resume(session_id)
            session.pause_gate.resume()
            self._persist(session)
            return session
        if session.status != "running":
            raise SpiritScoutError("任务已结束，不能切换暂停状态；可发送新消息继续对话")
        if session.status == "running":
            if paused:
                session.pause_gate.pause()
            else:
                session.pause_gate.resume()
            self._persist(session)
        return session

    def rename(self, session_id: str, title: str) -> Session | None:
        """重命名会话（任意状态可改；空串 = 恢复自动摘要名）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.title = title.strip()[:80]
        self._persist(session)
        return session

    def set_project(self, session_id: str, project_id: str) -> Session | None:
        """移动会话到项目（空串 = 未分类）：catalog 先写、meta 后写。

        运行中允许移动；旧项目记忆已注入本轮上下文，作为历史参考保留到
        本轮结束（提示词已声明未复核）。移动回填该会话全部历史观测。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        project_id = str(project_id or "").strip()[:64]
        store = self._resources()
        try:
            store.set_session_project(session_id, project_id)
        except ValueError as exc:
            raise SpiritScoutError(str(exc)) from exc
        session.project_id = project_id
        self._persist(session)
        project = store.project(project_id) if project_id else None
        try:
            AuditLogger(session.audit_path).log("session.project", "user", {
                "action": "moved", "project_id": project_id,
                "project": str((project or {}).get("name") or "")})
        except (OSError, SpiritScoutError):
            pass        # 审计失败不遮蔽移动结果：catalog 已是事实源
        return session

    def ensure_storage_idle(self, session_id: str) -> None:
        """清理期间禁止恢复和删除，避免后台线程操作已被替换的工作区。"""
        if session_id in self.storage_busy:
            raise SpiritScoutError("会话正在清理存储，请等待完成后再操作")

    async def delete(self, session_id: str, *, confirm: bool = False) -> Session | None:
        """删除会话：仅终态可删且需 confirm；连工作区目录一并移除。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        self.ensure_storage_idle(session_id)
        if session.status not in TERMINAL_STATES:
            raise SpiritScoutError(
                f"会话 {session_id} 仍在 {session.status}，请先停止再删除")
        if not confirm:
            raise SpiritScoutError("删除会话需显式 confirm=true（连工作区一并移除）")
        self._sessions.pop(session_id, None)

        async def remove_workspace() -> None:
            # 删除期间拒绝新采集；失败时恢复入口和元信息，允许用户重试。
            try:
                traffic = getattr(self, "_traffic_capture", None)
                if traffic is not None:
                    await traffic.stop(session_id, deleting=True)
                try:
                    shutil.rmtree(session.workspace)
                except FileNotFoundError:
                    if session.workspace.exists():
                        raise
            except BaseException as exc:
                self._sessions[session_id] = session
                if not session.meta_path.is_file():
                    try:
                        self._persist(session)
                    except OSError as restore_error:
                        raise SpiritScoutError(
                            f"删除会话未完成，恢复元数据失败：{restore_error}") from exc
                if isinstance(exc, OSError):
                    raise SpiritScoutError(f"删除会话工作区未完成，可重试：{exc}") from exc
                raise
            try:
                self._resources().remove_session(session_id)
            except (OSError, sqlite3.Error, SpiritScoutError) as exc:
                print(f"[spiritscout] 删除会话成员行失败（{session_id}）: {exc}",
                      file=sys.stderr)
            from spiritscout.traffic.proxy import _REQUIRED

            _REQUIRED.discard(str(session.workspace.resolve()))

        await finish_cleanup(remove_workspace())
        return session
