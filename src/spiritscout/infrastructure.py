"""单次任务基础设施的装配与统一回收，不执行模型规划。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spiritscout.agents.ring import RingLedger
from spiritscout.agents.sub.cache import LocalSharedCache
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.bus import create_bus
from spiritscout.bus.base import EventBus
from spiritscout.config import SystemConfig
from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import AuditLogger
from spiritscout.core.cleanup import finish_cleanup
from spiritscout.core.exceptions import (
    ConfigurationError,
    PolicyViolation,
)
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    Authorization,
    BlackboardLayer,
    RunMode,
)
from spiritscout.gateway import (
    ModelGateway,
    ModelRegistry,
)
from spiritscout.knowledge import KnowledgeStore
from spiritscout.knowledge.experience import (
    ExperienceStore,
    experience_db_path,
)
from spiritscout.policy import PolicyEngine, ScopeGuard
from spiritscout.tools.builtin import (
    BUILTIN_SPECS,
    RateLimiter,
    dns_resolve,
    file_analyze,
    http_probe,
    tcp_scan,
)
from spiritscout.tools.executor import (
    DockerExecutor,
    LocalExecutor,
    ToolExecutor,
    docker_available,
)
from spiritscout.tools.mcp_runtime import MCPRuntime
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.run_lock import ExecutionRunLock
from spiritscout.tools.sandbox import sandbox_exec


@dataclass
class _Infra:
    """一次任务共享的基础设施句柄（总线/黑板/网关/工具/策略/知识库）。"""

    cfg: SystemConfig
    authorization: Authorization
    audit: AuditLogger
    guard: ScopeGuard
    policy: PolicyEngine
    limiter: RateLimiter
    bus: EventBus
    blackboard: Blackboard
    convergence: ConvergenceEngine
    gateway: ModelGateway
    tools: ToolRegistry
    builtin_ctx: dict[str, Any]
    workspace: Path
    knowledge: KnowledgeStore
    resources: Any = None  # 资源控制台配置（外部情报 provider 等）
    agent_models: dict[str, str] = field(default_factory=dict)  # 按 Agent 绑定模型
    ring: Any = None  # RingLedger 环形台账（主/子 Agent 共持，随会话消亡）
    shared_cache: Any = None  # LocalSharedCache 环形共享缓存（同题并行 Agent 共持）
    mcp_clients: list[Any] = field(default_factory=list)  # MCP 客户端（关停回收）
    mcp_startup_tasks: list[asyncio.Task[Any]] = field(
        default_factory=list
    )  # 后台续握的 MCP 任务（close 先取消再回收客户端）
    mcp_tools: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # mcp__<server>__<tool> → {client, server, tool, timeout}
    questions: Any = None  # QuestionBroker（ask_user 工具后端；None=不可用）
    experience: Any = None  # ExperienceStore（全局经验库，跨会话持久）
    pause_gate: Any = None  # PauseGate 暂停门（Web 层注入；None=无暂停能力）
    run_lock: ExecutionRunLock | None = None
    outcome: str = "failed"
    experience_deferred: bool = False  # 退场提炼托管后台中：close 跳过经验库
    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False, compare=False)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await finish_cleanup(self._close_task)

    async def _close(self) -> None:
        try:
            jobs = getattr(self.tools, "background_jobs", None)
            if jobs is not None:
                await jobs.aclose()
            if self.questions is not None:
                self.questions.bind_context(None)
            if self.tools.journal is not None:
                self.tools.journal.finish_run(self.outcome)
        finally:
            # 后台仍在握手的 MCP：先取消任务再回收客户端（握手被中断的
            # start() 由 close 唤醒，不会悬挂）
            for task in self.mcp_startup_tasks:
                if not task.done():
                    task.cancel()
            if self.mcp_startup_tasks:
                with contextlib.suppress(BaseException):
                    await asyncio.gather(
                        *self.mcp_startup_tasks, return_exceptions=True
                    )
            try:
                await _teardown_infra(
                    mcp_clients=self.mcp_clients,
                    blackboard=self.blackboard,
                    # 提炼托管后台时经验库交由后台任务关闭（其余资源即刻回收）
                    experience=None if self.experience_deferred else self.experience,
                    knowledge=self.knowledge,
                    bus=self.bus,
                )
            finally:
                if self.run_lock is not None:
                    self.run_lock.close()


async def _teardown_infra(
    *,
    mcp_clients: list[Any],
    blackboard: Any,
    experience: Any,
    knowledge: Any,
    bus: Any,
) -> None:
    """基建统一收口：逐项容错，单项失败不阻断其余资源回收。

    _Infra.close 与 _build_infra 中断路径共用——初始化失败前已建的资源
    同样要回收（总线/黑板/知识库/经验库/MCP 子进程，泄漏会拖住进程退出）。
    """
    for client in mcp_clients:
        with contextlib.suppress(Exception):
            await client.close()
    if blackboard is not None:
        with contextlib.suppress(Exception):
            await blackboard.aclose()
    if experience is not None:
        with contextlib.suppress(Exception):
            experience.close()
    if knowledge is not None:
        with contextlib.suppress(Exception):
            knowledge.close()
    if bus is not None:
        with contextlib.suppress(Exception):
            await bus.stop()


def _bind_asset_memory(
    cfg: SystemConfig,
    authorization: Authorization,
    blackboard: Blackboard,
    resources: Any,
    audit: AuditLogger,
) -> None:
    def collect_asset(
        layer: BlackboardLayer, record_id: str, version: int, data: dict[str, Any]
    ) -> None:
        from spiritscout.resources.store import record_asset

        asset = record_asset(layer.value, data)
        if asset is None:
            return
        try:
            resources.observe(
                asset,
                f"{Path(cfg.workspace.root).name}:{layer.value}:{record_id}@{version}",
            )
        except Exception:  # noqa: BLE001
            audit.log(
                "assets.sync_failed",
                "runtime",
                {
                    "record_id": record_id,
                    "reason": "共享库暂时不可写；会话黑板已保留，可从项目页补同步",
                },
            )

    blackboard.record_observer = collect_asset
    on_demand = authorization.mode == RunMode.SRC
    project_id = resources.project_for_session(Path(cfg.workspace.root).name) if on_demand else ""
    blackboard.write(BlackboardLayer.TOPOLOGY, "history-policy", {"on_demand": on_demand, "project_id": project_id},
                     updated_by="runtime")
    if on_demand:
        from spiritscout.resources.store import project_context

        if project_id:
            memory = resources.project_memory(project_id, asset_limit=0, finding_limit=0)
            if memory:
                blackboard.write(BlackboardLayer.TOPOLOGY, "project-memory", project_context(memory),
                                 updated_by="project-memory")
        return
    # 只导入历史参考摘要；不复制凭据、Flag 或此前授权。
    from urllib.parse import urlsplit

    hosts = {
        value.lstrip("*.").lower() for value in authorization.scope.allowed_domains
    }
    hosts.update(urlsplit(value).hostname for value in authorization.scope.allowed_urls)
    prior = []
    for host in sorted(value for value in hosts if value)[:3]:
        prior.extend(resources.assets(host=host, limit=8)["items"])
    if prior:
        blackboard.write(
            BlackboardLayer.TOPOLOGY,
            "historical-assets",
            {
                "items": prior,
                "note": "跨会话历史参考，均未在本次会话复核；内容可能变化，不能据此声称漏洞仍存在或沿用授权。",
            },
            updated_by="asset-memory",
        )
    # 同项目共享记忆：分类摘要，不含 scope/凭据；未复核且不构成授权。
    # 会话目录名即 session_id（web 层 _session_cfg 已把 workspace 指到会话目录）。
    session_id = Path(cfg.workspace.root).name
    project_id = resources.project_for_session(session_id)
    if project_id:
        memory = resources.project_memory(project_id)
        if memory:
            blackboard.write(
                BlackboardLayer.TOPOLOGY,
                "project-memory",
                memory,
                updated_by="project-memory",
            )


def _create_executor(
    cfg: SystemConfig, authorization: Authorization, audit: AuditLogger
) -> ToolExecutor:
    # 执行器三态：本地 CLI 默认；docker 后端为子 Agent 沙箱隔离（加固）
    executor: ToolExecutor
    if cfg.executor.backend == "docker":
        if not docker_available():
            # 显式选择 Docker 时，不回退到宿主执行。
            raise ConfigurationError(
                "executor.backend=docker 但 Docker 守护进程不可用（docker CLI 缺失"
                "或 dockerd 未运行）。请启动 Docker 服务，或将 executor.backend "
                "改回 local 后重试。"
            )
        executor = DockerExecutor(
            image=cfg.executor.docker_image,
            # CTF 模式网络隔离（硬约束 C7）：容器 --network none
            network_isolation=(
                authorization.mode == RunMode.CTF and cfg.executor.ctf_network_isolation
            ),
            memory_mb=cfg.executor.docker_memory_mb,
            cpus=cfg.executor.docker_cpus,
            pids_limit=cfg.executor.docker_pids_limit,
            read_only=cfg.executor.docker_read_only,
            tmpfs=cfg.executor.docker_tmpfs,
            user=cfg.executor.docker_user,
            max_output_chars=cfg.executor.max_output_chars,
            audit=audit,
        )
    else:
        executor = LocalExecutor(
            max_output_chars=cfg.executor.max_output_chars,
            audit=audit,
        )
    return executor


def _register_builtin_tools(
    registry: ToolRegistry,
    cfg: SystemConfig,
    blackboard: Blackboard,
    workspace: Path,
    builtin_ctx: dict[str, Any],
    audit: AuditLogger,
    pause_gate: Any,
) -> None:
    from spiritscout.tools.builtin import asset_memory_lookup
    from spiritscout.tools.http_responses import SPEC as RESPONSE_SPEC
    from spiritscout.tools.http_responses import http_response_read

    registry.register_builtin("http_response_read", http_response_read, RESPONSE_SPEC)
    from spiritscout.tools.history import SPEC as HISTORY_SPEC
    from spiritscout.tools.history import history_lookup

    registry.register_builtin("history_lookup", history_lookup, HISTORY_SPEC)
    from spiritscout.tools.js_intel import SPEC as JS_READ_SPEC
    from spiritscout.tools.js_intel import js_source_read

    registry.register_builtin("js_source_read", js_source_read, JS_READ_SPEC)
    from spiritscout.traffic.tools import FUNCTIONS as TRAFFIC_FUNCTIONS
    from spiritscout.traffic.tools import SPECS as TRAFFIC_SPECS

    for name, function in TRAFFIC_FUNCTIONS.items():
        registry.register_builtin(name, function, TRAFFIC_SPECS[name])
    from spiritscout.tools.background import SPECS as BACKGROUND_SPECS
    from spiritscout.tools.background import ShellJobs

    registry.background_jobs = ShellJobs(
        str(workspace),
        archive_limit=cfg.storage.background_archive_max_mb * 1024 * 1024,
        audit=audit,
        guard_root=builtin_ctx.get("guard_root", ""),
        pause_gate=pause_gate,
    )
    for name, method in (
        ("shell_start", "start"),
        ("shell_status", "status"),
        ("shell_wait", "wait"),
        ("shell_watch", "watch"),
        ("shell_events", "events"),
        ("shell_stop", "stop"),
        ("task_wait", "delay"),
    ):
        registry.register_builtin(
            name, getattr(registry.background_jobs, method), BACKGROUND_SPECS[name]
        )

    from spiritscout.tools.cyberchef import SPECS as CYBERCHEF_SPECS
    from spiritscout.tools.cyberchef import cyberchef_bake, cyberchef_operations

    for fn in (cyberchef_operations, cyberchef_bake):
        registry.register_builtin(fn.__name__, fn, CYBERCHEF_SPECS[fn.__name__])

    for name, fn in (
        ("asset_memory_lookup", asset_memory_lookup),
        ("http_probe", http_probe),
        ("dns_resolve", dns_resolve),
        ("tcp_scan", tcp_scan),
        ("file_analyze", file_analyze),
        ("sandbox_exec", sandbox_exec),
    ):
        registry.register_builtin(name, fn, BUILTIN_SPECS[name])
    from spiritscout.tools.scanner import SPEC as SCANNER_SPEC
    from spiritscout.tools.scanner import NucleiScanner

    registry.register_builtin(
        "nuclei_scan", NucleiScanner(cfg.scanner, blackboard).run, SCANNER_SPEC
    )
    from spiritscout.tools.metasploit import SPEC as METASPLOIT_SPEC
    from spiritscout.tools.metasploit import MetasploitScanner

    registry.register_builtin(
        "metasploit_scan",
        MetasploitScanner(cfg.scanner, blackboard).run,
        METASPLOIT_SPEC,
    )
    from spiritscout.tools.images import SPEC as IMAGE_SPEC
    from spiritscout.tools.images import view_image

    registry.register_builtin("view_image", view_image, IMAGE_SPEC)


async def build_infra(
    cfg: SystemConfig,
    models: list[ModelEntry],
    authorization: Authorization,
    agent_models: dict[str, str] | None = None,
    approval_gate: Callable[[str, str], Awaitable[bool]] | None = None,
    ring: Any = None,
    question_broker: Any = None,
    pause_gate: Any = None,
    *,
    mcp_startup_grace: float,
) -> _Infra:
    # 启动前检查人工授权确认。
    if not authorization.human_confirmed:
        raise PolicyViolation(
            "授权信息未经人工确认（human_confirmed=false），拒绝启动（G1）"
        )
    audit = AuditLogger(cfg.audit_log_path)
    audit.log(
        "system.lifecycle",
        "runtime",
        {
            "action": "engagement_start",
            "mode": authorization.mode.value,
            "auth_id": authorization.auth_id,
            "target": authorization.target_description,
        },
    )
    if authorization.mode == RunMode.COMPETITION:
        from spiritscout.policy.questions import QuestionBroker

        # Web 与 CLI 共用入口，调用方即使传入交互 Broker 也不能让竞赛等用户。
        question_broker = QuestionBroker(audit, interactive=False)
        audit.log(
            "system.lifecycle",
            "runtime",
            {
                "action": "autonomous_mode_enabled",
                "mode": authorization.mode.value,
                "tool_approval": "automatic",
                "user_interaction": False,
            },
        )
    guard = ScopeGuard(authorization.scope)
    guard.assert_operable()
    if question_broker is not None:
        question_broker.bind_guard(guard)  # 用户勾选扩围 → 写入会话批准集
    # 以下资源在创建后任何一步失败都要回收：先置空占位，异常路径统一收口
    mcp_clients: list[Any] = []
    mcp_startup_tasks: list[asyncio.Task[Any]] = []  # 在握 MCP 任务（中断时取消回收）
    knowledge: KnowledgeStore | None = None
    experience: Any = None
    bus: Any = None
    blackboard: Blackboard | None = None
    run_lock: ExecutionRunLock | None = None
    registry: ToolRegistry | None = None
    journal: Any = None
    try:
        # 平台红线由知识库漏洞标准库装载（3.4 全量 7 平台规则，首次启动自动播种）
        knowledge = KnowledgeStore(cfg.knowledge.sqlite_path, auto_seed=True)
        from spiritscout.resources.store import ResourceStore, resource_root

        resources = ResourceStore(resource_root(cfg), cfg.resources.freshness_hours)
        knowledge.resources = resources
        from spiritscout.web import vault

        # Web 会话副本的 root 指向会话目录，guard_root 才是共享控制面根；CLI
        # 没有 guard_root 时直接使用 workspace.root。凭据不进入会话黑板或审计。
        vault.configure(cfg.workspace.guard_root or cfg.workspace.root)
        cfg = cfg.model_copy(deep=True)
        from spiritscout.gateway.lightweight import lightweight_enabled

        # 仅本次运行副本生效，编队包含云端模型或未标记条目时关闭补偿。
        cfg.gateway.lightweight_local = lightweight_enabled(
            cfg.gateway.lightweight_local, authorization.mode, models
        )
        cfg.mcp_servers = resources.effective_mcp(cfg.mcp_servers)
        run_lock = ExecutionRunLock(
            Path(cfg.workspace.root), Path(cfg.blackboard.sqlite_path)
        )
        run_lock.acquire()
        platform_rule = knowledge.platform_rule_view(authorization.src_platform)
        policy = PolicyEngine(
            authorization.mode,
            guard,
            platform_rule=platform_rule,
            audit=audit,
            approval_gate=approval_gate,
        )
        if authorization.mode == RunMode.SRC:
            policy.check_automation_allowed()  # 平台禁自动化时启动即阻断
        bus = create_bus(
            cfg.bus.backend,
            queue_maxsize=cfg.bus.queue_maxsize,
            rate_limit_per_second=cfg.bus.rate_limit_per_second,
            redis_url=os.environ.get(cfg.bus.redis_url_env) or cfg.bus.redis_url,
            channel_prefix=(
                f"ss:{cfg.bus.namespace}:session:"
                + hashlib.sha256(
                    str(Path(cfg.workspace.root).resolve()).encode()
                ).hexdigest()[:24]
            ),
            connect_timeout_seconds=cfg.bus.connect_timeout_seconds,
            nats_url=cfg.bus.nats_url,
        )
        await bus.start()
        limiter = RateLimiter(
            max(authorization.scope.max_requests_per_second, 0.1),
            shared_acquire=(
                lambda: bus.acquire_request_slot(
                    cfg.bus.shared_request_rps,
                    f"ss:{cfg.bus.namespace}:limits:requests",
                )
            )
            if cfg.bus.backend == "redis"
            else None,
        )
        blackboard = Blackboard(cfg.blackboard.sqlite_path, bus=bus)
        if question_broker is not None:
            question_broker.bind_context(blackboard)
        blackboard.start_notifications()
        from spiritscout.core.models import AgentType

        blackboard.write(
            BlackboardLayer.TARGET_AUTH,
            authorization.auth_id,
            authorization.model_dump(mode="json"),
            agent_type=AgentType.MASTER,
            updated_by="runtime",
        )
        _bind_asset_memory(cfg, authorization, blackboard, resources, audit)
        convergence = ConvergenceEngine(blackboard)
        gateway = ModelGateway(ModelRegistry(models), config=cfg.gateway, audit=audit,
                               pause_gate=pause_gate)
        from spiritscout.resources.runtime import SkillRuntime

        gateway.questions = question_broker
        gateway.skill_runtime = SkillRuntime(resources, pause_gate=pause_gate)
        gateway.set_mode(authorization.mode.value)  # 模式预算计量与熔断
        executor = _create_executor(cfg, authorization, audit)
        workspace = Path(cfg.workspace.root).resolve()
        if isinstance(executor, LocalExecutor):
            executor.traffic_workspace = workspace
        workspace.mkdir(parents=True, exist_ok=True)
        from spiritscout.tools.journal import ExecutionJournal

        journal = ExecutionJournal(blackboard, workspace, new_id("run"))
        journal.activate_run(run_lock)
        shared_ring = ring if ring is not None else RingLedger(audit)
        shared_ring.bind_blackboard(blackboard)
        registry = ToolRegistry(
            executor,
            policy=policy,
            audit=audit,
            journal=journal,
            attempts=shared_ring.exact,
            pause_gate=pause_gate,
        )
        from spiritscout.gateway.usage import bind_usage

        bind_usage(gateway, workspace / "usage.json")
        # 经验库默认跟随工作区；Web 会话在 _session_cfg 里把 root 指向共享目录，
        # 保证提炼出的经验跨会话可见（会话隔离不隔离经验）。
        experience = ExperienceStore(
            experience_db_path(cfg.experience.root, str(workspace)), audit=audit
        )
        for name in ("attachments", "sandbox", "artifacts"):
            if (workspace / name).is_symlink():
                raise ConfigurationError(f"共享目录不能是符号链接: {name}")
        attachments = workspace / "attachments"
        attachments.mkdir(exist_ok=True)
        (workspace / "reports").mkdir(exist_ok=True)
        # 浏览器/代理 HAR 附件 → 真实端点资产。只导入授权范围内的 URL，且不保存
        # Cookie、Authorization、参数值或响应正文。
        from spiritscout.traffic import import_har_directory

        import_har_directory(
            attachments, blackboard=blackboard, guard=guard, audit=audit
        )
        sandbox_dir = workspace / "sandbox"
        sandbox_dir.mkdir(exist_ok=True)
        artifacts_dir = workspace / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)
        registry.questions = question_broker
        builtin_ctx = {
            "guard": guard,
            "limiter": limiter,
            # 附件与沙箱产物目录都在附件分析工具的允许清单内——脚本产物
            # 可以直接接续 file_analyze/analyze_attachments
            "allowed_dirs": [str(attachments), str(sandbox_dir), str(artifacts_dir)],
            "workspace_root": str(workspace),
            "http_max_body_bytes": cfg.executor.http_max_body_bytes,
            # 控制面根目录（Web 会话为基础工作区）：沙箱需整层遮蔽后再
            # 开放会话产物目录（[]/空 = 无额外遮蔽，CLI 直连场景）
            "guard_root": str(getattr(cfg.workspace, "guard_root", "") or ""),
            "resources": resources,
            "experience": experience,
        }
        from spiritscout.tools.web_search import (
            SEARCH_SPEC,
            effective_search,
            search_service,
            web_search,
        )

        search_root = Path(cfg.workspace.guard_root or cfg.workspace.root)
        # 运行身份用保留键注入：模型工具参数（如 http_response_read 的
        # mode）不与环境键争名，同名不再撞「multiple values」
        builtin_ctx["_run_mode"] = authorization.mode
        builtin_ctx["_search"] = None
        if authorization.mode != RunMode.COMPETITION:
            search_config, search_key = effective_search(cfg.search, resources)
            if search_config.enabled:
                builtin_ctx["_search"] = search_service(
                    search_config, search_root, api_key=search_key
                )
        registry.register_builtin("web_search", web_search, SEARCH_SPEC)
        from spiritscout.cve.service import CVEService
        from spiritscout.tools.cve import SPEC as CVE_SPEC
        from spiritscout.tools.cve import cve_lookup

        builtin_ctx["_cve"] = CVEService(cfg.cve) if cfg.cve.enabled else None
        registry.register_builtin("cve_lookup", cve_lookup, CVE_SPEC)

        mcp_tools: dict[str, dict[str, Any]] = {}
        await MCPRuntime(
            cfg=cfg,
            authorization=authorization,
            resources=resources,
            workspace=workspace,
            registry=registry,
            audit=audit,
            question_broker=question_broker,
            mcp_clients=mcp_clients,
            mcp_startup_tasks=mcp_startup_tasks,
            mcp_tools=mcp_tools,
            startup_grace=mcp_startup_grace,
        ).start()
        _register_builtin_tools(
            registry, cfg, blackboard, workspace, builtin_ctx, audit, pause_gate
        )
        # 移除已从注册表删除的角色模型指派，并记录审计。
        bound = dict(agent_models or {})
        known_registry = ModelRegistry(models)
        for value in bound.values():
            if sum(m.model == value for m in models) > 1:
                known_registry.get(
                    value
                )  # 有歧义的旧指派必须明确报错，不能静默换供应商。
        stale = {
            k: v
            for k, v in bound.items()
            if not known_registry or not known_registry.has(v)
        }
        if stale:
            audit.log(
                "system.lifecycle",
                "runtime",
                {
                    "action": "agent_model_binding_dropped",
                    "stale": stale,
                    "reason": "模型不在注册表，按档位路由",
                },
            )
            bound = {k: v for k, v in bound.items() if known_registry.has(v)}
        # 按本次调用注入补偿资格，不能用进程全局变量串扰其他会话。
        builtin_ctx["_lightweight_local"] = cfg.gateway.lightweight_local
        return _Infra(
            cfg=cfg,
            authorization=authorization,
            audit=audit,
            guard=guard,
            policy=policy,
            limiter=limiter,
            bus=bus,
            blackboard=blackboard,
            convergence=convergence,
            gateway=gateway,
            tools=registry,
            builtin_ctx=builtin_ctx,
            workspace=workspace,
            knowledge=knowledge,
            resources=resources,
            agent_models=bound,
            ring=shared_ring,
            shared_cache=LocalSharedCache(default_ttl_seconds=3600),
            mcp_clients=mcp_clients,
            mcp_startup_tasks=mcp_startup_tasks,
            mcp_tools=mcp_tools,
            questions=question_broker,
            experience=experience,
            pause_gate=pause_gate,
            run_lock=run_lock,
        )
    except BaseException as exc:
        outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        # 失败退场也保留资源所有权，直到已启动的工作和连接真正关闭。
        async def close_failed_build() -> None:
            try:
                jobs = getattr(registry, "background_jobs", None)
                if jobs is not None:
                    await jobs.aclose()
                if question_broker is not None:
                    question_broker.bind_context(None)
                if journal is not None:
                    with contextlib.suppress(Exception):
                        journal.finish_run(outcome)
            finally:
                for task in mcp_startup_tasks:
                    if not task.done():
                        task.cancel()
                if mcp_startup_tasks:
                    with contextlib.suppress(BaseException):
                        await asyncio.gather(*mcp_startup_tasks, return_exceptions=True)
                try:
                    await _teardown_infra(
                        mcp_clients=mcp_clients, blackboard=blackboard,
                        experience=experience, knowledge=knowledge, bus=bus)
                finally:
                    if run_lock is not None:
                        run_lock.close()

        await finish_cleanup(close_failed_build())
        raise
