"""运行时编排：把各模块组装成一次可运行的授权任务。

run_engagement() 统一编排 SRC 链路，独立核验结果写入单条漏洞记录。
run_ctf_engagement() / run_competition_engagement() 赛事实例化装配：
    Flag 统一管理器 + 模式装配器（战术映射/元规划覆盖注入主 Agent）。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spiritscout.agents.master.agent import MasterAgent, MasterContext
from spiritscout.agents.sub.base import SubAgentContext
from spiritscout.blackboard import Blackboard
from spiritscout.config import SystemConfig
from spiritscout.config.settings import ModelEntry
from spiritscout.core.background import (
    _background_slot,
    _foreground_finished,
    _foreground_started,
    workspace_foreground_active,
)
from spiritscout.core.exceptions import (
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
from spiritscout.ctf.writeup import build_digest
from spiritscout.gateway import (
    AgentBoundGateway,
)
from spiritscout.infrastructure import _Infra, _teardown_infra
from spiritscout.infrastructure import build_infra as _assemble_infra
from spiritscout.knowledge.distillation import (
    _background_distill,
    _distill_best_effort,
    _distill_digest,
    _distill_experience,
    _distill_queue_dir,
    _distill_run,
    _persist_distill_snapshot,
    _prepare_distill,
    _schedule_background_distill,
    _update_distill_snapshot,
    _worth_distilling,
    _write_distill_snapshot,
    pending_distill_snapshots,
    resume_pending_distills,
)
from spiritscout.knowledge.experience import (
    ExperienceStore,
    format_insights,
)
from spiritscout.modes import ModeAssembler

if TYPE_CHECKING:
    from spiritscout.competition import CompetitionRules


# 保留旧运行时入口；后台调度状态仅由 core.background 持有。
__all__ = [
    "_Infra",
    "_teardown_infra",
    "_foreground_started",
    "_foreground_finished",
    "workspace_foreground_active",
    "_background_slot",
    "_distill_digest",
    "_worth_distilling",
    "_distill_experience",
    "_prepare_distill",
    "_distill_run",
    "_distill_best_effort",
    "_distill_queue_dir",
    "_write_distill_snapshot",
    "_update_distill_snapshot",
    "_persist_distill_snapshot",
    "pending_distill_snapshots",
    "_schedule_background_distill",
    "_background_distill",
    "resume_pending_distills",
    "EngagementResult",
    "run_engagement",
    "run_ctf_engagement",
    "run_competition_engagement",
    "run_engagement_sync",
    "run_agent_side_session",
]

# 宽限期内完成 MCP 握手的工具先注册；其余连接在后台完成后补入。
_MCP_STARTUP_GRACE = 10.0

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
    return await _assemble_infra(
        cfg, models, authorization, agent_models, approval_gate, ring,
        question_broker, pause_gate, mcp_startup_grace=_MCP_STARTUP_GRACE)


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


def _recall_experience(infra: _Infra, experience: ExperienceStore, query: str) -> list[dict[str, Any]]:
    """开局只读核对来源版本；来源丢失、更新或过期不删除经验，但不自动注入。"""
    if getattr(getattr(infra, "authorization", None), "mode", None) == RunMode.SRC:
        return []
    import sqlite3

    workspace = Path(infra.cfg.workspace.root).resolve()
    checked: dict[tuple[str, str], bool] = {}
    def valid(source: dict[str, Any]) -> bool:
        try:
            session = source.get('source_session', '')
            ref = str(source.get('ref', ''))
            layer, _, identifier = ref.partition(':')
            key, _, version = identifier.rpartition('@')
            if not session or Path(session).name != session or not key or not version.isdigit():
                return False
            identity = (session, ref)
            if identity in checked:
                return checked[identity]
            checked[identity] = False
            # 每次只持有一个外部只读连接，失效候选再多也不耗尽文件描述符。
            with contextlib.ExitStack() as stack:
                if session == workspace.name:
                    bb = infra.blackboard
                else:
                    if not workspace.name.startswith('sess-') or not session.startswith('sess-'):
                        return False
                    path = workspace.parent / session / 'bb.db'
                    if not path.is_file():
                        return False
                    bb = Blackboard(path, bus=None, readonly=True)
                    stack.callback(bb.close)
                if bb is None:
                    return False
                row = bb.read_versioned(BlackboardLayer(layer), key)
                checked[identity] = bool(row and row[1] == int(version) and row[0].get('evidence_state') not in {'conflicting', 'expired'})
                return checked[identity]
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return False
    return experience.recall(query, limit=infra.cfg.experience.recall_limit, source_validator=valid)


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
    on_user_feed: Any = None,
    followup_goal: bool = False,
    experience: Any = None,
    platform: Any = None,
    platform_facts: list[dict[str, Any]] | None = None,
    asset_strategy: str = "balanced",
) -> dict[str, Any]:
    """装配主 Agent 并运行多轮闭环，返回 run_summary。"""
    if (infra.cfg.gateway.lightweight_local and getattr(assembly, "single_executor", False)
            and infra.authorization.mode in (RunMode.CTF, RunMode.AWDP)):
        from spiritscout.ctf.single import run_single

        return await run_single(infra, assembly, mission=mission, standing_rules=standing_rules,
            dialogue_history=dialogue_history, user_inbox=user_inbox, on_user_feed=on_user_feed,
            followup_goal=followup_goal, platform=platform)
    from spiritscout.tools.journal import read_recovery_view

    recovery = read_recovery_view(Path(infra.cfg.blackboard.sqlite_path))
    recovery_context = (infra.tools.journal.planning_context(recovery)
                        if infra.tools.journal is not None else {})
    from spiritscout.gateway.world_state import sync_user_context

    # 接收时已落盘的原话按消息 ID 幂等恢复；任务更新也不能覆盖并发答复。
    sync_user_context(infra.blackboard, dialogue_history=dialogue_history,
                      mission=None if followup_goal else mission, standing_rules=standing_rules)
    # 每次运行只选一次相关、有来源的观察，其他会话新增经验不改写当前前缀。
    recall = (_recall_experience(infra, experience, ' '.join((mission, target, str(getattr(assembly, 'notes', '') or ''))))
              if experience and infra.authorization.mode != RunMode.SRC else [])
    master_extra: dict[str, Any] = {
        "run_mode": infra.authorization.mode.value,
        "mode_notes": assembly.notes, "mission": mission,
        "mode_prompt": getattr(assembly, "strategy_prompt", ""),
        "competition_context": getattr(assembly, "competition_context", {}),
        "execution_recovery": recovery_context,
        "secretary_enabled": infra.cfg.secretary.enabled,
        "secretary_gateway": AgentBoundGateway(infra.gateway, infra.agent_models.get("secretary", "")),
        "standing_rules": standing_rules,
        "recent_experience": format_insights(recall),
        "flag_manager": getattr(assembly, "flag_manager", None),
        "user_inbox": user_inbox if user_inbox is not None else [],
        "dialogue_history": dialogue_history,
        "on_user_feed": on_user_feed,
        "followup_goal": followup_goal,
        "lightweight_local": infra.cfg.gateway.lightweight_local,
        "asset_strategy": asset_strategy,
    }
    if platform is not None:
        # 平台适配器只在灵枢（主 Agent）侧：调度期开实例、完成期回收，
        # 事实流进复盘视图；子 Agent 不接触平台凭据与接口
        master_extra["platform"] = platform
        master_extra["platform_facts"] = (
            platform_facts if platform_facts is not None else [])
    async def lookup_history(arguments: dict[str, Any]) -> dict[str, Any]:
        result = await infra.tools.call("history_lookup", arguments, **infra.builtin_ctx)
        return {"ok": result.ok, "result": result.stdout or result.failure_reason(),
                "invocation_id": result.invocation_id}

    master = MasterAgent(MasterContext(
        bus=infra.bus, blackboard=infra.blackboard, convergence=infra.convergence,
        gateway=AgentBoundGateway(
            infra.gateway, infra.agent_models.get("master", ""), prefer_session=True),
        policy=infra.policy, agents=assembly.agents,
        audit=infra.audit, enable_llm_planning=enable_llm_planning,
        tactic_map=assembly.tactic_map, plan_override=assembly.plan_provider,
        extra=master_extra,
        knowledge=infra.knowledge,   # 主 Agent 攻击路径模板接线（3.3.6）
        ring=infra.ring,             # 灵枢督导通道（与子 Agent 共持同一台账）
        pause_gate=infra.pause_gate,  # 暂停门共持（图节点边界挂起）
        questions=infra.questions,   # 复盘向用户提问通道（ask_user 后端）
        journal=infra.tools.journal,
        history_lookup=lookup_history if infra.authorization.mode == RunMode.SRC else None,
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
    from spiritscout.agents.scan_control import ScannerController

    scanner = None
    if (enable_llm_planning and infra.cfg.scanner.enabled and not infra.cfg.gateway.lightweight_local
            and infra.authorization.mode in (RunMode.SRC, RunMode.CTF, RunMode.COMPETITION)):
        scanner = ScannerController(infra, assembly, mission=mission,
            standing_rules=standing_rules, dialogue_history=dialogue_history)
        master.ctx.extra['scan_controller'] = scanner
        infra.builtin_ctx['_scan_control'] = scanner
    run_summary: dict[str, Any] = {}
    try:
        run_summary = await master.run(authorization=infra.authorization, target=target)
        # 先停止发现分支，再排空复核队列，避免 drain 之后又提交新候选。
        if scanner is not None:
            await scanner.finish_discovery()
        await verifier.drain()
        run_summary["verification"] = verifier.stats()
    except asyncio.CancelledError:
        # 用户主动停止：提炼转后台（最长约一分钟），不干等——立即退场
        _schedule_background_distill(infra, assembly, experience, enable_llm_planning,
                                     run_summary or None, mission=mission,
                                     outcome="cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 — 后台提炼不阻塞异常上抛
        _schedule_background_distill(infra, assembly, experience, enable_llm_planning,
                                     run_summary or None, mission=mission,
                                     outcome="failed",
                                     error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if scanner is not None:
            await scanner.close()
            infra.builtin_ctx.pop('_scan_control', None)
        await verifier.close()
    run_summary["recovery_mode"] = recovery_context.get("mode", "history_replan")
    if infra.tools.journal is not None:
        run_summary["execution_run_id"] = infra.tools.journal.run_id
    run_summary["model_usage"] = infra.gateway.usage_report()

    # 灵枢收尾提炼经验：从全场战报中提炼可复用洞察（非步骤），存入经验库。
    # 完成态提炼移到调用方与报告生成并行（两笔模型调用互不依赖，串行磨
    # 是收尾等待的大头）；取消/失败路径无报告可搭，仍在原地提炼
    # 保持本次运行的召回快照；新的经验在下一次运行重新筛选。
    master.ctx.extra["recent_experience"] = format_insights(recall)
    run_summary["mode_notes"] = assembly.notes
    if mission:
        run_summary["mission"] = mission
    return run_summary


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
    solved_ids = {str(cid) for cid in platform.get("solved_ids") or []}
    # 平台明确给出空完成列表时，不能把多 Flag 题的部分提交算成通关。
    completed_ids = solved_ids if "solved_ids" in platform else accepted_ids
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
            f"- 平台完成：{len(completed_ids)} / "
            f"{platform.get('total_challenges', platform.get('listed', '未知'))}",
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
        status = "平台确认完成" if cid in completed_ids else "未确认完成"
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
    """各 CTF 入口共用持久写作队列；无模型时仅保存明确标识的材料。"""
    from spiritscout.ctf.report_jobs import enqueue, status

    digest = build_digest(challenges=challenges, flags=flag_manager.all(), run_summary=run_summary,
                          blackboard=infra.blackboard, journal=getattr(getattr(infra, 'tools', None), 'journal', None), workspace=infra.workspace)
    if getattr(flag_manager, 'local_only', False):
        from spiritscout.web.events import read_activity_page

        digest['local_answer_completion'] = flag_manager.completions
        digest['chronology'] = [{"type": row['event']['type'],
                                 "turn": row['event'].get('detail', {}).get('turn'),
                                 "text": str(row['event'].get('detail', {}).get('text')
                                             or row['event'].get('detail', {}).get('note') or '')[:700]}
                                for row in read_activity_page(Path(infra.cfg.audit_log_path), limit=300)['items']
                                if row['event']['type'] in {'agent.progress', 'agent.note', 'agent.tool_result'}]
    path = Path(report_path)
    task = enqueue(infra.workspace, path, digest,
                   AgentBoundGateway(infra.gateway, infra.agent_models.get("master", ""), prefer_session=True)
                   if enable_llm else None,
                   infra.audit, enabled=enable_llm)
    if not enable_llm and task is not None:
        await task
    run_summary['report_job'] = {**status(infra.workspace), 'mode': 'background' if enable_llm else 'local'}
    return path


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
    on_user_feed: Any = None,
    followup_goal: bool = False,
    asset_strategy: str = "balanced",
) -> EngagementResult:
    """SRC 模式任务入口：侦察 → 探测 → 全链路核验。

    mission：用户自然语言任务意图，原样注入主 Agent 规划提示词，
    由主 Agent 自行决策下发子任务；授权边界仍在装配层确定性提取执行。
    dialogue_history：会话对话史（续传接着聊的上文），直达灵枢对话通道。
    report_path：旧调用方兼容参数，单条报告在漏洞页读取。
    """
    if authorization.mode != RunMode.SRC:
        raise SpiritScoutError("run_engagement 仅用于 SRC 模式（CTF/渗透赛请用专用入口）")
    infra = await _build_infra(cfg, models, authorization,
                               agent_models=agent_models,
                               approval_gate=approval_gate, ring=ring,
                               question_broker=question_broker,
                               pause_gate=pause_gate)
    try:
        _foreground_started(infra.workspace)
        assembly = _make_assembler(infra, enable_llm_planning).assemble(RunMode.SRC)
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning,
            mission=mission, user_inbox=user_inbox,
            standing_rules=standing_rules,
            dialogue_history=dialogue_history,
            on_user_feed=on_user_feed, followup_goal=followup_goal,
            experience=infra.experience, asset_strategy=asset_strategy)

        # 独立复测已在候选提交时运行，不能再用模板成功覆盖用户规则裁决。
        verify_stats = run_summary.get("verification", {})

        await _distill_best_effort(
            infra, assembly, infra.experience, enable_llm_planning,
            run_summary, mission=mission, outcome="completed")
        infra.audit.log("system.lifecycle", "runtime", {
            "action": "engagement_complete", "mode": "src",
            "report": None, "verify": verify_stats,
            "rounds": run_summary["rounds_executed"],
        })
        infra.outcome = "completed"
        return EngagementResult(run_summary=run_summary, verify_stats=verify_stats,
                                report_path=None)
    except asyncio.CancelledError:
        infra.outcome = "cancelled"
        raise
    finally:
        try:
            await infra.close()
        finally:
            _foreground_finished(infra.workspace)


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
    mission: str = "",
    reopen_challenges: list[str] | None = None,
    dialogue_history: list[dict[str, str]] | None = None,
    on_user_feed: Any = None,
    followup_goal: bool = False,
    platform_profile: dict[str, Any] | None = None,
) -> EngagementResult:
    """CTF/AWDP 入口：普通模式五方向专项，合格轻量模式直接单执行者。

        CTF 模式所有工具操作自动审批并留痕；专项能力由角色工具目录提供。
        flag_submit_fn=None 时只提取不提交（提交接口由使用者按赛方规范接入）。
        resume=True（续传）：Flag 库照常重载，已破题的赛题不再重解，
        未破题的与用户意见进入新一轮决策环。
        platform_profile：平台档案（托管题/跑分平台）——列题/开容器/完成回收/
        提交全部经适配器执行，题目源以平台清单为准；不提供时行为与原来一致。
        """
    if authorization.mode not in (RunMode.CTF, RunMode.AWDP):
        raise SpiritScoutError("run_ctf_engagement 要求授权模式为 CTF/AWDP")
    infra = await _build_infra(cfg, models, authorization,
                               agent_models=agent_models,
                               approval_gate=approval_gate, ring=ring,
                               question_broker=question_broker,
                               pause_gate=pause_gate)
    adapter: Any = None
    try:
        _foreground_started(infra.workspace)
        challenges = [dict(ch) for ch in challenges]
        type_hints = copy.deepcopy(challenges[0].get("user_type_hints")) if len(challenges) == 1 else None
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
                adapter = PlatformAdapter(
                    profile, audit=infra.audit,
                    progress_path=infra.workspace / "ctf_progress.json")
                infra.audit.log("platform.fact", "runtime", {
                    "action": "profile_loaded", "profile": profile.redacted()})
                await adapter.precheck()
                listed = await adapter.list_challenges()
                if listed:
                    # 平台列题即题目源：URL/文本推导的赛题不参与平台模式
                    challenges = listed
                    if len(challenges) == 1 and type_hints:
                        challenges[0]["user_type_hints"] = type_hints
                else:
                    infra.audit.log("platform.fact", "runtime", {
                        "action": "list_empty",
                        "detail": ("平台未列出题目，沿用任务提供的赛题清单"
                                   if challenges else "平台未列出题目，任务未提供赛题")})
        local_answers = (adapter is None and flag_submit_fn is None and
                         (len(challenges) == 1 or (infra.cfg.gateway.lightweight_local and bool(challenges))))
        flag_manager = FlagManager(
            local_only=local_answers or (infra.cfg.gateway.lightweight_local and authorization.mode == RunMode.AWDP),
            multiple_answers=(local_answers and challenges[0].get("multiple_answers") is True
                              and authorization.mode != RunMode.AWDP),
            fix_mode=authorization.mode == RunMode.AWDP,
            flag_patterns=flag_patterns,
            submit_fn=flag_submit_fn or (adapter.submit if adapter is not None else None),
            audit=infra.audit, bus=infra.bus,
            store_path=infra.workspace / "flags.json",
        )
        for challenge_id in reopen_challenges or []:
            if challenge_id not in {str(ch.get("id") or "") for ch in challenges}:
                raise SpiritScoutError("重新确认的赛题不属于当前会话")
            flag_manager.reopen(challenge_id, mission)
        assembly = _make_assembler(infra, enable_llm_planning).assemble(
            authorization.mode, challenges=challenges, flag_manager=flag_manager,
            resume=resume and not followup_goal, platform=adapter)
        target = next(
            (str(c["url"]) for c in challenges if c.get("url")),
            "ctf://challenges",
        )
        run_summary = await _run_master(
            infra, assembly, target, enable_llm_planning=enable_llm_planning,
            mission=mission,
            user_inbox=user_inbox,
            standing_rules=standing_rules,
            dialogue_history=dialogue_history,
            on_user_feed=on_user_feed, followup_goal=followup_goal,
            experience=infra.experience,
            platform=adapter,
            platform_facts=adapter.facts if adapter is not None else None)

        submit_stats = await flag_manager.submit_all() if not local_answers else {"mode": "local_answers"}
        run_summary["flag_submit"] = submit_stats
        flag_stats = flag_manager.stats()
        if adapter is not None:
            run_summary["platform"] = adapter.summary()
        report_path = report_path or _default_report_path(infra.workspace, "ctf_writeup")
        if local_answers:
            run_summary["answers_complete"] = (bool(challenges) and all(
                str(ch.get("id") or "") in flag_manager.completed_ids() for ch in challenges)
                if getattr(assembly, "single_executor", False) else bool(flag_manager.completed_ids()))
        out = await _render_writeup(infra, challenges, flag_manager, run_summary, report_path,
            contest="ctf", enable_llm=enable_llm_planning, fallback_context={"flag_stats": flag_stats})
        outcome = "interrupted" if local_answers and not run_summary.get("answers_complete", False) else "completed"
        distill_task = (None if getattr(assembly, "single_executor", False) else _schedule_background_distill(
            infra, assembly, infra.experience, enable_llm_planning,
            run_summary, mission="", outcome=outcome))
        run_summary["experience_distill"] = {
            "status": "queued" if distill_task is not None else "skipped",
            "mode": "background",
        }
        infra.audit.log("system.lifecycle", "runtime", {
            "action": "engagement_complete", "mode": "ctf", "report": str(out), "outcome": outcome,
            "flags": flag_stats, "rounds": run_summary["rounds_executed"],
        })
        infra.outcome = outcome
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
            _foreground_finished(infra.workspace)


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
    on_user_feed: Any = None,
    followup_goal: bool = False,
    platform_profile: dict[str, Any] | None = None,
    mission: str = "",
    preserve_instances_on_return: bool = False,
    generate_wp: bool = False,
) -> EngagementResult:
    """智能渗透赛任务入口：赛制自适应调度。

        用户选择智能体模式后，模型按原始规则和平台反馈动态规划；不按赛制
        标签排序、裁剪角色或选择固定流程，结束时产出轻量结果摘要。

        platform_profile：平台档案（托管题/跑分平台）——列题/开容器/完成回收/
        提交全部经适配器执行（与 CTF 模式同一套语义）；不提供时行为与原来一致。
        generate_wp：默认仅生成赛况摘要；开启后在解题结束时复用普通 CTF 的 WP 与技术证据收集。
        preserve_instances_on_return：外层负责整场生命周期时，正常分段返回保留实例；
        异常和取消仍清理。调用方必须在最终退出时回收实例。
        """
    from spiritscout.competition import CompetitionEngine

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
        _foreground_started(infra.workspace)
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
                adapter = PlatformAdapter(
                    profile, audit=infra.audit,
                    progress_path=infra.workspace / "competition_progress.json",
                    continuous=True)
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

        # 保留赛方列表与分值事实，排题策略由模型按实际规则裁决。
        ordered = list(challenges)

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
            on_user_feed=on_user_feed, followup_goal=followup_goal,
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
            # 已判定正确的 Flag → 标记赛题完成，驱动调度器资源转移
            for cand in flag_manager.accepted_flags():
                engine.mark_solved([], cand.challenge_id)   # 审计留痕（卡片态在调度循环内维护）
            flag_stats = flag_manager.stats()
            if adapter is not None:
                run_summary["platform"] = adapter.summary()

            report_path = report_path or _default_report_path(
                infra.workspace, "competition_writeup" if generate_wp else "competition_summary")
            if generate_wp:
                out = await _render_writeup(
                    infra, ordered, flag_manager, run_summary, report_path,
                    contest="ctf", enable_llm=enable_llm_planning,
                    fallback_context={"contest_type": rules.contest_type.value,
                                      "flag_stats": flag_stats,
                                      "submit_enabled": flag_manager.submit_fn is not None,
                                      "notes": assembly.notes,
                                      "current_task_ids": [task_id for node in run_summary.get("dag_nodes", [])
                                                           for task_id in node.get("task_ids", [])]})
            else:
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
                # 外层续跑可保留现场；最终停止、异常与普通入口仍执行清理。
                if not preserve_instances_on_return or infra.outcome != "completed":
                    with contextlib.suppress(Exception):
                        await adapter.close_all(reason="会话结束")
                with contextlib.suppress(Exception):
                    await adapter.aclose()
            await infra.close()
        finally:
            _foreground_finished(infra.workspace)


def run_engagement_sync(**kwargs: Any) -> EngagementResult:
    """同步包装（CLI/调用方便利）。"""
    return asyncio.run(run_engagement(**kwargs))


async def run_agent_side_session(*, cfg: SystemConfig, models: list[ModelEntry],
                                 authorization: Authorization, agent_key: str,
                                 mission: str, context: dict[str, Any],
                                 agent_models: dict[str, str], approval_gate: Any,
                                 question_broker: Any, ring: Any, pause_gate: Any,
                                 dialogue_history: list[dict[str, str]],
                                 standing_rules: str = "",
                                 on_reply: Any = None) -> EngagementResult:
    """独立执行选定子 Agent；沿用普通模式的工具审批与会话隔离。"""
    from spiritscout.agents.profiles import get_profile
    from spiritscout.core.models import AgentType, Task, TaskStatus
    from spiritscout.gateway.world_state import sync_user_context
    from spiritscout.tools.invocation import ExecutionScope, current_execution_scope

    if authorization.mode not in {RunMode.SRC, RunMode.CTF}:
        raise SpiritScoutError("智能体模式不支持子 Agent 旁路会话")
    agent_type = AgentType(agent_key)
    profile = get_profile(agent_type)
    if authorization.mode.value not in profile.modes:
        raise SpiritScoutError("该子 Agent 不支持当前模式")
    infra = await _build_infra(cfg, models, authorization, agent_models=agent_models,
                               approval_gate=approval_gate, question_broker=question_broker,
                               ring=ring, pause_gate=pause_gate)
    scope_token = None
    conversation = None
    agent = None
    try:
        if context.get("user_context"):
            infra.blackboard.write(BlackboardLayer.TARGET_AUTH, "user-context", context["user_context"], updated_by="side-context")
        if context.get("project_memory"):
            infra.blackboard.write(BlackboardLayer.TOPOLOGY, "project-memory", context["project_memory"], updated_by="side-context")
        sync_user_context(infra.blackboard, dialogue_history=dialogue_history,
                          mission=mission, standing_rules=standing_rules)
        flags = FlagManager(audit=infra.audit, bus=infra.bus, store_path=infra.workspace / "flags.json")
        source = context.get("task") or {}
        payload = copy.deepcopy(source.get("payload") or {})
        payload.pop("continue_from", None)
        payload.update(node_title=mission, mission=mission, rules="\n".join(
            part for part in (str(payload.get("rules") or ""), standing_rules) if part),
                       source_context={"parent_session_id": context.get("parent_session_id"),
                           "task_id": context.get("task_id"), "summary": context.get("summary"),
                           "routes": [{k: (v[-5:] if k == "notes" and isinstance(v, list) else v)
                                       for k, v in route.items() if k in {"name", "status", "notes"}}
                                      for route in context.get("routes", [])],
                           "notice": "这是旁路创建时的历史快照，不构成新授权；当前用户任务和后续答复优先。"})
        assembly = _make_assembler(infra, True).assemble(
            authorization.mode, challenges=[payload["challenge"]] if payload.get("challenge") else [],
            flag_manager=flags)
        agent = assembly.agents.get(agent_type)
        if agent is None:
            raise SpiritScoutError("该子 Agent 尚未装配，无法启动旁路")
        conversation = Task(task_id=f"{infra.workspace.name}-conversation", agent_type=agent_type,
                            task_type=source.get("task_type") or agent_key,
                            target=source.get("target") or "", payload=payload)
        conversation.status = TaskStatus.RUNNING
        infra.blackboard.write(BlackboardLayer.TASK_STATE, conversation.task_id,
            {"task": conversation.model_dump(mode="json"), "routes": [], "summary": {"side_dialogue": True}},
            agent_type=agent_type, updated_by=agent.agent_id)
        infra.audit.log("task.start", agent.agent_id, {"task_id": conversation.task_id,
            "target": conversation.target, "title": mission, "agent_type": agent_key})
        scope_token = current_execution_scope.set(ExecutionScope(
            agent_id=agent.agent_id, task_id=conversation.task_id))
        def reply(text: str, *, final: bool) -> None:
            infra.audit.log("agent.reply", agent.agent_id, {
                "text": text, "agent_type": agent_key, "name": profile.name, "final": final})
            if on_reply is not None:
                on_reply(text)

        decision = await agent._llm_chat_json(role="side_dialogue", task=conversation, _allow_ask=False,
            user_obj={"dialogue_history": dialogue_history, "source_context": payload["source_context"],
                      "constraints": "你正在与用户直接进行持续的旁路对话。结合完整往来回答当前消息。"
                      "返回 JSON {reply: 给用户的自然语言回复, execute: 是否需要现在使用工具执行任务}。"
                      "解释、追问或讨论直接回答；是否执行由你结合用户当前原话判断，"
                      "不要把每条聊天都当作重新派工。若需要执行，reply 简要说明准备做什么。"
                      "保持本角色身份，不自称灵枢；继承的记录是历史参考，当前授权和答复优先。"})
        if (not isinstance(decision, dict) or not isinstance(decision.get("reply"), str)
                or not decision["reply"].strip() or type(decision.get("execute")) is not bool):
            raise SpiritScoutError("子 Agent 未返回有效对话回复，请重试")
        reply(decision["reply"], final=not decision["execute"])
        summary: dict[str, Any] = {"conversation_only": True}
        if decision["execute"]:
            task = conversation.model_copy(update={"task_id": new_id("task"), "status": TaskStatus.PENDING}, deep=True)
            summary = await agent.run(task)
            evidence = infra.blackboard.read(BlackboardLayer.TASK_STATE, task.task_id) or {}
            answer = await agent._llm_chat_json(role="side_reply", task=conversation, _allow_ask=False,
                user_obj={"dialogue_history": dialogue_history, "execution": evidence or summary,
                          "constraints": "根据本次真实执行记录直接回复用户，返回 JSON {reply: 回复正文}。"
                          "说明已核实的结论、证据和仍未解决之处；不能把工具退出成功当作目标完成。"
                          "使用自然语言，保持子 Agent 身份，不输出内部调度 JSON。"})
            if not isinstance(answer, dict) or not str(answer.get("reply") or "").strip():
                raise SpiritScoutError("执行记录已保存，但子 Agent 未返回有效总结回复")
            reply(str(answer["reply"]), final=True)
        conversation.status = TaskStatus.DONE
        infra.blackboard.write(BlackboardLayer.TASK_STATE, conversation.task_id,
            {"task": conversation.model_dump(mode="json"), "routes": [], "summary": {"side_dialogue": True}},
            agent_type=agent_type, updated_by=agent.agent_id)
        infra.audit.log("task.done", agent.agent_id, {"task_id": conversation.task_id})
        infra.outcome = "completed"
        return EngagementResult(run_summary={"side_agent": agent_key, "task": summary,
                                             "model_usage": infra.gateway.usage_report()},
                                verify_stats={}, report_path=None, flag_stats=flags.stats())
    except asyncio.CancelledError:
        infra.outcome = "cancelled"
        raise
    finally:
        try:
            if conversation is not None and agent is not None and conversation.status == TaskStatus.RUNNING:
                conversation.status = TaskStatus.CANCELLED if infra.outcome == "cancelled" else TaskStatus.FAILED
                infra.blackboard.write(BlackboardLayer.TASK_STATE, conversation.task_id,
                    {"task": conversation.model_dump(mode="json"), "routes": []},
                    agent_type=agent_type, updated_by=agent.agent_id)
                infra.audit.log(f"task.{conversation.status.value}", agent.agent_id, {"task_id": conversation.task_id})
        finally:
            if scope_token is not None:
                current_execution_scope.reset(scope_token)
            await infra.close()
