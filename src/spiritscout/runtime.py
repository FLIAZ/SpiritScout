"""运行时编排：把各模块组装成一次可运行的授权任务。

run_engagement() 统一编排 SRC 链路，收敛后先跑核验管道再生成报告。
run_ctf_engagement() / run_competition_engagement() 赛事实例化装配：
    Flag 统一管理器 + 模式装配器（战术映射/元规划覆盖注入主 Agent）。
"""

from __future__ import annotations

import asyncio
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
    CollabGateway,
    ModelGateway,
    ModelRegistry,
)
from spiritscout.knowledge import KnowledgeStore
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
from spiritscout.tools.sandbox import sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec
from spiritscout.verify import VerifyPipeline

if TYPE_CHECKING:
    from spiritscout.competition import CompetitionRules


def _mcp_embedded_scope_targets(params: dict[str, Any]) -> list[str]:
    """从 MCP 复合入参中提取网络目标。

    Yakit ``http_fuzzer`` 的主目标位于 ``request`` 原始报文的
    Host 头，而不是独立 ``url`` 字段。这里同时校验 absolute-form
    请求行与 Host，避免二者不一致时绕过授权边界。
    """
    scheme = "https" if bool(params.get("isHttps")) else "http"
    targets: list[str] = []

    def add(value: Any) -> None:
        raw = str(value or "").strip()
        if not raw:
            return
        if raw.startswith(("http://", "https://")):
            target = raw
        elif any(ch.isspace() for ch in raw) or "/" in raw:
            return
        else:
            target = f"{scheme}://{raw}/"
        if target not in targets:
            targets.append(target)

    add(params.get("actualAddr"))
    batch = params.get("batchTarget")
    if isinstance(batch, (list, tuple, set)):
        for item in batch:
            add(item)

    request = params.get("request")
    if not isinstance(request, str) or not request.strip():
        return targets
    # 只解析首个 HTTP 头块；不读请求体，避免把体内文本误当目标。
    header = request[:65536].replace("\r\n", "\n").split("\n\n", 1)[0]
    lines = header.splitlines()
    request_target = ""
    if lines:
        first = lines[0].strip().split()
        if len(first) >= 2:
            request_target = first[1]
            if request_target.startswith(("http://", "https://")):
                add(request_target)
    hosts = [line.split(":", 1)[1].strip() for line in lines[1:]
             if line.lower().startswith("host:")]
    path = request_target if request_target.startswith("/") else "/"
    for host in hosts:
        add(f"{scheme}://{host}{path}")
    return targets


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
    mcp_tools: dict[str, dict[str, Any]] = field(
        default_factory=dict)   # mcp__<server>__<tool> → {client, server, tool, timeout}
    questions: Any = None       # QuestionBroker（ask_user 工具后端；None=不可用）

    async def close(self) -> None:
        await self.bus.stop()
        self.blackboard.close()
        self.knowledge.close()
        for client in self.mcp_clients:
            try:
                await client.close()
            except Exception:  # noqa: S110, BLE001 —— 关停回收失败不阻断收尾
                pass


