"""主 Agent 的 LangGraph 状态机：规划、派发、汇总和复盘。"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import operator
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.profiles import agent_system_prompt, get_profile
from spiritscout.agents.sub.base import SubAgent
from spiritscout.blackboard import ConvergenceEngine
from spiritscout.blackboard.store import Blackboard
from spiritscout.bus.base import EventBus
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import ScopeViolation, SpiritScoutError
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    AgentType,
    Authorization,
    BlackboardLayer,
    Event,
    Task,
    TaskStatus,
)
from spiritscout.gateway import ModelGateway, chat_decision
from spiritscout.gateway.backends import extract_json
from spiritscout.gateway.context import (
    compact_json,
    decision_memory,
    decision_messages,
    shared_context,
)
from spiritscout.policy.engine import PolicyEngine
from spiritscout.tools.search_policy import search_allowed

# 战术 → 子 Agent 映射（3.3.6 子Agent↔战术映射；MVP 装配 recon/web_vuln）
TACTIC_AGENT_MAP: dict[str, tuple[AgentType, str]] = {
    "BUSINESS-CONTEXT": (AgentType.BUSINESS_CONTEXT, "business_context"),
    "TA0043": (AgentType.RECON, "recon"),          # 侦察
    "TA0001": (AgentType.WEB_VULN, "web_probe"),   # 初始访问（T1190 Web 打点）
    "TA0007": (AgentType.PORT_SCAN, "port_scan"),  # 发现（端口与服务测绘）
    "HOST-VULN": (AgentType.HOST_VULN, "host_probe"),  # 主机与服务审计
    "TA0006": (AgentType.POST_EXPLOIT, "post_exploit"),  # 凭据线索与横向测绘
}


class MasterState(TypedDict):
    target: str
    round_no: int
    dag: dict[str, Any]
    tasks_dispatched: Annotated[list[dict], operator.add]
    round_views: Annotated[list[dict], operator.add]
    continue_iteration: bool
    stop_reason: str
    plan_source: str


@dataclass
class MasterContext:
    bus: EventBus
    blackboard: Blackboard
    convergence: ConvergenceEngine
    gateway: ModelGateway
    policy: PolicyEngine
    agents: dict[AgentType, SubAgent]
    default_routes: int = 3
    audit: AuditLogger | None = None
    enable_llm_planning: bool = True
    # 模式装配器注入：CTF/渗透赛覆盖战术映射与规划来源
    tactic_map: dict[str, tuple[AgentType, str]] | None = None
    plan_override: Any = None   # Callable[[MasterState], dict] 返回 {"dag_nodes": [...]}
    extra: dict[str, Any] = field(default_factory=dict)
    knowledge: Any = None       # KnowledgeStore（攻击路径模板接线，None=不接）
    ring: Any = None            # RingLedger 环形台账（与子 Agent 共持；None=不督导）
    pause_gate: Any = None      # 暂停门（与子 Agent 共持；None=无暂停能力）
    questions: Any = None       # QuestionBroker（复盘向用户提问；None=不可用）
    journal: Any = None         # 执行台账与加密检查点

    def __post_init__(self) -> None:
        if self.audit is None:
            self.audit = NullAuditLogger()


class MasterAgent:
    """主 Agent（元规划决策层）。"""

    # 常规规划使用最高档；竞赛首轮只装配顺序待发队列，走低强度以尽快开工。
    # 子 Agent 的动态裁定见 _assign_dispatch_plan。模型未声明该档位时按端点
    # 默认档执行（网关门禁忽略未声明值，不猜不代填）
    _MASTER_EFFORT = "max"

    def __init__(self, ctx: MasterContext):
        self.ctx = ctx
        self.agent_id = new_id("master")
        self._graph = self._build_graph()

    # ------------------------------------------------------------ 图构建

    def _build_graph(self):
        graph = StateGraph(MasterState)
        graph.add_node("meta_plan", self._checkpoint_node("meta_plan", self._meta_plan))
        graph.add_node("dispatch", self._checkpoint_node("dispatch", self._dispatch))
        graph.add_node("aggregate", self._checkpoint_node("aggregate", self._aggregate))
        graph.add_node("review", self._checkpoint_node("review", self._review))
        graph.set_entry_point("meta_plan")
        graph.add_edge("meta_plan", "dispatch")
        graph.add_edge("dispatch", "aggregate")
        graph.add_edge("aggregate", "review")
        # 单段以 review 收束：继续/收手由外层分段循环携带状态决策——
        # 框架递归深度只覆盖一段（4 个节点），不构成长期迭代的上限
        graph.add_edge("review", END)
        return graph.compile()

    def _checkpoint_node(self, name: str, handler: Any) -> Any:
        async def execute(state: MasterState) -> dict[str, Any]:
            journal = getattr(self.ctx, "journal", None)
            if journal is not None:
                journal.checkpoint("master", "master", f"before_{name}", dict(state))
            update = await handler(state)
            if journal is not None:
                merged = {**state, **update}
                # 与图中的 operator.add reducer 保持一致，避免遗漏本轮任务/视图。
                for key in ("tasks_dispatched", "round_views"):
                    merged[key] = list(state.get(key, [])) + list(update.get(key, []))
                journal.checkpoint("master", "master", f"after_{name}", merged)
            return update

        return execute

    async def run(self, authorization: Authorization, target: str) -> dict[str, Any]:
        """启动多轮闭环迭代，直至模型复盘判定收敛或用户停止。

        分段执行：每段一轮（规划→派发→汇总→复盘），段间状态由外层循环
        携带——单段步数固定，框架递归上限不约束业务收敛；是否继续只由
        复盘决策与用户停止决定，不设固定回合顶。
        """
        assert self.ctx.audit is not None
        self.ctx.audit.log("decision.change", self.agent_id, {
            "action": "engagement_start", "target": target, "mode": authorization.mode.value})
        # 授权写入黑板目标与授权层
        self.ctx.blackboard.write(
            BlackboardLayer.TARGET_AUTH, authorization.auth_id,
            authorization.model_dump(mode="json"),
            agent_type=AgentType.MASTER, updated_by=self.agent_id,
            dedup_key=f"auth|{authorization.auth_id}",
        )
        state: MasterState = {
            "target": target,
            "round_no": 0,
            "dag": TaskDAG().to_dict(),
            "tasks_dispatched": [],
            "round_views": [],
            "continue_iteration": True,
            "stop_reason": "",
            "plan_source": "",
        }
        # 用户直达通道：与派发/督导隔离，整场运行期间并行消费用户消息
        # （开工时间戳供对话通道回答「跑了多久」类问题，不依赖主循环）
        self.ctx.extra.setdefault("engagement_started_at", time.time())
        chat_task = (asyncio.create_task(self._user_chat_watch())
                     if self.ctx.enable_llm_planning else None)
        total_dispatched = 0
        try:
            while True:
                final = await self._graph.ainvoke(state)
                total_dispatched += len(final["tasks_dispatched"])
                if not final["continue_iteration"]:
                    break
                # 下一段携带全部累积态势；本轮派发记录不带入（路由摘要按轮取用）
                state = {**final, "tasks_dispatched": []}
        finally:
            if chat_task is not None:
                chat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await chat_task
        final_dag = TaskDAG.from_dict(final["dag"])
        summary = {
            "target": target,
            "rounds_executed": final["round_no"],
            "stop_reason": final["stop_reason"],
            "dag_stats": final_dag.stats(),
            "dag_nodes": self._dag_snapshot(final_dag),
            "round_views": final["round_views"],
            "tasks_dispatched": total_dispatched,
        }
        self.ctx.audit.log("decision.change", self.agent_id,
                           {"action": "engagement_end", **summary})
        return summary

    # ------------------------------------------------------------ 节点：元规划

    async def _gate_wait(self) -> None:
        """暂停门检查点：图节点边界挂起（在途节点跑完才停）。"""
        if self.ctx.pause_gate is not None:
            await self.ctx.pause_gate.wait()

    async def _meta_plan(self, state: MasterState, *,
                         dispatch_only: bool = False,
                         live_dag: TaskDAG | None = None,
                         blocked_challenge_ids: set[str] | None = None,
                         on_dispatch: Callable[[dict[str, Any], Task], None] | None = None,
                         ) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx.audit is not None
        await self._gate_wait()
        dag = live_dag if live_dag is not None else TaskDAG.from_dict(state["dag"])
        round_no = state["round_no"]
        plan_source = "llm"

        if not dispatch_only and (round_no == 0 or self._needs_replan(dag, state)):
            if ctx.plan_override is not None:
                plan = ctx.plan_override(state)
                plan_source = "mode_assembly"
            else:
                plan = await self._llm_plan(state) if ctx.enable_llm_planning else None
                if plan is None:
                    if ctx.extra.get("mode_prompt"):
                        plan = {"dag_nodes": [], "analysis": "模型规划不可用，不套用预设赛制流程"}
                        plan_source = "unavailable"
                    else:
                        plan = self._rule_plan(state)
                        plan_source = "rule"
            plan["dag_nodes"] = self._ensure_business_gate(
                plan.get("dag_nodes", []))
            rejected_reason = ""
            try:
                result = dag.upsert_from_plan(
                    self._resolve_plan_challenges(plan.get("dag_nodes", [])), round_no)
            except SpiritScoutError as exc:
                # 计划不可落图（未知依赖/成环）：明确落痕后回退规则计划，
                # 不让一次坏规划卡死整轮
                ctx.audit.log("decision.change", self.agent_id, {
                    "action": "plan_rejected", "round": round_no,
                    "source": plan_source, "reason": str(exc)[:200]})
                rejected_reason = str(exc)
                self._record_plan_result(round_no, [], [], rejected=str(exc))
                if plan_source != "llm":
                    raise
                if ctx.extra.get("mode_prompt"):
                    plan = {"dag_nodes": [], "analysis": "规划被拒绝，等待模型修正，不套用赛制模板"}
                    plan_source = "unavailable"
                else:
                    plan = self._rule_plan(state)
                    plan_source = "rule"
                plan["dag_nodes"] = self._ensure_business_gate(
                    plan.get("dag_nodes", []))
                result = dag.upsert_from_plan(plan.get("dag_nodes", []), round_no)
            await self._apply_competition_decisions(dag, plan)
            self._record_plan_result(round_no, result.created, result.skipped,
                                     rejected=rejected_reason)
            ctx.audit.log("decision.change", self.agent_id, {
                "action": "meta_plan", "round": round_no, "source": plan_source,
                "new_nodes": [n.node_id for n in result.created],
                "skipped_nodes": self._skip_digest(result.skipped),
                "analysis": plan.get("analysis", "")[:500]})
        else:
            ctx.audit.log("decision.change", self.agent_id,
                          {"action": "meta_plan", "round": round_no, "source": "continue"})

        # 就绪节点 → 任务包（任务依赖：前置条件满足才下发，4.1.2）
        tactic_map = ctx.tactic_map or TACTIC_AGENT_MAP
        platform = ctx.extra.get("platform")
        continuous = platform is not None and getattr(platform, "continuous", False)
        task_objs: list[tuple[Task, str]] = []
        deferred_ids = {cid for cid, item in self._challenge_states(dag).items()
                        if item["deferred"]}
        for node in dag.ready_nodes():
            # 重攻节点战术名带 @轮次 后缀（同题换角度再攻），按基名解析归属
            mapping = (tactic_map.get(node.tactic)
                       or tactic_map.get(node.tactic.split("@", 1)[0]))
            if mapping is None:
                # 未映射战术（MVP 未装配对应子 Agent）：标记跳过，避免死锁
                dag.mark(node.node_id, TaskStatus.CANCELLED)
                node.meta["skip_reason"] = "no_agent_mapped_in_mvp"
                continue
            agent_type, task_type = mapping
            if agent_type not in ctx.agents:
                dag.mark(node.node_id, TaskStatus.CANCELLED)
                node.meta["skip_reason"] = "agent_not_assembled"
                continue
            # 赛题绑定：规划器节点自带；复盘新增节点按 id 匹配/单题沿用，
            # 保证 CTF 任务永远带着赛题载荷下发（载荷为空的节点无题可解）
            challenge = node.meta.get("challenge") or self._bind_challenge(dag, node)
            if str((challenge or {}).get("id") or "") in (blocked_challenge_ids or set()):
                continue
            if str((challenge or {}).get("id") or "") in deferred_ids:
                node.meta["deferred"] = True
                node.meta["cancel_reason"] = "赛题仍暂缓，需模型通过 retry_challenges 解除"
                dag.mark(node.node_id, TaskStatus.CANCELLED)
                continue
            if challenge and platform is not None:
                if (getattr(platform, "continuous", False)
                        and platform.is_completed(str(challenge.get("id") or ""))):
                    dag.mark(node.node_id, TaskStatus.CANCELLED)
                    node.meta["cancel_reason"] = "平台已确认整题完成"
                    continue
                cid = str(challenge.get("id") or "")
                if (continuous and platform.owns(cid)
                        and not platform.can_start_instance(cid)):
                    continue
                # 供给中也占据节点，避免慢启动期间的回收误关新实例。
                dag.mark(node.node_id, TaskStatus.DISPATCHED)
                self._write_dag_snapshot(dag, round_no)
                try:
                    provisioned = (await platform.ensure_instance(challenge)
                                   if platform.owns(str(challenge.get("id") or ""))
                                   else challenge)
                except Exception as exc:  # noqa: BLE001 —— 平台故障不连坐其他节点
                    ctx.audit.log("platform.fact", self.agent_id, {
                        "action": "ensure_error",
                        "challenge": str(challenge.get("id") or ""),
                        "error": str(exc)[:200]})
                    provisioned = None
                finally:
                    if node.status == TaskStatus.DISPATCHED:
                        dag.mark(node.node_id, TaskStatus.PENDING)
                if provisioned is None:
                    # 活跃名额已满 / 平台暂未接受：本轮不派发，节点保持待发，
                    # 下轮补位（名额靠完成回收与平台自身节奏释放）
                    continue
                if node.status == TaskStatus.CANCELLED:
                    continue
                challenge = provisioned
                self._approve_platform_target(challenge)
            if challenge:
                node.meta["challenge"] = challenge
            task = Task(
                task_id=new_id("task"), task_type=task_type,
                target=self._task_target(node, challenge, str(state["target"])),
                agent_type=agent_type, round_no=round_no,
                parent_dag_node=node.node_id,
                payload={"tactic": node.tactic, "technique": node.technique or "",
                         "node_title": node.title,
                         "mission": self._mission(),
                         "execution_recovery": self.ctx.extra.get("execution_recovery", {}),
                         "rules": self._rules(),
                         "platform_handoff": self._platform_handoff(platform, challenge),
                         "continue_from": copy.deepcopy(node.meta.get("continue_from") or {}),
                         "recent_experience": (self.ctx.extra.get("recent_experience") or [])[-3:],
                         # 题级事实深拷贝注入：同一赛题的多个任务各持副本，
                         # 一方的运行期改写不污染其他方向/其他轮次
                         "challenge": copy.deepcopy(challenge) if challenge else None},
            )
            dag.attach_task(node.node_id, task.task_id)
            dag.mark(node.node_id, TaskStatus.RUNNING)
            task_objs.append((task, node.node_id))
            if on_dispatch is not None:
                on_dispatch({"task": task.model_dump(mode="json"), "node_id": node.node_id}, task)
            # 首个任务先交执行环开工；后续供给在连续调度器中逐个启动、即时派发。
            if continuous and not dispatch_only:
                break

        # 思考强度/路线预算的下发决策不再挡在开工路径上（见 _dispatch：
        # 与子 Agent 执行并行裁定，落定后即时生效）
        tasks = [{"task": t.model_dump(mode="json"), "node_id": nid}
                 for t, nid in task_objs]

        self._write_dag_snapshot(dag, round_no)
        return {"dag": dag.to_dict(), "tasks_dispatched": tasks,
                "plan_source": plan_source}

    @staticmethod
    def _platform_handoff(platform: Any, challenge: dict[str, Any] | None) -> dict[str, Any]:
        """把主控已经完成的操作回执交给执行者，不要求其重跑全队流程。"""
        if platform is None or not challenge:
            return {}
        cid = str(challenge.get("id") or "")
        if not platform.owns(cid):
            return {}
        facts = getattr(platform, "facts", [])
        receipts = copy.deepcopy(getattr(platform, "operation_receipts", {}))
        for action in ("precheck", "list_refreshed"):
            fact = next((f for f in reversed(facts) if f.get("action") == action), None)
            if fact:
                receipts[action] = {k: fact[k] for k in
                                    ("action", "ts", "ok", "status", "count") if k in fact}
        return {"coordinator": "master", "challenge_id": cid,
                "entry_url": challenge.get("url", ""),
                "completed_operations": receipts,
                "instance_provisioning": copy.deepcopy(challenge.get("instance_provisioning") or {})}

    @staticmethod
    def _task_target(node: Any, challenge: dict[str, Any] | None,
                     fallback: str) -> str:
        """任务目标：赛题 URL > 规划节点指定的 target > 会话目标。

        多域 SRC 由规划器按域拆节点并各填 target；裸主机补 https://，
        否则 http_probe 会把主机名当未知协议。
        """
        raw = str((challenge or {}).get("url")
                  or (getattr(node, "meta", None) or {}).get("target")
                  or fallback or "").strip()
        if (raw and "://" not in raw and "/" not in raw
                and not raw.startswith("*")):
            raw = "https://" + raw
        return raw

    @staticmethod
    def _bind_challenge(dag: TaskDAG, node: Any) -> dict[str, Any]:
        """复盘新增节点的赛题绑定：按 challenge_id 匹配既有节点携带的赛题；
        单题会话（唯一赛题）直接沿用——CTF 语境下「哪道题」不能靠猜。"""
        want = str((node.meta or {}).get("challenge_id") or "")
        found: dict[str, dict[str, Any]] = {}
        for other in dag.all_nodes():
            ch = (other.meta or {}).get("challenge") or {}
            cid = str(ch.get("id") or "")
            if cid and cid not in found:
                found[cid] = ch
        if want and want in found:
            return found[want]
        if len(found) == 1:
            return next(iter(found.values()))
        return {}

    def _approve_platform_target(self, challenge: dict[str, Any]) -> None:
        """平台下发的容器地址并入会话范围（平台供给的实例即本题授权对象）。

        只登记批准事实（去掉运行期的「范围之外」标注），不匹配排除列表；
        规则冲突由模型结合用户答复判断，不合法目标静默保持范围外标注。
        """
        url = str(challenge.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return
        guard = getattr(self.ctx.policy, "guard", None)
        if guard is None or guard.is_allowed(url):
            return
        try:
            entry = guard.approve_target(url)
        except (ScopeViolation, ValueError):
            return
        self.ctx.audit.log("scope.extended", self.agent_id, {
            "target": entry, "challenge": str(challenge.get("id") or ""),
            "source": "platform"})

    async def _platform_release(self) -> None:
        """平台完成态回收：每轮任务收口后关闭已完成赛题的容器（释放名额）。

        平台声明了按题完成态时以平台通报为准；未声明时以「首个被平台判对
        的 Flag」为完成信号（该判定在适配器内完成）。回收失败只留痕——
        不连坐解题主链路，容器由会话结束时的一次性回收兜底。
        """
        extra = getattr(self.ctx, "extra", None) or {}
        platform = extra.get("platform")
        if platform is None:
            return
        accepted: set[str] = set()
        flag_manager = extra.get("flag_manager")
        if flag_manager is not None:
            try:
                accepted = {str(f.challenge_id)
                            for f in flag_manager.accepted_flags()}
            except Exception:  # noqa: BLE001 —— 统计失败不阻断回收
                accepted = set()
        try:
            released = await platform.release_completed(accepted)
        except Exception as exc:  # noqa: BLE001 —— 回收失败不连坐主链路
            self.ctx.audit.log("platform.fact", self.agent_id, {
                "action": "release_failed", "error": str(exc)[:200]})
            return
        if released:
            self.ctx.audit.log("platform.fact", self.agent_id, {
                "action": "instances_released", "challenges": list(released)})

    def _challenge_states(self, dag: TaskDAG) -> dict[str, dict[str, Any]]:
        """复盘视图：按赛题聚合节点状态与模型已登记的暂缓原因。"""
        states: dict[str, dict[str, Any]] = {}
        for node in dag.all_nodes():
            meta = node.meta or {}
            challenge = meta.get("challenge") or {}
            cid = str(challenge.get("id") or "")
            if not cid:
                continue
            item = states.setdefault(cid, {
                "id": cid,
                "title": str(challenge.get("title") or cid)[:120],
                "deferred": bool(meta.get("deferred")),
                "defer_reason": str(meta.get("defer_reason") or "")[:200],
                "nodes": [],
            })
            item["deferred"] = item["deferred"] or bool(meta.get("deferred"))
            if meta.get("defer_reason") and not item["defer_reason"]:
                item["defer_reason"] = str(meta.get("defer_reason"))[:200]
            item["nodes"].append({
                "tactic": node.tactic, "status": node.status.value,
                "title": node.title[:100], "node_id": node.node_id,
                "task_ids": list(node.task_ids),
                "cancel_reason": meta.get("cancel_reason", ""),
                "skip_reason": meta.get("skip_reason", "")})
        return states

    def _competition_status(self, dag: TaskDAG) -> dict[str, Any]:
        """完整保留短小的调度事实；流式观察可裁剪，完成态和取消原因不可裁剪。"""
        platform = self.ctx.extra.get("platform")
        if platform is None or not getattr(platform, "continuous", False):
            return {}
        progress = platform.progress()
        challenges = self._challenge_states(dag)
        for item in challenges.values():
            nodes = item["nodes"]
            counts: dict[str, int] = {}
            latest = {}
            active = []
            for node in nodes:
                counts[node["status"]] = counts.get(node["status"], 0) + 1
                if node["status"] in {"pending", "dispatched", "running"}:
                    active.append(node)
                else:
                    latest[node["tactic"].split("@", 1)[0]] = node
            item.update(attempt_count=len(nodes), status_counts=counts,
                        nodes=[*latest.values(), *active],
                        history_note="历史按战术汇总，展示各战术最新回执与全部在途节点；完整历史在 DAG/任务记录")
            bb = getattr(self.ctx, "blackboard", None)
            if bb is not None:
                for node in item["nodes"]:
                    refs = []
                    for tid in node["task_ids"]:
                        task_state = bb.read(BlackboardLayer.TASK_STATE, tid) or {}
                        refs.extend({"task_id": tid, "route_id": r["route_id"], "name": r.get("name", "")}
                                    for r in task_state.get("routes", []) if r.get("route_id"))
                    node["conversation_refs"] = refs
        return {
            "solved_ids": list(progress.get("solved_ids") or []),
            "unsolved_ids": list(progress.get("unsolved_ids") or []),
            "challenges": challenges,
            "active_instance_ids": sorted(platform.active_instance_ids()),
            "retained_instance_ids": sorted({
                str((n.meta.get("challenge") or {}).get("id") or "")
                for n in dag.all_nodes() if n.meta.get("retain_instance") is True
                and not n.meta.get("deferred")}),
            "spending": self._spending_observation(progress),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _spending_observation(self, progress: dict[str, Any]) -> dict[str, Any]:
        """把得分未变化期间的实际消耗交模型权衡，不设置回合顶或预算熔断。"""
        budget = getattr(getattr(self.ctx, "gateway", None), "budget", None)
        calls = getattr(budget, "call_log", None)
        if not isinstance(calls, list):
            return {"available": False}
        manager = self.ctx.extra.get("flag_manager")
        accepted = manager.stats().get("accepted") if manager is not None else None
        signature = (tuple(sorted(progress.get("solved_ids") or [])), accepted)
        observation = self.ctx.extra.get("_score_cost_observation")
        if observation is None or observation["signature"] != signature:
            observation = {"signature": signature, "since": time.time(), "call_offset": len(calls)}
            self.ctx.extra["_score_cost_observation"] = observation
        recent = calls[observation["call_offset"]:]
        priced = [c for c in recent if c.get("estimated_cost_cny_min") is not None]
        return {"available": True, "observed_since": observation["since"],
                "seconds_without_observed_score_change": int(time.time() - observation["since"]),
                "calls_since_observed_score_change": len(recent),
                "uncached_input_tokens": sum(c.get("input_tokens", 0) -
                    (c.get("cached_input_tokens") or 0) for c in recent
                    if c.get("cached_input_tokens") is not None),
                "estimated_cny_min": sum(c["estimated_cost_cny_min"] for c in priced),
                "priced_calls": len(priced), "unpriced_calls": len(recent) - len(priced),
                "note": "仅统计已观测区间，不是预算上限；余额是否正常须有供应商证据"}

    def _decision_nodes(self, dag: TaskDAG) -> list[Any]:
        """调度图保持完整，模型观察只合并同题同战术的已结束历史。"""
        platform = self.ctx.extra.get("platform")
        if platform is None or not getattr(platform, "continuous", False):
            return dag.all_nodes()
        latest = {}
        active = []
        for node in dag.all_nodes():
            cid = str((node.meta.get("challenge") or {}).get("id") or "")
            if not cid or node.status in {TaskStatus.PENDING, TaskStatus.DISPATCHED, TaskStatus.RUNNING}:
                active.append(node)
            else:
                latest[(cid, node.tactic.split("@", 1)[0])] = node
        return [*latest.values(), *active]

    def _competition_auto_continue(self, dag: TaskDAG) -> bool:
        """竞赛列表保留未完成题且有待发节点时，跳过“要不要继续”的复盘。"""
        if not str(self.ctx.extra.get("mode_prompt") or "").strip():
            return False
        platform = self.ctx.extra.get("platform")
        if platform is None or not hasattr(platform, "progress"):
            return False
        try:
            progress = platform.progress()
        except Exception:  # noqa: BLE001 —— 进度读取失败回退模型复盘
            return False
        unsolved = {str(cid) for cid in progress.get("unsolved_ids") or []}
        if not unsolved:
            return False
        for node in dag.ready_nodes():
            challenge = (node.meta or {}).get("challenge") or {}
            if str(challenge.get("id") or "") in unsolved:
                return True
        return False

    async def _apply_competition_decisions(
            self, dag: TaskDAG, decision: dict[str, Any]) -> None:
        """执行模型给出的暂缓/重试裁定；后端只校验 id 并操作资源。"""
        if not str(self.ctx.extra.get("mode_prompt") or "").strip():
            return
        states = self._challenge_states(dag)
        known = set(states)
        # 生命周期由模型明确选择；保留现场不意味着增加并发或阻止其后暂缓。
        raw_retained = decision.get("retain_instances") or []
        retained = {str(cid) for cid in (raw_retained if isinstance(raw_retained, list) else [])
                    if isinstance(cid, str) and cid in known}
        for node in dag.all_nodes():
            if str((node.meta.get("challenge") or {}).get("id") or "") in retained:
                node.meta["retain_instance"] = True

        retried: list[str] = []
        raw_retry = decision.get("retry_challenges") or []
        if isinstance(raw_retry, list):
            for item in raw_retry:
                cid = str(item.get("id") if isinstance(item, dict) else item).strip()
                if not cid or cid not in known:
                    continue
                for node in dag.all_nodes():
                    ch = (node.meta or {}).get("challenge") or {}
                    if str(ch.get("id") or "") != cid:
                        continue
                    node.meta["deferred"] = False
                    node.meta.pop("defer_reason", None)
                retried.append(cid)

        deferred: list[dict[str, str]] = []
        raw_defer = decision.get("defer_challenges") or []
        if isinstance(raw_defer, list):
            for item in raw_defer:
                if isinstance(item, dict):
                    cid = str(item.get("id") or "").strip()
                    reason = str(item.get("reason") or "模型判定暂缓")[:200]
                else:
                    cid, reason = str(item).strip(), "模型判定暂缓"
                if not cid or cid not in known or cid in retried:
                    continue
                for node in dag.all_nodes():
                    ch = (node.meta or {}).get("challenge") or {}
                    if str(ch.get("id") or "") != cid:
                        continue
                    node.meta["deferred"] = True
                    node.meta.pop("retain_instance", None)
                    node.meta["defer_reason"] = reason
                    if node.status in (TaskStatus.PENDING, TaskStatus.DISPATCHED,
                                       TaskStatus.RUNNING):
                        dag.mark(node.node_id, TaskStatus.CANCELLED)
                        node.meta["cancel_reason"] = f"赛题暂缓: {reason}"
                deferred.append({"id": cid, "reason": reason})

        platform = self.ctx.extra.get("platform")
        released: list[str] = []
        if deferred and platform is not None:
            for item in deferred:
                try:
                    if await platform.close_instance(
                            item["id"], reason=f"赛题暂缓: {item['reason']}"):
                        released.append(item["id"])
                except Exception as exc:  # noqa: BLE001 —— 回收失败不连坐主链路
                    self.ctx.audit.log("platform.fact", self.agent_id, {
                        "action": "defer_close_failed", "challenge": item["id"],
                        "error": str(exc)[:200]})

        if retried or deferred or released or retained:
            self.ctx.audit.log("decision.change", self.agent_id, {
                "action": "competition_challenge_state",
                "deferred": deferred, "retried": retried, "released": released,
                "retained": sorted(retained)})

    def _effort_options(self, task: Task) -> list[str]:
        """该任务可用的思考强度取值——按实际会用到的模型各自声明的档位算。

        绑定模型的 Agent 取其声明；未绑定时取该任务路由档位内全部模型声明值
        的并集。各模型的档位并不一致（有的 low/medium/high，有的另有 ultra），
        所以候选只能由模型自己声明，后端不预设任何固定枚举。
        """
        registry = getattr(self.ctx.gateway, "registry", None)
        if registry is None:
            return []
        agent = self.ctx.agents.get(task.agent_type)
        bound = ""
        if agent is not None:
            bound = str(getattr(agent.ctx, "agent_models", {}).get(
                task.agent_type.value, "") or "")
        if bound and registry.has(bound):
            return list(registry.get(bound).reasoning_effort_levels)
        entries = registry.all()
        out: list[str] = []
        for e in entries:
            for level in e.reasoning_effort_levels:
                if level not in out:
                    out.append(level)
        return out

    async def _assign_dispatch_plan(self, task_objs: list[tuple[Task, str]]) -> None:
        """下发前决策（灵枢）：按任务复杂度裁定并发分支路线预算与思考强度。

        路线预算 1~8：LLM 建议，规则只做边界钳制（防扇出失控）——解题任务
        同样按题面信息量与线索面发散并行分支（分支共享现场相互接力，任一
        分支夺旗其余收尾）。
        思考强度：取值必须落在该任务可用模型自己声明的档位内（见
        _effort_options；各模型档位不同，后端不预设候选）；模型没声明就只能
        留空（用端点默认档），裁定结果非法则丢弃并留痕，不猜、不代填。
        LLM 不可用/应答非法 → 保持未指定（子 Agent 按模板自然条数、端点默认
        思考档），留痕不静默。
        """
        ctx = self.ctx
        assert ctx.audit is not None
        digest = []
        options: dict[int, list[str]] = {}
        for i, (t, _) in enumerate(task_objs):
            levels = self._effort_options(t)
            options[i] = levels
            digest.append({
                "index": i, "task_type": t.task_type, "agent": t.agent_type.value,
                "target": t.target, "title": str(t.payload.get("node_title", "")),
                "effort_options": levels,
                "challenge": {k: v for k, v in (t.payload.get("challenge") or {}).items()
                              if k in ("title", "direction", "category", "points",
                                       "description")},
            })
        try:
            resp = await chat_decision(
                ctx.gateway,
                [
                    {"role": "system", "content": self._system_prompt()
                      + "\nROLE: dispatch_plan"},
                    {"role": "user", "content": json.dumps({
                        "tasks": digest,
                        "world_state": self._world_state(),
                        "project_rules": self._project_rules(),
                        "constraints": (
                            "先判断每个任务的复杂度，再据此给出：并发分支路线预算"
                            " route_budget（1~8 的整数；复杂/高价值目标 5~8，常规 "
                            "3~4，简单/窄面 1~2）与思考强度 reasoning_effort。"
                            "reasoning_effort 只能从该任务自己的 effort_options 里"
                            "原样挑一个——各模型支持的档位不同，选项就是该任务可用"
                            "模型的真实档位，列表为空表示该模型不吃思考强度，此时"
                            "必须留空字符串。简单机械的任务选低档省 token，需要多步"
                            "推理、试错与自我纠偏的任务选高档。"
                            "返回 JSON：{\"tasks\": [{\"index\": int, "
                            "\"route_budget\": int, \"reasoning_effort\": str, "
                            "\"complexity\": \"low\"|\"medium\"|\"high\", "
                            "\"rationale\": str}]}，每个任务都必须给出。"),
                    }, ensure_ascii=False)},
                ],
                audit=ctx.audit, actor=self.agent_id,
                task_type="meta_planning", response_format="json",
                reasoning_effort="low",
            )
            data = extract_json(resp.content)
        except Exception as exc:  # noqa: BLE001 —— 决策失败不阻断任务下发
            ctx.audit.log("decision.change", self.agent_id, {
                "action": "dispatch_plan_fallback",
                "reason": f"LLM 下发决策不可用: {str(exc)[:160]}"})
            return
        decided: dict[int, dict[str, Any]] = {}
        for item in (data or {}).get("tasks") or (data or {}).get("budgets") or []:
            try:
                decided[int(item["index"])] = item
            except (KeyError, TypeError, ValueError):
                continue
        if not decided:
            ctx.audit.log("decision.change", self.agent_id, {
                "action": "dispatch_plan_fallback",
                "reason": "LLM 应答无有效决策，保持模板自然条数与默认思考档"})
            return
        assigned: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for i, (t, _) in enumerate(task_objs):
            item = decided.get(i)
            if item is None:
                continue
            record: dict[str, Any] = {"task_id": t.task_id}
            try:
                t.max_routes = max(1, min(int(item["route_budget"]), 8))
                record["route_budget"] = t.max_routes
            except (KeyError, TypeError, ValueError):
                record["route_budget"] = None
            want = str(item.get("reasoning_effort") or "").strip()
            if want and want in options[i]:
                t.reasoning_effort = want
            elif want:
                rejected.append({"task_id": t.task_id, "requested": want,
                                 "declared": options[i]})
            record["reasoning_effort"] = t.reasoning_effort
            record["complexity"] = str(item.get("complexity", ""))[:16]
            record["rationale"] = str(item.get("rationale", ""))[:80]
            assigned.append(record)
        ctx.audit.log("decision.change", self.agent_id,
                      {"action": "dispatch_plan", "assigned": assigned})
        if rejected:
            ctx.audit.log("decision.change", self.agent_id,
                          {"action": "effort_rejected", "rejected": rejected,
                           "reason": "思考强度不在该任务可用模型声明的档位内，按默认档执行"})

    # ------------------------------------------------------------ 拓扑快照（Web 可视化）

    @staticmethod
    def _dag_snapshot(dag: TaskDAG) -> list[dict[str, Any]]:
        return [{
            "node_id": n.node_id, "tactic": n.tactic, "title": n.title,
            "status": n.status.value, "depends_on": list(n.depends_on),
            "task_ids": list(n.task_ids), "round_created": n.round_created,
            "challenge_id": str((n.meta.get("challenge") or {}).get("id") or ""),
            "cancel_reason": n.meta.get("cancel_reason", ""),
            "skip_reason": n.meta.get("skip_reason", ""),
        } for n in dag.all_nodes()]

    def _write_dag_snapshot(self, dag: TaskDAG, round_no: int | None = None) -> None:
        """DAG 快照写黑板任务状态层：Web 拓扑视图运行中实时数据源。

        快照记录以 dag_snapshot 键标记，读侧依此与任务记录区分；
        黑板文件随会话工作区持久化，终态会话拓扑同源可读。round 一并
        落盘，供运行中对话通道等旁路读取「现在进行到哪一轮」。
        """
        if self.ctx.blackboard is None:
            return
        data: dict[str, Any] = {"dag_snapshot": self._dag_snapshot(dag)}
        if round_no is not None:
            data["round"] = round_no
        self.ctx.blackboard.write(
            BlackboardLayer.TASK_STATE, "dag-snapshot",
            data,
            agent_type=AgentType.MASTER, updated_by=self.agent_id,
            dedup_key="dag|snapshot",
        )

    # ------------------------------------------------------------ 节点：调度

    _REFILL_INTERVAL = 1.0       # 无事件时重查资源退避，不依赖其他解题任务结束
    _QUEUE_RETRY_INTERVAL = 5.0  # 规划失败或没有新增节点时的重试间隔

    async def _dispatch(self, state: MasterState) -> dict[str, Any]:
        ctx = self.ctx
        await self._gate_wait()
        dag = TaskDAG.from_dict(state["dag"])
        # 取本轮任务（meta_plan 刚按 round_no 生成）；任务对象只重建一次，
        # 与执行侧共享同一实例——思考强度裁定落定后对后续每次调用即时生效
        round_specs = [t for t in state["tasks_dispatched"]
                       if Task.model_validate(t["task"]).round_no == state["round_no"]]
        round_tasks = [Task.model_validate(s["task"]) for s in round_specs]

        async def _run_one(spec: dict[str, Any], task: Task) -> tuple[str, TaskStatus]:
            agent = ctx.agents[task.agent_type]
            try:
                await agent.run(task)
                status = task.status if task.status in {
                    TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.PRUNED, TaskStatus.CANCELLED
                } else TaskStatus.FAILED
                return spec["node_id"], status
            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED
                bb = getattr(ctx, "blackboard", None)
                if bb is not None:
                    data = bb.read(BlackboardLayer.TASK_STATE, task.task_id)
                    if data is not None:
                        data["task"] = task.model_dump(mode="json")
                        for route in data.get("routes", []):
                            if route.get("status") in {"pending", "dispatched", "running"}:
                                route["status"] = TaskStatus.CANCELLED.value
                        bb.write(BlackboardLayer.TASK_STATE, task.task_id, data,
                                 agent_type=task.agent_type, updated_by=self.agent_id,
                                 dedup_key=f"task|{task.task_id}")
                ctx.audit.log("task.cancelled", self.agent_id, {"task_id": task.task_id})
                raise
            except Exception as exc:  # noqa: BLE001 —— 单子 Agent 故障不影响全局（2.1-3）
                assert ctx.audit is not None
                ctx.audit.log("task.failed", self.agent_id,
                              {"task_id": task.task_id, "error": str(exc)[:300]})
                return spec["node_id"], TaskStatus.FAILED

        # 下发决策（复杂度/路线预算/思考强度）与子 Agent 执行并行：裁定本身
        # 是一次模型调用（重推理端点上可达十余秒），串行挡在开工路径上纯属
        # 等待。裁定未落定期间首轮调用按端点默认档起步，落定后即时生效。
        assign_task = None
        if round_tasks and ctx.enable_llm_planning:
            assign_task = asyncio.create_task(self._assign_dispatch_plan(
                list(zip(round_tasks, (s["node_id"] for s in round_specs)))))

        # 环形督导：派发期间灵枢实时观察各子 Agent 路线进度，
        # 按全局态势给方向建议（建议经环形台账投递，子 Agent 在 LLM 决策上下文消费）
        watch = None
        watched_ids = [t.task_id for t in round_tasks]
        decisions: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        if ctx.ring is not None and ctx.enable_llm_planning:
            watch = asyncio.create_task(
                self._steering_watch(watched_ids, decisions))
        try:
            platform = (getattr(ctx, "extra", None) or {}).get("platform")
            refill_specs: list[dict[str, Any]] = []
            if platform is not None and getattr(platform, "continuous", False):
                running = {asyncio.create_task(_run_one(s, t)): s["node_id"]
                           for s, t in zip(round_specs, round_tasks)}
                completed = asyncio.create_task(platform.completion_queue.get())
                adjudicated = asyncio.create_task(decisions.get())
                planning: asyncio.Task[Any] | None = None
                provisioning: asyncio.Task[Any] | None = None
                cleanup: set[asyncio.Task[Any]] = set()
                closing_ids: set[str] = set()
                next_close_at: dict[str, float] = {}
                next_plan_at = 0.0
                last_plan_fingerprint = ""
                planning_fingerprint = ""
                model_recheck_at = float("inf")

                def launch(spec: dict[str, Any], task: Task) -> None:
                    # 同一份 DAG 就地更新；每个容器就绪就立即开工，不等其余供给。
                    refill_specs.append(spec)
                    watched_ids.append(task.task_id)
                    running[asyncio.create_task(_run_one(spec, task))] = spec["node_id"]
                    self._write_dag_snapshot(dag, state["round_no"])

                async def close_idle(cid: str) -> None:
                    try:
                        await platform.close_instance(cid, reason="本题执行已收尾，释放名额供后续题")
                    finally:
                        closing_ids.discard(cid)
                        next_close_at[cid] = time.monotonic() + self._QUEUE_RETRY_INTERVAL

                async def finish_deferral(decision: dict[str, Any], jobs: list[asyncio.Task[Any]],
                                          ids: set[str]) -> None:
                    try:
                        # 同题必须先退场再关容器；其他题的补位不等待这里的清理。
                        await asyncio.gather(*jobs, return_exceptions=True)
                        await self._apply_competition_decisions(dag, decision)
                    finally:
                        closing_ids.difference_update(ids)
                        for cid in ids:
                            next_close_at[cid] = time.monotonic() + self._QUEUE_RETRY_INTERVAL

                try:
                    while True:
                        listed = (ctx.extra.get("competition_context") or {}).get("challenges") or []
                        covered = set(self._challenge_states(dag))
                        missing = {str(ch["id"]) for ch in listed
                                   if isinstance(ch, dict) and ch.get("id")
                                   and str(ch["id"]) not in covered
                                   and not platform.is_completed(str(ch["id"]))}
                        # 回访队列也要持续补位，不能只补从未进入过 DAG 的题。
                        active_ids = {str((n.meta.get("challenge") or {}).get("id") or "")
                                      for n in dag.all_nodes() if n.status in {
                                          TaskStatus.PENDING, TaskStatus.DISPATCHED, TaskStatus.RUNNING}}
                        revisit = {cid for cid in covered - active_ids - closing_ids
                                   if not platform.is_completed(cid)
                                   and platform.can_start_instance(cid)}
                        retained_idle = {
                            str((n.meta.get("challenge") or {}).get("id") or "")
                            for n in dag.all_nodes() if n.meta.get("retain_instance") is True
                            and not n.meta.get("deferred")} - active_ids - closing_ids
                        ready = dag.ready_nodes()
                        capacity_blocked = bool(ready) and all(
                            platform.owns(str((n.meta.get("challenge") or {}).get("id") or ""))
                            and not platform.can_start_instance(
                                str((n.meta.get("challenge") or {}).get("id") or "")) for n in ready)
                        can_plan = bool(ctx.enable_llm_planning and (
                            missing or (running and not ready and revisit)
                            or (retained_idle and capacity_blocked)))
                        # 有效规划已看过的现场不因计时器反复付费；失败仍按原间隔重试。
                        # 空位、任务状态、用户约定或实际执行证据变化均会重新唤醒。
                        refill_fingerprint = json.dumps({
                            "missing": sorted(missing), "revisit": sorted(revisit),
                            "retained_idle": sorted(retained_idle),
                            "closing": sorted(closing_ids),
                            "instances": sorted(platform.active_instance_ids()),
                            "nodes": [(n.node_id, n.status.value) for n in dag.all_nodes()],
                            "world": self._world_state(),
                            "execution": self._progress_digest(watched_ids),
                        }, ensure_ascii=False, sort_keys=True, default=str)
                        can_plan = can_plan and (refill_fingerprint != last_plan_fingerprint
                                                 or time.monotonic() >= model_recheck_at)
                        if can_plan and planning is None and time.monotonic() >= next_plan_at:
                            planning_fingerprint = refill_fingerprint
                            planning = asyncio.create_task(self._llm_plan({
                                **state, "dag": dag.to_dict(), "queue_refill": True}))
                        if provisioning is None and dag.ready_nodes():
                            ready = dag.ready_nodes()
                            if any(not platform.owns(str((n.meta.get("challenge") or {}).get("id") or ""))
                                   or platform.can_start_instance(
                                       str((n.meta.get("challenge") or {}).get("id") or ""))
                                   for n in ready):
                                provisioning = asyncio.create_task(self._meta_plan(
                                    {**state, "dag": dag.to_dict()}, dispatch_only=True,
                                    live_dag=dag, blocked_challenge_ids=closing_ids,
                                    on_dispatch=launch))
                        # 回收按具体实例安排，避免慢 close 阻塞其他空位；供给中的
                        # DISPATCHED 节点纳入保护，不能按旧快照误关新启动的容器。
                        active = {str((n.meta.get("challenge") or {}).get("id") or "")
                                  for n in dag.all_nodes()
                                  if n.status in {TaskStatus.PENDING, TaskStatus.RUNNING,
                                                  TaskStatus.DISPATCHED}}
                        active.update(str((n.meta.get("challenge") or {}).get("id") or "")
                                      for n in dag.all_nodes() if n.meta.get("retain_instance") is True
                                      and not n.meta.get("deferred")
                                      and not platform.is_completed(
                                          str((n.meta.get("challenge") or {}).get("id") or "")))
                        for cid in platform.active_instance_ids() - active - closing_ids:
                            if time.monotonic() < next_close_at.get(cid, 0.0):
                                continue
                            closing_ids.add(cid)
                            cleanup.add(asyncio.create_task(close_idle(cid)))
                        if not (running or planning or provisioning or cleanup
                                or dag.ready_nodes() or can_plan):
                            break
                        waiters = set(running) | cleanup | {completed, adjudicated}
                        if planning is not None:
                            waiters.add(planning)
                        if provisioning is not None:
                            waiters.add(provisioning)
                        # 资源事件优先唤醒；超时只重查退避与资源，不触发固定回合上限。
                        done, _ = await asyncio.wait(waiters, timeout=self._REFILL_INTERVAL,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        for job in done & running.keys():
                            node_id, status = job.result()
                            dag.mark(node_id, status)
                            running.pop(job)
                        for job in done & cleanup:
                            cleanup.remove(job)
                            try:
                                job.result()
                            except Exception as exc:  # noqa: BLE001 —— 清理故障留痕供后续重试
                                ctx.audit.log("platform.fact", self.agent_id, {
                                    "action": "release_failed", "error": str(exc)[:200]})
                        if adjudicated in done:
                            decision = adjudicated.result()
                            adjudicated = asyncio.create_task(decisions.get())
                            ids = {str(item.get("id") or "")
                                   for item in decision.get("defer_challenges", [])
                                   if isinstance(item, dict)
                                   and str(ctx.extra.get("mode_prompt") or "").strip()
                                   and not platform.is_completed(str(item.get("id") or ""))
                                   and str(item.get("id") or "") not in closing_ids}
                            decision = {"defer_challenges": [
                                i for i in decision.get("defer_challenges", [])
                                if isinstance(i, dict) and str(i.get("id") or "") in ids]}
                            cancel_provisioning = any(
                                n.status == TaskStatus.DISPATCHED
                                and str((n.meta.get("challenge") or {}).get("id") or "") in ids
                                for n in dag.all_nodes())
                            # 先冻结同题节点，异步清理期间也不能被续规划重新派发。
                            for node in dag.all_nodes():
                                cid = str((node.meta.get("challenge") or {}).get("id") or "")
                                if cid in ids:
                                    node.meta["deferred"] = True
                                    reason = next(str(i.get("reason") or "模型判定暂缓")
                                                  for i in decision["defer_challenges"] if str(i["id"]) == cid)
                                    node.meta["defer_reason"] = reason[:200]
                                    if node.status in {TaskStatus.PENDING, TaskStatus.DISPATCHED,
                                                       TaskStatus.RUNNING}:
                                        dag.mark(node.node_id, TaskStatus.CANCELLED)
                            cancelled = [job for job, nid in running.items()
                                         if str((dag.get(nid).meta.get("challenge") or {})
                                                .get("id") or "") in ids]
                            for job in cancelled:
                                job.cancel()
                                running.pop(job)
                            # 供给与同题清理发生交叉时先收口供给，避免关后又开。
                            if provisioning is not None and cancel_provisioning:
                                provisioning.cancel()
                                cancelled.append(provisioning)
                                provisioning = None
                            if ids:
                                closing_ids.update(ids)
                                cleanup.add(asyncio.create_task(finish_deferral(decision, cancelled, ids)))
                        if planning is not None and planning in done:
                            plan = planning.result()
                            planning = None
                            if plan is not None:
                                last_plan_fingerprint = planning_fingerprint
                                delay = plan.get("recheck_after_seconds")
                                model_recheck_at = (time.monotonic() + delay
                                    if type(delay) in {int, float} and math.isfinite(delay) and delay > 0
                                    else float("inf"))
                            next_plan_at = time.monotonic() + self._QUEUE_RETRY_INTERVAL
                            if plan:
                                try:
                                    # 规划和督导的暂缓走同一退场通道：先取消同题执行，再关实例。
                                    await self._apply_competition_decisions(
                                        dag, {**plan, "defer_challenges": []})
                                    if plan.get("defer_challenges"):
                                        decisions.put_nowait({
                                            "defer_challenges": plan["defer_challenges"]})
                                    result = dag.upsert_from_plan(self._resolve_plan_challenges(
                                        plan.get("dag_nodes", [])), state["round_no"])
                                    self._record_plan_result(state["round_no"],
                                                             result.created, result.skipped)
                                    if result.created:
                                        next_plan_at = 0.0
                                    ctx.audit.log("decision.change", self.agent_id, {
                                        "action": "queue_refilled",
                                        "new_nodes": [n.node_id for n in result.created]})
                                except SpiritScoutError as exc:
                                    self._record_plan_result(state["round_no"], [], [], rejected=str(exc))
                                    ctx.audit.log("decision.change", self.agent_id, {
                                        "action": "plan_rejected", "source": "queue_refill",
                                        "reason": str(exc)[:200]})
                        if provisioning is not None and provisioning in done:
                            provisioning.result()
                            provisioning = None
                        if completed in done:
                            completed.result()
                            completed = asyncio.create_task(platform.completion_queue.get())
                        self._write_dag_snapshot(dag, state["round_no"])
                finally:
                    completed.cancel()
                    adjudicated.cancel()
                    pending = (set(running) | cleanup
                               | ({planning} if planning is not None else set())
                               | ({provisioning} if provisioning is not None else set()))
                    for job in pending:
                        job.cancel()
                    await asyncio.gather(completed, adjudicated, *pending, return_exceptions=True)
                results = []
            else:
                results = await asyncio.gather(
                    *(_run_one(s, t) for s, t in zip(round_specs, round_tasks)))
        finally:
            if watch is not None:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch
            if assign_task is not None and not assign_task.done():
                # 任务全部先于裁定完成：裁定已无用，取消并回收
                assign_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await assign_task
        for node_id, status in results:
            dag.mark(node_id, status)
        await self._platform_release()
        self._write_dag_snapshot(dag, state["round_no"])
        return {"dag": dag.to_dict(), "tasks_dispatched": refill_specs}

    # ------------------------------------------------------------ 用户直达通道

    _CHAT_INTERVAL = 1.0      # 消息轮询节拍（秒）——用户消息 1s 内直达灵枢
    _CHAT_HISTORY_MAX = 12    # 单次应答携带的对话窗口（上限；有会话对话史时取史末尾）

    async def _user_chat_watch(self) -> None:
        """用户 ↔ 灵枢的即时对话通道（与子 Agent 的派发/督导完全隔离）。

        用户会话中发来的消息 1 秒内直达灵枢，不等待子 Agent 的回合边界：
        灵枢在自己的独立对话里直接回答用户（reply 经审计流实时可见），
        需要子 Agent 调整方向或分析新附件时经环形台账下发建议——子 Agent
        在其下一回合消费（执行中的模型调用无法被打断，这是两条通道之间
        唯一的交接点）。通道失败不影响主链路。
        """
        assert self.ctx.audit is not None
        # 对话上下文：会话对话史（跨续传持久化，用户消息已由 /messages
        # 随发随落）优先——灵枢应答原地追加、封顶由会话层负责；无对话史
        # 的装配（测试/离线）退回进程内独立窗口，行为同旧
        seed = self.ctx.extra.get("dialogue_history")
        own_history = not isinstance(seed, list)
        history: list[dict[str, str]] = [] if own_history else seed
        while True:
            await asyncio.sleep(self._CHAT_INTERVAL)
            feed = self._drain_user_feed()
            if not feed:
                continue
            messages = [str(item.get("text") or "").strip()
                        for item in feed if isinstance(item, dict)]
            messages = [m for m in messages if m]
            attachments = [str(item.get("path"))
                           for item in feed if isinstance(item, dict)
                           and item.get("path")]
            if not messages and not attachments:
                continue
            reply, steering = await self._llm_user_chat(
                messages, attachments, history)
            if own_history:
                for message in messages:
                    history.append({"role": "user", "content": message[:1000]})
            if not reply:
                # 应答失败也要给用户可见的回执（意见内容已留在意见流不丢失）
                reply = ("已收到你的消息并转达给执行中的智能体；灵枢应答通道"
                         "暂时不可用，稍后会继续跟进。")
            history.append({"role": "assistant", "content": reply[:2000]})
            if own_history:
                del history[:-self._CHAT_HISTORY_MAX]
            self.ctx.audit.log("master.reply", self.agent_id, {
                "text": reply[:4000]})
            # 应答落黑板：拓扑图「你 → 灵枢」节点的回执数据源
            with contextlib.suppress(Exception):
                self.ctx.blackboard.write(
                    BlackboardLayer.TOPOLOGY, new_id("reply"),
                    {"kind": "reply", "text": reply[:1000],
                     "about": messages[0][:300] if messages else "",
                     "ts": time.time()},
                    agent_type=AgentType.MASTER, updated_by=self.agent_id,
                    dedup_key=f"reply|{time.time()}")
            for item in steering:
                self.ctx.ring.steer(item["agent"], item["hint"],
                                    rationale=item.get("rationale", ""),
                                    task_id=item.get("task_id", ""))
            if steering:
                self.ctx.audit.log("decision.change", self.agent_id, {
                    "action": "user_chat_steering", "count": len(steering)})

    async def _llm_user_chat(self, messages: list[str], attachments: list[str],
                             history: list[dict[str, str]]
                             ) -> tuple[str, list[dict[str, str]]]:
        """灵枢回答用户（独立上下文）：直接答复 + 可选的子 Agent 方向建议。"""
        roster = "；".join(
            f"{get_profile(agent_type).name}({agent_type.value})"
            for agent_type in self.ctx.agents)
        paused = bool(self.ctx.pause_gate is not None and self.ctx.pause_gate.paused)
        situation = self._engagement_status()
        payload = {
            "mission": self._mission()[:1000],
            "world_state": self._world_state(),
            "agents": [a.value for a in self.ctx.agents],
            "roster": roster,
            "engagement_paused": paused,
            "situation": situation,
            "active_tasks": situation.get("active_tasks", []),
            "chat_history": history[-self._CHAT_HISTORY_MAX:],
            "new_messages": messages,
            "attachments": attachments,
            "constraints": (
                "你是灵枢，安全测试负责人。用户在会话进行中直接向你发消息，"
                "这条对话与子 Agent 的执行互不干扰。engagement_paused 为真表示"
                "用户手动暂停了任务推进——你照常回答问题、聊方向，但不要在回复里"
                "承诺立即推进执行（恢复由用户操作）。situation 是从黑板读到的"
                "实时态势（进行到第几轮、各节点状态、已有成果计数与在途任务）——"
                "回答进度类问题以它为准，不要凭任务书或臆测说「刚开始」之类的话。"
                "reply 用简洁的中文直接"
                "回答用户：确认收到指导意见、回答疑问、简述当前态势与下一步"
                "打算。需要子 Agent 调整方向或分析新上传的附件时，同时在 "
                "steering 里给出建议（agent 必须取自 agents 列表，hint 一句话，"
                "涉及附件写明文件路径，子 Agent 可直接读取；hint 会随会话页"
                "展示给用户——用平实中文，不用 HAR、PoC 这类行话缩写）；"
                "无需调整则 steering 给空列表。"
                "针对具体任务的指令须填写 active_tasks 中的 task_id；仅当明确需要"
                "通知该类型所有任务时才填写 broadcast=true。"
                "返回 JSON：{\"reply\": str, "
                "\"steering\": [{\"agent\": str, \"task_id\": str, \"broadcast\": bool, \"hint\": str, "
                "\"rationale\": str}]}"),
        }
        try:
            resp = await chat_decision(
                self.ctx.gateway,
                decision_messages(self._system_prompt() + "\nROLE: user_chat", payload, 16000),
                memory=decision_memory(self, "user_chat"), audit=self.ctx.audit, actor=self.agent_id,
                task_type="meta_planning", response_format="json",
                reasoning_effort="low",   # 对话应答要快，不与重推理抢档
            )
            data = extract_json(resp.content) or {}
            reply = str(data.get("reply") or "").strip()[:4000]
            steering = self._target_steering(
                data.get("steering") or [], situation.get("active_tasks", []),
                allow_broadcast=True)
            return reply, steering
        except (SpiritScoutError, OSError, ValueError, TypeError) as exc:
            # 对话通道失败不影响主链路，但留痕可见
            self.ctx.audit.log("master.reply_failed", self.agent_id,
                               {"error": str(exc)[:200]})
            return "", []

    # ------------------------------------------------------------ 环形督导

    _STEER_INTERVAL = 2.0   # 观察节拍（秒）；无进展变化不调用 LLM

    def _drain_user_feed(self) -> list[dict[str, Any]]:
        """取走用户会话中发来的消息与附件记录（用户直达通道的消费点）。"""
        inbox = self.ctx.extra.get("user_inbox")
        if not inbox:
            return []
        from spiritscout.gateway.world_state import record_user_statement

        items = list(inbox)
        for item in items:
            if isinstance(item, dict) and item.get("text"):
                statement = {"kind": "message", "text": str(item["text"])}
                if item.get("statement_id"):
                    statement["statement_id"] = item["statement_id"]
                record_user_statement(self.ctx.blackboard, statement)
        inbox.clear()
        return items

    async def _steering_watch(
            self, task_ids: list[str],
            decisions: asyncio.Queue[dict[str, Any]] | None = None) -> None:
        """派发期并发督导：读各任务实时路线笔记 → LLM 判读全局 → 写方向建议。

        新补位任务通过共享名单进入观察；方向建议交子 Agent 权衡，竞赛暂缓
        裁定经队列交调度器取消同题任务并回收实例。是否暂缓始终由模型决定。
        用户消息不在这里消费——它们走独立的直达通道（_user_chat_watch），
        两条通道并行互不阻塞。
        """
        assert self.ctx.audit is not None
        last_fingerprint = ""
        while True:
            await asyncio.sleep(self._STEER_INTERVAL)
            digest = self._progress_digest(task_ids)
            if not digest["tasks"]:
                continue
            # 投递新指令本身不是现场进展，避免“发指令→摘要变化→再发指令”自激循环。
            facts = {**digest, "tasks": [{k: v for k, v in t.items() if k != "steering"}
                                          for t in digest["tasks"]]}
            fingerprint = json.dumps(facts, ensure_ascii=False, sort_keys=True)
            if fingerprint == last_fingerprint:
                continue
            last_fingerprint = fingerprint
            steering = await self._llm_steer(digest, decisions)
            for item in steering:
                self.ctx.ring.steer(item["agent"], item["hint"],
                                    rationale=item.get("rationale", ""),
                                    task_id=item.get("task_id", ""))

    def _progress_digest(self, task_ids: list[str]) -> dict[str, Any]:
        """本轮任务的实时进度摘要（子 Agent 随步上报的路线笔记）。"""
        bb = getattr(self.ctx, "blackboard", None)
        tasks: list[dict[str, Any]] = []
        for tid in task_ids:
            data = bb.read(BlackboardLayer.TASK_STATE, tid) if bb is not None else None
            if data is None:
                continue
            task = data.get("task", {})
            if task.get("status") not in {
                    TaskStatus.PENDING.value, TaskStatus.DISPATCHED.value,
                    TaskStatus.RUNNING.value}:
                continue
            routes = [
                {"route": r.get("name"), "status": r.get("status"),
                 "progress": r.get("progress"),
                 "notes": [str(n)[:500] for n in (r.get("notes") or [])[-3:]]}
                for r in data.get("routes", [])
            ]
            challenge = (task.get("payload") or {}).get("challenge") or {}
            tasks.append({"agent": task.get("agent_type"),
                          "task_id": tid,
                          "challenge": {k: challenge[k] for k in ("id", "title")
                                        if k in challenge},
                          "steering": self.ctx.ring.steerings_for(
                              task.get("agent_type"), task_id=tid) if self.ctx.ring else [],
                          "title": str(task.get("title", ""))[:80],
                          "routes": routes})
        platform = self.ctx.extra.get("platform")
        return {"tasks": tasks,
                "platform_progress": {k: v for k, v in platform.progress().items() if k != "updated_at"} if platform is not None
                and hasattr(platform, "progress") else {}}

    def _route_digest(self, state: MasterState) -> list[dict[str, Any]]:
        """本轮各子 Agent 路线的执行笔记摘要（任务结果流向复盘）。

        路线观察/结论是复盘判断「还有哪些方向没试过」的第一手依据；
        笔记显示「LLM 不可用/调查停摆」属基础设施问题，不是无解题空间。
        """
        bb = self.ctx.blackboard
        routes: list[dict[str, Any]] = []
        for spec in state["tasks_dispatched"]:
            if len(routes) >= 20:
                break
            try:
                task = Task.model_validate(spec["task"])
            except Exception:  # noqa: BLE001, S112 —— 脏数据跳过，不影响复盘
                continue
            if task.round_no != state["round_no"]:
                continue
            data = bb.read(BlackboardLayer.TASK_STATE, task.task_id)
            if data is None:
                continue
            for r in data.get("routes", []):
                if len(routes) >= 20:
                    break
                routes.append({
                    "agent": task.agent_type.value,
                    "node": str(task.payload.get("node_title", ""))[:60],
                    "route": r.get("name"), "status": r.get("status"),
                    "notes": [str(n)[:160] for n in (r.get("notes") or [])[-2:]],
                })
        return routes

    async def _llm_steer(
            self, digest: dict[str, Any],
            decisions: asyncio.Queue[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """LLM 判读全局进度，产出需调整方向的子 Agent 建议（白名单校验）。"""
        valid_agents = sorted({t["agent"] for t in digest["tasks"]
                               if t.get("agent")})
        if not valid_agents:
            return []
        try:
            payload: dict[str, Any] = {
                "action": "steer",
                "mission": self._mission()[:1000],
                "world_state": self._world_state(),
                "business_context": shared_context(self.ctx.blackboard),
                # 按任务 ID 编址，某条路线变化只追加该任务的补丁，不重发整批。
                "progress": {**digest, "tasks": {
                    t["task_id"]: {k: v for k, v in t.items() if k != "steering"}
                    for t in digest["tasks"] if t.get("task_id")}},
                "active_tasks": [{k: t.get(k) for k in ("task_id", "agent", "challenge")}
                                 for t in digest["tasks"]],
                "steering_status": {t["task_id"]: t.get("steering", [])
                                    for t in digest["tasks"] if t.get("task_id")},
                "competition": {
                    "challenges": [{k: ch[k] for k in
                                    ("id", "title", "difficulty", "score", "solved_count")
                                    if k in ch}
                                   for ch in (self.ctx.extra.get("competition_context") or {})
                                   .get("challenges", []) if isinstance(ch, dict)]},
                "agents": valid_agents,
                "constraints": (
                            "你在实时督导并行作业的子 Agent。依据各路线进度与最新"
                            "笔记判断：谁走进了死胡同（建议换方向）、谁发现了高价值"
                            "线索（建议深挖或提醒同伴接力）、谁在同质重复做功"
                            "（建议避开同伴已覆盖的入口）。竞赛中某题反复探测而无新增"
                            "有效信息时，建议保存断点、以待解结论收尾，把名额交给列表"
                            "后续题，首轮扫完后再回访；不要只要求继续换路径穷举。"
                            "进度百分比和命令成功不等于得分或有效线索。只对有明确调整必要的 "
                            "Agent 给建议，一切正常则返回空列表。竞赛连续调度中，"
                            "比较卡题与清单中尚未尝试的容易题的得分效率；决定暂缓时，"
                            "输出 defer_challenges，系统会取消同题在途任务、关闭容器并补位。"
                            "reason 简短保留已排除方向、有效线索和回访条件。"
                            "不要按固定时长或回合数机械跳题。有实质突破可继续。返回 JSON："
                            "{\"steering\": [{\"agent\": 仅 agents 列表之一, "
                            "\"task_id\": active_tasks 中对应任务的 task_id, "
                            "\"hint\": 下一步指令, \"rationale\": 判断依据}], "
                            "\"defer_challenges\": [{\"id\": 进度中在途赛题ID, \"reason\": str}]}"),
            }
            payload["constraints"] += (
                "每条指令必须指定 task_id，不能把某题指令发给所有同类型任务。"
                "steering.feedback 中 delivered 仅表示已投递，accepted 仅表示计划执行；"
                "completed/blocked 是子 Agent 自报，结合后续工具结果判断实际效果。"
                "若前次指令尚未奏效，结合反馈与新证据纠偏或暂缓，不要重复改写同一建议。"
                "只引用对应任务的可见事实，不把截断摘要当完整源码，不臆测已经具备的能力；"
                "以 platform_progress 的已完成题目为准，不能把已完成题当未尝试题。")
            resp = await chat_decision(
                self.ctx.gateway,
                decision_messages(self._system_prompt() + "\nROLE: collab_judge", payload, 16000),
                audit=self.ctx.audit, actor=self.agent_id,
                memory=decision_memory(self, "steering"),
                task_type="meta_planning", response_format="json",
                reasoning_effort=self._MASTER_EFFORT,
            )
            data = extract_json(resp.content) or {}
            active_ids = {str((t.get("challenge") or {}).get("id") or "")
                          for t in digest["tasks"]}
            raw_defer = data.get("defer_challenges") or []
            deferred = [i for i in (raw_defer if isinstance(raw_defer, list) else [])
                        if isinstance(i, dict) and str(i.get("id") or "") in active_ids
                        and i.get("id") and str(i.get("reason") or "").strip()]
            if deferred and decisions is not None:
                decisions.put_nowait({"defer_challenges": deferred})
            items = data.get("steering")
            if not isinstance(items, list):
                raise TypeError("steering 应答不是列表")
            return self._target_steering(items, digest["tasks"])
        except Exception:  # noqa: BLE001 —— 督导失败不拖垮派发
            return []

    def _target_steering(self, items: list[Any], tasks: list[dict[str, Any]], *,
                         allow_broadcast: bool = False) -> list[dict[str, str]]:
        """校验模型选定的收件任务；不把歧义指令静默广播给全部同类型任务。"""
        out = []
        valid_agents = {a.value for a in self.ctx.agents} if allow_broadcast else {
            t.get("agent") for t in tasks}
        for item in items:
            if (not isinstance(item, dict) or item.get("agent") not in valid_agents
                    or not isinstance(item.get("hint"), str) or not item["hint"].strip()):
                continue
            candidates = [t for t in tasks if t.get("agent") == item["agent"]]
            tid = str(item.get("task_id") or "")
            broadcast = allow_broadcast and item.get("broadcast") is True and not tid
            if not tid and not broadcast and len(candidates) == 1:
                tid = str(candidates[0].get("task_id") or "")
            if not broadcast and (not tid or tid not in {t.get("task_id") for t in candidates}):
                self.ctx.audit.log("decision.change", self.agent_id, {
                    "action": "steering_rejected", "agent": item["agent"],
                    "task_id": tid, "reason": "收件任务缺失、歧义或不属于该 Agent"})
                continue
            out.append({"agent": item["agent"], "task_id": tid,
                        "hint": item["hint"][:2000],
                        "rationale": str(item.get("rationale", ""))[:600]})
        return out

    # ------------------------------------------------------------ 节点：汇聚

    async def _aggregate(self, state: MasterState) -> dict[str, Any]:
        await self._gate_wait()
        if drain := self.ctx.extra.get("verification_drain"):
            await drain()
        view = self.ctx.convergence.aggregate_round_view(state["round_no"])
        flag_manager = self.ctx.extra.get("flag_manager")
        if flag_manager is not None:
            view["flags"] = {**flag_manager.stats(),
                             "submit_enabled": flag_manager.submit_fn is not None}
        extra = getattr(self.ctx, "extra", None) or {}
        platform = extra.get("platform")
        if platform is not None:
            # 平台操作事实进复盘视图：名额、容器开关、提交与列题异常都是
            # 决策输入（如「活跃名额已满」解释为什么还有节点没开工）
            facts = extra.get("platform_facts") or []
            try:
                summary = platform.summary()
            except Exception:  # noqa: BLE001 —— 摘要失败不阻断复盘
                summary = {}
            view["platform"] = {"summary": summary,
                                "recent": [dict(f) for f in facts][-8:]}
        await self.ctx.bus.publish(Event(topic="round.view", data_id=f"round-{state['round_no']}",
                                         source=self.agent_id, payload=view))
        return {"round_views": [view]}

    # ------------------------------------------------------------ 节点：复盘

    async def _review(self, state: MasterState) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx.audit is not None
        await self._gate_wait()
        dag = TaskDAG.from_dict(state["dag"])
        view = state["round_views"][-1]
        next_round = state["round_no"] + 1

        # 漏洞终审（4.1.1 主 Agent 终审权）：合规已通过且 PoC 验证 → 确认收录
        verdicts = self._final_verdicts()

        # 收敛判定交由模型复盘：节点跑完只是态势输入（DAG 状态随视图进入
        # 复盘上下文），目标是否达成、是否补节点、何时收手由模型判断——
        # 确定性数据为空不替代目标完成判断，不设固定回合顶。用户随时
        # 可停；唯一确定性快路是「Flag 已收且全部节点完成」（该收手无需
        # 再问一次模型）。
        stop_reason = ""
        flags_pending = self._ctf_flags_pending(view)
        if self._flag_hard_stop(view, dag):
            stop_reason = "核心目标已达成，全部节点完成"
        # 本轮完全没派出任务时保留模型复盘，避免平台持续拒绝启动却空转。
        auto_continue = bool(not stop_reason and state.get("tasks_dispatched")
                             and self._competition_auto_continue(dag))
        llm_decision = (await self._llm_review(state, view)
                        if (ctx.enable_llm_planning and not stop_reason
                            and not auto_continue) else None)
        if llm_decision is not None and (
                not isinstance(llm_decision, dict)
                or type(llm_decision.get("continue_iteration")) is not bool):
            # 只有明确的布尔决策能收尾；缺字段或字符串 false 不是停止指令。
            ctx.audit.log("llm.review_failed", self.agent_id, {
                "round": state["round_no"], "error": "复盘缺少有效的 continue_iteration 布尔值"})
            llm_decision = None
        deferred_replan = False
        if llm_decision and not stop_reason:
            await self._apply_competition_decisions(dag, llm_decision)
            if llm_decision.get("continue_iteration", False):
                await self._ask_user_from_review(llm_decision)
            if not llm_decision.get("continue_iteration", False):
                stop_reason = f"主Agent决策收敛: {llm_decision.get('reason', '')[:120]}"
                # 派发阶段已经等待本轮执行者收尾；残留节点不否决模型的
                # 停止决策，撤销尚未派发的节点并保留明确原因。
                for node in dag.all_nodes():
                    if node.status in (TaskStatus.PENDING, TaskStatus.DISPATCHED):
                        dag.mark(node.node_id, TaskStatus.CANCELLED)
                        node.meta["cancel_reason"] = stop_reason
            elif llm_decision.get("dag_nodes"):
                nodes = self._filter_plannable(llm_decision["dag_nodes"])
                if ctx.plan_override is not None:
                    # 模式自带规划器（CTF/渗透赛）：重攻节点由规划器按方向轮换
                    # 统一生成并绑定赛题；LLM 建议只作方向参考，不直接入图
                    ctx.audit.log("decision.change", self.agent_id, {
                        "action": "llm_nodes_deferred", "round": state["round_no"],
                        "count": len(nodes),
                        "titles": [str(n.get("title", ""))[:80] for n in nodes[:6]]})
                    # 模式规划器负责生成下一轮的完整节点，因此这里不直接把模型
                    # 建议写入 DAG；但“延后交给下一轮规划”本身就是继续条件。
                    # 旧逻辑随后看到 DAG 已清空，反而立刻收敛，导致明确的继续
                    # 决策永远到不了下一轮。
                    deferred_replan = bool(nodes)
                else:
                    try:
                        result = dag.upsert_from_plan(self._resolve_plan_challenges(nodes), next_round)
                    except SpiritScoutError as exc:
                        # 复盘建议不可落图（未知依赖/成环）：留痕后不入图，
                        # 本轮收敛判定按「模型继续但无新节点」走下方回元规划
                        ctx.audit.log("decision.change", self.agent_id, {
                            "action": "llm_nodes_rejected", "round": state["round_no"],
                            "reason": str(exc)[:200]})
                        self._record_plan_result(
                            state["round_no"], [], [], rejected=str(exc))
                    else:
                        self._record_plan_result(
                            state["round_no"], result.created, result.skipped)
                        # 落图回执全量留痕：新建与跳过（含原因）都可追查——
                        # 静默吞掉会让下一轮复盘误判「在途」而空等
                        ctx.audit.log("decision.change", self.agent_id, {
                            "action": "llm_nodes_applied",
                            "round": state["round_no"],
                            "created": [n.node_id for n in result.created],
                            "skipped_nodes": self._skip_digest(result.skipped)})

        if (not stop_reason and not deferred_replan
                and not dag.ready_nodes() and not dag.has_unfinished()):
            if llm_decision is not None and llm_decision.get("continue_iteration"):
                # 模型明确要继续但没给新节点：回元规划重规划（有全量态势
                # 可补节点）——继续是决策，不因「无就绪任务」拦下
                deferred_replan = True
            elif flags_pending:
                stop_reason = ("Flag 未收且复盘不可用（LLM 未应答/未启用），收敛"
                               if llm_decision is None
                               else "Flag 未收且复盘未给出新方向")
            else:
                stop_reason = "无新发现，无就绪任务"
        continue_iteration = not stop_reason

        ctx.audit.log("decision.change", self.agent_id, {
            "action": "review", "round": state["round_no"],
            "continue": continue_iteration, "stop_reason": stop_reason,
            "deferred_replan": deferred_replan,
            "auto_continue": auto_continue,
            "llm_reason": str((llm_decision or {}).get("reason", ""))[:400],
            "view": view, "verdicts": verdicts})
        return {"dag": dag.to_dict(), "round_no": next_round,
                "continue_iteration": continue_iteration, "stop_reason": stop_reason}

    # ------------------------------------------------------------ 内部

    def _flag_hard_stop(self, view: dict[str, Any], dag: TaskDAG) -> bool:
        """确定性收手快路：Flag 已收且全部节点完成（该收手无需再问模型）。

        平台托管多题会话（平台有按题完成态）例外：拿到一个 Flag 只是单题
        得分，平台视野内还有未完成题时不走快路——继续重攻剩余赛题，是否
        收手交模型复盘按全局态势裁量。
        """
        if not (bool(view.get("flags")) and not self._ctf_flags_pending(view)
                and not dag.has_unfinished()
                and not view.get("vuln_in_pipeline")):
            return False
        platform = (getattr(self.ctx, "extra", None) or {}).get("platform")
        if (platform is not None and platform.has_completion_signal()
                and not platform.all_completed()):
            return False
        return True

    def _ctf_flags_pending(self, view: dict[str, Any]) -> bool:
        """CTF/渗透赛语境且 Flag 尚未收录：硬收敛门让位于 LLM 复盘。"""
        flags = view.get("flags")
        return bool(flags) and not flags.get("accepted")

    @staticmethod
    def _needs_replan(dag: TaskDAG, state: MasterState) -> bool:
        """动态重规划触发：无节点，或无就绪且无运行中节点（Incalmo 防刚性停滞）。"""
        if not dag.all_nodes():
            return True
        return not dag.ready_nodes() and not any(
            n.status == TaskStatus.RUNNING for n in dag.all_nodes()
        )

    def _final_verdicts(self) -> list[dict[str, Any]]:
        """终审：复核独立核验（鉴真）结论（规则引擎再确认，模型仅建议）。"""
        from spiritscout.core.models import Vulnerability, VulnStatus

        verdicts = []
        for rec in self.ctx.blackboard.query(BlackboardLayer.VULN, limit=10000):
            vuln = Vulnerability.model_validate(rec["data"])
            policy = self.ctx.blackboard.read(BlackboardLayer.TOPOLOGY, "verification-policy") or {}
            independently_verified = (not policy.get("independent_retest_required")
                                      or vuln.verification.get("verdict") == "confirmed")
            if vuln.status == VulnStatus.ACCEPTED and (not vuln.poc_verified or not independently_verified):
                # 终审兜底：无 PoC 实证不得收录（AWE/MAPTA 证据门控原则）
                vuln.status = VulnStatus.REJECTED_UNREPRO
                vuln.false_positive_reason = "终审驳回：PoC 未实际验证"
                self.ctx.blackboard.write(
                    BlackboardLayer.VULN, rec["record_id"], vuln.model_dump(mode="json"),
                    agent_type=AgentType.MASTER, updated_by=self.agent_id,
                    dedup_key=vuln.dedup_key(), expected_version=rec["version"],
                )
                verdicts.append({"vuln_id": vuln.vuln_id, "verdict": "vetoed_no_poc"})
            elif vuln.status == VulnStatus.ACCEPTED:
                verdicts.append({"vuln_id": vuln.vuln_id, "verdict": "confirmed"})
        return verdicts

    async def _llm_plan(self, state: MasterState) -> dict[str, Any] | None:
        """LLM 主路径元规划：全量上下文喂云端，规则规划仅兜底。

        上下文：可用战术映射（LLM 只能在这些战术上规划）+ 黑板态势摘要
        （资产/端点/线索/漏洞流水线）+ 既有 DAG 节点 + 近三轮视图。
        产出校验（规则引擎裁决）：未知战术节点在入图前丢弃并留痕——
        LLM 是规划建议者，调度裁决权不外包。
        """
        try:
            tactic_map = self.ctx.tactic_map or TACTIC_AGENT_MAP
            dag = TaskDAG.from_dict(state["dag"])
            # 只调整调用强度，不替模型选择题目、战术或判定停滞。
            competition_opening = (state["round_no"] == 0 and not dag.all_nodes()
                                   and bool(self.ctx.extra.get("mode_prompt"))
                                   and bool((self.ctx.extra.get("competition_context")
                                             or {}).get("challenges")))
            resp = await chat_decision(
                self.ctx.gateway,
                decision_messages(self._system_prompt() + "\nROLE: meta_planner", {
                        "target": state["target"], "round": state["round_no"],
                        "mission": self._mission()[:2000],
                        "competition_context": self.ctx.extra.get("competition_context", {}),
                        "competition_status": self._competition_status(dag),
                        "queue_refill": bool(state.get("queue_refill")),
                        "planned_challenge_ids": list(self._challenge_states(dag)),
                        "platform_facts": (self.ctx.extra.get("platform_facts") or [])[-8:],
                        "world_state": self._world_state(),
                        "project_rules": self._project_rules(),
                        "execution_recovery": self.ctx.extra.get("execution_recovery", {}),
                        "available_tactics": {
                            t: {"agent": a.value, "task_type": tt}
                            for t, (a, tt) in tactic_map.items()},
                        "situation": self._situation_digest(),
                        "existing_nodes": [
                            {"tactic": n.tactic, "title": n.title,
                             "status": n.status.value,
                             "target": str((n.meta or {}).get("target") or "")}
                            for n in self._decision_nodes(dag)],
                        "last_plan_result": self.ctx.extra.get("last_plan_result") or {},
                        "views": state["round_views"][-3:],
                        "constraints": (
                            "返回 JSON：{\"analysis\": str, \"dag_nodes\": [{\"tactic\": "
                            "仅可用战术之一, \"title\": str, \"technique\": str, "
                            "\"target\": str（可选）, \"depends_on\": [依赖战术名，可空；"
                            "填已有或本计划内节点的 tactic，不要填标题], "
                            "\"challenge_id\": str（赛题节点必填，非赛题节点省略）, "
                            "\"continue_from\": {\"task_id\": str, \"route_id\": str}（可选）}], "
                            "\"retry_challenges\": [{\"id\": str, \"reason\": str}], "
                            "\"retain_instances\": [需跨节点保留现场的赛题ID], "
                            "\"defer_challenges\": [{\"id\": str, \"reason\": str}], "
                            "\"recheck_after_seconds\": 正数（可选，等待后复查）}。"
                            "competition_context 提供赛方规则原文与实际题目清单；"
                            "不得按赛制名称假定计分、轮次、提交或完成条件。"
                            "赛题节点仅输出清单中的 challenge_id，系统会装配该题完整原始对象；"
                            "不要复写题面、描述或整份赛题对象。analysis 简述派工依据和缺口，"
                            "不复述已有规则、题目清单和工具回执。平台提供独立题目列表时，"
                            "默认按原列表顺序为未完成题形成简短待发节点，title 只需题号与"
                            "完成目标，technique 可留空，analysis 用一句话说明即可。"
                            "不要先比较各题难度、分值、收益或安排做题排名，不展开各题解法；"
                            "不要因容器名额有限只规划首批，后续待发节点供名额释放后立即补位。"
                            "实际依赖或用户明确指定顺序时遵循其要求。没有独立题目列表的任务"
                            "仍按现场形成最小可执行计划。"
                            "queue_refill=true 表示其他题正在执行，本次补待发队列，不等待在途任务。"
                            "competition_status 是完整完成态与逐题执行回执，优先于节选历史；"
                            "先补尚未尝试的未完成题；首轮扫完后，为空位安排未完成题的回访。"
                            "在途/待发题不重复，已完成题不重建；回访用唯一 tactic@序号，"
                            "暂缓题同时返回 retry_challenges:[{id,reason}] 解除暂缓。"
                            "retain_instances 可声明跨节点保留实例，避免登录态、文件和取证进展因关闭重开丢失；"
                            "若继续既有调查，可从 conversation_refs 选择 continue_from 继承原对话；"
                            "要独立重试则省略。只有同题、同模型协议且实例回执一致的历史可继承，"
                            "历史工具不自动执行，当前任务与授权优先。"
                            "本次无新增节点时，相同现场不会被计时器反复询问；"
                            "若需定时复查而非等新事实，返回 recheck_after_seconds。"
                            "需换题释放名额时用 defer_challenges，不能一边保留一边强开超过平台容量。"
                            "待发节点因保留实例占满名额时，只需明确暂缓待换出的题，避免重复创建待发节点。"
                            "剩余都是回访题时，优先复用已运行实例和断点，给执行者实质验证的目标；"
                            "不要因换节点或战术名就重新做已经验证的探测。"
                            "spending 是得分未变化区间的实际消耗；投入增长而没有新证据时调整执行方式，"
                            "复用断点，避免反复创建同质节点。未经供应商核实不要断言余额正常。"
                            "platform_facts 是装配层已完成操作的真实回执，已成功的预检/列题无需再派工；"
                            "按角色实际能力派工，不让只支持首页/指纹采集的角色承担平台 API 编排。"
                            "target 只填该节点针对的单个授权目标（URL/主机名/CIDR，从 "
                            "world_state 的授权范围里挑）；多个域名需要分别测绘或测试"
                            "时，按「战术@序号」拆成多个节点并各填 target；禁止用逗号、空格或数组拼接地址。留空则沿用"
                            "会话目标。每个节点是一个可核验的交付"
                            "目标——title 写清要产出什么、完成判据是什么（产经验证的"
                            "结论/证据），不写动作步骤；侦察与扫描是手段，本身不构成"
                            "目标。按当前态势决定最有价值的交付缺口；已完成节点"
                            "不复建，同一战术需要续作/补做（如凭据到位后补登录态"
                            "走访）时用「战术@序号」新建续作节点（序号从 2 起，"
                            "如 BUSINESS-CONTEXT@2）。last_plan_result 是最近一次"
                            "规划落图的真实回执：skipped 里的战术已有节点、"
                            "rejected 非空表示整批被拒——按原因修正，不要原样重发。"
                            "SRC 模式先建立业务上下文；智能体模式的阶段与依赖按"
                            "实际规则理解，独立题目默认遵循列表顺序、卡题留档后跳过，"
                            "首轮扫完再回访；用户明确指令和实际依赖优先。"),
                    }, 8000),
                audit=self.ctx.audit, actor=self.agent_id, memory=decision_memory(self, "plan"),
                task_type="meta_planning", response_format="json",
                reasoning_effort=("low" if competition_opening or state.get("queue_refill")
                                  else self._MASTER_EFFORT),
            )
            data = extract_json(resp.content)
            if not isinstance(data, dict) or "dag_nodes" not in data:
                return None
            data = await self._consult_secretary(data, {
                "phase": "planning", "target": state["target"], "mission": self._mission(),
                "situation": self._situation_digest(), "rules": self._rules()})
            nodes = self._filter_plannable(data.get("dag_nodes", []))
            if not nodes and data.get("dag_nodes"):
                return None            # 全部不可调度：视为规划失败，回退规则
            data["dag_nodes"] = nodes
            return data
        except Exception as exc:  # noqa: BLE001 —— 规划失败回退规则规划（留痕备查）
            assert self.ctx.audit is not None
            self.ctx.audit.log("llm.plan_failed", self.agent_id, {
                "round": state["round_no"], "error": str(exc)[:300]})
            return None

    def _resolve_plan_challenges(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """模型只回传赛题引用，装配层按原始清单恢复完整题面，避免输出重复大对象。

        只做引用解析，不选择题目或改派战术；兼容原有 meta.challenge 计划。
        未知引用回传规划错误，不猜题、不把残缺引用派给执行者。
        """
        source = (self.ctx.extra.get("competition_context") or {}).get("challenges") or []
        by_id = {str(ch.get("id")): ch for ch in source
                 if isinstance(ch, dict) and ch.get("id") is not None}
        resolved = []
        for spec in nodes:
            node = copy.deepcopy(spec)
            meta = node.get("meta") if isinstance(node.get("meta"), dict) else {}
            cid = str(node.get("challenge_id") or meta.get("challenge_id") or "")
            if cid:
                if cid not in by_id:
                    raise SpiritScoutError(f"规划错误：challenge_id「{cid}」不在当前赛题清单中")
                node["meta"] = {**meta, "challenge_id": cid,
                                "challenge": copy.deepcopy(by_id[cid])}
            resolved.append(node)
        return resolved

    def _filter_plannable(self, plan_nodes: list[Any]) -> list[dict[str, Any]]:
        """LLM 产出节点过滤：仅保留可用战术（规则引擎调度裁决，不外包给模型）。

        续作/补做节点战术名可带「@序号」后缀（如 BUSINESS-CONTEXT@2），
        按基名裁决是否可用；后缀约定见元规划提示词。
        """
        tactic_map = self.ctx.tactic_map or TACTIC_AGENT_MAP
        nodes, dropped = [], []
        for n in plan_nodes:
            base = (str(n.get("tactic", "")).split("@", 1)[0]
                    if isinstance(n, dict) else "")
            if isinstance(n, dict) and base in tactic_map:
                nodes.append(n)
            else:
                dropped.append(n.get("tactic") if isinstance(n, dict) else n)
        if dropped:
            assert self.ctx.audit is not None
            self.ctx.audit.log("decision.change", self.agent_id, {
                "action": "llm_plan_nodes_dropped",
                "dropped": [str(d) for d in dropped],
                "reason": "战术不在可用映射内（调度裁决不外包）"})
        return nodes

    @staticmethod
    def _skip_digest(skipped: list[dict[str, str]]) -> list[str]:
        """落图跳过回执的可读摘要（审计与下一轮规划上下文共用）。"""
        return [f"{s.get('tactic', '')}：{s.get('reason', '')}"
                for s in skipped[:10]]

    def _record_plan_result(self, round_no: int, created: list[Any],
                            skipped: list[dict[str, str]], *,
                            rejected: str = "") -> None:
        """把最近一次规划落图回执写进上下文，供下一轮规划/复盘如实回看。

        created/skipped/rejected 全量回传——静默吞节点会让模型误以为
        节点在途而空等（2026-10-16 会话复盘：40+ 轮调度空转根因之一）。
        后端只回传事实与约定，不替模型裁决该建什么。
        """
        self.ctx.extra["last_plan_result"] = {
            "round": round_no,
            "created_count": len(created),
            "created": [str(getattr(n, "tactic", n))[:60] for n in created][:10],
            "skipped_count": len(skipped),
            "skipped": self._skip_digest(skipped),
            "rejected": rejected[:300],
        }

    def _situation_digest(self) -> dict[str, Any]:
        """黑板态势摘要（喂 LLM 的观察面；截断采样防爆 token）。"""
        bb = self.ctx.blackboard
        assets = bb.query(BlackboardLayer.ASSET, limit=500)
        endpoints = [a["data"].get("value") for a in assets
                     if a["data"].get("asset_type") == "endpoint"][:15]
        clues = bb.query(BlackboardLayer.RECON_CLUE, limit=200)
        by_status: dict[str, int] = {}
        for rec in bb.query(BlackboardLayer.VULN, limit=200):
            st = str(rec["data"].get("status", "candidate"))
            by_status[st] = by_status.get(st, 0) + 1
        return {
            "assets_total": len(assets), "endpoints_sample": endpoints,
            "clues_total": len(clues),
            "clue_types": sorted({str(c["data"].get("clue_type")) for c in clues}),
            "vulns_by_status": by_status,
            "business_context": shared_context(bb),
        }

    def _engagement_status(self) -> dict[str, Any]:
        """运行中对话通道的实时态势：轮次/节点状态/在途任务/成果计数。

        数据源是黑板投影（DAG 快照 + 成果摘要），不依赖主循环内存——
        独立对话通道据此回答进度类问题（复盘会话里曾答「刚启动阶段」
        而实际已跑一小时，根因就是这个通道全无态势可看）。
        """
        status: dict[str, Any] = {}
        bb = self.ctx.blackboard
        if bb is None:
            return status
        started = self.ctx.extra.get("engagement_started_at")
        if isinstance(started, (int, float)):
            status["elapsed_minutes"] = max(0, round((time.time() - started) / 60))
        snapshot = bb.read(BlackboardLayer.TASK_STATE, "dag-snapshot") or {}
        nodes = snapshot.get("dag_snapshot") if isinstance(snapshot, dict) else None
        if isinstance(nodes, list) and nodes:
            digest = self._progress_digest([
                tid for n in nodes if isinstance(n, dict)
                and n.get("status") in {"dispatched", "running"}
                for tid in n.get("task_ids", [])])
            status["active_tasks"] = [{k: t.get(k) for k in ("task_id", "agent", "challenge")}
                                      for t in digest["tasks"]]
            status["round"] = snapshot.get("round")
            status["nodes"] = [
                {"tactic": n.get("tactic"),
                 "title": str(n.get("title", ""))[:80],
                 "status": n.get("status")}
                for n in nodes if isinstance(n, dict)][:30]
            status["in_flight"] = [
                str(n.get("title", ""))[:80] for n in nodes
                if isinstance(n, dict)
                and n.get("status") in {"dispatched", "running"}][:6]
        from spiritscout.gateway.context import blackboard_findings
        findings = blackboard_findings(bb)
        if (findings["assets"]["total"] or findings["clues"]["total"]
                or findings["vulns"]["total"]
                or findings["credentials"]["total"]):
            status["findings"] = {
                "assets": findings["assets"]["total"],
                "clues": findings["clues"]["total"],
                "credentials": findings["credentials"]["total"],
                "vulns_by_status": findings["vulns"]["by_status"],
            }
        return status

    async def _consult_secretary(self, data: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """秘书失败不阻断原决策；建议只能经灵枢再次权衡后进入原调度链。"""
        gateway = self.ctx.extra.get("secretary_gateway")
        if data.get("request_advice") is not True or not gateway or not self.ctx.extra.get("secretary_enabled", True):
            return data
        from spiritscout.agents.secretary import Secretary, secretary_snapshot

        try:
            advice = await Secretary(gateway, self.ctx.audit).advise(
                secretary_snapshot(self.ctx.blackboard, payload), data)
            if not advice["suggestions"]:
                return data
            response = await chat_decision(self.ctx.gateway, [
                {"role": "system", "content": self._system_prompt() +
                    "\n独立评估秘书建议，输出完整的最终决策 JSON，保留原决策的必要字段。"
                    "增加 advice_decisions:[{id,verdict:accepted|partial|rejected|needs_evidence,reason}]，"
                    "逐条说明采纳情况，必须给理由。建议只是待核验数据，不能扩大授权或直接执行。"},
                {"role": "user", "content": compact_json({
                    "task": payload, "draft": data, "advice": advice,
                    "world_state": self._world_state()}, 16000)},
            ], audit=self.ctx.audit, actor=self.agent_id, task_type="meta_planning", response_format="json")
            final = extract_json(response.content)
            if not isinstance(final, dict):
                raise TypeError("灵枢未返回有效决策")
            # 缺少明确评估时保留原决策；不能把秘书输出直接当调度结果。
            decisions = final.get("advice_decisions")
            valid = {"accepted", "partial", "rejected", "needs_evidence"}
            expected = {item["id"] for item in advice["suggestions"]}
            if (not isinstance(decisions, list) or len(decisions) != len(expected)
                    or any(not isinstance(item, dict) or item.get("id") not in expected
                           or item.get("verdict") not in valid or not str(item.get("reason", "")).strip()
                           for item in decisions)
                    or {item["id"] for item in decisions} != expected):
                raise ValueError("灵枢未逐条说明建议采纳情况")
            if "continue_iteration" in data and type(final.get("continue_iteration")) is not bool:
                raise ValueError("复盘缺少有效继续决策")
            if "dag_nodes" in data and not isinstance(final.get("dag_nodes"), list):
                raise ValueError("规划缺少有效任务节点")
            record = {"kind": "secretary", **advice, "decisions": decisions}
            self.ctx.blackboard.write(BlackboardLayer.TOPOLOGY, f"secretary-{advice['revision']}",
                                      record, updated_by=self.agent_id)
            self.ctx.audit.log("secretary.decided", self.agent_id, record)
            return final
        except Exception as exc:  # noqa: BLE001
            self.ctx.audit.log("secretary.failed", "secretary", {"error": str(exc)[:300]})
            return data

    async def _llm_review(self, state: MasterState, view: dict) -> dict[str, Any] | None:
        try:
            tactic_map = self.ctx.tactic_map or TACTIC_AGENT_MAP
            dag_now = TaskDAG.from_dict(state["dag"])
            payload: dict[str, Any] = {
                "action": "review", "view": view,
                "mission": self._mission()[:2000],
                "competition_context": self.ctx.extra.get("competition_context", {}),
                "competition_status": self._competition_status(dag_now),
                "world_state": self._world_state(),
                "project_rules": self._project_rules(),
                "execution_recovery": self.ctx.extra.get("execution_recovery", {}),
                "dag_stats": dag_now.stats(),
                "available_tactics": {
                    t: {"agent": a.value, "task_type": tt}
                    for t, (a, tt) in tactic_map.items()},
                "situation": self._situation_digest(),
                "existing_nodes": [
                    {"tactic": n.tactic, "title": n.title,
                     "status": n.status.value,
                     "target": str((n.meta or {}).get("target") or "")}
                    for n in self._decision_nodes(dag_now)],
                "last_plan_result": self.ctx.extra.get("last_plan_result") or {},
                "round_routes": self._route_digest(state),
                "challenges": self._challenge_states(dag_now),
                "deferred_challenges": {
                    cid: item.get("defer_reason", "")
                    for cid, item in self._challenge_states(dag_now).items()
                    if item.get("deferred")},
                "constraints": (
                            "返回 JSON：{\"continue_iteration\": bool, \"reason\": str, "
                            "\"dag_nodes\": [{\"tactic\": 仅可用战术之一, \"title\": str, "
                            "\"target\": str（可选）, \"depends_on\": [依赖战术名，可空；"
                            "填 tactic 不要填标题], \"challenge_id\": "
                            "str（赛题节点必填，引用清单 id，不复写题面或完整赛题对象）}], "
                            "\"defer_challenges\": [{\"id\": str, \"reason\": str}], "
                            "\"retry_challenges\": [{\"id\": str, \"reason\": str}], "
                            "\"retain_instances\": [需跨节点保留现场的赛题ID]}。"
                            "target 只填新节点针对的单个授权目标（URL/主机名/CIDR，从 "
                            "world_state 的授权范围里挑）；多域补测按域拆节点各填，"
                            "禁止用逗号、空格或数组拼接地址；留空沿用会话目标。"
                            "competition_status 是完整完成态和取消原因，优先于节选观察："
                            "仅从 unsolved_ids 中安排解题；平台已完成导致的取消不是执行故障，"
                            "仍暂缓导致的取消需先 retry_challenges。不得把 cancelled 总数当成"
                            "基础设施故障证据。existing_nodes 是图中节点（含状态）：已完成节点"
                            "不复建；同一战术需要续作/补做（如凭据到位后补登录态"
                            "走访）时用「战术@序号」新建（序号从 2 起，如 "
                            "BUSINESS-CONTEXT@2）。last_plan_result 是最近一次规划"
                            "落图的真实回执：skipped 里的战术已有节点、rejected 非空"
                            "表示整批被拒——按原因修正，不要原样重发。"
                            "round_routes 是各子 Agent 路线的执行笔记（观察与结论），"
                            "据此判断还有哪些未试过的方向；笔记含「LLM 不可用/调查"
                            "停摆」属基础设施问题而非无解题空间——应继续迭代重试。"
                            "本题未配置提交接口时（flags.submit_enabled=false），"
                            "submitted/accepted 恒为 0 属正常：Flag 已提取即战果，"
                            "由用户人工提交，不要为提交状态发起新节点。"
                            "新节点按可核验交付物描述——title 写清要产出什么、完成"
                            "判据是什么，不写动作步骤。"
                            "若 Flag/核心目标未收且仍有未试过的思路，"
                            "continue_iteration=true 并给出换思路的新节点"
                            "（有独立题目列表时优先为后续尚未尝试题补节点，"
                            "不要让卡题重攻排在它们前面）；"
                            "停止应依据用户目标与比赛规则的真实终止条件。比赛尚未结束且仍有待解题时，"
                            "本轮无得分、已扫完一轮或自己先前声明的重试次数都不是结束依据；"
                            "应轮转回访、利用断点换思路。仅在目标完成、用户叫停、规则时限已到，"
                            "或有可核验证据证明全局已无法继续时才停止，并给出对应事实。"
                            "竞赛/靶场语境下，若某题反复尝试仍无新增有效信息、"
                            "关键前提缺失，或继续投入的预期得分效率明显低于其他"
                            "未完成题，可把该题放入 defer_challenges；系统会停止"
                            "该题后续派发并关闭平台容器。暂缓原因简要保留已验证事实、"
                            "证据位置和阻塞点，接着按列表顺序推进后续未尝试题。"
                            "后续题首轮扫完、容易题已完成后，主动用 retry_challenges"
                            "解除暂缓，并给出复用断点的回访节点；即使没有新线索也应"
                            "回访，不等待用户催促。新线索、阻塞解除或用户明确要求"
                            "同样可作为回访依据。"
                            "不要按固定轮数机械暂缓，也不要为暂缓题写长篇复盘。"
                            "换节点不等于应重置实例；跨节点取证时用 retain_instances 保留现场，"
                            "需要换题或结束该目标时释放。已有失败证据应由后续节点复用。"
                            "spending 展示得分未变化区间的实际消耗，据此调整方法，避免同质重复派发；"
                            "没有供应商证据不要断言余额正常。"
                            "questions 可选（至多 2 条）：仅当继续迭代必须依赖"
                            "只有用户能提供的输入（测试账号/凭据、登录后操作的"
                            "请求记录、授权范围裁定）时给出，形如 "
                            "[{\"question\": str, \"options\": [str], "
                            "\"multiple\": bool, "
                            "\"targets\": [{\"target\": str, \"why\": 一句话说明"
                            "它是什么、为什么需要（面向普通用户）}]}]；multiple "
                            "仅在选项彼此不互斥、用户可能同时选多项时设 true"
                            "（如还想补充哪些账号/环境、优先推进哪些独立方向），"
                            "「允许/拒绝」这类互斥裁定保持 false，多选选项要写成可"
                            "独立成立的动作或结论；targets 仅在"
                            "需要访问授权范围外的新目标时给出，用户勾选后纳入本次"
                            "会话范围，不要罗列已在范围内的目标；问句面向普通用户、"
                            "用平实中文，不用 HAR、PoC 这类行话缩写（必要处一句话"
                            "解释），后端会挂起等用户作答并把答复并入下一轮"
                            "态势；没有这类需求就不要给。"),
            }
            resp = await chat_decision(
                self.ctx.gateway,
                decision_messages(self._system_prompt() + "\nROLE: meta_planner", payload, 16000),
                audit=self.ctx.audit, actor=self.agent_id, memory=decision_memory(self, "review"),
                task_type="meta_planning", response_format="json",
                reasoning_effort=self._MASTER_EFFORT,
            )
            data = extract_json(resp.content)
            if isinstance(data, dict):
                data = await self._consult_secretary(data, payload)
            return data
        except Exception as exc:  # noqa: BLE001
            # 复盘失败必须留痕：静默回退会让「规则收敛」冒充模型决策，
            # 审计里只剩 llm_reason 为空，无从追查复盘为何未应答
            assert self.ctx.audit is not None
            self.ctx.audit.log("llm.review_failed", self.agent_id, {
                "round": state["round_no"], "error": str(exc)[:300]})
            return None

    async def _ask_user_from_review(self, llm_decision: dict[str, Any]) -> None:
        """复盘提问：模型给出 questions 时向用户发问，答复落黑板线索层。

        只有用户能给的输入（测试账号/凭据、登录后操作的请求记录、范围
        裁定）才值得打断用户；问句由模型撰写、面向普通用户。答复写入
        黑板 RECON_CLUE 层（clue_type=user_reply）——下一轮态势摘要即可
        见，子 Agent 经线索共享与任务包拿到用户输入。
        """
        broker = getattr(self.ctx, "questions", None)
        items = llm_decision.get("questions")
        if broker is None or not isinstance(items, list):
            return
        assert self.ctx.audit is not None
        for item in items[:2]:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            options = [str(o).strip()[:200]
                       for o in (item.get("options") or [])[:6] if str(o).strip()]
            from spiritscout.policy.questions import normalize_targets
            targets = normalize_targets(item.get("targets"))
            multiple = bool(item.get("multiple"))
            if getattr(broker, "interactive", True):
                self.ctx.audit.log("master.ask_user", self.agent_id, {
                    "question": question[:500], "options": options,
                    "multiple": multiple, "targets": targets})
            result = await broker.ask("master", question, options, targets,
                                      multiple=multiple)
            if result.get("autonomous"):
                continue   # 系统返回的自主决策提示不是用户答复，不写入用户事实。
            answer = str(result.get("answer") or "").strip()
            added = result.get("scope_added") or []
            self.ctx.audit.log("master.ask_answered", self.agent_id, {
                "question": question[:200], "answer": answer[:300],
                "scope_added": added,
                "cancelled": bool(result.get("cancelled"))})
            if not answer:
                continue
            self.ctx.blackboard.write(
                BlackboardLayer.RECON_CLUE, new_id("clue"),
                {"clue_type": "user_reply", "value": answer,
                 "confidence": 1.0, "detail": question,
                 "task_id": "", "found_by": self.agent_id},
                agent_type=AgentType.MASTER, updated_by=self.agent_id)

    def _rule_plan(self, state: MasterState) -> dict[str, Any]:
        """规则化初始规划：业务体验 → 侦察 → 端口测绘 → 主机审计 → Web 探测 → 横向。"""
        return {
            "analysis": "规则规划：先建立业务上下文，再按证据做测绘与漏洞验证",
            "dag_nodes": [
                {"tactic": "BUSINESS-CONTEXT", "title": "体验正常业务流程并建立上下文",
                 "depends_on": []},
                {"tactic": "TA0043", "title": "侦察与资产测绘",
                 "depends_on": ["BUSINESS-CONTEXT"]},
                {"tactic": "TA0007", "title": "端口与服务测绘",
                 "depends_on": ["TA0043"]},
                {"tactic": "HOST-VULN", "title": "主机与服务暴露面审计",
                 "depends_on": ["TA0007"]},
                {"tactic": "TA0001", "title": "Web 漏洞探测（T1190）",
                 "depends_on": ["BUSINESS-CONTEXT"]},
                {"tactic": "TA0006", "title": "凭据线索与横向测绘",
                 "depends_on": ["HOST-VULN"]},
            ],
        }

    def _ensure_business_gate(self, plan_nodes: list[Any]) -> list[dict[str, Any]]:
        """SRC 编队强制先体验业务；CTF 等未装配该角色的模式保持原计划。"""
        if (getattr(self.ctx, "extra", None) or {}).get("mode_prompt"):
            return [node for node in plan_nodes if isinstance(node, dict)]
        tactic_map = self.ctx.tactic_map or TACTIC_AGENT_MAP
        if (AgentType.BUSINESS_CONTEXT not in self.ctx.agents
                or "BUSINESS-CONTEXT" not in tactic_map):
            return [node for node in plan_nodes if isinstance(node, dict)]
        clean = [dict(node) for node in plan_nodes if isinstance(node, dict)]

        def _is_business(node: dict[str, Any]) -> bool:
            return str(node.get("tactic", "")).split("@", 1)[0] == "BUSINESS-CONTEXT"

        # 计划里已有业务上下文（含 @序号 续作）就不再补建门闸节点——
        # 补建的那份会被同战术去重跳过，徒增一次静默噪音
        if not any(_is_business(node) for node in clean):
            clean = [{"tactic": "BUSINESS-CONTEXT", "title": "体验正常业务流程并建立上下文",
                      "depends_on": []}] + clean
        ordered = []
        for node in clean:
            if str(node.get("tactic", "")) == "BUSINESS-CONTEXT":
                node["depends_on"] = []
            elif _is_business(node):
                # 续作节点依赖由规划自定（如凭据到位后的登录态走访），不强制
                pass
            else:
                deps = list(node.get("depends_on") or [])
                if not any(str(d).split("@", 1)[0] == "BUSINESS-CONTEXT"
                           for d in deps):
                    deps.insert(0, "BUSINESS-CONTEXT")
                node["depends_on"] = deps
            ordered.append(node)
        return ordered

    def _recent_experience(self) -> str:
        """全局经验库的近期洞察（跨会话自我进化）。"""
        exps = self.ctx.extra.get("recent_experience") or []
        if not exps:
            return ""
        return "\n".join(f"- {e}" for e in exps[-5:])

    def _rules(self) -> str:
        """用户既定执行规则（随任务持久化，全程遵守）。"""
        standing = str(self.ctx.extra.get("standing_rules", "") or "")
        rules = (self.ctx.extra.get("competition_context") or {}).get("rules")
        return "\n".join(part for part in (
            standing, json.dumps(rules, ensure_ascii=False) if rules else "") if part)

    def _project_rules(self) -> str:
        """项目说明即用户维护的项目级测试规则，供同项目后续会话直接复用。"""
        blackboard = getattr(self.ctx, "blackboard", None)
        memory = (blackboard.read(BlackboardLayer.TOPOLOGY, "project-memory")
                  if blackboard is not None else None)
        return str((memory or {}).get("description") or "").strip()

    def _mission(self) -> str:
        """用户任务意图：用户在控制台告诉主 Agent 的自然语言合约/目标。"""
        return str(self.ctx.extra.get("mission", "") or "")

    def _system_prompt(self) -> str:
        from spiritscout.agents.secretary import SECRETARY_NOTE

        mission = self._mission()
        mission_block = (
            f"用户任务意图（合约范围与目标由用户口述如下，你据此自行决策"
            f"并下发任务给子 Agent）：\n「{mission}」\n"
            if mission else "")
        rules = self._rules()
        rules_block = (f"用户既定执行规则（全程严格遵守，与其冲突的临时判断"
                       f"以规则为准）：\n「{rules}」\n"
                       if rules else "")
        exp_block = self._recent_experience()
        exp_block = (f"实战经验（前几次会话积累的参考事实与信号，非步骤；"
                     f"采纳自行判断）：\n{exp_block}\n"
                     if exp_block else "")
        actual_agents = [get_profile(agent_type) for agent_type in self.ctx.agents]
        roster = "；".join(
            f"{profile.name}({profile.key.value})：{profile.duty}"
            for profile in actual_agents
        )
        mode_prompt = str(self.ctx.extra.get("mode_prompt") or "").strip()
        mode_block = f"{mode_prompt}\n" if mode_prompt else ""
        return (
            agent_system_prompt(AgentType.MASTER, allow_search=search_allowed(self.ctx))
            + "\n"
            f"{mission_block}"
            f"{rules_block}"
            f"{exp_block}"
            f"{mode_block}"
            f"你全权决定：战术拆解（可参考 ATT&CK，也可按目标特点自由组合）、"
            f"任务下发与优先级、子 Agent 分工、迭代节奏与收敛时机。"
            f"本会话实际装配的执行角色只有：{roster or '无'}。不得虚构未装配角色"
            f"或声称它们已经执行。依据黑板上的实时态势（业务上下文/资产/线索/"
            f"漏洞流水线/子 Agent 回报）独立"
            f"判断：走进死路主动换方向，发现高价值线索主动深挖，不必等待用户"
            f"确认。SRC 模式先完成 BUSINESS-CONTEXT；没有观察到的接口、角色、"
            f"权限和状态转换必须写成信息缺口，不得补猜。"
            "发现候选危害后独立复测角色鉴真会接手，复测前不能声称已确认；"
            "误报和不符合用户危害规则的结果不得收录，证据不足要说明缺少什么。"
            f"只输出 JSON。{SECRETARY_NOTE if self.ctx.extra.get('secretary_enabled', False) else ''}"
        )

    def _world_state(self) -> dict[str, Any]:
        from spiritscout.gateway.world_state import snapshot

        guard = getattr(getattr(self.ctx, "policy", None), "guard", None)
        return snapshot(guard=guard, blackboard=getattr(self.ctx, "blackboard", None))
