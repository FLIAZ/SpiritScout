"""主 Agent 的任务派发与平台协调：并发执行、资源回收和竞赛补位。"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import time
from datetime import datetime, timezone
from typing import Any

from spiritscout.agents.master.contracts import MasterState
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.core.exceptions import ScopeViolation, SpiritScoutError
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    Task,
    TaskStatus,
)
from spiritscout.gateway import chat_decision
from spiritscout.gateway.backends import extract_json


class MasterCoordination:
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

