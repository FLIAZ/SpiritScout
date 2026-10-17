"""主 Agent：LangGraph 元决策状态机（执行→汇聚→决策→调度闭环，5.1）。

状态机节点：
    meta_plan  元规划：目标拆解/动态补 DAG 节点/生成本轮任务包
    dispatch   调度：任务包并发分发给环形子 Agent 集群
    aggregate  汇聚：结果汇聚引擎产出全局视图
    review     复盘：全局分析、漏洞终审、迭代/收敛决策 ─┐
        └────────── continue_iteration ────────────────┘
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import operator
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.profiles import agent_system_prompt, get_profile
from spiritscout.agents.sub.base import SubAgent
from spiritscout.blackboard import ConvergenceEngine
from spiritscout.blackboard.store import Blackboard
from spiritscout.bus.base import EventBus
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import SpiritScoutError
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

    # 灵枢自身的思考强度：固定取最高档，不做动态裁定（动态裁定只用于
    # 子 Agent——见 _assign_dispatch_plan）。模型未声明该档位时按端点
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
        # 授权写入黑板目标与授权层（4.8.1）
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

    async def _meta_plan(self, state: MasterState) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx.audit is not None
        await self._gate_wait()
        dag = TaskDAG.from_dict(state["dag"])
        round_no = state["round_no"]
        plan_source = "llm"

        if round_no == 0 or self._needs_replan(dag, state):
            if ctx.plan_override is not None:
                plan = ctx.plan_override(state)
                plan_source = "mode_assembly"
            else:
                plan = await self._llm_plan(state) if ctx.enable_llm_planning else None
                if plan is None:
                    plan = self._rule_plan(state)
                    plan_source = "rule"
            plan["dag_nodes"] = self._ensure_business_gate(
                plan.get("dag_nodes", []))
            try:
                result = dag.upsert_from_plan(plan.get("dag_nodes", []), round_no)
            except SpiritScoutError as exc:
                # 计划不可落图（未知依赖/成环）：明确落痕后回退规则计划，
                # 不让一次坏规划卡死整轮
                ctx.audit.log("decision.change", self.agent_id, {
                    "action": "plan_rejected", "round": round_no,
                    "source": plan_source, "reason": str(exc)[:200]})
                self._record_plan_result(round_no, [], [], rejected=str(exc))
                if plan_source != "llm":
                    raise
                plan = self._rule_plan(state)
                plan_source = "rule"
                plan["dag_nodes"] = self._ensure_business_gate(
                    plan.get("dag_nodes", []))
                result = dag.upsert_from_plan(plan.get("dag_nodes", []), round_no)
            self._record_plan_result(round_no, result.created, result.skipped)
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
        task_objs: list[tuple[Task, str]] = []
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
            if challenge:
                node.meta["challenge"] = challenge
            task = Task(
                task_id=new_id("task"), task_type=task_type,
                target=str(challenge.get("url") or state["target"]),
                agent_type=agent_type, round_no=round_no,
                parent_dag_node=node.node_id,
                payload={"tactic": node.tactic, "technique": node.technique or "",
                         "node_title": node.title,
                         "mission": self._mission(),
                         "execution_recovery": self.ctx.extra.get("execution_recovery", {}),
                         "rules": self._rules()[:1500],
                         "recent_experience": (self.ctx.extra.get("recent_experience") or [])[-3:],
                         "challenge": challenge or None},
            )
            dag.attach_task(node.node_id, task.task_id)
            dag.mark(node.node_id, TaskStatus.RUNNING)
            task_objs.append((task, node.node_id))

        # 思考强度/路线预算的下发决策不再挡在开工路径上（见 _dispatch：
        # 与子 Agent 执行并行裁定，落定后即时生效）
        tasks = [{"task": t.model_dump(mode="json"), "node_id": nid}
                 for t, nid in task_objs]

        self._write_dag_snapshot(dag, round_no)
        return {"dag": dag.to_dict(), "tasks_dispatched": tasks,
                "plan_source": plan_source}

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
        if ctx.ring is not None and ctx.enable_llm_planning and round_tasks:
            watch = asyncio.create_task(
                self._steering_watch([t.task_id for t in round_tasks]))
        try:
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
        self._write_dag_snapshot(dag, state["round_no"])
        return {"dag": dag.to_dict()}

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
                                    rationale=item.get("rationale", ""))
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
        payload = {
            "mission": self._mission()[:1000],
            "agents": [a.value for a in self.ctx.agents],
            "roster": roster,
            "engagement_paused": paused,
            "situation": self._engagement_status(),
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
                "无需调整则 steering 给空列表。返回 JSON：{\"reply\": str, "
                "\"steering\": [{\"agent\": str, \"hint\": str, "
                "\"rationale\": str}]}"),
        }
        try:
            resp = await self.ctx.gateway.chat(
                [
                    {"role": "system", "content": self._system_prompt()
                     + "\nROLE: user_chat"},
                    {"role": "user",
                     "content": compact_json(payload, max_chars=16000)},
                ],
                task_type="meta_planning", response_format="json",
                reasoning_effort="low",   # 对话应答要快，不与重推理抢档
            )
            data = extract_json(resp.content) or {}
            reply = str(data.get("reply") or "").strip()[:4000]
            valid = {a.value for a in self.ctx.agents}
            steering = [
                {"agent": str(i["agent"]), "hint": str(i["hint"])[:300],
                 "rationale": str(i.get("rationale", ""))[:200]}
                for i in (data.get("steering") or [])
                if isinstance(i, dict) and i.get("agent") in valid
                and isinstance(i.get("hint"), str) and i["hint"].strip()]
            return reply, steering
        except (SpiritScoutError, OSError, ValueError, TypeError) as exc:
            # 对话通道失败不影响主链路，但留痕可见
            self.ctx.audit.log("master.reply_failed", self.agent_id,
                               {"error": str(exc)[:200]})
            return "", []

    # ------------------------------------------------------------ 环形督导

    _STEER_INTERVAL = 2.0   # 观察节拍（秒）；无进展变化不调用 LLM
    _STEER_MAX_CALLS = 3    # 单轮派发内督导 LLM 调用上限（预算护栏）

    def _drain_user_feed(self) -> list[dict[str, Any]]:
        """取走用户会话中发来的消息与附件记录（用户直达通道的消费点）。"""
        inbox = self.ctx.extra.get("user_inbox")
        if not inbox:
            return []
        items = list(inbox)
        inbox.clear()
        return items

    async def _steering_watch(self, task_ids: list[str]) -> None:
        """派发期并发督导：读各任务实时路线笔记 → LLM 判读全局 → 写方向建议。

        确定性边界：本函数只做「读进度、问模型、投递建议」；建议是否采纳由
        子 Agent 的 LLM 在其决策上下文中自行权衡，执行仍走原确定性通道。
        用户消息不在这里消费——它们走独立的直达通道（_user_chat_watch），
        两条通道并行互不阻塞。
        """
        assert self.ctx.audit is not None
        last_fingerprint = ""
        calls = 0
        while True:
            await asyncio.sleep(self._STEER_INTERVAL)
            digest = self._progress_digest(task_ids)
            if not digest["tasks"]:
                continue
            fingerprint = json.dumps(digest, ensure_ascii=False, sort_keys=True)
            if fingerprint == last_fingerprint or calls >= self._STEER_MAX_CALLS:
                continue
            last_fingerprint = fingerprint
            steering = await self._llm_steer(digest)
            calls += 1
            for item in steering:
                self.ctx.ring.steer(item["agent"], item["hint"],
                                    rationale=item.get("rationale", ""))

    def _progress_digest(self, task_ids: list[str]) -> dict[str, Any]:
        """本轮任务的实时进度摘要（子 Agent 随步上报的路线笔记）。"""
        bb = self.ctx.blackboard
        tasks: list[dict[str, Any]] = []
        for tid in task_ids:
            data = bb.read(BlackboardLayer.TASK_STATE, tid)
            if data is None:
                continue
            task = data.get("task", {})
            routes = [
                {"route": r.get("name"), "status": r.get("status"),
                 "progress": r.get("progress"),
                 "last_note": (r.get("notes") or [""])[-1][:120]}
                for r in data.get("routes", [])
            ]
            tasks.append({"agent": task.get("agent_type"),
                          "title": str(task.get("title", ""))[:80],
                          "routes": routes})
        return {"tasks": tasks}

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

    async def _llm_steer(self, digest: dict[str, Any]) -> list[dict[str, Any]]:
        """LLM 判读全局进度，产出需调整方向的子 Agent 建议（白名单校验）。"""
        valid_agents = sorted({t["agent"] for t in digest["tasks"]
                               if t.get("agent")})
        if not valid_agents:
            return []
        try:
            payload: dict[str, Any] = {
                "action": "steer",
                "mission": self._mission()[:1000],
                "business_context": shared_context(self.ctx.blackboard),
                "progress": digest,
                "agents": valid_agents,
                "constraints": (
                            "你在实时督导并行作业的子 Agent。依据各路线进度与最新"
                            "笔记判断：谁走进了死胡同（建议换方向）、谁发现了高价值"
                            "线索（建议深挖或提醒同伴接力）、谁在同质重复做功"
                            "（建议避开同伴已覆盖的入口）。只对有明确调整必要的 "
                            "Agent 给建议，一切正常则返回空列表。返回 JSON："
                            "{\"steering\": [{\"agent\": 仅 agents 列表之一, "
                            "\"hint\": 一句话方向建议, \"rationale\": 判断依据}]}"),
            }
            resp = await chat_decision(
                self.ctx.gateway,
                [
                    {"role": "system", "content": self._system_prompt()
                     + "\nROLE: collab_judge"},
                    {"role": "user", "content": compact_json(payload, max_chars=16000)},
                ],
                audit=self.ctx.audit, actor=self.agent_id,
                task_type="meta_planning", response_format="json",
                reasoning_effort=self._MASTER_EFFORT,
            )
            data = extract_json(resp.content) or {}
            items = data.get("steering")
            if not isinstance(items, list):
                raise TypeError("steering 应答不是列表")
            out = [
                {"agent": str(i["agent"]), "hint": str(i["hint"])[:300],
                 "rationale": str(i.get("rationale", ""))[:200]}
                for i in items
                if isinstance(i, dict) and i.get("agent") in valid_agents
                and isinstance(i.get("hint"), str) and i["hint"].strip()
            ]
            return out
        except Exception:  # noqa: BLE001 —— 督导失败不拖垮派发
            return []

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
        flag_done = (bool(view.get("flags"))
                     and not flags_pending
                     and not dag.has_unfinished()
                     and not view.get("vuln_in_pipeline"))
        if flag_done:
            stop_reason = "核心目标已达成，全部节点完成"
        llm_decision = (await self._llm_review(state, view)
                        if ctx.enable_llm_planning and not stop_reason else None)
        if llm_decision is not None and (
                not isinstance(llm_decision, dict)
                or type(llm_decision.get("continue_iteration")) is not bool):
            # 只有明确的布尔决策能收尾；缺字段或字符串 false 不是停止指令。
            ctx.audit.log("llm.review_failed", self.agent_id, {
                "round": state["round_no"], "error": "复盘缺少有效的 continue_iteration 布尔值"})
            llm_decision = None
        deferred_replan = False
        if llm_decision and not stop_reason:
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
                        result = dag.upsert_from_plan(nodes, next_round)
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
            "llm_reason": str((llm_decision or {}).get("reason", ""))[:400],
            "view": view, "verdicts": verdicts})
        return {"dag": dag.to_dict(), "round_no": next_round,
                "continue_iteration": continue_iteration, "stop_reason": stop_reason}

    # ------------------------------------------------------------ 内部

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
        """终审：复核核验流水线结论（规则引擎再确认，模型仅建议）。"""
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
            resp = await chat_decision(
                self.ctx.gateway,
                decision_messages(self._system_prompt() + "\nROLE: meta_planner", {
                        "target": state["target"], "round": state["round_no"],
                        "mission": self._mission()[:2000],
                        "execution_recovery": self.ctx.extra.get("execution_recovery", {}),
                        "available_tactics": {
                            t: {"agent": a.value, "task_type": tt}
                            for t, (a, tt) in tactic_map.items()},
                        "situation": self._situation_digest(),
                        "existing_nodes": [
                            {"tactic": n.tactic, "title": n.title,
                             "status": n.status.value}
                            for n in dag.all_nodes()][:30],
                        "last_plan_result": self.ctx.extra.get("last_plan_result") or {},
                        "views": state["round_views"][-3:],
                        "constraints": (
                            "返回 JSON：{\"analysis\": str, \"dag_nodes\": [{\"tactic\": "
                            "仅可用战术之一, \"title\": str, \"technique\": str, "
                            "\"depends_on\": [依赖战术名，可空；填已有或本计划内节点的 "
                            "tactic，不要填标题]}]}。每个节点是一个可核验的交付"
                            "目标——title 写清要产出什么、完成判据是什么（产经验证的"
                            "结论/证据），不写动作步骤；侦察与扫描是手段，本身不构成"
                            "目标。按当前态势决定最有价值的交付缺口；已完成节点"
                            "不复建，同一战术需要续作/补做（如凭据到位后补登录态"
                            "走访）时用「战术@序号」新建续作节点（序号从 2 起，"
                            "如 BUSINESS-CONTEXT@2）。last_plan_result 是最近一次"
                            "规划落图的真实回执：skipped 里的战术已有节点、"
                            "rejected 非空表示整批被拒——按原因修正，不要原样重发。"
                            "有 BUSINESS-CONTEXT 时必须先建立业务上下文，其他"
                            "安全测试节点依赖它。"),
                    }, 8000),
                audit=self.ctx.audit, actor=self.agent_id, memory=decision_memory(self, "plan"),
                task_type="meta_planning", response_format="json",
                reasoning_effort=self._MASTER_EFFORT,
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
                {"role": "user", "content": compact_json({"task": payload, "draft": data, "advice": advice}, 16000)},
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
                "execution_recovery": self.ctx.extra.get("execution_recovery", {}),
                "dag_stats": dag_now.stats(),
                "available_tactics": {
                    t: {"agent": a.value, "task_type": tt}
                    for t, (a, tt) in tactic_map.items()},
                "situation": self._situation_digest(),
                "existing_nodes": [
                    {"tactic": n.tactic, "title": n.title,
                     "status": n.status.value}
                    for n in dag_now.all_nodes()][:30],
                "last_plan_result": self.ctx.extra.get("last_plan_result") or {},
                "round_routes": self._route_digest(state),
                "constraints": (
                            "返回 JSON：{\"continue_iteration\": bool, \"reason\": str, "
                            "\"dag_nodes\": [{\"tactic\": 仅可用战术之一, \"title\": str, "
                            "\"depends_on\": [依赖战术名，可空；填 tactic 不要填标题]}]}。"
                            "existing_nodes 是图中全部节点（含状态）：已完成节点"
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
                            "（dag_nodes 必填，否则等于收手）；"
                            "确认无解题空间时 continue_iteration=false 并说明理由。"
                            "questions 可选（至多 2 条）：仅当继续迭代必须依赖"
                            "只有用户能提供的输入（测试账号/凭据、登录后操作的"
                            "请求记录、授权范围裁定）时给出，形如 "
                            "[{\"question\": str, \"options\": [str], "
                            "\"targets\": [{\"target\": str, \"why\": 一句话说明"
                            "它是什么、为什么需要（面向普通用户）}]}]；targets 仅在"
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
            self.ctx.audit.log("master.ask_user", self.agent_id, {
                "question": question[:500], "options": options,
                "targets": targets})
            result = await broker.ask("master", question, options, targets)
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
                {"clue_type": "user_reply", "value": answer[:500],
                 "confidence": 1.0, "detail": question[:200],
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
        return str(self.ctx.extra.get("standing_rules", "") or "")

    def _mission(self) -> str:
        """用户任务意图：用户在控制台告诉主 Agent 的自然语言合约/目标。"""
        return str(self.ctx.extra.get("mission", "") or "")

    def _system_prompt(self) -> str:
        from spiritscout.agents.secretary import SECRETARY_NOTE

        scope = self.ctx.policy.guard.scope
        allowed = scope.allowed_domains + scope.allowed_cidrs + scope.allowed_urls
        mission = self._mission()
        mission_block = (
            f"用户任务意图（合约范围与目标由用户口述如下，你据此自行决策"
            f"并下发任务给子 Agent）：\n「{mission}」\n"
            if mission else "")
        rules = self._rules()
        rules_block = (f"用户既定执行规则（全程严格遵守，与其冲突的临时判断"
                       f"以规则为准）：\n「{rules[:2500]}」\n"
                       if rules else "")
        exp_block = self._recent_experience()
        exp_block = (f"实战经验（前几次会话积累的解题思维，供参考）：\n{exp_block}\n"
                     if exp_block else "")
        actual_agents = [get_profile(agent_type) for agent_type in self.ctx.agents]
        roster = "；".join(
            f"{profile.name}({profile.key.value})：{profile.duty}"
            for profile in actual_agents
        )
        return (
            agent_system_prompt(AgentType.MASTER, allowed)
            + "\n"
            f"{mission_block}"
            f"{rules_block}"
            f"{exp_block}"
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
