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
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.sub.base import SubAgent
from spiritscout.blackboard import ConvergenceEngine
from spiritscout.blackboard.store import Blackboard
from spiritscout.bus.base import EventBus
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    AgentType,
    Authorization,
    BlackboardLayer,
    Event,
    Task,
    TaskStatus,
)
from spiritscout.gateway import ModelGateway
from spiritscout.gateway.backends import extract_json
from spiritscout.gateway.registry import TASK_TIER_MAP
from spiritscout.policy.engine import PolicyEngine

# 战术 → 子 Agent 映射（3.3.6 子Agent↔战术映射；MVP 装配 recon/web_vuln）
TACTIC_AGENT_MAP: dict[str, tuple[AgentType, str]] = {
    "TA0043": (AgentType.RECON, "recon"),          # 侦察
    "TA0001": (AgentType.WEB_VULN, "web_probe"),   # 初始访问（T1190 Web 打点）
}


class MasterState(TypedDict):
    target: str
    round_no: int
    max_rounds: int
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
    max_rounds: int = 20
    default_routes: int = 3
    audit: AuditLogger | None = None
    enable_llm_planning: bool = True
    # 模式装配器注入：CTF/渗透赛覆盖战术映射与规划来源
    tactic_map: dict[str, tuple[AgentType, str]] | None = None
    plan_override: Any = None   # Callable[[MasterState], dict] 返回 {"dag_nodes": [...]}
    extra: dict[str, Any] = field(default_factory=dict)
    knowledge: Any = None       # KnowledgeStore（攻击路径模板接线，None=不接）
    ring: Any = None            # RingLedger 环形台账（与子 Agent 共持；None=不督导）

    def __post_init__(self) -> None:
        if self.audit is None:
            self.audit = NullAuditLogger()


