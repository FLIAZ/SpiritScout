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
)
from spiritscout.tools.executor import (
    DockerExecutor,
    LocalExecutor,
    ToolExecutor,
    docker_available,
)
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_exec
from spiritscout.verify import VerifyPipeline

if TYPE_CHECKING:
    from spiritscout.competition import CompetitionRules


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

    async def close(self) -> None:
        await self.bus.stop()
        self.blackboard.close()
        self.knowledge.close()


async def _build_infra(
    cfg: SystemConfig,
    models: list[ModelEntry],
    authorization: Authorization,
    agent_models: dict[str, str] | None = None,
    approval_gate: Callable[[str, str], Awaitable[bool]] | None = None,
    ring: Any = None,
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
    builtin_ctx = {
        "guard": guard,
        "limiter": limiter,
        "allowed_dirs": [str(attachments)],
        "workspace_root": str(workspace),
    }
    for name, fn in (("http_probe", http_probe), ("dns_resolve", dns_resolve),
                     ("file_analyze", file_analyze), ("sandbox_exec", sandbox_exec)):
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
               "flag_manager": getattr(assembly, "flag_manager", None)},
        knowledge=infra.knowledge,   # 主 Agent 攻击路径模板接线（3.3.6）
        ring=infra.ring,             # 灵枢督导通道（与子 Agent 共持同一台账）
    ))
    run_summary = await master.run(authorization=infra.authorization, target=target)
    run_summary["model_usage"] = infra.gateway.usage_report()
    run_summary["mode_notes"] = assembly.notes
    if mission:
        run_summary["mission"] = mission
    return run_summary


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
) -> EngagementResult:
    """SRC 模式任务入口：侦察 → 探测 → 全链路核验 → 报告。

    mission：用户自然语言任务意图，原样注入主 Agent 规划提示词，
    由主 Agent 自行决策下发子任务；授权边界仍在装配层确定性提取执行。
    """
    if authorization.mode != RunMode.SRC:
        raise SpiritScoutError("run_engagement 仅用于 SRC 模式（CTF/渗透赛请用专用入口）")
    infra = await _build_infra(cfg, models, authorization,
                               agent_models=agent_models,
                               approval_gate=approval_gate, ring=ring)
    try:
        assembly = _make_assembler(infra, enable_llm_planning).assemble(RunMode.SRC)
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning,
            mission=mission)

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
) -> EngagementResult:
    """CTF 专项任务入口（4.4/5.2）：五方向专项 Agent + Flag 统一管理。

    硬约束 C7：CTF 模式不启用端口扫描（PolicyEngine 模式禁用表强制，
    即使 LLM 规划出扫描动作也会在工具门被拒）。
    flag_submit_fn=None 时只提取不提交（提交接口由使用者按赛方规范接入）。
    """
    if authorization.mode != RunMode.CTF:
        raise SpiritScoutError("run_ctf_engagement 要求授权模式为 CTF")
    infra = await _build_infra(cfg, models, authorization,
                               agent_models=agent_models,
                               approval_gate=approval_gate, ring=ring)
    try:
        flag_manager = FlagManager(
            flag_patterns=flag_patterns, submit_fn=flag_submit_fn,
            audit=infra.audit, bus=infra.bus,
            store_path=infra.workspace / "flags.json",
        )
        assembly = _make_assembler(infra, enable_llm_planning).assemble(
            RunMode.CTF, challenges=challenges, flag_manager=flag_manager)
        target = next(
            (str(c["url"]) for c in challenges if c.get("url")),
            "ctf://challenges",
        )
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning)

        # ---- Flag 统一提交（4.4.6：格式校验/去重/频率控制/退避重试）
        submit_stats = await flag_manager.submit_all()
        run_summary["flag_submit"] = submit_stats
        flag_stats = flag_manager.stats()

        report_path = report_path or _default_report_path(infra.workspace, "ctf_writeup")
        report = ReportAgent(infra.blackboard)
        out = report.generate_writeup(
            challenges, flag_manager.all(), report_path,
            context={"contest_type": "ctf", "flag_stats": flag_stats,
                     "submit_enabled": flag_manager.submit_fn is not None,
                     "notes": assembly.notes},
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
                               approval_gate=approval_gate, ring=ring)
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
            infra, assembly, target, enable_llm_planning=enable_llm_planning)

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
            out = report.generate_writeup(
                ordered, flag_manager.all(), report_path,
                context={"contest_type": rules.contest_type.value,
                         "flag_stats": flag_stats,
                         "submit_enabled": flag_manager.submit_fn is not None,
                         "notes": assembly.notes},
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