async def _build_infra(
    cfg: SystemConfig,
    models: list[ModelEntry],
    authorization: Authorization,
    agent_models: dict[str, str] | None = None,
    approval_gate: Callable[[str, str], Awaitable[bool]] | None = None,
    ring: Any = None,
    question_broker: Any = None,
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
    platform_rule = knowledge.platform_rule_view(authorization.src_platform)
    policy = PolicyEngine(authorization.mode, guard,
                          platform_rule=platform_rule, audit=audit,
                          approval_gate=approval_gate)
    if authorization.mode == RunMode.SRC:
        policy.check_automation_allowed()   # 平台禁自动化时启动即阻断
    limiter = RateLimiter(max(authorization.scope.max_requests_per_second, 0.1))

    bus = create_bus(
        cfg.bus.backend,
        queue_maxsize=cfg.bus.queue_maxsize,
        rate_limit_per_second=cfg.bus.rate_limit_per_second,
        redis_url=cfg.bus.redis_url,
        nats_url=cfg.bus.nats_url,
    )
    await bus.start()
    blackboard = Blackboard(cfg.blackboard.sqlite_path, bus=bus)
    from spiritscout.core.models import AgentType

    blackboard.write(
        BlackboardLayer.TARGET_AUTH, authorization.auth_id,
        authorization.model_dump(mode="json"),
        agent_type=AgentType.MASTER, updated_by="runtime",
    )
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
    registry = ToolRegistry(executor, policy=policy, audit=audit)
    workspace = Path(cfg.workspace.root)
    workspace.mkdir(parents=True, exist_ok=True)
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
    builtin_ctx = {
        "guard": guard,
        "limiter": limiter,
        # 附件与沙箱产物目录都在附件分析工具的允许清单内——脚本产物
        # 可以直接接续 file_analyze/analyze_attachments
        "allowed_dirs": [str(attachments), str(sandbox_dir)],
        "workspace_root": str(workspace),
    }
    # 外部 MCP 服务器：握手 + 工具清单并入智能体工具目录（失败留痕跳过，
    # 不阻断开工——MCP 是增益能力而非主链依赖；多台服务器并发握手，
    # 慢服务器不拖累开工时序）
    mcp_clients: list[Any] = []
    mcp_tools: dict[str, dict[str, Any]] = {}
    enabled_specs = [s for s in cfg.mcp_servers if s.enabled]
    if enabled_specs:
        from spiritscout.tools.mcp_client import build_client

        async def _start_mcp(spec: Any) -> tuple[Any, Any, list[dict[str, Any]], str]:
            try:
                client = build_client(spec)
                return spec, client, await client.start(), ""
            except Exception as exc:  # noqa: BLE001 —— 不可达：留痕后继续
                return spec, None, [], str(exc)[:300]

        for spec, client, tools, err in await asyncio.gather(
                *(_start_mcp(s) for s in enabled_specs)):
            if client is None:
                audit.log("system.lifecycle", "runtime", {
                    "action": "mcp_server_failed", "server": spec.name,
                    "error": err})
                continue
            mcp_clients.append(client)
            for t in tools:
                exposed_name = f"mcp__{spec.name}__{t['name']}"
                annotations = t.get("annotations") or {}
                destructive = annotations.get(
                    "destructiveHint", annotations.get("destructive_hint"))
                read_only = annotations.get(
                    "readOnlyHint", annotations.get("read_only_hint"))
                if destructive is True:
                    risk = RiskLevel.DESTRUCTIVE
                elif read_only is True:
                    risk = RiskLevel.READONLY
                else:
                    risk = RiskLevel(spec.default_risk)
                properties = (t.get("inputSchema") or {}).get("properties") or {}
                target_params = (() if spec.scope_enforcement == "none" else
                                 tuple(p for p in spec.target_params if p in properties))
                embedded_extractor = (
                    _mcp_embedded_scope_targets
                    if spec.scope_enforcement != "none"
                    and {"request", "actualAddr", "batchTarget"} & set(properties)
                    else None)
                tool_spec = ToolSpec(
                    name=exposed_name, kind=ToolKind.MCP,
                    description=t.get("description") or t["name"], risk=risk,
                    timeout_seconds=max(1, int(spec.timeout)),
                    allowed_params=set(properties), target_params=target_params,
                    scope_required=spec.scope_enforcement == "required",
                    scope_target_extractor=embedded_extractor)

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
                                          error=str(exc)[:300])
                    return ToolResult(
                        tool=_name, ok=not result.get("is_error"),
                        returncode=1 if result.get("is_error") else 0,
                        stdout=str(result.get("text", ""))[:16000])

                registry.register_builtin(exposed_name, _call_mcp, tool_spec)
                mcp_tools[exposed_name] = {
                    "client": client, "server": spec.name,
                    "tool": t["name"], "timeout": float(spec.timeout),
                    "schema": t, "risk": risk.value,
                    "target_params": list(target_params),
                    "scope_enforcement": spec.scope_enforcement}
            audit.log("system.lifecycle", "runtime", {
                "action": "mcp_server_ready", "server": spec.name,
                "transport": spec.transport, "tools": [t["name"] for t in tools]})
    for name, fn in (("http_probe", http_probe), ("dns_resolve", dns_resolve),
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
        mcp_clients=mcp_clients, mcp_tools=mcp_tools,
        questions=question_broker,
    )


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
    experience: Any = None,
) -> dict[str, Any]:
    """装配主 Agent 并运行多轮闭环，返回 run_summary。"""
    master = MasterAgent(MasterContext(
        bus=infra.bus, blackboard=infra.blackboard, convergence=infra.convergence,
        gateway=AgentBoundGateway(
            infra.gateway, infra.agent_models.get("master", "")),
        policy=infra.policy, agents=assembly.agents,
        max_rounds=infra.cfg.iteration_max_rounds,
        audit=infra.audit, enable_llm_planning=enable_llm_planning,
        tactic_map=assembly.tactic_map, plan_override=assembly.plan_provider,
        extra={"mode_notes": assembly.notes, "mission": mission,
               "standing_rules": standing_rules,
               "recent_experience": [
                   e["insight"] for e in (experience.recent(limit=5) if experience else [])],
               "flag_manager": getattr(assembly, "flag_manager", None),
               "user_inbox": user_inbox if user_inbox is not None else []},
        knowledge=infra.knowledge,   # 主 Agent 攻击路径模板接线（3.3.6）
        ring=infra.ring,             # 灵枢督导通道（与子 Agent 共持同一台账）
    ))
    run_summary = await master.run(authorization=infra.authorization, target=target)
    run_summary["model_usage"] = infra.gateway.usage_report()

    # 灵枢收尾提炼经验：从全场审计与结论中提炼解题思维（非步骤），存入经验库
    if enable_llm_planning and experience:
        try:
            await _extract_experience(
                infra.gateway, experience, run_summary, infra.audit)
        except (SpiritScoutError, OSError, ValueError, TypeError):
            pass

    # 刷新灵枢 extra 中的经验（新提炼的经验立即可用于下一轮规划）
    recent_exp = experience.recent(limit=5) if experience else []
    master.ctx.extra["recent_experience"] = [e["insight"] for e in recent_exp]
    run_summary["mode_notes"] = assembly.notes
    if mission:
        run_summary["mission"] = mission
    return run_summary


