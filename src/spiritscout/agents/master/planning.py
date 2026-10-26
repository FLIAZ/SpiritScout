"""主 Agent 的规划、汇聚与复盘；保留原提示词、消息构造和收敛语义。"""
from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from typing import Any

from spiritscout.agents.master.contracts import TACTIC_AGENT_MAP, MasterState
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.profiles import agent_system_prompt, get_profile
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    Event,
    Task,
    TaskStatus,
)
from spiritscout.gateway import chat_decision
from spiritscout.gateway.backends import extract_json
from spiritscout.gateway.context import (
    compact_json,
    decision_memory,
    decision_messages,
    shared_context,
)
from spiritscout.tools.search_policy import search_allowed


class MasterPlanning:
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
