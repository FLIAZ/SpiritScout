"""运行时编排：把各模块组装成一次可运行的授权任务。

run_engagement() 统一编排 SRC 链路，收敛后先跑核验管道再生成报告。
run_ctf_engagement() / run_competition_engagement() 赛事实例化装配：
    Flag 统一管理器 + 模式装配器（战术映射/元规划覆盖注入主 Agent）。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spiritscout.agents.master.agent import MasterAgent, MasterContext
from spiritscout.agents.report.agent import ReportAgent
from spiritscout.agents.ring import RingLedger
from spiritscout.agents.sub.base import SubAgentContext
from spiritscout.agents.sub.cache import LocalSharedCache
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.bus import create_bus
from spiritscout.bus.base import EventBus
from spiritscout.config import SystemConfig
from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import (
    ConfigurationError,
    PolicyViolation,
    SpiritScoutError,
)
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    Authorization,
    BlackboardLayer,
    RunMode,
)
from spiritscout.ctf import FlagManager
from spiritscout.ctf.flags import SubmitFn
from spiritscout.ctf.writeup import build_digest, writeup_body
from spiritscout.gateway import (
    AgentBoundGateway,
    ModelGateway,
    ModelRegistry,
)
from spiritscout.knowledge import KnowledgeStore
from spiritscout.knowledge.experience import (
    ExperienceStore,
    experience_db_path,
    format_insights,
)
from spiritscout.modes import ModeAssembler
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
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.run_lock import ExecutionRunLock
from spiritscout.tools.sandbox import sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

if TYPE_CHECKING:
    from spiritscout.competition import CompetitionRules


# MCP 握手开工宽限：快服务器（秒级）宽限内就绪即开工前并入；超时的转入
# 后台续握、就绪后中途并入——握手不再门控开工（实测 npx 解析 @latest 可
# 挂满 60s 握手超时，整场白等一台最终失败的服务器）
_MCP_STARTUP_GRACE = 10.0

# 同一进程内：前台解题优先，后台提炼串行且仅在无前台任务时运行。
_FOREGROUND_DEPTH = 0
_BACKGROUND_ACTIVE = 0
_BACKGROUND_COOLDOWN_UNTIL = 0.0
_BACKGROUND_COOLDOWN_SECONDS = 2.0


def _foreground_started() -> None:
    global _FOREGROUND_DEPTH
    _FOREGROUND_DEPTH += 1


def _foreground_finished() -> None:
    global _FOREGROUND_DEPTH
    _FOREGROUND_DEPTH = max(0, _FOREGROUND_DEPTH - 1)


@contextlib.asynccontextmanager
async def _background_slot() -> Any:
    """后台低优先槽：前台活跃时等待，且同进程只跑一个提炼调用。"""
    global _BACKGROUND_ACTIVE, _BACKGROUND_COOLDOWN_UNTIL
    while (_FOREGROUND_DEPTH > 0 or _BACKGROUND_ACTIVE > 0
           or time.monotonic() < _BACKGROUND_COOLDOWN_UNTIL):
        await asyncio.sleep(1.0)
    _BACKGROUND_ACTIVE += 1
    try:
        yield
    finally:
        _BACKGROUND_ACTIVE = max(0, _BACKGROUND_ACTIVE - 1)
        _BACKGROUND_COOLDOWN_UNTIL = time.monotonic() + _BACKGROUND_COOLDOWN_SECONDS


@dataclass
class EngagementResult:
    """一次授权任务的产出。"""

    run_summary: dict[str, Any]
    verify_stats: dict[str, int]
    report_path: str | None
    flag_stats: dict[str, int] | None = None

    @property
    def summary(self) -> dict[str, Any]:
        return self.run_summary


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
    agent_models: dict[str, str] = field(default_factory=dict)  # 按 Agent 绑定模型
    ring: Any = None            # RingLedger 环形台账（主/子 Agent 共持，随会话消亡）
    shared_cache: Any = None    # LocalSharedCache 环形共享缓存（同题并行 Agent 共持）
    mcp_clients: list[Any] = field(default_factory=list)   # MCP 客户端（关停回收）
    mcp_startup_tasks: list[asyncio.Task[Any]] = field(
        default_factory=list)   # 后台续握的 MCP 任务（close 先取消再回收客户端）
    mcp_tools: dict[str, dict[str, Any]] = field(
        default_factory=dict)   # mcp__<server>__<tool> → {client, server, tool, timeout}
    questions: Any = None       # QuestionBroker（ask_user 工具后端；None=不可用）
    experience: Any = None      # ExperienceStore（全局经验库，跨会话持久）
    pause_gate: Any = None      # PauseGate 暂停门（Web 层注入；None=无暂停能力）
    run_lock: ExecutionRunLock | None = None
    outcome: str = "failed"
    experience_deferred: bool = False   # 退场提炼托管后台中：close 跳过经验库

    async def close(self) -> None:
        if self.questions is not None:
            self.questions.bind_context(None)
        try:
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
                    await asyncio.gather(*self.mcp_startup_tasks,
                                         return_exceptions=True)
            try:
                await _teardown_infra(
                    mcp_clients=self.mcp_clients, blackboard=self.blackboard,
                    # 提炼托管后台时经验库交由后台任务关闭（其余资源即刻回收）
                    experience=None if self.experience_deferred else self.experience,
                    knowledge=self.knowledge, bus=self.bus)
            finally:
                if self.run_lock is not None:
                    self.run_lock.close()


async def _teardown_infra(*, mcp_clients: list[Any], blackboard: Any,
                          experience: Any, knowledge: Any, bus: Any) -> None:
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


async def _build_infra(
    cfg: SystemConfig,
    models: list[ModelEntry],
    authorization: Authorization,
    agent_models: dict[str, str] | None = None,
    approval_gate: Callable[[str, str], Awaitable[bool]] | None = None,
    ring: Any = None,
    question_broker: Any = None,
    pause_gate: Any = None,
) -> _Infra:
    # G1：授权未经人工确认/范围为空/时间窗过期 → 拒绝启动（异常中断，非静默）
    if not authorization.human_confirmed:
        raise PolicyViolation("授权信息未经人工确认（human_confirmed=false），拒绝启动（G1）")
    audit = AuditLogger(cfg.audit_log_path)
    audit.log("system.lifecycle", "runtime", {
        "action": "engagement_start", "mode": authorization.mode.value,
        "auth_id": authorization.auth_id, "target": authorization.target_description,
    })
    guard = ScopeGuard(authorization.scope)
    guard.assert_operable()
    if question_broker is not None:
        question_broker.bind_guard(guard)   # 用户勾选扩围 → 写入会话批准集
    # 平台红线由知识库漏洞标准库装载（3.4 全量 7 平台规则，首次启动自动播种）
    knowledge = KnowledgeStore(cfg.knowledge.sqlite_path, auto_seed=True)
    from spiritscout.resources.store import ResourceStore, resource_root

    resources = ResourceStore(resource_root(cfg), cfg.resources.freshness_hours)
    knowledge.resources = resources
    cfg = cfg.model_copy(deep=True)
    cfg.mcp_servers = resources.effective_mcp(cfg.mcp_servers)
    # 以下资源在创建后任何一步失败都要回收：先置空占位，异常路径统一收口
    mcp_clients: list[Any] = []
    mcp_startup_tasks: list[asyncio.Task[Any]] = []   # 在握 MCP 任务（中断时取消回收）
    experience: Any = None
    bus: Any = None
    blackboard: Blackboard | None = None
    run_lock = ExecutionRunLock(Path(cfg.workspace.root), Path(cfg.blackboard.sqlite_path))
    journal: Any = None
    try:
        run_lock.acquire()
        platform_rule = knowledge.platform_rule_view(authorization.src_platform)
        policy = PolicyEngine(authorization.mode, guard,
                              platform_rule=platform_rule, audit=audit,
                              approval_gate=approval_gate)
        if authorization.mode == RunMode.SRC:
            policy.check_automation_allowed()   # 平台禁自动化时启动即阻断
        bus = create_bus(
            cfg.bus.backend,
            queue_maxsize=cfg.bus.queue_maxsize,
            rate_limit_per_second=cfg.bus.rate_limit_per_second,
            redis_url=os.environ.get(cfg.bus.redis_url_env) or cfg.bus.redis_url,
            channel_prefix=(f"ss:{cfg.bus.namespace}:session:"
                            + hashlib.sha256(str(Path(cfg.workspace.root).resolve()).encode())
                            .hexdigest()[:24]),
            connect_timeout_seconds=cfg.bus.connect_timeout_seconds,
            nats_url=cfg.bus.nats_url,
        )
        await bus.start()
        limiter = RateLimiter(
            max(authorization.scope.max_requests_per_second, 0.1),
            shared_acquire=(lambda: bus.acquire_request_slot(
                cfg.bus.shared_request_rps, f"ss:{cfg.bus.namespace}:limits:requests"))
            if cfg.bus.backend == "redis" else None)
        blackboard = Blackboard(cfg.blackboard.sqlite_path, bus=bus)
        if question_broker is not None:
            question_broker.bind_context(blackboard)
        blackboard.start_notifications()
        from spiritscout.core.models import AgentType

        blackboard.write(
            BlackboardLayer.TARGET_AUTH, authorization.auth_id,
            authorization.model_dump(mode="json"),
            agent_type=AgentType.MASTER, updated_by="runtime",
        )
        def collect_asset(layer: BlackboardLayer, record_id: str, version: int, data: dict[str, Any]) -> None:
            from spiritscout.resources.store import record_asset

            asset = record_asset(layer.value,data)
            if asset is None:
                return
            try:
                resources.observe(asset, f"{Path(cfg.workspace.root).name}:{layer.value}:{record_id}@{version}")
            except Exception:  # noqa: BLE001
                audit.log("assets.sync_failed", "runtime", {"record_id": record_id,
                          "reason": "共享库暂时不可写；会话黑板已保留，可从项目页补同步"})

        blackboard.record_observer = collect_asset
        # 只导入历史参考摘要；不复制凭据、Flag 或此前授权。
        from urllib.parse import urlsplit

        hosts = {value.lstrip("*.").lower() for value in authorization.scope.allowed_domains}
        hosts.update(urlsplit(value).hostname for value in authorization.scope.allowed_urls)
        prior = []
        for host in sorted(value for value in hosts if value)[:3]:
            prior.extend(resources.assets(host=host, limit=8)["items"])
        if prior:
            blackboard.write(BlackboardLayer.TOPOLOGY, "historical-assets", {
                "items": prior, "note": "跨会话历史参考，均未在本次会话复核；内容可能变化，不能据此声称漏洞仍存在或沿用授权。"},
                updated_by="asset-memory")
        # 同项目共享记忆：分类摘要，不含 scope/凭据；未复核且不构成授权。
        # 会话目录名即 session_id（web 层 _session_cfg 已把 workspace 指到会话目录）。
        session_id = Path(cfg.workspace.root).name
        project_id = resources.project_for_session(session_id)
        if project_id:
            memory = resources.project_memory(project_id)
            if memory:
                blackboard.write(BlackboardLayer.TOPOLOGY, "project-memory",
                                 memory, updated_by="project-memory")
        convergence = ConvergenceEngine(blackboard)
        gateway = ModelGateway(ModelRegistry(models), config=cfg.gateway, audit=audit)
        gateway.set_mode(authorization.mode.value)   # 模式预算计量与熔断（4.9.4）
        # 执行器三态（4.1）：本地 CLI 默认；docker 后端为子 Agent 沙箱隔离（加固）
        executor: ToolExecutor
        if cfg.executor.backend == "docker":
            if not docker_available():
                # 不静默降级：显式选择的 docker 后端不可用时拒绝启动并给出出路
                raise ConfigurationError(
                    "executor.backend=docker 但 Docker 守护进程不可用（docker CLI 缺失"
                    "或 dockerd 未运行）。请启动 Docker 服务，或将 executor.backend "
                    "改回 local 后重试。")
            executor = DockerExecutor(
                image=cfg.executor.docker_image,
                # CTF 模式网络隔离（硬约束 C7）：容器 --network none
                network_isolation=(authorization.mode == RunMode.CTF
                                   and cfg.executor.ctf_network_isolation),
                memory_mb=cfg.executor.docker_memory_mb,
                cpus=cfg.executor.docker_cpus,
                pids_limit=cfg.executor.docker_pids_limit,
                read_only=cfg.executor.docker_read_only,
                tmpfs=cfg.executor.docker_tmpfs,
                user=cfg.executor.docker_user,
                max_output_chars=cfg.executor.max_output_chars, audit=audit,
            )
        else:
            executor = LocalExecutor(
                max_output_chars=cfg.executor.max_output_chars, audit=audit,
            )
        workspace = Path(cfg.workspace.root)
        workspace.mkdir(parents=True, exist_ok=True)
        from spiritscout.tools.journal import ExecutionJournal

        journal = ExecutionJournal(blackboard, workspace, new_id("run"))
        journal.activate_run(run_lock)
        registry = ToolRegistry(executor, policy=policy, audit=audit, journal=journal)
        from spiritscout.gateway.usage import bind_usage

        bind_usage(gateway, workspace / "usage.json")
        # 经验库默认跟随工作区；Web 会话在 _session_cfg 里把 root 指向共享目录，
        # 保证提炼出的经验跨会话可见（会话隔离不隔离经验）。
        experience = ExperienceStore(
            experience_db_path(cfg.experience.root, str(workspace)), audit=audit)
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
            attachments, blackboard=blackboard, guard=guard, audit=audit)
        sandbox_dir = workspace / "sandbox"
        sandbox_dir.mkdir(exist_ok=True)
        artifacts_dir = workspace / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)
        builtin_ctx = {
            "guard": guard,
            "limiter": limiter,
            # 附件与沙箱产物目录都在附件分析工具的允许清单内——脚本产物
            # 可以直接接续 file_analyze/analyze_attachments
            "allowed_dirs": [str(attachments), str(sandbox_dir), str(artifacts_dir)],
            "workspace_root": str(workspace),
            # 控制面根目录（Web 会话为基础工作区）：沙箱需整层遮蔽后再
            # 开放会话产物目录（[]/空 = 无额外遮蔽，CLI 直连场景）
            "guard_root": str(getattr(cfg.workspace, "guard_root", "") or ""),
            "resources": resources,
        }
        # 外部 MCP 服务器：握手 + 工具清单并入智能体工具目录（失败留痕跳过，
        # 不阻断开工——MCP 是增益能力而非主链依赖；多台服务器并发握手，
        # 慢服务器不拖累开工时序）
        mcp_tools: dict[str, dict[str, Any]] = {}
        enabled_specs = [s for s in cfg.mcp_servers if s.enabled]
        if enabled_specs:
            from spiritscout.tools.diagnostics import (
                browser_failure,
                browser_tool_guidance,
                chrome_tool_guidance,
                clean_tool_text,
            )
            from spiritscout.tools.mcp_client import build_client, resolve_mcp_risk

            async def _start_mcp(spec: Any) -> tuple[Any, Any, list[dict[str, Any]], str]:
                client = None
                try:
                    if list(spec.args[:2]) == ["-m", "spiritscout.tools.chrome_mcp"]:
                        spec = spec.model_copy(update={"env": {
                            **spec.env, "SPIRITSCOUT_CHROME_OUTPUT_DIR": str(workspace / "browser-output")}})
                    client = build_client(spec)
                    # 启动前登记所有权；gather 尚未完成时取消也能完整回收。
                    mcp_clients.append(client)
                    return spec, client, await client.start(), ""
                except Exception as exc:  # noqa: BLE001 —— 不可达：留痕后继续
                    if client is not None:
                        with contextlib.suppress(Exception):
                            await client.close()
                    return spec, None, [], str(exc)[:300]

            def _register_mcp(result: tuple[Any, Any, list[dict[str, Any]], str],
                              *, late: bool = False) -> None:
                """单台服务器的工具注册：开工前与后台续握完成时共用。"""
                spec, client, tools, err = result
                if client is None:
                    audit.log("system.lifecycle", "runtime", {
                        "action": "mcp_server_failed", "server": spec.name,
                        "error": err, "late": late})
                    return
                for t in tools:
                    t = {**t, "description": (t.get("description") or t["name"])
                         + browser_tool_guidance(t["name"])
                         + (chrome_tool_guidance(t["name"]) if spec.name == "chrome" else "")}
                    exposed_name = f"mcp__{spec.name}__{t['name']}"
                    risk = resolve_mcp_risk(spec, t)
                    properties = (t.get("inputSchema") or {}).get("properties") or {}
                    target_params = (() if spec.scope_enforcement == "none" else
                                     tuple(p for p in spec.target_params if p in properties))
                    # 行为声明与测试范围解耦：危险语义仍按每次调用即时审批。
                    declaration = spec.risk_by_action or risk == RiskLevel.INTRUSIVE
                    action_param = "_safety_action" if declaration else "action"
                    model_tool = copy.deepcopy(t)
                    if declaration:
                        from spiritscout.tools.spec import ActionCategory

                        if action_param in properties:
                            raise ConfigurationError(f"MCP 参数 {action_param} 与宿主行为声明冲突")
                        schema = model_tool.setdefault("inputSchema", {})
                        schema.setdefault("type", "object")
                        schema.setdefault("properties", {})[action_param] = {
                            "type": "string", "enum": [a.value for a in ActionCategory],
                            "description": "声明本次实际行为，而非工具能力：普通浏览、点击、授权登录、"
                            "只读探测与查询选 analysis；改写业务数据、上传、重启服务等选 "
                            "intrusive_write；删除文件、爆破、反弹连接、WebShell、容器逃逸选对应"
                            "类别，执行前逐次审批。与原生 action 独立。"}
                        schema["required"] = list(dict.fromkeys([*schema.get("required", []), action_param]))
                    tool_spec = ToolSpec(
                        name=exposed_name, kind=ToolKind.MCP,
                        description=t.get("description") or t["name"], risk=risk,
                        timeout_seconds=max(1, int(spec.timeout)),
                        allowed_params=set(properties) | ({action_param} if declaration else set()),
                        action_required=declaration, action_param=action_param,
                        automatic_replay=not declaration, target_params=target_params,
                        allowed_agents=tuple(role.value for role in spec.allowed_agents))

                    async def _call_mcp(*, _client=client, _tool=t["name"],
                                        _timeout=float(spec.timeout),
                                        _properties=frozenset(properties),
                                        _name=exposed_name, **kwargs: Any) -> ToolResult:
                        # ToolRegistry 会追加内置上下文；远端只接收 schema 声明的参数。
                        arguments = {k: v for k, v in kwargs.items() if k in _properties}
                        try:
                            result = await _client.call(_tool, arguments, timeout=_timeout)
                        except Exception as exc:  # noqa: BLE001 —— 归一为工具失败
                            return ToolResult(tool=_name, ok=False, returncode=-2,
                                              error=f"{type(exc).__name__}: {exc!s}"[:1000])
                        failed = bool(result.get("is_error"))
                        text = clean_tool_text(str(result.get("text", "")))[:16000]
                        diagnostic = browser_failure(_tool, text) if failed else {}
                        reason = text or f"MCP 工具 {_tool} 返回 isError=true，未提供文本诊断"
                        if diagnostic:
                            reason = diagnostic["failure_summary"] + "\n" + reason
                        return ToolResult(
                            tool=_name, ok=not failed,
                            returncode=1 if failed else 0, stdout=text,
                            error=reason if failed else "",
                            # 服务已给出确定失败结果，交给模型修参；不原样自动重放。
                            parsed={"retryable": False, **diagnostic} if failed else {})

                    registry.register_builtin(exposed_name, _call_mcp, tool_spec)
                    mcp_tools[exposed_name] = {
                        "client": client, "server": spec.name,
                        "tool": t["name"], "timeout": float(spec.timeout),
                        "schema": model_tool, "risk": risk.value,
                        "target_params": list(target_params),
                        "scope_enforcement": spec.scope_enforcement}
                audit.log("system.lifecycle", "runtime", {
                    "action": "mcp_server_ready", "server": spec.name,
                    "transport": spec.transport, "late": late,
                    "tools": [t["name"] for t in tools]})

            # 并发握手但只等一个宽限期：快服务器照常开工前并入；慢/卡死的
            # 转入后台续握、就绪一台并入一台（活目录，智能体下回合可见）
            startup = [asyncio.ensure_future(_start_mcp(s)) for s in enabled_specs]
            # 先登记后等待：宽限期内遭中断/异常时，这些在握任务同样要取消
            # 回收——若等 wait 返回后才登记，收口路径就会漏掉它们
            mcp_startup_tasks.extend(startup)
            done, pending = await asyncio.wait(startup, timeout=_MCP_STARTUP_GRACE)
            for finished in done:
                _register_mcp(finished.result())
            if pending:

                async def _finish_mcp_startup() -> None:
                    # 后台续握：逐台注册；取消时静默退出（客户端回收在收口）
                    for fut in asyncio.as_completed(pending):
                        try:
                            _register_mcp(await fut, late=True)
                        except asyncio.CancelledError:
                            raise
                        except BaseException:  # noqa: BLE001 —— 单台异常不拖累其余
                            continue

                mcp_startup_tasks.append(asyncio.ensure_future(_finish_mcp_startup()))
        from spiritscout.tools.builtin import asset_memory_lookup

        for name, fn in (("asset_memory_lookup", asset_memory_lookup), ("http_probe", http_probe), ("dns_resolve", dns_resolve),
                         ("tcp_scan", tcp_scan), ("file_analyze", file_analyze),
                         ("sandbox_exec", sandbox_exec)):
            registry.register_builtin(name, fn, BUILTIN_SPECS[name])
        # 按 Agent 绑定的模型分配——失效模型（注册表已删）剔除并留痕，
        # 不静默绑定到不存在的模型导致路由期失败
        bound = dict(agent_models or {})
        known = {m.model for m in models}
        stale = {k: v for k, v in bound.items() if v not in known}
        if stale:
            audit.log("system.lifecycle", "runtime", {
                "action": "agent_model_binding_dropped", "stale": stale,
                "reason": "模型不在注册表，按档位路由"})
            bound = {k: v for k, v in bound.items() if v in known}
        return _Infra(
            cfg=cfg, authorization=authorization, audit=audit, guard=guard,
            policy=policy, limiter=limiter, bus=bus, blackboard=blackboard,
            convergence=convergence, gateway=gateway, tools=registry,
            builtin_ctx=builtin_ctx, workspace=workspace, knowledge=knowledge,
            agent_models=bound, ring=ring if ring is not None else RingLedger(audit),
            shared_cache=LocalSharedCache(default_ttl_seconds=3600),
            mcp_clients=mcp_clients, mcp_startup_tasks=mcp_startup_tasks,
            mcp_tools=mcp_tools,
            questions=question_broker,
            experience=experience,
            pause_gate=pause_gate,
            run_lock=run_lock,
        )
    except BaseException as exc:
        # 初始化中断不留泄漏：已建资源统一收口（MCP/黑板/经验库/知识库/总线）
        if question_broker is not None:
            question_broker.bind_context(None)
        try:
            if journal is not None:
                with contextlib.suppress(Exception):
                    journal.finish_run("cancelled" if isinstance(exc, asyncio.CancelledError)
                                       else "failed")
            # 后台仍在握手的 MCP：取消任务后统一回收客户端
            for task in mcp_startup_tasks:
                if not task.done():
                    task.cancel()
            if mcp_startup_tasks:
                with contextlib.suppress(BaseException):
                    await asyncio.gather(*mcp_startup_tasks, return_exceptions=True)
            await _teardown_infra(
                mcp_clients=mcp_clients, blackboard=blackboard,
                experience=experience, knowledge=knowledge, bus=bus)
        finally:
            run_lock.close()
        raise


def _sub_ctx(infra: _Infra) -> SubAgentContext:
    return SubAgentContext(
        bus=infra.bus, blackboard=infra.blackboard, gateway=infra.gateway,
        tools=infra.tools, convergence=infra.convergence,
        audit=infra.audit, policy=infra.policy,
        builtin_ctx=infra.builtin_ctx,
        knowledge=infra.knowledge,   # 子 Agent 经验库检索接线（3.3.6）
        agent_models=infra.agent_models,   # 子 Agent 初始化时按名册绑定
        ring=infra.ring,             # 环形台账共持（避让登记 + 督导消费）
        shared_cache=infra.shared_cache,   # 环形共享缓存共持（同题现场/笔记/Flag）
        mcp_tools=infra.mcp_tools,   # 外部 MCP 工具（mcp__<server>__<tool>）
        questions=infra.questions,   # 模型发起用户询问的通道（ask_user）
        pause_gate=infra.pause_gate,  # 暂停门共持（步骤边界挂起）
        journal=infra.tools.journal,
        mode=infra.authorization.mode,
    )


def _make_assembler(infra: _Infra, enable_llm_planning: bool) -> ModeAssembler:
    return ModeAssembler(
        _sub_ctx(infra),
        default_routes=infra.cfg.default_routes_per_agent,
        max_routes=infra.cfg.max_routes_per_agent,
        enable_llm_planning=enable_llm_planning,
    )


async def _run_master(
    infra: _Infra,
    assembly,
    target: str,
    *,
    enable_llm_planning: bool,
    mission: str = "",
    user_inbox: list[dict[str, Any]] | None = None,
    standing_rules: str = "",
    dialogue_history: list[dict[str, str]] | None = None,
    experience: Any = None,
    platform: Any = None,
    platform_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """装配主 Agent 并运行多轮闭环，返回 run_summary。"""
    from spiritscout.tools.journal import read_recovery_view

    recovery = read_recovery_view(Path(infra.cfg.blackboard.sqlite_path))
    recovery_context = (infra.tools.journal.planning_context(recovery)
                        if infra.tools.journal is not None else {})
    from spiritscout.gateway.world_state import sync_user_context

    # 接收时已落盘的原话按消息 ID 幂等恢复；任务更新也不能覆盖并发答复。
    sync_user_context(infra.blackboard, dialogue_history=dialogue_history,
                      mission=mission, standing_rules=standing_rules)
    # 开局召回：带标签与适用场景的经验，帮灵枢判断是否与本目标同场景
    recall = (experience.recent(limit=infra.cfg.experience.recall_limit)
              if experience else [])
    master_extra: dict[str, Any] = {
        "mode_notes": assembly.notes, "mission": mission,
        "mode_prompt": getattr(assembly, "strategy_prompt", ""),
        "execution_recovery": recovery_context,
        "secretary_enabled": infra.cfg.secretary.enabled,
        "secretary_gateway": AgentBoundGateway(infra.gateway, infra.agent_models.get("secretary", "")),
        "standing_rules": standing_rules,
        "recent_experience": format_insights(recall),
        "flag_manager": getattr(assembly, "flag_manager", None),
        "user_inbox": user_inbox if user_inbox is not None else [],
        "dialogue_history": dialogue_history,
    }
    if platform is not None:
        # 平台适配器只在灵枢（主 Agent）侧：调度期开实例、完成期回收，
        # 事实流进复盘视图；子 Agent 不接触平台凭据与接口
        master_extra["platform"] = platform
        master_extra["platform_facts"] = (
            platform_facts if platform_facts is not None else [])
    master = MasterAgent(MasterContext(
        bus=infra.bus, blackboard=infra.blackboard, convergence=infra.convergence,
        gateway=AgentBoundGateway(
            infra.gateway, infra.agent_models.get("master", "")),
        policy=infra.policy, agents=assembly.agents,
        audit=infra.audit, enable_llm_planning=enable_llm_planning,
        tactic_map=assembly.tactic_map, plan_override=assembly.plan_provider,
        extra=master_extra,
        knowledge=infra.knowledge,   # 主 Agent 攻击路径模板接线（3.3.6）
        ring=infra.ring,             # 灵枢督导通道（与子 Agent 共持同一台账）
        pause_gate=infra.pause_gate,  # 暂停门共持（图节点边界挂起）
        questions=infra.questions,   # 复盘向用户提问通道（ask_user 后端）
        journal=infra.tools.journal,
    ))
    from spiritscout.agents.verifier import VerificationQueue

    def verification_rules() -> dict[str, Any]:
        history = master.ctx.extra.get("dialogue_history") or []
        replies = infra.blackboard.query(BlackboardLayer.RECON_CLUE, limit=10000,
                                         data_filter=("clue_type", "user_reply"))
        return {"mission": master._mission(), "standing_rules": master._rules(),
                "user_messages": [str(item.get("content", "")) for item in history if item.get("role") == "user"],
                "user_replies": [rec["data"].get("value", "") for rec in replies]}

    verifier = VerificationQueue(_sub_ctx(infra), verification_rules, enabled=enable_llm_planning)
    verifier.start()
    master.ctx.extra["verification_drain"] = verifier.drain
    run_summary: dict[str, Any] = {}
    try:
        run_summary = await master.run(authorization=infra.authorization, target=target)
        await verifier.drain()
        run_summary["verification"] = verifier.stats()
    except asyncio.CancelledError:
        # 用户主动停止：提炼转后台（最长约一分钟），不干等——立即退场
        _schedule_background_distill(infra, assembly, experience, enable_llm_planning,
                                     run_summary or None, mission=mission,
                                     outcome="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 —— 失败同样不拖终态：后台沉淀教训后原样抛
        _schedule_background_distill(infra, assembly, experience, enable_llm_planning,
                                     run_summary or None, mission=mission,
                                     outcome="failed",
                                     error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        await verifier.close()
    run_summary["recovery_mode"] = recovery_context.get("mode", "history_replan")
    if infra.tools.journal is not None:
        run_summary["execution_run_id"] = infra.tools.journal.run_id
    run_summary["model_usage"] = infra.gateway.usage_report()

    # 灵枢收尾提炼经验：从全场战报中提炼可复用洞察（非步骤），存入经验库。
    # 完成态提炼移到调用方与报告生成并行（两笔模型调用互不依赖，串行磨
    # 是收尾等待的大头）；取消/失败路径无报告可搭，仍在原地提炼
    # 刷新灵枢 extra 中的经验（提炼结果立即对后续派工可见）
    recent_exp = (experience.recent(limit=infra.cfg.experience.recall_limit)
                  if experience else [])
    master.ctx.extra["recent_experience"] = format_insights(recent_exp)
    run_summary["mode_notes"] = assembly.notes
    if mission:
        run_summary["mission"] = mission
    return run_summary


def _write_report_file(path: Path, body: str, *, contest: str) -> Path:
    header = (f"# 灵枢 SpiritScout CTF WriteUp\n\n> 生成时间："
              f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} ｜ 赛制：{contest}\n\n")
    path.write_text(header + body.strip() + "\n", encoding="utf-8")
    return path


def _write_competition_summary(
    path: str | Path,
    *,
    contest: str,
    run_summary: dict[str, Any],
    flag_manager: Any,
    challenges: list[dict[str, Any]],
) -> Path:
    """竞赛短摘要：只留得分事实与赛题状态，不生成 WP/复现教程。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = flag_manager.stats()
    platform = run_summary.get("platform") or {}
    accepted_ids = {str(f.challenge_id) for f in flag_manager.accepted_flags()}
    lines = [
        "# 智能渗透赛结果摘要",
        "",
        f"> 生成时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        f" ｜ 赛制：{contest}",
        "",
        "## 得分与提交",
        "",
        f"- 平台已确认 Flag：{flags.get('accepted', 0)}",
        f"- 已提交 Flag：{flags.get('submitted', 0)}",
        f"- 提取候选：{flags.get('extracted', 0)}",
        "- 平台未提供总分时，本摘要不推算或冒充平台总分。",
    ]
    if platform:
        lines.extend([
            f"- 平台：{platform.get('platform', '未命名')}",
            f"- 平台列题：{platform.get('listed', '未知')}",
            f"- 活跃容器：{platform.get('active_instances', '未知')}",
        ])
    lines.extend(["", "## 赛题状态", ""])
    seen: set[str] = set()
    for challenge in challenges:
        cid = str(challenge.get("id") or challenge.get("title") or "?")
        if cid in seen:
            continue
        seen.add(cid)
        title = str(challenge.get("title") or cid)
        status = "平台确认完成" if cid in accepted_ids else "未确认完成"
        lines.append(f"- {title}（{cid}）：{status}")
    if not seen:
        lines.append("- 本次没有可汇报的赛题状态。")
    lines.extend([
        "", "## 赛后沉淀", "",
        "经验提炼在后台异步进行；本摘要不等待总结，也不包含逐题解题步骤。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


async def _render_writeup(
    infra: _Infra, challenges: list[dict[str, Any]], flag_manager: Any,
    run_summary: dict[str, Any], report_path: str | Path, *, contest: str,
    enable_llm: bool, fallback_context: dict[str, Any],
) -> Path:
    """WriteUp 优先由灵枢亲自撰写（全场脉络在它手里）；LLM 不可用回退模板。"""
    body = None
    if enable_llm:
        body = await writeup_body(
            AgentBoundGateway(infra.gateway, infra.agent_models.get("master", "")),
            digest=build_digest(challenges=challenges, flags=flag_manager.all(),
                                run_summary=run_summary, blackboard=infra.blackboard),
            audit=infra.audit)
    if body:
        return _write_report_file(Path(report_path), body, contest=contest)
    report = ReportAgent(infra.blackboard)
    return report.generate_writeup(challenges, flag_manager.all(), report_path,
                                   context=fallback_context)


def _distill_digest(infra: _Infra, assembly: Any,
                    run_summary: dict[str, Any] | None,
                    mission: str, outcome: str, error: str = "") -> dict[str, Any]:
    """收尾提炼的战报：把成/败/坑的可复用信号压成小块 JSON（不含步骤序列）。"""
    summary = run_summary or {}
    digest: dict[str, Any] = {
        "mode": infra.authorization.mode.value,
        "platform": infra.authorization.src_platform or "",
        "target": str(summary.get("target")
                      or infra.authorization.target_description or "")[:200],
        "mission": str(mission or "")[:200],
        "outcome": outcome,
        "rounds": summary.get("rounds_executed", 0),
        "stop_reason": str(summary.get("stop_reason") or "")[:160],
    }
    if error:
        digest["error"] = error[:300]
    flag_manager = getattr(assembly, "flag_manager", None)
    if flag_manager is not None:
        try:
            digest["flags"] = flag_manager.stats()
        except Exception:  # noqa: BLE001 —— 统计失败不阻断提炼
            pass
    dag = summary.get("dag_nodes") or []
    if dag:
        digest["plan"] = [{"title": str(node.get("title", ""))[:80],
                           "status": str(node.get("status", ""))}
                          for node in dag[:10]]
    try:
        bb = infra.blackboard
        findings: dict[str, Any] = {"total": bb.count(BlackboardLayer.VULN),
                                    "by_status": bb.status_counts(BlackboardLayer.VULN)}
        accepted = []
        for rec in bb.query(BlackboardLayer.VULN, limit=200):
            data = rec["data"]
            if str(data.get("status")) == "accepted" and data.get("title"):
                accepted.append({"title": str(data["title"])[:100],
                                 "severity": str(data.get("severity", "")),
                                 "type": str(data.get("vuln_type", ""))})
        if accepted:
            findings["accepted"] = accepted[:8]
        digest["findings"] = findings
        trials: list[dict[str, str]] = []
        for rec in bb.query(BlackboardLayer.TASK_STATE, limit=100):
            data = rec["data"]
            if "dag_snapshot" in data:
                continue
            task = data.get("task", {})
            for route in data.get("routes", []):
                if str(route.get("status")) in {"pruned", "failed"}:
                    note = (route.get("notes") or [""])[-1]
                    trials.append({"agent": str(task.get("agent_type", "")),
                                   "route": str(route.get("name", ""))[:60],
                                   "status": str(route.get("status")),
                                   "note": str(note)[:140]})
        if trials:
            digest["trials"] = trials[:8]
        digest["tasks_recorded"] = bb.count(BlackboardLayer.TASK_STATE)
    except Exception:  # noqa: BLE001 —— 黑板读取异常时保留最小战报
        pass
    return digest


def _worth_distilling(digest: dict[str, Any]) -> bool:
    """正常收尾总提炼（提示词自行决定是否空数组）；中止/失败需确实跑过东西。"""
    if digest.get("outcome") == "completed":
        return True
    flags = digest.get("flags") or {}
    findings = digest.get("findings") or {}
    return bool(digest.get("rounds") or digest.get("tasks_recorded")
                or flags.get("extracted") or findings.get("total"))


async def _distill_experience(gateway: Any, experience: ExperienceStore,
                              digest: dict[str, Any], *,
                              timeout_seconds: int,
                              audit: Any = None,
                              source_session: str = "") -> dict[str, Any]:
    """一次提炼调用：返回结果摘要（审计留痕由调用方负责）。

    走 chat_decision：思考型模型可能把输出额度全耗在思考上、正文为空
    ——加大余量重试一次，而不是让整场经验提炼静默失败（实测 glm-5.3
    思考可吃满 1024 额度，此前经验库因此长期为空）。
    """
    import json as _json

    from spiritscout.gateway.backends import extract_json
    from spiritscout.gateway.gateway import chat_decision

    resp = await asyncio.wait_for(chat_decision(
        gateway,
        [{"role": "system", "content": (
            "你是灵枢。一场安全测试刚结束（正常收尾、被中断或失败均可能）。"
            "从以下战报中提炼 3~5 条可复用的实战洞察——不是解题步骤，"
            "而是「这类场景下我学到了什么」：目标/平台特性、手法适用条件、"
            "容易踩的坑、判读信号、中断或失败的教训。"
            "纪律：①洞察必须脱离本题仍然成立——单题发现要抽象成同类场景"
            "的信号与事实，抽象不出来的（具体路径、参数、口令、端口、命令、"
            "flag 原文）一律不写；②不写操作序列与处方（「先…再…」「应优先…」"
            "「直接…即可」），只写陈述句：什么条件下、能看到什么信号、"
            "为什么值得注意。反例（禁止写成这样）：「本题入口位于 /xxx 路径，"
            "应优先探测登录/日志等端点」——单题坐标加步骤处方。"
            "没有值得提炼的就返回空数组。"
            "每条一句话独立可复用；context 写适用场景（目标类型/业务/"
            "技术栈），tags 写 2~4 个检索标签（如 web、登录、越权、ctf、java）。"
            '返回 JSON：{"insights": [{"insight": str, "context": str, '
            '"tags": [str]}]}')},
         {"role": "user", "content": _json.dumps(digest, ensure_ascii=False)[:8000]}],
        task_type="meta_planning", response_format="json",
        # 起步额度给足：思考型模型首调常把额度耗在思考上（正文 0 字符
        # 作废重试白烧一次调用）——实测 glm-5.3 提炼可吃满 2048 额度，
        # 8192 起步一次成功，比失败后重试省一整轮
        max_tokens=8192, reasoning_effort="low",
        timeout_seconds=timeout_seconds,
        audit=audit, actor="master"),
        timeout=timeout_seconds + 40)
    data = extract_json(resp.content) or {}
    added = 0
    for item in (data.get("insights") or [])[:5]:
        if isinstance(item, dict) and str(item.get("insight") or "").strip():
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            if experience.add(str(item["insight"]),
                              context=str(item.get("context", "")),
                              tags=[str(tag) for tag in tags],
                              source_session=source_session,
                              source_model="master"):
                added += 1
    return {"result": "ok" if added else "empty", "insights": added}


def _prepare_distill(infra: _Infra, assembly: Any, enabled: bool, experience: Any,
                     run_summary: dict[str, Any] | None, *,
                     mission: str, outcome: str,
                     error: str = "") -> dict[str, Any] | None:
    """提炼前判定：战报同步取好（读黑板/统计），不值得提炼则留痕返回 None。

    同步完成是硬要求：退场路径必须在 infra 回收前拿到快照，后台任务才能
    脱离黑板/知识库等已回收资源独立跑完。
    """
    if not enabled or experience is None:
        return None
    digest = _distill_digest(infra, assembly, run_summary, mission, outcome, error)
    if not _worth_distilling(digest):
        infra.audit.log("experience.distill", "master", {
            "result": "skipped", "outcome": outcome,
            "reason": "没有值得沉淀的活动"})
        return None
    return digest


async def _distill_run(infra: _Infra, experience: Any, digest: dict[str, Any], *,
                       outcome: str) -> dict[str, Any]:
    """一次提炼执行 + 审计留痕（完成态原地等待与退场后台任务共用）。"""
    started = time.monotonic()
    # 来源记真实会话 ID（Web 会话工作区名即 sess-xxx，经验页据此回链）
    workspace_root = getattr(
        getattr(getattr(infra, "cfg", None), "workspace", None), "root", "")
    try:
        result = await _distill_experience(
            AgentBoundGateway(infra.gateway, infra.agent_models.get("master", "")),
            experience, digest,
            timeout_seconds=infra.cfg.experience.distill_timeout_seconds,
            audit=infra.audit,
            source_session=Path(str(workspace_root)).name if workspace_root else "")
    except Exception as exc:  # noqa: BLE001 —— 不吞 CancelledError（停止语义优先）
        result = {"result": "error", "insights": 0,
                  "error": f"{type(exc).__name__}: {exc}"[:200]}
    infra.audit.log("experience.distill", "master", {
        **result, "outcome": outcome,
        "duration_ms": round((time.monotonic() - started) * 1000, 1)})
    return result


async def _distill_best_effort(infra: _Infra, assembly: Any, experience: Any,
                               enabled: bool,
                               run_summary: dict[str, Any] | None,
                               *, mission: str, outcome: str,
                               error: str = "") -> None:
    """终态提炼（尽力而为，原地等待）：正常收尾与报告并行，用户等得起。"""
    digest = _prepare_distill(infra, assembly, enabled, experience, run_summary,
                              mission=mission, outcome=outcome, error=error)
    if digest is None:
        return
    await _distill_run(infra, experience, digest, outcome=outcome)


_BACKGROUND_DISTILLS: set[asyncio.Task[Any]] = set()


def _distill_queue_dir(workspace: Path) -> Path:
    return workspace / "experience_distill"


def _write_distill_snapshot(path: Path, payload: dict[str, Any]) -> None:
    """原子写任务快照；后台任务与重启恢复共读同一格式。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def _update_distill_snapshot(path: Path | None, **updates: Any) -> None:
    if path is None:
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(updates)
        _write_distill_snapshot(path, payload)
    except (OSError, ValueError, TypeError):
        pass


def _persist_distill_snapshot(
        infra: _Infra, digest: dict[str, Any], *, outcome: str) -> Path | None:
    workspace = getattr(infra, "workspace", None)
    if not isinstance(workspace, Path):
        return None
    try:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = _distill_queue_dir(workspace) / f"{stamp}-{new_id('distill')}.json"
        _write_distill_snapshot(path, {
            "schema": 1, "status": "pending", "attempts": 0,
            "outcome": outcome, "digest": digest,
            "session": workspace.name,
            "created_at": datetime.now(UTC).isoformat(),
        })
        return path
    except OSError as exc:
        infra.audit.log("experience.distill", "master", {
            "result": "queue_persist_failed", "outcome": outcome,
            "error": f"{type(exc).__name__}: {exc}"[:200]})
        return None


def pending_distill_snapshots(workspace: Path) -> list[Path]:
    """待恢复的提炼快照；已完成或重试耗尽的任务不再入队。"""
    queue = _distill_queue_dir(workspace)
    out: list[Path] = []
    for path in sorted(queue.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        status = str(payload.get("status") or "pending")
        try:
            attempts = int(payload.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if status != "done" and attempts < 3:
            out.append(path)
    return out


def _schedule_background_distill(infra: _Infra, assembly: Any, experience: Any,
                                 enabled: bool,
                                 run_summary: dict[str, Any] | None,
                                 *, mission: str, outcome: str,
                                 error: str = "") -> asyncio.Task[Any] | None:
    """赛后提炼转后台：用户不再等待，前台任务始终优先。

    战报在 infra 回收前同步取好；经验库托付后台任务收尾（close 跳过它），
    其余资源照常即刻回收。快照先持久化，进程中止后可由会话管理器恢复；
    后台槽位串行且前台活跃时等待，避免与当前或下一场比赛争抢模型额度。
    """
    digest = _prepare_distill(infra, assembly, enabled, experience, run_summary,
                              mission=mission, outcome=outcome, error=error)
    if digest is None:
        return None
    snapshot = _persist_distill_snapshot(infra, digest, outcome=outcome)
    infra.experience_deferred = True
    task = asyncio.create_task(_background_distill(infra, experience, digest,
                                                   outcome=outcome,
                                                   snapshot=snapshot))
    _BACKGROUND_DISTILLS.add(task)
    task.add_done_callback(_BACKGROUND_DISTILLS.discard)
    return task


async def _background_distill(infra: _Infra, experience: Any,
                              digest: dict[str, Any], *, outcome: str,
                              snapshot: Path | None = None) -> None:
    """后台提炼收尾：执行提炼并关闭托付的经验库（连接不泄漏）。

    任何失败只吞掉不冒泡——后台任务无人接管异常，会话终态早已落定。
    """
    try:
        async with _background_slot():
            prior = 0
            if snapshot is not None:
                try:
                    prior = int(json.loads(
                        snapshot.read_text(encoding="utf-8")).get("attempts") or 0)
                except (OSError, ValueError, TypeError):
                    prior = 0
                _update_distill_snapshot(
                    snapshot, status="running", attempts=prior + 1,
                    started_at=datetime.now(UTC).isoformat())
            result = await _distill_run(
                infra, experience, digest, outcome=outcome)
            failed = result.get("result") == "error"
            _update_distill_snapshot(
                snapshot, status="failed" if failed else "done",
                completed_at=datetime.now(UTC).isoformat(), result=result)
    finally:
        with contextlib.suppress(Exception):
            experience.close()


async def resume_pending_distills(
    *,
    workspace: Path,
    cfg: SystemConfig,
    models: list[ModelEntry],
    agent_models: dict[str, str] | None = None,
) -> int:
    """恢复一个会话目录中的待提炼快照；供 Web 会话管理器启动时调用。"""
    snapshots = pending_distill_snapshots(workspace)
    if not snapshots or not models:
        return 0
    audit = AuditLogger(cfg.audit_log_path)
    gateway = ModelGateway(ModelRegistry(models), config=cfg.gateway, audit=audit)
    store = ExperienceStore(
        experience_db_path(cfg.experience.root, str(workspace)), audit=audit)
    completed = 0
    try:
        for snapshot in snapshots:
            try:
                payload = json.loads(snapshot.read_text(encoding="utf-8"))
                digest = payload.get("digest") or {}
                attempts = int(payload.get("attempts") or 0)
            except (OSError, ValueError, TypeError):
                continue
            async with _background_slot():
                _update_distill_snapshot(
                    snapshot, status="running", attempts=attempts + 1,
                    started_at=datetime.now(UTC).isoformat())
                try:
                    result = await _distill_experience(
                        AgentBoundGateway(
                            gateway, str((agent_models or {}).get("master", ""))),
                        store, digest,
                        timeout_seconds=cfg.experience.distill_timeout_seconds,
                        audit=audit,
                        source_session=str(payload.get("session") or workspace.name))
                    failed = result.get("result") == "error"
                except Exception as exc:  # noqa: BLE001 —— 单条失败保留快照重试
                    result = {"result": "error", "insights": 0,
                              "error": f"{type(exc).__name__}: {exc}"[:200]}
                    failed = True
                _update_distill_snapshot(
                    snapshot, status="failed" if failed else "done",
                    completed_at=datetime.now(UTC).isoformat(), result=result)
                if not failed:
                    completed += 1
    finally:
        store.close()
    return completed


def _default_report_path(workspace: Path, prefix: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return workspace / "reports" / f"{prefix}_{stamp}.md"


async def run_engagement(
    *,
    cfg: SystemConfig,
    models: list[ModelEntry],
    authorization: Authorization,
    target: str,
    report_path: str | Path | None = None,
    enable_llm_planning: bool = True,
    mission: str = "",
    agent_models: dict[str, str] | None = None,
    approval_gate: Callable[[str, str], Awaitable[bool]] | None = None,
    ring: Any = None,
    user_inbox: list[dict[str, Any]] | None = None,
    question_broker: Any = None,
    standing_rules: str = "",
    pause_gate: Any = None,
    dialogue_history: list[dict[str, str]] | None = None,
) -> EngagementResult:
    """SRC 模式任务入口：侦察 → 探测 → 全链路核验 → 报告。

    mission：用户自然语言任务意图，原样注入主 Agent 规划提示词，
    由主 Agent 自行决策下发子任务；授权边界仍在装配层确定性提取执行。
    dialogue_history：会话对话史（续传接着聊的上文），直达灵枢对话通道。
    """
    if authorization.mode != RunMode.SRC:
        raise SpiritScoutError("run_engagement 仅用于 SRC 模式（CTF/渗透赛请用专用入口）")
    infra = await _build_infra(cfg, models, authorization,
                               agent_models=agent_models,
                               approval_gate=approval_gate, ring=ring,
                               question_broker=question_broker,
                               pause_gate=pause_gate)
    try:
        _foreground_started()
        assembly = _make_assembler(infra, enable_llm_planning).assemble(RunMode.SRC)
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning,
            mission=mission, user_inbox=user_inbox,
            standing_rules=standing_rules,
            dialogue_history=dialogue_history,
            experience=infra.experience)

        # 独立复测已在候选提交时运行，不能再用模板成功覆盖用户规则裁决。
        verify_stats = run_summary.get("verification", {})

        # 经验提炼与报告互不依赖：并行执行，收尾等待不再串行叠加两段
        distill = asyncio.ensure_future(_distill_best_effort(
            infra, assembly, infra.experience, enable_llm_planning,
            run_summary, mission=mission, outcome="completed"))
        try:
            # ---- 报告生成（4.6）
            report_path = report_path or _default_report_path(
                infra.workspace, "src_report")
            report = ReportAgent(infra.blackboard)
            out = report.generate(infra.authorization, target, report_path,
                                  run_summary=run_summary)
        finally:
            await distill
        infra.audit.log("system.lifecycle", "runtime", {
            "action": "engagement_complete", "mode": "src",
            "report": str(out), "verify": verify_stats,
            "rounds": run_summary["rounds_executed"],
        })
        infra.outcome = "completed"
        return EngagementResult(run_summary=run_summary, verify_stats=verify_stats,
                                report_path=str(out))
    except asyncio.CancelledError:
        infra.outcome = "cancelled"
        raise
    finally:
        try:
            await infra.close()
        finally:
            _foreground_finished()


async def run_ctf_engagement(
    *,
    cfg: SystemConfig,
    models: list[ModelEntry],
    authorization: Authorization,
    challenges: list[dict[str, Any]],
    flag_patterns: list[str] | None = None,
    flag_submit_fn: SubmitFn | None = None,
    report_path: str | Path | None = None,
    enable_llm_planning: bool = True,
    agent_models: dict[str, str] | None = None,
    approval_gate: Callable[[str, str], Awaitable[bool]] | None = None,
    ring: Any = None,
    user_inbox: list[dict[str, Any]] | None = None,
    question_broker: Any = None,
    standing_rules: str = "",
    pause_gate: Any = None,
    resume: bool = False,
    dialogue_history: list[dict[str, str]] | None = None,
    platform_profile: dict[str, Any] | None = None,
) -> EngagementResult:
    """CTF 专项任务入口（4.4/5.2）：五方向专项 Agent + Flag 统一管理。

    硬约束 C7：CTF 模式不启用端口扫描（PolicyEngine 模式禁用表强制，
    即使 LLM 规划出扫描动作也会在工具门被拒）。
    flag_submit_fn=None 时只提取不提交（提交接口由使用者按赛方规范接入）。
    resume=True（续传）：Flag 库照常重载，已破题的赛题不再重解，
    未破题的与用户意见进入新一轮决策环。
    platform_profile：平台档案（托管题/跑分平台）——列题/开容器/完成回收/
    提交全部经适配器执行，题目源以平台清单为准；不提供时行为与原来一致。
    """
    if authorization.mode != RunMode.CTF:
        raise SpiritScoutError("run_ctf_engagement 要求授权模式为 CTF")
    infra = await _build_infra(cfg, models, authorization,
                               agent_models=agent_models,
                               approval_gate=approval_gate, ring=ring,
                               question_broker=question_broker,
                               pause_gate=pause_gate)
    adapter: Any = None
    try:
        _foreground_started()
        challenges = [dict(ch) for ch in challenges]
        if platform_profile:
            from spiritscout.ctf.platform import (
                PlatformAdapter,
                parse_platform_profile,
            )

            try:
                profile = parse_platform_profile(platform_profile)
            except ValueError as exc:  # 档案已两处校验过；兜底不阻断任务
                infra.audit.log("platform.fact", "runtime", {
                    "action": "profile_invalid", "error": str(exc)[:200]})
            else:
                adapter = PlatformAdapter(profile, audit=infra.audit)
                infra.audit.log("platform.fact", "runtime", {
                    "action": "profile_loaded", "profile": profile.redacted()})
                await adapter.precheck()
                listed = await adapter.list_challenges()
                if listed:
                    # 平台列题即题目源：URL/文本推导的赛题不参与平台模式
                    challenges = listed
                else:
                    infra.audit.log("platform.fact", "runtime", {
                        "action": "list_empty",
                        "detail": ("平台未列出题目，沿用任务提供的赛题清单"
                                   if challenges else "平台未列出题目，任务未提供赛题")})
        flag_manager = FlagManager(
            flag_patterns=flag_patterns,
            submit_fn=flag_submit_fn or (adapter.submit if adapter is not None else None),
            audit=infra.audit, bus=infra.bus,
            store_path=infra.workspace / "flags.json",
        )
        assembly = _make_assembler(infra, enable_llm_planning).assemble(
            RunMode.CTF, challenges=challenges, flag_manager=flag_manager,
            resume=resume, platform=adapter)
        target = next(
            (str(c["url"]) for c in challenges if c.get("url")),
            "ctf://challenges",
        )
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning,
            user_inbox=user_inbox,
            standing_rules=standing_rules,
            dialogue_history=dialogue_history,
            experience=infra.experience,
            platform=adapter,
            platform_facts=adapter.facts if adapter is not None else None)

        # ---- Flag 统一提交（4.4.6：格式校验/去重/频率控制/退避重试）
        # 经验提炼与提交/WriteUp 互不依赖：并行执行（两笔模型调用串行磨
        # 曾让 flag 后还要白等三分半钟）
        distill = asyncio.ensure_future(_distill_best_effort(
            infra, assembly, infra.experience, enable_llm_planning,
            run_summary, mission="", outcome="completed"))
        try:
            submit_stats = await flag_manager.submit_all()
            run_summary["flag_submit"] = submit_stats
            flag_stats = flag_manager.stats()
            if adapter is not None:
                run_summary["platform"] = adapter.summary()

            report_path = report_path or _default_report_path(
                infra.workspace, "ctf_writeup")
            out = await _render_writeup(
                infra, challenges, flag_manager, run_summary, report_path,
                contest="ctf", enable_llm=enable_llm_planning,
                fallback_context={
                    "contest_type": "ctf", "flag_stats": flag_stats,
                    "submit_enabled": flag_manager.submit_fn is not None,
                    "notes": assembly.notes,
                    "current_task_ids": [
                        task_id for node in run_summary.get("dag_nodes", [])
                        for task_id in node.get("task_ids", [])]},
            )
        finally:
            await distill
        infra.audit.log("system.lifecycle", "runtime", {
            "action": "engagement_complete", "mode": "ctf", "report": str(out),
            "flags": flag_stats, "rounds": run_summary["rounds_executed"],
        })
        infra.outcome = "completed"
        return EngagementResult(run_summary=run_summary, verify_stats={},
                                report_path=str(out), flag_stats=flag_stats)
    except asyncio.CancelledError:
        infra.outcome = "cancelled"
        raise
    finally:
        try:
            if adapter is not None:
                # 会话结束回收全部活跃容器（名额交还平台；失败只兜底不抛出）
                with contextlib.suppress(Exception):
                    await adapter.close_all(reason="会话结束")
                with contextlib.suppress(Exception):
                    await adapter.aclose()
            await infra.close()
        finally:
            _foreground_finished()


async def run_competition_engagement(
    *,
    cfg: SystemConfig,
    models: list[ModelEntry],
    authorization: Authorization,
    rules: CompetitionRules,
    challenges: list[dict[str, Any]],
    flag_submit_fn: SubmitFn | None = None,
    report_path: str | Path | None = None,
    enable_llm_planning: bool = True,
    total_duration_seconds: float = 8 * 3600,
    agent_models: dict[str, str] | None = None,
    approval_gate: Callable[[str, str], Awaitable[bool]] | None = None,
    ring: Any = None,
    user_inbox: list[dict[str, Any]] | None = None,
    question_broker: Any = None,
    standing_rules: str = "",
    pause_gate: Any = None,
    dialogue_history: list[dict[str, str]] | None = None,
    platform_profile: dict[str, Any] | None = None,
    mission: str = "",
) -> EngagementResult:
    """智能渗透赛任务入口（4.5/5.3）：赛制自适应调度。

    - Jeopardy：6.7 优先级公式排序后按 CTF 池分发；
    - AWD：攻击线允许端口扫描（仅限赛方授权 CIDR，已并入 scope）；
    - 靶场：SRC 链路 + 全链路核验，产出轻量结果摘要。

    platform_profile：平台档案（托管题/跑分平台）——列题/开容器/完成回收/
    提交全部经适配器执行（与 CTF 模式同一套语义）；不提供时行为与原来一致。
    """
    from spiritscout.competition import ChallengeCard, CompetitionEngine, ContestType

    if authorization.mode != RunMode.COMPETITION:
        raise SpiritScoutError("run_competition_engagement 要求授权模式为 COMPETITION")
    # 赛方授权 CIDR 并入授权范围（AWD 攻击线的合法边界，4.5.1）
    if rules.allowed_cidrs:
        merged_scope = authorization.scope.model_copy(update={
            "allowed_cidrs": sorted(
                set(authorization.scope.allowed_cidrs) | set(rules.allowed_cidrs))
        })
        authorization = authorization.model_copy(update={"scope": merged_scope})
    infra = await _build_infra(cfg, models, authorization,
                               agent_models=agent_models,
                               approval_gate=approval_gate, ring=ring,
                               question_broker=question_broker,
                               pause_gate=pause_gate)
    adapter: Any = None
    try:
        _foreground_started()
        # 平台档案（托管题/跑分平台）：列题即题目源；赛制（Jeopardy/AWD/靶场）
        # 不受影响——平台只换题源与提交/供给通道
        if platform_profile:
            from spiritscout.ctf.platform import (
                PlatformAdapter,
                parse_platform_profile,
            )

            try:
                profile = parse_platform_profile(platform_profile)
            except ValueError as exc:  # 档案已两处校验过；兜底不阻断任务
                infra.audit.log("platform.fact", "runtime", {
                    "action": "profile_invalid", "error": str(exc)[:200]})
            else:
                adapter = PlatformAdapter(profile, audit=infra.audit)
                infra.audit.log("platform.fact", "runtime", {
                    "action": "profile_loaded", "profile": profile.redacted()})
                await adapter.precheck()
                listed = await adapter.list_challenges()
                if listed:
                    # 平台列题即题目源：URL/文本推导的赛题不参与平台模式
                    challenges = listed
                else:
                    infra.audit.log("platform.fact", "runtime", {
                        "action": "list_empty",
                        "detail": ("平台未列出题目，沿用任务提供的赛题清单"
                                   if challenges else "平台未列出题目，任务未提供赛题")})
        # 赛方禁止行为按工具名登记为动态禁用（4.5.1 规则冲突之外的运行时边界）
        for behavior in rules.forbidden_behaviors:
            infra.policy.forbid_tool(behavior)
        engine = CompetitionEngine(
            rules, audit=infra.audit, bus=infra.bus,
            total_duration_seconds=total_duration_seconds,
        )
        flag_manager = FlagManager(
            flag_patterns=rules.flag_patterns or None,
            submit_fn=flag_submit_fn or (adapter.submit if adapter is not None else None),
            audit=infra.audit, bus=infra.bus,
            rate_limit_per_minute=rules.submit.rate_limit_per_minute,
            store_path=infra.workspace / "flags.json",
        )

        # Jeopardy：按 6.7 优先级公式排题（高分 × 成功率 × 时间衰减 ÷ 成本）
        ordered = list(challenges)
        if rules.contest_type == ContestType.JEOPARDY and ordered:
            cards = [
                ChallengeCard(
                    challenge_id=str(c.get("id", f"ch{i}")),
                    title=str(c.get("title", "")),
                    points=float(c.get("points", 100.0)),
                    estimated_cost=float(c.get("estimated_cost", 1.0)),
                )
                for i, c in enumerate(ordered)
            ]
            engine.schedule_jeopardy(cards)   # 审计留痕本轮选择
            rank = {c.challenge_id: engine.priority(c) for c in cards}
            ordered.sort(key=lambda c: rank.get(str(c.get("id", "")), 0.0),
                         reverse=True)

        assembly = _make_assembler(infra, enable_llm_planning).assemble(
            RunMode.COMPETITION, challenges=ordered, rules=rules,
            flag_manager=flag_manager, competition_engine=engine,
            platform=adapter)
        target = next(
            (str(c["url"]) for c in ordered if c.get("url")),
            authorization.target_description or "competition://contest",
        )
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning,
            mission=mission,
            user_inbox=user_inbox,
            standing_rules=standing_rules,
            dialogue_history=dialogue_history,
            experience=infra.experience,
            platform=adapter,
            platform_facts=adapter.facts if adapter is not None else None)

        verify_stats = run_summary.get("verification", {})

        # 赛后提炼持久化后进入低优先后台槽；主任务不等模型总结。
        distill_task = _schedule_background_distill(
            infra, assembly, infra.experience, enable_llm_planning,
            run_summary, mission=mission, outcome="completed")
        run_summary["experience_distill"] = {
            "status": "queued" if distill_task is not None else "skipped",
            "mode": "background",
        }
        try:
            submit_stats = await flag_manager.submit_all()
            run_summary["flag_submit"] = submit_stats
            # 已判定正确的 Flag → 标记赛题完成，驱动调度器资源转移（4.5.2）
            for cand in flag_manager.accepted_flags():
                engine.mark_solved([], cand.challenge_id)   # 审计留痕（卡片态在调度循环内维护）
            flag_stats = flag_manager.stats()
            if adapter is not None:
                run_summary["platform"] = adapter.summary()

            report_path = report_path or _default_report_path(
                infra.workspace, "competition_summary")
            out = _write_competition_summary(
                report_path, contest=rules.contest_type.value,
                run_summary=run_summary, flag_manager=flag_manager,
                challenges=ordered)
        except Exception:
            if distill_task is not None:
                distill_task.cancel()
            raise
        infra.audit.log("system.lifecycle", "runtime", {
            "action": "engagement_complete", "mode": "competition",
            "contest_type": rules.contest_type.value, "report": str(out),
            "flags": flag_stats, "verify": verify_stats,
        })
        infra.outcome = "completed"
        return EngagementResult(run_summary=run_summary, verify_stats=verify_stats,
                                report_path=str(out), flag_stats=flag_stats)
    except asyncio.CancelledError:
        infra.outcome = "cancelled"
        raise
    finally:
        try:
            if adapter is not None:
                # 会话结束回收全部活跃容器（名额交还平台；失败只兜底不抛出）
                with contextlib.suppress(Exception):
                    await adapter.close_all(reason="会话结束")
                with contextlib.suppress(Exception):
                    await adapter.aclose()
            await infra.close()
        finally:
            _foreground_finished()


def run_engagement_sync(**kwargs: Any) -> EngagementResult:
    """同步包装（CLI/调用方便利）。"""
    return asyncio.run(run_engagement(**kwargs))