class MasterAgent:
    """主 Agent（元规划决策层）。"""

    def __init__(self, ctx: MasterContext):
        self.ctx = ctx
        self.agent_id = new_id("master")
        self._graph = self._build_graph()

    # ------------------------------------------------------------ 图构建

    def _build_graph(self):
        graph = StateGraph(MasterState)
        graph.add_node("meta_plan", self._meta_plan)
        graph.add_node("dispatch", self._dispatch)
        graph.add_node("aggregate", self._aggregate)
        graph.add_node("review", self._review)
        graph.set_entry_point("meta_plan")
        graph.add_edge("meta_plan", "dispatch")
        graph.add_edge("dispatch", "aggregate")
        graph.add_edge("aggregate", "review")
        graph.add_conditional_edges(
            "review",
            lambda s: "continue" if s["continue_iteration"] else "end",
            {"continue": "meta_plan", "end": END},
        )
        return graph.compile()

    async def run(self, authorization: Authorization, target: str) -> dict[str, Any]:
        """启动多轮闭环迭代，直至收敛或轮次耗尽。"""
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
        initial: MasterState = {
            "target": target,
            "round_no": 0,
            "max_rounds": self.ctx.max_rounds,
            "dag": TaskDAG().to_dict(),
            "tasks_dispatched": [],
            "round_views": [],
            "continue_iteration": True,
            "stop_reason": "",
            "plan_source": "",
        }
        final = await self._graph.ainvoke(initial)
        final_dag = TaskDAG.from_dict(final["dag"])
        summary = {
            "target": target,
            "rounds_executed": final["round_no"],
            "stop_reason": final["stop_reason"],
            "dag_stats": final_dag.stats(),
            "dag_nodes": self._dag_snapshot(final_dag),
            "round_views": final["round_views"],
            "tasks_dispatched": len(final["tasks_dispatched"]),
        }
        self.ctx.audit.log("decision.change", self.agent_id,
                           {"action": "engagement_end", **summary})
        return summary

    # ------------------------------------------------------------ 节点：元规划

    async def _meta_plan(self, state: MasterState) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx.audit is not None
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
            created = dag.upsert_from_plan(plan.get("dag_nodes", []), round_no)
            ctx.audit.log("decision.change", self.agent_id, {
                "action": "meta_plan", "round": round_no, "source": plan_source,
                "new_nodes": [n.node_id for n in created],
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
            attack_paths = self._kb_attack_paths(node.tactic)
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
                         "attack_paths": attack_paths,
                         "challenge": challenge or None},
            )
            if attack_paths:
                ctx.audit.log("kb.guidance", self.agent_id, {
                    "task_id": task.task_id, "tactic": node.tactic,
                    "attack_paths": [p["path_id"] for p in attack_paths]})
            dag.attach_task(node.node_id, task.task_id)
            dag.mark(node.node_id, TaskStatus.RUNNING)
            task_objs.append((task, node.node_id))

        # 灵枢按任务复杂度逐任务决策并发分支数与思考强度（LLM 主路径，规则只做钳制）
        if task_objs and ctx.enable_llm_planning:
            await self._assign_dispatch_plan(task_objs)
        tasks = [{"task": t.model_dump(mode="json"), "node_id": nid}
                 for t, nid in task_objs]

        self._write_dag_snapshot(dag)
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
        entries = (registry.find_by_tier(TASK_TIER_MAP.get("execution", "fast"))
                   or registry.all())
        out: list[str] = []
        for e in entries:
            for level in e.reasoning_effort_levels:
                if level not in out:
                    out.append(level)
        return out

    async def _assign_dispatch_plan(self, task_objs: list[tuple[Task, str]]) -> None:
        """下发前决策（灵枢）：按任务复杂度裁定并发分支路线预算与思考强度。

        路线预算 1~8：LLM 建议，规则只做边界钳制（防扇出失控）。CTF 解题任务
        的路线预算固定为 1（单线起步，未破题由复盘轮换角度重攻）——但思考
        强度仍由 LLM 裁定，解题环正是 token 消耗大头。
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
            if t.task_type == "ctf_solve":
                t.max_routes = 1          # 单线起步是策略，不是复杂度判断
            levels = self._effort_options(t)
            options[i] = levels
            digest.append({
                "index": i, "task_type": t.task_type, "agent": t.agent_type.value,
                "target": t.target, "title": str(t.payload.get("node_title", "")),
                "route_budget_fixed": 1 if t.task_type == "ctf_solve" else None,
                "effort_options": levels,
                "challenge": {k: v for k, v in (t.payload.get("challenge") or {}).items()
                              if k in ("title", "direction", "category", "points",
                                       "description")},
            })
        # 无可裁量项即不调用模型（省一次往返，不损质量——没有选择就没有决策）：
        # 全部为 CTF 解题任务（路线预算固定）且每个任务的可用模型至多一个
        # 思考档位（或未声明，用端点默认档）
        if (all(t.task_type == "ctf_solve" for t, _ in task_objs)
                and all(len(opts) <= 1 for opts in options.values())):
            for i, (t, _) in enumerate(task_objs):
                if options[i]:
                    t.reasoning_effort = options[i][0]
            ctx.audit.log("decision.change", self.agent_id, {
                "action": "dispatch_plan_skipped", "count": len(task_objs),
                "reason": "路线预算固定且思考档位无可选空间，按声明档直接采用"})
            return
        try:
            resp = await ctx.gateway.chat(
                [
                    {"role": "system", "content": self._system_prompt()
                      + "\nROLE: dispatch_plan"},
                    {"role": "user", "content": json.dumps({
                        "tasks": digest,
                        "constraints": (
                            "先判断每个任务的复杂度，再据此给出：并发分支路线预算"
                            " route_budget（1~8 的整数；复杂/高价值目标 5~8，常规 "
                            "3~4，简单/窄面 1~2；route_budget_fixed 非空的任务此项"
                            "固定，不用给）与思考强度 reasoning_effort。"
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
                task_type="meta_planning", response_format="json",
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
            if t.task_type != "ctf_solve":
                try:
                    t.max_routes = max(1, min(int(item["route_budget"]), 8))
                    record["route_budget"] = t.max_routes
                except (KeyError, TypeError, ValueError):
                    record["route_budget"] = None
            else:
                record["route_budget"] = t.max_routes
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

    def _write_dag_snapshot(self, dag: TaskDAG) -> None:
        """DAG 快照写黑板任务状态层：Web 拓扑视图运行中实时数据源。

        快照记录以 dag_snapshot 键标记，读侧依此与任务记录区分；
        黑板文件随会话工作区持久化，终态会话拓扑同源可读。
        """
        if self.ctx.blackboard is None:
            return
        self.ctx.blackboard.write(
            BlackboardLayer.TASK_STATE, "dag-snapshot",
            {"dag_snapshot": self._dag_snapshot(dag)},
            agent_type=AgentType.MASTER, updated_by=self.agent_id,
            dedup_key="dag|snapshot",
        )

    # ------------------------------------------------------------ 节点：调度

    async def _dispatch(self, state: MasterState) -> dict[str, Any]:
        ctx = self.ctx
        dag = TaskDAG.from_dict(state["dag"])
        # 取本轮任务（meta_plan 刚按 round_no 生成）
        round_tasks = [t for t in state["tasks_dispatched"]
                       if Task.model_validate(t["task"]).round_no == state["round_no"]]

        async def _run_one(spec: dict[str, Any]) -> tuple[str, bool]:
            task = Task.model_validate(spec["task"])
            agent = ctx.agents[task.agent_type]
            try:
                await agent.run(task)
                return spec["node_id"], True
            except Exception as exc:  # noqa: BLE001 —— 单子 Agent 故障不影响全局（2.1-3）
                assert ctx.audit is not None
                ctx.audit.log("task.failed", self.agent_id,
                              {"task_id": task.task_id, "error": str(exc)[:300]})
                return spec["node_id"], False

        # 环形督导：派发期间灵枢实时观察各子 Agent 路线进度，
        # 按全局态势给方向建议（建议经环形台账投递，子 Agent 在 LLM 决策上下文消费）
        watch = None
        if ctx.ring is not None and ctx.enable_llm_planning and round_tasks:
            watch = asyncio.create_task(self._steering_watch(
                [Task.model_validate(s["task"]).task_id for s in round_tasks]))
        try:
            results = await asyncio.gather(*(_run_one(s) for s in round_tasks))
        finally:
            if watch is not None:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch
        for node_id, ok in results:
            dag.mark(node_id, TaskStatus.DONE if ok else TaskStatus.FAILED)
        self._write_dag_snapshot(dag)
        return {"dag": dag.to_dict()}

    # ------------------------------------------------------------ 环形督导

    _STEER_INTERVAL = 2.0   # 观察节拍（秒）；无进展变化不调用 LLM
    _STEER_MAX_CALLS = 3    # 单轮派发内督导 LLM 调用上限（预算护栏）
    _STEER_FEED_MAX_CALLS = 6   # 用户意见触发的督导调用单独预算（不挤占自动督导）

    def _drain_user_feed(self) -> list[dict[str, Any]]:
        """取走用户会话中发来的意见与附件记录（不重不漏地抵达一个决策点）。"""
        inbox = self.ctx.extra.get("user_inbox")
        if not inbox:
            return []
        items = list(inbox)
        inbox.clear()
        return items

    def _return_user_feed(self, feed: list[dict[str, Any]]) -> None:
        """决策调用失败时把用户意见原样退回收件箱（不丢用户的话）。"""
        if feed:
            self.ctx.extra.setdefault("user_inbox", [])[:0] = feed

    async def _steering_watch(self, task_ids: list[str]) -> None:
        """派发期并发督导：读各任务实时路线笔记 + 用户发来的意见 → LLM 判读
        全局 → 写方向建议。

        确定性边界：本函数只做「读进度、问模型、投递建议」；建议是否采纳由
        子 Agent 的 LLM 在其决策上下文中自行权衡，执行仍走原确定性通道。
        用户意见绕过进度指纹门（意见本身就是新信息），预算单独计；
        模型不可用/输出不合规 → 意见退回收件箱留给下一个决策点，不丢。
        """
        assert self.ctx.audit is not None
        last_fingerprint = ""
        calls = 0
        feed_calls = 0
        while True:
            await asyncio.sleep(self._STEER_INTERVAL)
            digest = self._progress_digest(task_ids)
            if not digest["tasks"]:
                continue
            feed = (self._drain_user_feed()
                    if feed_calls < self._STEER_FEED_MAX_CALLS else [])
            fingerprint = json.dumps(digest, ensure_ascii=False, sort_keys=True)
            if not feed and (fingerprint == last_fingerprint
                             or calls >= self._STEER_MAX_CALLS):
                continue
            last_fingerprint = fingerprint
            steering = await self._llm_steer(digest, user_feed=feed)
            if feed:
                feed_calls += 1
            else:
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

    async def _llm_steer(self, digest: dict[str, Any],
                         user_feed: list[dict[str, Any]] | None = None
                         ) -> list[dict[str, Any]]:
        """LLM 判读全局进度，产出需调整方向的子 Agent 建议（白名单校验）。

        user_feed：用户会话中发来的意见与附件记录——优先响应；失败原样退回。
        """
        valid_agents = sorted({t["agent"] for t in digest["tasks"] if t.get("agent")})
        feed = list(user_feed or [])
        if not valid_agents:
            self._return_user_feed(feed)
            return []
        try:
            payload: dict[str, Any] = {
                "action": "steer",
                "mission": self._mission()[:1000],
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
            if feed:
                payload["user_feed"] = feed
                payload["constraints"] += (
                    " user_feed 是用户在会话进行中直接发来的意见（message）与上传的"
                    "附件（attachment，文件已放在会话工作区 attachments/ 下，子 "
                    "Agent 可直接读取）。优先响应意见：给出与用户意图一致的方向"
                    "建议；附件则建议合适的 Agent 尽快分析，hint 里写明文件路径。"
                    "用户正在等灵枢的反应，除非确实无需调整，不要返回空列表。")
            resp = await self.ctx.gateway.chat(
                [
                    {"role": "system", "content": self._system_prompt()
                     + "\nROLE: collab_judge"},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                task_type="meta_planning", response_format="json",
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
            if feed:
                assert self.ctx.audit is not None
                self.ctx.audit.log("decision.change", self.agent_id, {
                    "action": "user_feed_consumed", "channel": "steering",
                    "count": len(feed), "hints": len(out)})
            return out
        except Exception:  # noqa: BLE001 —— 督导失败意见退回，不拖垮派发
            self._return_user_feed(feed)
            return []

    # ------------------------------------------------------------ 节点：汇聚

    async def _aggregate(self, state: MasterState) -> dict[str, Any]:
        view = self.ctx.convergence.aggregate_round_view(state["round_no"])
        flag_manager = self.ctx.extra.get("flag_manager")
        if flag_manager is not None:
            view["flags"] = flag_manager.stats()
        await self.ctx.bus.publish(Event(topic="round.view", data_id=f"round-{state['round_no']}",
                                         source=self.agent_id, payload=view))
        return {"round_views": [view]}

    # ------------------------------------------------------------ 节点：复盘

    async def _review(self, state: MasterState) -> dict[str, Any]:
        ctx = self.ctx
        assert ctx.audit is not None
        dag = TaskDAG.from_dict(state["dag"])
        view = state["round_views"][-1]
        next_round = state["round_no"] + 1

        # 漏洞终审（4.1.1 主 Agent 终审权）：合规已通过且 PoC 验证 → 确认收录
        verdicts = self._final_verdicts()

        # 收敛判定（5.1.4 结束条件：时间/轮次耗尽、核心目标完成、无新发现）
        stop_reason = ""
        flags_pending = self._ctf_flags_pending(view)
        if next_round >= state["max_rounds"]:
            stop_reason = "轮次上限"
        elif not dag.has_unfinished() and not view["vuln_in_pipeline"] and not flags_pending:
            stop_reason = "DAG 全部完成且核验流水线清空"

        # CTF/渗透赛：骨架路线跑完 ≠ 题解尽——Flag 未收时硬收敛门放行，
        # 交由 LLM 复盘决定换思路再攻还是收手；连续无进展则由规则引擎熔断
        if flags_pending and self._ctf_stale_rounds(state) >= 2:
            stop_reason = "连续 2 轮 Flag 无进展，规则引擎熔断收敛"
            flags_pending = False

        # LLM 全局复盘（建议者）；规则引擎裁决：DAG 未竟时否决收敛建议。
        # 收敛已由规则确定且无用户意见待消费时，复盘建议无从发挥作用——
        # 直接跳过（省一次往返；建议本就不会被采纳，不损质量）
        pending_feed = bool(self.ctx.extra.get("user_inbox"))
        llm_decision = (await self._llm_review(state, view)
                        if ctx.enable_llm_planning and (not stop_reason or pending_feed)
                        else None)
        if llm_decision and not stop_reason:
            if not llm_decision.get("continue_iteration", False):
                if dag.has_unfinished():
                    ctx.audit.log("decision.change", self.agent_id, {
                        "action": "llm_stop_vetoed", "round": state["round_no"],
                        "reason": "DAG 存在未完成任务，规则引擎否决 LLM 收敛建议"})
                else:
                    stop_reason = f"主Agent决策收敛: {llm_decision.get('reason', '')[:120]}"
            elif llm_decision.get("dag_nodes"):
                nodes = self._filter_plannable(llm_decision["dag_nodes"])
                if ctx.plan_override is not None:
                    # 模式自带规划器（CTF/渗透赛）：重攻节点由规划器按方向轮换
                    # 统一生成并绑定赛题；LLM 建议只作方向参考，不直接入图
                    ctx.audit.log("decision.change", self.agent_id, {
                        "action": "llm_nodes_deferred", "round": state["round_no"],
                        "count": len(nodes),
                        "titles": [str(n.get("title", ""))[:80] for n in nodes[:6]]})
                else:
                    dag.upsert_from_plan(nodes, next_round)

        if not stop_reason and not dag.ready_nodes() and not dag.has_unfinished():
            if flags_pending:
                stop_reason = ("Flag 未收且复盘不可用（LLM 未应答/未启用），收敛"
                               if llm_decision is None
                               else "Flag 未收且复盘未给出新方向")
            else:
                stop_reason = "无新发现，无就绪任务"
        continue_iteration = not stop_reason

        ctx.audit.log("decision.change", self.agent_id, {
            "action": "review", "round": state["round_no"],
            "continue": continue_iteration, "stop_reason": stop_reason,
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
    def _ctf_stale_rounds(state: MasterState) -> int:
        """连续无 Flag 进展的轮数（accepted 与 extracted 均无增长）。"""
        stale = 0
        last_key: tuple[int, int] | None = None
        for v in reversed(state["round_views"]):
            flags = v.get("flags")
            if not flags:
                break
            key = (flags.get("accepted", 0), flags.get("extracted", 0))
            if last_key is None:
                last_key = key
                stale = 1
                continue
            if key == last_key:
                stale += 1
            else:
                break
        return stale

    def _kb_attack_paths(self, tactic: str) -> list[dict[str, Any]]:
        """按战术检索攻击路径模板注入任务包（3.3.6：related_tactics 用于 DAG 生成）。

        敏感路径（钓鱼/外泄）仅在授权范围显式允许敏感动作时放行；
        路径为执行参考（步骤标题序列），不替代子 Agent 路线模板。
        """
        kb = self.ctx.knowledge
        if kb is None or not tactic:
            return []
        allow_sensitive = bool(
            getattr(self.ctx.policy.guard.scope, "allow_sensitive_actions", False))
        paths = kb.attack_paths(include_sensitive=allow_sensitive)
        matched = [p for p in paths if tactic in p.get("related_tactics", [])]
        return [
            {"path_id": p["path_id"], "scenario": p["scenario"],
             "entry_conditions": p["entry_conditions"],
             "steps": [s.get("action", "") for s in p.get("step_sequence", [])][:12]}
            for p in matched[:2]
        ]

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
            if vuln.status == VulnStatus.ACCEPTED and not vuln.poc_verified:
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
            resp = await self.ctx.gateway.chat(
                [
                    {"role": "system", "content": self._system_prompt() + "\nROLE: meta_planner"},
                    {"role": "user", "content": json.dumps({
                        "target": state["target"], "round": state["round_no"],
                        "mission": self._mission()[:2000],
                        "available_tactics": {
                            t: {"agent": a.value, "task_type": tt}
                            for t, (a, tt) in tactic_map.items()},
                        "situation": self._situation_digest(),
                        "existing_nodes": [
                            {"tactic": n.tactic, "title": n.title,
                             "status": n.status.value}
                            for n in dag.all_nodes()][:30],
                        "views": state["round_views"][-3:],
                        "constraints": (
                            "返回 JSON：{\"analysis\": str, \"dag_nodes\": [{\"tactic\": "
                            "仅可用战术之一, \"title\": str, \"technique\": str, "
                            "\"depends_on\": [战术]}]}。按当前态势决定下一步最有价值的"
                            "探测方向；不重复已完成节点。"),
                    }, ensure_ascii=False)[:8000]},
                ],
                task_type="meta_planning", response_format="json",
            )
            data = extract_json(resp.content)
            if not isinstance(data, dict) or "dag_nodes" not in data:
                return None
            nodes = self._filter_plannable(data.get("dag_nodes", []))
            if not nodes and data.get("dag_nodes"):
                return None            # 全部不可调度：视为规划失败，回退规则
            data["dag_nodes"] = nodes
            return data
        except Exception:  # noqa: BLE001 —— 规划失败回退规则规划
            return None

    def _filter_plannable(self, plan_nodes: list[Any]) -> list[dict[str, Any]]:
        """LLM 产出节点过滤：仅保留可用战术（规则引擎调度裁决，不外包给模型）。"""
        tactic_map = self.ctx.tactic_map or TACTIC_AGENT_MAP
        nodes, dropped = [], []
        for n in plan_nodes:
            if isinstance(n, dict) and n.get("tactic") in tactic_map:
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
        }

    async def _llm_review(self, state: MasterState, view: dict) -> dict[str, Any] | None:
        feed = self._drain_user_feed()
        try:
            tactic_map = self.ctx.tactic_map or TACTIC_AGENT_MAP
            payload: dict[str, Any] = {
                "action": "review", "view": view,
                "mission": self._mission()[:2000],
                "dag_stats": TaskDAG.from_dict(state["dag"]).stats(),
                "available_tactics": {
                    t: {"agent": a.value, "task_type": tt}
                    for t, (a, tt) in tactic_map.items()},
                "situation": self._situation_digest(),
                "round_routes": self._route_digest(state),
                "constraints": (
                            "返回 JSON：{\"continue_iteration\": bool, \"reason\": str, "
                            "\"dag_nodes\": [{\"tactic\": 仅可用战术之一, \"title\": str, "
                            "\"depends_on\": []}]}。"
                            "round_routes 是各子 Agent 路线的执行笔记（观察与结论），"
                            "据此判断还有哪些未试过的方向；笔记含「LLM 不可用/调查"
                            "停摆」属基础设施问题而非无解题空间——应继续迭代重试。"
                            "若 Flag/核心目标未收且仍有未试过的思路，"
                            "continue_iteration=true 并给出换思路的新节点"
                            "（dag_nodes 必填，否则等于收手）；"
                            "确认无解题空间时 continue_iteration=false 并说明理由。"),
            }
            if feed:
                payload["user_feed"] = feed
                payload["constraints"] += (
                    " user_feed 是用户在会话中发来的意见与上传的附件（attachment "
                    "的文件在工作区 attachments/ 下，构造新节点时可在 title 里点明"
                    "分析目标）。请优先结合用户意图决策。")
            resp = await self.ctx.gateway.chat(
                [
                    {"role": "system", "content": self._system_prompt() + "\nROLE: meta_planner"},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                task_type="meta_planning", response_format="json",
            )
            data = extract_json(resp.content)
            if feed:
                assert self.ctx.audit is not None
                self.ctx.audit.log("decision.change", self.agent_id, {
                    "action": "user_feed_consumed", "channel": "review",
                    "count": len(feed)})
            return data
        except Exception:  # noqa: BLE001
            self._return_user_feed(feed)
            return None

    def _rule_plan(self, state: MasterState) -> dict[str, Any]:
        """规则化初始规划（LLM 不可用兜底）：侦察 → Web 漏洞探测。"""
        return {
            "analysis": "规则规划：按 ATT&CK 战术拆解 TA0043→TA0001",
            "dag_nodes": [
                {"tactic": "TA0043", "title": "侦察与资产测绘", "depends_on": []},
                {"tactic": "TA0001", "title": "Web 漏洞探测（T1190）", "depends_on": ["TA0043"]},
            ],
        }

    def _mission(self) -> str:
        """用户任务意图：用户在控制台告诉主 Agent 的自然语言合约/目标。"""
        return str(self.ctx.extra.get("mission", "") or "")

    def _system_prompt(self) -> str:
        scope = self.ctx.policy.guard.scope
        allowed = scope.allowed_domains + scope.allowed_cidrs + scope.allowed_urls
        mission = self._mission()
        mission_block = (
            f"用户任务意图（合约范围与目标由用户口述如下，你据此自行决策"
            f"并下发任务给子 Agent）：\n「{mission}」\n"
            if mission else "")
        return (
            f"你是灵探 SpiritScout 的主 Agent「灵枢」，一次已获授权安全测试的"
            f"最高决策者。"
            f"{mission_block}"
            f"你全权决定：战术拆解（可参考 ATT&CK，也可按目标特点自由组合）、"
            f"任务下发与优先级、子 Agent 分工、迭代节奏与收敛时机。"
            f"依据黑板上的实时态势（资产/线索/漏洞流水线/子 Agent 回报）独立"
            f"判断：走进死路主动换方向，发现高价值线索主动深挖，不必等待用户"
            f"确认。你麾下的子 Agent 各有专长（侦察/漏洞探测/后渗透/CTF 各方向），"
            f"把合适的任务交给合适的 Agent，并给足上下文让它们放手执行。"
            f"不可逾越的边界只有三条：① 一切动作严格限于授权范围 {allowed}；"
            f"② 不伪造证据、不臆断漏洞——结论必须有可复现证据支撑；"
            f"③ 预算有限，把算力花在最有价值的方向上。"
            f"只输出 JSON。"
        )