def _write_report_file(path: Path, body: str, *, contest: str) -> Path:
    header = (f"# 灵枢 SpiritScout CTF WriteUp\n\n> 生成时间："
              f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} ｜ 赛制：{contest}\n\n")
    path.write_text(header + body.strip() + "\n", encoding="utf-8")
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


async def _extract_experience(gateway, experience, run_summary, audit):
    """灵枢收尾提炼经验：从全场数据中提炼可复用的解题思维（非步骤）。

    不是解题步骤（模型已经会做），而是「这类场景下我学到了什么」——
    平台特性、绕过手法的适用条件、容易踩的坑。存入全局经验库供
    后续会话检索。
    """
    import json as _json

    from spiritscout.gateway.backends import extract_json

    flags = run_summary.get("flag_stats", {})
    digest = {
        "flags": flags,
        "rounds": run_summary.get("rounds_executed", 0),
        "target": str(run_summary.get("target", "")),
    }
    resp = await gateway.chat(
        [{"role": "system", "content": (
            "你是灵枢。一场安全测试刚结束。从以下数据中提炼出 3~5 条"
            "可复用的解题思维——不是解题步骤（模型已经会做），而是"
            "「这类场景下我学到了什么」：平台特性、绕过手法的适用条件、"
            "容易踩的坑、判读信号。每条一句话，独立可复用。"
            "没有值得提炼的就返回空数组。"
            '返回 JSON：{"insights": [{"insight": str, "context": str, '
            '"tags": [str]}]}')},
         {"role": "user", "content": _json.dumps(digest, ensure_ascii=False)[:6000]}],
        task_type="meta_planning", response_format="json",
        max_tokens=1024, reasoning_effort="low",
        timeout_seconds=60)
    data = extract_json(resp.content) or {}
    for item in (data.get("insights") or [])[:5]:
        if isinstance(item, dict) and item.get("insight"):
            experience.add(
                item["insight"][:600],
                context=item.get("context", "")[:300],
                tags=item.get("tags", []),
                source_session=str(run_summary.get("target", "")),
                source_model="master")


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
) -> EngagementResult:
    """SRC 模式任务入口：侦察 → 探测 → 全链路核验 → 报告。

    mission：用户自然语言任务意图，原样注入主 Agent 规划提示词，
    由主 Agent 自行决策下发子任务；授权边界仍在装配层确定性提取执行。
    """
    if authorization.mode != RunMode.SRC:
        raise SpiritScoutError("run_engagement 仅用于 SRC 模式（CTF/渗透赛请用专用入口）")
    infra = await _build_infra(cfg, models, authorization,
                               agent_models=agent_models,
                               approval_gate=approval_gate, ring=ring,
                               question_broker=question_broker)
    try:
        assembly = _make_assembler(infra, enable_llm_planning).assemble(RunMode.SRC)
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning,
            mission=mission, user_inbox=user_inbox,
            standing_rules=standing_rules,
            experience=infra.experience)

        # ---- 全链路核验（4.3）：仅 ACCEPTED 漏洞入报告收录池
        # 接入知识库（误报特征/P0~P4 分级/PoC 模板/verify_method 联动）
        # 高危/严重收录走多模型投票复核（4.9.3-2；分歧降级人工复核）
        pipeline = VerifyPipeline(
            infra.blackboard, infra.tools, infra.policy,
            builtin_ctx=infra.builtin_ctx, audit=infra.audit,
            knowledge=infra.knowledge,
            collab=CollabGateway(infra.gateway),
        )
        verify_stats = await pipeline.run()

        # ---- 报告生成（4.6）
        report_path = report_path or _default_report_path(infra.workspace, "src_report")
        report = ReportAgent(infra.blackboard)
        out = report.generate(infra.authorization, target, report_path,
                              run_summary=run_summary)
        infra.audit.log("system.lifecycle", "runtime", {
            "action": "engagement_complete", "mode": "src",
            "report": str(out), "verify": verify_stats,
            "rounds": run_summary["rounds_executed"],
        })
        return EngagementResult(run_summary=run_summary, verify_stats=verify_stats,
                                report_path=str(out))
    finally:
        await infra.close()


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
    resume: bool = False,
) -> EngagementResult:
    """CTF 专项任务入口（4.4/5.2）：五方向专项 Agent + Flag 统一管理。

    硬约束 C7：CTF 模式不启用端口扫描（PolicyEngine 模式禁用表强制，
    即使 LLM 规划出扫描动作也会在工具门被拒）。
    flag_submit_fn=None 时只提取不提交（提交接口由使用者按赛方规范接入）。
    resume=True（续传）：Flag 库照常重载，已破题的赛题不再重解，
    未破题的与用户意见进入新一轮决策环。
    """
    if authorization.mode != RunMode.CTF:
        raise SpiritScoutError("run_ctf_engagement 要求授权模式为 CTF")
    infra = await _build_infra(cfg, models, authorization,
                               agent_models=agent_models,
                               approval_gate=approval_gate, ring=ring,
                               question_broker=question_broker)
    try:
        flag_manager = FlagManager(
            flag_patterns=flag_patterns, submit_fn=flag_submit_fn,
            audit=infra.audit, bus=infra.bus,
            store_path=infra.workspace / "flags.json",
        )
        assembly = _make_assembler(infra, enable_llm_planning).assemble(
            RunMode.CTF, challenges=challenges, flag_manager=flag_manager,
            resume=resume)
        target = next(
            (str(c["url"]) for c in challenges if c.get("url")),
            "ctf://challenges",
        )
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning,
            user_inbox=user_inbox,
            standing_rules=standing_rules,
            experience=infra.experience)

        # ---- Flag 统一提交（4.4.6：格式校验/去重/频率控制/退避重试）
        submit_stats = await flag_manager.submit_all()
        run_summary["flag_submit"] = submit_stats
        flag_stats = flag_manager.stats()

        report_path = report_path or _default_report_path(infra.workspace, "ctf_writeup")
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
        infra.audit.log("system.lifecycle", "runtime", {
            "action": "engagement_complete", "mode": "ctf", "report": str(out),
            "flags": flag_stats, "rounds": run_summary["rounds_executed"],
        })
        return EngagementResult(run_summary=run_summary, verify_stats={},
                                report_path=str(out), flag_stats=flag_stats)
    finally:
        await infra.close()


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
) -> EngagementResult:
    """智能渗透赛任务入口（4.5/5.3）：赛制自适应调度。

    - Jeopardy：6.7 优先级公式排序后按 CTF 池分发；
    - AWD：攻击线允许端口扫描（仅限赛方授权 CIDR，已并入 scope）；
    - 靶场：SRC 链路 + 全链路核验，产出渗透报告（5.3.4）。
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
                               question_broker=question_broker)
    try:
        # 赛方禁止行为按工具名登记为动态禁用（4.5.1 规则冲突之外的运行时边界）
        for behavior in rules.forbidden_behaviors:
            infra.policy.forbid_tool(behavior)
        engine = CompetitionEngine(
            rules, audit=infra.audit, bus=infra.bus,
            total_duration_seconds=total_duration_seconds,
        )
        flag_manager = FlagManager(
            flag_patterns=rules.flag_patterns or None,
            submit_fn=flag_submit_fn, audit=infra.audit, bus=infra.bus,
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
            flag_manager=flag_manager, competition_engine=engine)
        target = next(
            (str(c["url"]) for c in ordered if c.get("url")),
            authorization.target_description or "competition://contest",
        )
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning,
            user_inbox=user_inbox,
            standing_rules=standing_rules,
            experience=infra.experience)

        # 靶场/AWD 攻击线产出漏洞 → 全链路核验（4.3， 接入知识库）
        verify_stats: dict[str, int] = {}
        if rules.contest_type != ContestType.JEOPARDY:
            pipeline = VerifyPipeline(
                infra.blackboard, infra.tools, infra.policy,
                builtin_ctx=infra.builtin_ctx, audit=infra.audit,
                knowledge=infra.knowledge,
                collab=CollabGateway(infra.gateway),
            )
            verify_stats = await pipeline.run()

        submit_stats = await flag_manager.submit_all()
        run_summary["flag_submit"] = submit_stats
        # 已判定正确的 Flag → 标记赛题完成，驱动调度器资源转移（4.5.2）
        for cand in flag_manager.accepted_flags():
            engine.mark_solved([], cand.challenge_id)   # 审计留痕（卡片态在调度循环内维护）
        flag_stats = flag_manager.stats()

        report = ReportAgent(infra.blackboard)
        if rules.contest_type == ContestType.RANGE:
            # 靶场赛交付物为渗透报告（5.3.4）
            report_path = report_path or _default_report_path(
                infra.workspace, "range_report")
            out = report.generate(infra.authorization, target, report_path,
                                  run_summary=run_summary)
        else:
            report_path = report_path or _default_report_path(
                infra.workspace, "competition_writeup")
            out = await _render_writeup(
                infra, ordered, flag_manager, run_summary, report_path,
                contest=rules.contest_type.value, enable_llm=enable_llm_planning,
                fallback_context={
                    "contest_type": rules.contest_type.value,
                    "flag_stats": flag_stats,
                    "submit_enabled": flag_manager.submit_fn is not None,
                    "notes": assembly.notes,
                    "current_task_ids": [
                        task_id for node in run_summary.get("dag_nodes", [])
                        for task_id in node.get("task_ids", [])]},
            )
        infra.audit.log("system.lifecycle", "runtime", {
            "action": "engagement_complete", "mode": "competition",
            "contest_type": rules.contest_type.value, "report": str(out),
            "flags": flag_stats, "verify": verify_stats,
        })
        return EngagementResult(run_summary=run_summary, verify_stats=verify_stats,
                                report_path=str(out), flag_stats=flag_stats)
    finally:
        await infra.close()


def run_engagement_sync(**kwargs: Any) -> EngagementResult:
    """同步包装（CLI/调用方便利）。"""
    return asyncio.run(run_engagement(**kwargs))
