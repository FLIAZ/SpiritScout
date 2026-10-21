"""子 Agent 基座：路线生成 → 并发执行 → 健康度剪枝 → 局部反思 → 结果上报。

路线是**交付目标**而非固定流程：strategy 声明本路线要产出的可核验结果
（证据 → 结论），步骤只是探测单元与合规通道，怎么走到验证点由执行者
按现场证据自定；灵枢任务包携带的任务意图（mission/node_title）随每次
LLM 决策下发，执行者不持有全局提示词。

硬约束 C2 落地：
- 差异化路线：route_templates 按交付目标绑定探测单元；LLM 只在声明了
  动作白名单的子 Agent 上增广路线（提议→白名单校验→规则放行）；
- 路线间经 LocalSharedCache 共享中间结果；
- BranchHealthManager 每步评估、自动剪枝/合并；
- 局部反思：每 3 步复盘，连续失败自动调整（毫秒级本地决策，6.4）。
"""

from __future__ import annotations

import abc
import asyncio
import dataclasses
import json
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from spiritscout.agents.ring import RingLedger, attempt_key
from spiritscout.agents.sub.cache import LocalSharedCache
from spiritscout.agents.sub.health import BranchHealthManager
from spiritscout.blackboard import ConvergenceEngine
from spiritscout.blackboard.store import Blackboard
from spiritscout.bus.base import EventBus
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    Event,
    RouteState,
    Task,
    TaskStatus,
)
from spiritscout.gateway import ModelGateway, chat_decision
from spiritscout.gateway.backends import extract_json
from spiritscout.gateway.context import decision_messages, shared_context
from spiritscout.policy.engine import PolicyEngine
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope
from spiritscout.tools.registry import AgentToolView, ToolRegistry


@dataclass
class RoutePlan:
    """测试路线计划（路线生成器输出）。"""

    name: str
    strategy: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    """单步执行结果（子类 execute_step 返回）。"""

    ok: bool
    value: float = 0.0                       # 本步价值增量 0~1（喂健康度价值分）
    progress_delta: float = 0.0
    findings: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    cache_writes: dict[str, Any] = field(default_factory=dict)
    risk_delta: float = 0.0
    detection_signals: int = 0               # 防护拦截信号（WAF/403/429，喂 6.5 风险分）
    tokens_used: int = 0
    final_note: bool = False                 # 收尾结论文本（路线笔记按更长限额保留）


@dataclass
class SubAgentContext:
    """子 Agent 运行上下文（依赖注入）。"""

    bus: EventBus
    blackboard: Blackboard
    convergence: ConvergenceEngine
    gateway: ModelGateway
    tools: ToolRegistry | AgentToolView
    policy: PolicyEngine
    audit: AuditLogger | None = None
    builtin_ctx: dict[str, Any] = field(default_factory=dict)  # guard/limiter 等
    knowledge: Any = None                  # KnowledgeStore（经验库检索接线，None=不接）
    agent_models: dict[str, str] = field(default_factory=dict)  # 按 Agent 绑定模型
    # 环形协同台账：已尝试登记 + 灵枢督导（会话内所有 Agent 共持一份；
    # for_agent 的 dataclasses.replace 共享同一引用）
    ring: RingLedger | None = None
    # 环形共享缓存：同题并行 Agent 的共享现场（已探入口/共享笔记/已夺 Flag）
    # 按命名空间隔离；会话内共持一份，for_agent 的 replace 共享同一引用
    shared_cache: LocalSharedCache | None = None
    # 外部 MCP 工具（mcp__<server>__<tool> → {client, server, tool, schema}）；
    # 会话基建握手拉取、后台续握的活目录（for_agent 生成按角色过滤的活视图）
    mcp_tools: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    # 模型发起的用户询问通道（ask_user 工具的后端：挂起-作答-回传；
    # for_agent 的 replace 共享同一引用）
    questions: Any = None
    # 暂停门：用户暂停时步骤边界挂起（for_agent 的 replace 共享同一引用；
    # None = 无暂停能力，如测试/离线装配）
    pause_gate: Any = None
    journal: Any = None

    def __post_init__(self) -> None:
        if self.audit is None:
            self.audit = NullAuditLogger()
        if self.ring is None:
            self.ring = RingLedger(self.audit)
        if self.shared_cache is None:
            self.shared_cache = LocalSharedCache(default_ttl_seconds=3600)

    def for_agent(self, agent_key: str) -> SubAgentContext:
        """为角色绑定模型，并收窄工具与 MCP 目录。"""
        model = self.agent_models.get(agent_key, "")
        gateway = self.gateway
        if model:
            from spiritscout.gateway import AgentBoundGateway

            gateway = AgentBoundGateway(self.gateway, model)
        tool_root = (self.tools._registry
                     if isinstance(self.tools, AgentToolView) else self.tools)
        tool_view = AgentToolView(tool_root, agent_key, audit=self.audit)
        return dataclasses.replace(
            self,
            gateway=gateway,
            tools=tool_view,
            mcp_tools=_AgentMcpView(self.mcp_tools, tool_view.allows),
        )


class _AgentMcpView(Mapping):
    """按 Agent 可用性过滤的 MCP 工具活视图。

    会话基建里 MCP 服务器后台续握、就绪一台注册一台（源 dict 实时生长）
    ——活视图让长生命周期 Agent 的目录在下次读取时自然长出新工具，
    无需重建上下文。
    """

    def __init__(self, source: Mapping[str, dict[str, Any]],
                 allowed: Any) -> None:
        self._source = source
        self._allowed = allowed

    def __iter__(self) -> Iterator[str]:
        return iter([k for k in self._source if self._allowed(k)])

    def __len__(self) -> int:
        return sum(1 for k in self._source if self._allowed(k))

    def __getitem__(self, key: str) -> dict[str, Any]:
        if key in self._source and self._allowed(key):
            return self._source[key]
        raise KeyError(key)


# 用户询问通道说明（随每次 LLM 决策载荷下发，预算内保底）：模型遇「只有
# 用户能给的输入」可在返回 JSON 里带 ask_user，管道挂起等用户作答后带着
# 答复重裁本决策。问句文案由模型撰写、面向普通用户（不行话缩写）。
_ASK_CHANNEL_NOTE = (
    "【可询问用户】仅当本决策必须依赖只有用户能提供的输入（测试账号/凭据、"
    "登录后操作的请求记录、授权范围裁定）时，在返回 JSON 里加 "
    "\"ask_user\": {\"question\": str, \"options\": [str], \"multiple\": bool, "
    "\"targets\": [{\"target\": str, \"why\": str}]}。multiple 仅在选项彼此"
    "不互斥、用户可能同时选多项时设 true（如「接下来优先做哪些独立检查」"
    "「需要开通哪些账号/权限」）；「允许/拒绝」「方案 A/B」这类互斥决策"
    "保持 false（默认单选，点选即提交），多选问题的选项要写成可独立成立的"
    "动作或结论。targets 仅在需要访问当前授权范围外的新目标时给出；why 用"
    "一句话说明它是什么、为什么需要（面向普通用户）——控制台会渲染成"
    "「允许访问」勾选项，只有用户勾选才会纳入本会话范围；不要在 targets 里"
    "罗列已在范围内的目标。问题面向普通用户，用平实中文，不用 HAR、PoC 这类"
    "行话缩写（必要处一句话解释）。后端会挂起等用户作答，答复回灌后你重新"
    "给出本决策；没有这类需求就不要加该字段。")


class SubAgent(abc.ABC):
    """探测执行类子 Agent 通用基座（环形对等节点）。"""

    agent_type: AgentType = AgentType.RECON
    route_namespace: str = "routes"
    # LLM 增广路线允许编排的确定性动作白名单：action → 必填字符串字段；
    # 空表 = 本 Agent 不接受 LLM 增广（预算只起截断作用）
    llm_route_actions: ClassVar[dict[str, list[str]]] = {}
    # 健康度剪枝开关：默认开启（低分路线让位高分路线）；逐轮探索型子 Agent
    # 置 False——起步阶段进展分被步数顶摊薄，剪枝会误杀在途调查
    health_prune_enabled: ClassVar[bool] = True

    def __init__(
        self,
        ctx: SubAgentContext,
        *,
        health: BranchHealthManager | None = None,
        default_routes: int = 3,
        max_routes: int = 8,
        enable_llm_planning: bool = True,
    ):
        self.ctx = ctx.for_agent(self.agent_type.value)   # 按名册绑定模型
        self.health_mgr = health or BranchHealthManager(max_routes=max_routes)
        self.default_routes = default_routes
        self.max_routes = max_routes
        self.enable_llm_planning = enable_llm_planning
        # 共享缓存按会话共持：同一会话内各 Agent/路线按命名空间隔离读写，
        # 环形共享现场（ctf_shared 等）因此跨 Agent 可见
        self.cache = self.ctx.shared_cache or LocalSharedCache(default_ttl_seconds=3600)
        self.agent_id = new_id(self.agent_type.value.replace("_", "-"))
        # 灵枢督导注入水位：同一条建议只进一次决策载荷（见 _llm_chat_json）
        self._steer_seen_ts = 0.0

    @staticmethod
    def task_effort(task: Task) -> str:
        """任务的思考强度（灵枢按复杂度裁定，取值来自模型自己声明的档位；
        空 = 该任务模型未声明思考强度，用端点默认档）。"""
        return str(getattr(task, "reasoning_effort", "") or "")

    # ------------------------------------------------------------ 主流程

    async def run(self, task: Task) -> dict[str, Any]:
        """任务规划阶段也保留角色归属，路线内部会进一步绑定路线 ID。"""
        token = current_execution_scope.set(dataclasses.replace(
            current_execution_scope.get() or ExecutionScope(),
            task_id=task.task_id, route_id="", agent_id=self.agent_id))
        try:
            return await self._run_task(task)
        finally:
            current_execution_scope.reset(token)

    async def _run_task(self, task: Task) -> dict[str, Any]:
        """执行一个任务包：路线生成 → 并发执行 → 汇总上报。"""
        task.status = TaskStatus.RUNNING
        self.ctx.audit.log("task.start", self.agent_id,
                           {"task_id": task.task_id, "type": task.task_type, "target": task.target})
        # 开工即开回合：时间线以回合块聚合本 Agent 的步骤动作与结论
        # （SRC 侧此前只记 task.start/done，执行过程在会话页是黑盒）
        self.ctx.audit.log("agent.turn", self.agent_id, {
            "turn": 1,
            "angle": str(task.payload.get("node_title")
                         or task.payload.get("tactic") or "")[:200],
        })
        plans = await self.plan_routes(task)
        routes = [RouteState(route_id=new_id("route"), task_id=task.task_id, name=p.name)
                  for p in plans]
        # 并发执行各路线（对等并行，无阶段瓶颈）
        # 单条路线异常时收口其余路线，不能在任务已失败后继续留下执行者。
        async with asyncio.TaskGroup() as group:
            for plan, route in zip(plans, routes):
                group.create_task(self._run_route(task, plan, route, routes))
        if any(r.status == TaskStatus.DONE for r in routes):
            task.status = TaskStatus.DONE
        elif any(r.status == TaskStatus.FAILED for r in routes):
            task.status = TaskStatus.FAILED
        elif any(r.status == TaskStatus.PRUNED for r in routes):
            task.status = TaskStatus.PRUNED
        else:
            task.status = TaskStatus.CANCELLED
        summary = self._summarize(task, routes)
        task.result_summary = summary
        # 任务收尾：各路线最后一条现场笔记拼成可读结论，随事件流上时间线。
        # 已单独发过 agent.conclude 的路线（conclusion_published，如 CTF
        # 解题环收尾）跳过——同一段结论文本重复上时间线，用户看到两遍
        conclude = "；".join(
            f"{r.name}：{r.notes[-1] if r.notes else r.status.value}"
            for r in routes if not r.conclusion_published)[:2000]
        if conclude:
            self.ctx.audit.log("agent.conclude", self.agent_id,
                               {"summary": conclude})
        await self._report(task, routes, summary)
        return summary

    # ------------------------------------------------------------ 路线生成

    async def plan_routes(self, task: Task) -> list[RoutePlan]:
        """生成差异化路线：交付目标为骨架，主 Agent 预算裁决条数。

        条数语义：task.max_routes 是主 Agent 按复杂度决策的并发预算——
        超出则按模板优先级截断，不足则在声明了动作白名单的子 Agent 上由
        LLM 增广新路线；未指定（None）按模板自然条数。
        情报驱动的动态追加路线在 max_routes 弹性上限内放行（1~8 弹性，6.2）。
        """
        plans = self.route_templates(task)
        if task.max_routes is not None:
            budget = max(1, min(task.max_routes, self.max_routes))
            if len(plans) > budget:
                plans = plans[:budget]
            elif len(plans) < budget and self.enable_llm_planning \
                    and self.llm_route_actions:
                plans = await self._llm_extend_routes(task, plans,
                                                      budget - len(plans))
        plans = plans[: self.max_routes]
        if self.ctx.knowledge is not None:
            self._attach_kb_guidance(task, plans)   # 经验库核验参考接线（3.3.6 联动）
        return plans

    async def _llm_extend_routes(
        self, task: Task, plans: list[RoutePlan], slots: int
    ) -> list[RoutePlan]:
        """LLM 按预算空缺席位增广新路线（动作白名单校验，规则引擎放行）。

        LLM 只可在本 Agent 声明的确定性动作内编排步骤；任何不合规动作/
        字段整条路线丢弃并留痕——LLM 提议、规则放行，不静默执行未校验动作。
        """
        assert self.ctx.audit is not None
        try:
            resp = await self.ctx.gateway.chat(
                [
                    {"role": "system", "content": self._system_prompt()
                      + "\nROLE: route_planner"},
                    {"role": "user", "content": json.dumps({
                        "task": task.task_type, "target": task.target,
                        "world_state": self._world_state(),
                        "existing_routes": [
                            {"name": p.name, "strategy": p.strategy} for p in plans],
                        "allowed_actions": {a: {"required_fields": f}
                                            for a, f in
                                            self.llm_route_actions.items()},
                        "slots": slots,
                        "constraints": (
                            "返回 JSON：{\"new_routes\": [{\"name\": 英文短名, "
                            "\"strategy\": 一句话策略, \"steps\": [{\"action\": "
                            "白名单动作之一, ...参数字段}]}]}。每条新路线是一个"
                            "可核验的交付目标：strategy 写清要产出什么、完成判据"
                            "是什么，steps 只是最小验证动作。必须与既有路线"
                            "思路不同；每条 1~6 步；最多 slots 条。"),
                    }, ensure_ascii=False)},
                ],
                task_type="execution", response_format="json",
                reasoning_effort=self.task_effort(task),
                task=task,
            )
            data = extract_json(resp.content)
        except Exception as exc:  # noqa: BLE001 —— 增广失败保持模板路线
            self.ctx.audit.log("llm.step_fallback", self.agent_id,
                               {"role": "route_planner/extend",
                                "error": str(exc)[:200]})
            return plans
        added: list[RoutePlan] = []
        names = {p.name for p in plans}
        for raw in (data or {}).get("new_routes") or []:
            if len(added) >= slots:
                break
            steps = self._validate_llm_steps(task, raw.get("steps"))
            name = re.sub(r"[^a-z0-9_]+", "_",
                          str(raw.get("name", "")).lower()).strip("_")[:32]
            if not steps or not name or name in names:
                continue
            names.add(name)
            added.append(RoutePlan(name=name,
                                   strategy=str(raw.get("strategy", ""))[:200],
                                   steps=steps))
        if added:
            self.ctx.audit.log("llm.route_extended", self.agent_id, {
                "task_id": task.task_id,
                "added": [p.name for p in added]})
        return [*plans, *added]

    def _validate_llm_steps(self, task: Task, raw_steps: Any) -> list[dict[str, Any]]:
        """LLM 增广路线的步骤校验：动作白名单 + 必填字段 + 长度封顶。"""
        if not isinstance(raw_steps, list):
            return []
        steps: list[dict[str, Any]] = []
        for raw in raw_steps[:6]:
            if not isinstance(raw, dict):
                return []
            action = str(raw.get("action", ""))
            required = self.llm_route_actions.get(action)
            if required is None:
                return []
            step: dict[str, Any] = {"action": action}
            for fname in required:
                value = raw.get(fname)
                if not isinstance(value, str) or not value or len(value) > 300:
                    return []
                step[fname] = value
            steps.append(step)
        return steps

    def _attach_kb_guidance(self, task: Task, plans: list[RoutePlan]) -> None:
        """按战术/技术 + 漏洞类检索经验库，注入**核验参考**（3.3.6 联动）。

        只带核验面信息：前置条件、核验方法、常见坑与合规红线——不带步骤
        序列：怎么走到验证点由执行者按现场证据自定，推送步骤等于规定打法；
        经验库全量条目可经 kb_search 随需检索（CTF 方向），需要时自取。
        """
        kb = self.ctx.knowledge
        tactic = str(task.payload.get("tactic") or "") or None
        technique = str(task.payload.get("technique") or "") or None
        experiences = kb.experiences(tactic_id=tactic, technique_id=technique)[:3]
        hunt_templates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for vuln_class in task.payload.get("vuln_classes") or []:
            for tpl in kb.hunt_templates(vuln_type=str(vuln_class)):
                if tpl["template_id"] not in seen:
                    seen.add(tpl["template_id"])
                    hunt_templates.append(tpl)
        if not experiences and not hunt_templates:
            return
        guidance = {
            "experiences": [
                {"entry_id": e["entry_id"], "title": e["title"],
                 "preconditions": e.get("preconditions", []),
                 "verify_method": e.get("verify_method", "")}
                for e in experiences
            ],
            "hunt_templates": [
                {"template_id": t["template_id"], "scene": t["scene"],
                 "precondition": t.get("precondition", ""),
                 "common_pitfall": t.get("common_pitfall", ""),
                 "compliance_redline": t.get("compliance_redline", "")}
                for t in hunt_templates[:5]
            ],
        }
        for plan in plans:
            plan.meta["kb_guidance"] = guidance
        assert self.ctx.audit is not None
        self.ctx.audit.log("kb.guidance", self.agent_id, {
            "task_id": task.task_id,
            "experiences": [e["entry_id"] for e in experiences],
            "hunt_templates": [t["template_id"] for t in hunt_templates[:5]],
        })

    @abc.abstractmethod
    def route_templates(self, task: Task) -> list[RoutePlan]:
        """子类实现：按任务生成差异化路线模板（≥3 条，覆盖不同技术路径）。"""

    # ------------------------------------------------------------ 路线执行

    async def _run_route(
        self, task: Task, plan: RoutePlan, route: RouteState,
        routes: list[RouteState] | None = None,
    ) -> None:
        token = current_execution_scope.set(ExecutionScope(
            task_id=task.task_id, route_id=route.route_id, agent_id=self.agent_id))
        try:
            await self._run_route_inner(task, plan, route, routes)
        finally:
            current_execution_scope.reset(token)

    async def _run_route_inner(
        self, task: Task, plan: RoutePlan, route: RouteState,
        routes: list[RouteState] | None = None,
    ) -> None:
        total_steps = max(len(plan.steps), 1)
        for step_index, step in enumerate(plan.steps):
            # 暂停门：用户暂停时路线在步骤边界挂起（在途步骤跑完才停，
            # 不中断半截工具态）；灵枢对话与审批通道不经此门，照常可用
            if self.ctx.pause_gate is not None:
                await self.ctx.pause_gate.wait()
            # 健康度剪枝门（每步评估；宽限期内不剪，见 BranchHealthManager）
            if self.health_prune_enabled and self.health_mgr.should_prune(route):
                self.health_mgr.prune(route, f"健康度 {route.health:.2f} 低于阈值")
                self.ctx.audit.log("route.pruned", self.agent_id,
                                   {"route": route.name, "health": round(route.health, 3)})
                break
            # 步骤级实况：动作卡片随执行即时上时间线（开始 → 完成/失败），
            # 让会话页看得见子 Agent 的实时关键产出而非黑盒等待
            step_id = f"{task.task_id}:{route.name}:{step_index}"
            journal = getattr(self.ctx, "journal", None)
            if journal is not None:
                journal.checkpoint("route", route.route_id, "before_step", {
                    "task": task.model_dump(mode="json"), "route": route.model_dump(mode="json"),
                    "plan": dataclasses.asdict(plan), "step_index": step_index})
            self.ctx.audit.log("agent.tool_call", self.agent_id, {"calls": [{
                "id": step_id, "name": str(step.get("action", "step")),
                "arguments": {k: v for k, v in step.items() if k != "action"}}]})
            # 环形避让：同伴已在同一入口做过无产出尝试 → 不再深入（互不等候、
            # 互不重复做功）；有自己历史的入口由路线内去重负责，不在此拦截
            akey = self._attempt_key(task, step)
            prior = (self.ctx.ring.should_avoid(akey, agent_id=self.agent_id)
                     if akey and self.ctx.ring is not None else None)
            if prior is not None:
                result = StepResult(
                    ok=True, value=0.0,
                    note=f"环形共享：{prior['agent_name']} 已尝试此入口"
                         f"（无产出），跳过深入")
                self.ctx.audit.log("ring.avoid", self.agent_id, {
                    "route": route.name, "key": akey[:200],
                    "prior_agent": prior["agent_name"],
                    "prior_note": prior["note"][:120]})
            else:
                try:
                    result = await self.execute_step(task, route, plan, step)
                except Exception as exc:  # noqa: BLE001 —— 单步失败不拖垮路线（容错 2.1-3）
                    result = StepResult(ok=False, note=f"step exception: {exc}")
                    # 步异常必须留痕于事件流：只进路线笔记会让「智能体零回合
                    # 收工」在时间线上看起来像正常完成
                    self.ctx.audit.log("agent.error", self.agent_id, {
                        "route": route.name, "step": str(step.get("action", "")),
                        "error": str(exc)[:300]})
                # 已尝试登记：结果价值供同伴避让参考（高价值先写不被低价值覆盖）
                if akey and self.ctx.ring is not None:
                    self.ctx.ring.record_attempt(
                        akey, agent_id=self.agent_id,
                        agent_name=self.agent_type.value, action=str(step.get("action", "")),
                        outcome=result.value, note=result.note)
            self.ctx.audit.log("agent.tool_result", self.agent_id, {
                "id": step_id, "ok": result.ok,
                "note": (result.note or "")[:300]})
            self._update_route(route, result, total_steps)
            if journal is not None:
                journal.checkpoint("route", route.route_id, "after_step", {
                    "task": task.model_dump(mode="json"), "route": route.model_dump(mode="json"),
                    "plan": dataclasses.asdict(plan), "step_index": step_index,
                    "result": dataclasses.asdict(result)})
            # 实时进度上报（节流）：拓扑/督导的数据面——路线笔记随步可见
            if routes is not None:
                await self._live_report(task, routes)
            # 本地缓存共享（路线间复用）
            for key, value in result.cache_writes.items():
                self.cache.put(self.route_namespace, key, value)
            # 局部反思：每 3 步复盘一次（4.3.1）
            if route.steps_executed % 3 == 0:
                self._reflect(route, plan)
        if route.status == TaskStatus.RUNNING:
            route.status = (TaskStatus.DONE if route.steps_succeeded
                            else TaskStatus.FAILED if route.steps_failed
                            else TaskStatus.CANCELLED)
        # 终评（记录最终健康度）
        self.health_mgr.evaluate(route)

    def _attempt_key(self, task: Task, step: dict) -> str | None:
        """步骤 → 归一化尝试键（仅对产生网络请求的步骤；相对路径补全目标）。"""
        url = step.get("url") or step.get("path")
        if not isinstance(url, str) or not url:
            return None
        if url.startswith("/"):
            url = task.target.rstrip("/") + url
        params = step.get("params")
        return attempt_key("GET", url, params if isinstance(params, dict) else None)

    _LIVE_INTERVAL = 1.0   # 实时上报节流（秒/任务）

    async def _live_report(self, task: Task, routes: list[RouteState]) -> None:
        """路线状态实时落黑板（任务态记录随步刷新；终态 _report 语义不变）。

        上报失败不拖垮路线（数据面可短暂滞后，终态一定完整）。
        """
        now = time.monotonic()
        live_ts = getattr(self, "_live_ts", {})
        if now - live_ts.get(task.task_id, 0.0) < self._LIVE_INTERVAL:
            return
        live_ts[task.task_id] = now
        self._live_ts = live_ts
        try:
            self.ctx.blackboard.write(
                BlackboardLayer.TASK_STATE,
                task.task_id,
                {"task": task.model_dump(mode="json"),
                 "summary": {"live": True},
                 "routes": [r.model_dump(mode="json") for r in routes]},
                agent_type=self.agent_type,
                updated_by=self.agent_id,
                dedup_key=f"task|{task.task_id}",
            )
        except Exception:  # noqa: BLE001, S110 —— 实时面可滞后，不阻断执行
            pass

    def _update_route(self, route: RouteState, result: StepResult, total_steps: int) -> None:
        route.steps_executed += 1
        route.tokens_used += result.tokens_used
        route.tool_calls += 1
        # 6.5 风险分原料：步骤时间戳（探测频率，滑动窗口上限 60 条）+ 拦截信号
        route.step_times.append(time.monotonic())
        del route.step_times[:-60]
        route.detection_signals += result.detection_signals
        if result.ok:
            route.steps_succeeded += 1
            route.consecutive_failures = 0
            route.value_score = min(route.value_score + result.value, 1.0)
            route.progress = min(route.progress + (result.progress_delta or 1.0 / total_steps), 1.0)
        else:
            route.steps_failed += 1
            route.consecutive_failures += 1
        route.risk_score = min(route.risk_score + result.risk_delta, 1.0)
        if result.note:
            # 收尾结论是路线最有分量的文本（最终答案与依据），给长限额；
            # 过程笔记只留摘要
            route.notes.append(result.note[:1600] if result.final_note
                               else result.note[:300])
        self.health_mgr.evaluate(route)

    def _reflect(self, route: RouteState, plan: RoutePlan) -> None:
        """局部反思与纠错（规则化，毫秒级本地决策）：

        - 连续失败 ≥2 → 记录原因假设并提示后续步骤降风险/换参数；
        - 价值为零且过半 → 提前标记低产（下步剪枝门会拦截）。
        """
        if route.consecutive_failures >= 2:
            route.notes.append(
                f"reflect: 连续失败 {route.consecutive_failures} 次，调整策略（降速/换参数）"
            )
            route.risk_score = min(route.risk_score + 0.1, 1.0)  # 失败后提速重试会抬升暴露风险
        elif route.steps_executed >= 3 and route.value_score == 0:
            route.notes.append("reflect: 3 步无有效产出，进入低产观察")

    # ------------------------------------------------------------ 汇总上报

    def _summarize(self, task: Task, routes: list[RouteState]) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type.value,
            "routes_total": len(routes),
            "routes_done": sum(1 for r in routes if r.status == TaskStatus.DONE),
            "routes_pruned": sum(1 for r in routes if r.status == TaskStatus.PRUNED),
            "routes_failed": sum(1 for r in routes if r.status == TaskStatus.FAILED),
            "routes_cancelled": sum(1 for r in routes if r.status == TaskStatus.CANCELLED),
            "status": task.status.value,
            "cache_stats": self.cache.stats(),
            "route_health": {r.name: round(r.health, 3) for r in routes},
        }

    async def _report(
        self, task: Task, routes: list[RouteState], summary: dict[str, Any]
    ) -> None:
        """上报：任务状态写黑板任务状态层 + 结果事件发布总线（4.3.3）。"""
        assert self.ctx.audit is not None
        self.ctx.blackboard.write(
            BlackboardLayer.TASK_STATE,
            task.task_id,
            {"task": task.model_dump(mode="json"), "summary": summary,
             "routes": [r.model_dump(mode="json") for r in routes]},
            agent_type=self.agent_type,
            updated_by=self.agent_id,
            dedup_key=f"task|{task.task_id}",
        )
        self.ctx.audit.log(f"task.{task.status.value}", self.agent_id,
                           {"task_id": task.task_id, "summary": summary})
        await self.ctx.bus.publish(Event(
            topic="task.result", data_id=task.task_id, source=self.agent_id,
            payload={"agent_type": self.agent_type.value, "summary": summary},
        ))

    # ------------------------------------------------------------ LLM 步骤决策

    async def _llm_chat_json(self, role: str, user_obj: dict[str, Any],
                             *, max_user_chars: int = 6000,
                             timeout_seconds: int | None = None,
                             reasoning_effort: str = "",
                             task: Task | None = None,
                             _allow_ask: bool = True) -> dict[str, Any] | None:
        """云端 LLM 决策调用（JSON 应答）；不可用/解析失败 → None（调用方回退规则）。

        统一的降级语义：LLM 是操作建议的主路径，规则模板是骨架与兜底；
        任何一步 LLM 失败都不阻断任务（规则引擎裁决原则 4.9.5 不动）。
        timeout_seconds 透传网关：重推理角色（如解题环）须放宽单次调用硬顶。
        task 非空时把灵枢任务包携带的任务意图（mission/node_title）并入
        决策上下文——开局提示词只进灵枢，执行者从任务包知道自己服务于
        什么目标、交付什么。
        ask 通道：决策可带 ask_user 向用户提问（挂起等答、答复回灌重裁），
        见 _settle_ask；重裁调用经 _allow_ask=False 关闭再问（至多一问）。
        """
        if not self.enable_llm_planning:
            return None
        assert self.ctx.audit is not None
        if task is not None:
            intent = {"mission": str(task.payload.get("mission", ""))[:600],
                      "node": str(task.payload.get("node_title", ""))[:120]}
            rules = str(task.payload.get("rules", "")).strip()
            if rules:
                intent["rules"] = rules[:600]
            exps = task.payload.get("recent_experience") or []
            if exps:
                intent["experience"] = [str(e)[:200] for e in exps[-3:]]
            intent = {k: v for k, v in intent.items() if v}
            if intent:
                user_obj = {**user_obj, "task_intent": intent}
        world = self._world_state()
        if world:
            user_obj = {**user_obj, "world_state": world}
        # 灵枢督导：主 Agent 按全局实时进度给出的方向建议（上下文而非指令）。
        # 一条建议只注入一次（ts 水位）——同一条建议跟着每次决策重复出现
        # 只是花 Token 的脏上下文；换新建议（ts 更新）才再次注入
        steering = (self.ctx.ring.steering_for(self.agent_type.value)
                    if self.ctx.ring is not None else None)
        if steering and float(steering.get("ts") or 0) > self._steer_seen_ts:
            user_obj = {**user_obj, "灵枢督导": {
                "hint": steering["hint"], "rationale": steering.get("rationale", "")}}
            self._steer_seen_ts = float(steering.get("ts") or 0)
        # 后续执行读取最新持久化事实；业务建模本身仍以本次直接观测为准。
        if task is not None and role != "business_analyst" and "business_context" not in user_obj:
            shared = shared_context(getattr(self.ctx, "blackboard", None))
            if shared:
                user_obj = {**user_obj, "shared_context": shared}
        if task is not None:
            from spiritscout.resources.store import ResourceStore

            resources = getattr(getattr(self.ctx, "knowledge", None), "resources", None)
            if isinstance(resources, ResourceStore):
                query = str(task.payload.get("node_title", "")) + " " + role.replace("_", " ")
                user_obj = {**user_obj, "reference_skills": [
                    {"id":item["skill_id"],"version":item.get("version",0),"description":item.get("description",""),
                     "reference":item.get("body","")[:1000]} for item in resources.skills(query,include_body=True)
                    if item.get("enabled",True)][:3]}
        # 询问通道说明单独保留，观察数据按完整结构缩减，不能截断 JSON。
        budget = max_user_chars - (len(_ASK_CHANNEL_NOTE) if _allow_ask else 0)

        from spiritscout.gateway.context import decision_memory

        execution = current_execution_scope.get() or ExecutionScope()
        memory_key = f"{task.task_id if task else 'local'}:{execution.route_id}:{role}"
        try:
            resp = await chat_decision(
                self.ctx.gateway,
                decision_messages(self._system_prompt() + f"\nROLE: {role}"
                                  + ("\n" + _ASK_CHANNEL_NOTE if _allow_ask else ""),
                                  user_obj, max(budget, 500)),
                audit=self.ctx.audit, actor=self.agent_id,
                memory=decision_memory(self, memory_key),
                task_type="execution", response_format="json",
                timeout_seconds=timeout_seconds,
                reasoning_effort=reasoning_effort,
            )
            data = extract_json(resp.content)
            if not isinstance(data, dict):
                return None
            return await self._settle_ask(
                data, role=role, user_obj=user_obj,
                max_user_chars=max_user_chars,
                timeout_seconds=timeout_seconds,
                reasoning_effort=reasoning_effort, task=task,
                allow_ask=_allow_ask)
        except Exception as exc:  # noqa: BLE001 —— 降级为规则骨架，不阻断（留痕可见）
            self.ctx.audit.log("llm.step_fallback", self.agent_id,
                               {"role": role, "error": str(exc)[:200]})
            return None

    async def _settle_ask(self, data: dict[str, Any], *, role: str,
                          user_obj: dict[str, Any], max_user_chars: int,
                          timeout_seconds: int | None,
                          reasoning_effort: str,
                          task: Task | None,
                          allow_ask: bool = True) -> dict[str, Any] | None:
        """决策里的 ask_user 落地：向用户发问（挂起等答），答复回灌重裁本决策。

        会话停止/用户未给明确答复 → 按模型原决策继续（与 CTF ask_user 同
        语义，问题不可代答也不阻塞收尾）；会话未接询问通道时剥除该字段并
        留痕——提示词承诺过的能力缺通道时至少可见。答复有效则带着答复重新
        发起同参决策，重裁调用不再开放提问（一轮决策至多一问，防循环）。
        """
        ask = data.pop("ask_user", None)
        if not allow_ask:
            # 重裁不再开放提问：一轮决策至多一问，防问答循环
            return data
        if not isinstance(ask, dict) or not str(ask.get("question") or "").strip():
            return data
        assert self.ctx.audit is not None
        question = str(ask["question"]).strip()[:2000]
        options = [str(o).strip()[:200] for o in (ask.get("options") or [])[:6]
                   if str(o).strip()]
        from spiritscout.policy.questions import normalize_targets
        targets = normalize_targets(ask.get("targets"))
        multiple = bool(ask.get("multiple"))
        broker = self.ctx.questions
        if broker is None:
            self.ctx.audit.log("agent.ask_user", self.agent_id, {
                "role": role, "question": question[:300],
                "note": "会话未接询问通道，按原决策继续"})
            return data
        self.ctx.audit.log("agent.ask_user", self.agent_id, {
            "role": role, "question": question[:500],
            "options": options, "multiple": multiple, "targets": targets})
        result = await broker.ask(self.agent_type.value, question, options, targets,
                                  multiple=multiple)
        answer = str(result.get("answer") or "").strip()
        added = result.get("scope_added") or []
        self.ctx.audit.log("agent.ask_answered", self.agent_id, {
            "role": role, "answer": answer[:300], "scope_added": added})
        if result.get("cancelled") or not answer:
            return data
        return await self._llm_chat_json(
            role, {**user_obj, "用户答复": {
                "question": question, "answer": answer, "scope_added": added}},
            max_user_chars=max_user_chars, timeout_seconds=timeout_seconds,
            reasoning_effort=reasoning_effort, task=task, _allow_ask=False)

    # ------------------------------------------------------------ 提示词

    def _system_prompt(self) -> str:
        """稳定 system prompt：授权等运行时事实在 world_state 片段里更新。"""
        from spiritscout.agents.profiles import agent_system_prompt

        return agent_system_prompt(self.agent_type)

    def _world_state(self) -> dict[str, Any]:
        from spiritscout.gateway.world_state import snapshot

        guard = getattr(getattr(self.ctx, "policy", None), "guard", None)
        return snapshot(guard=guard)

    @abc.abstractmethod
    async def execute_step(
        self, task: Task, route: RouteState, plan: RoutePlan, step: dict[str, Any]
    ) -> StepResult:
        """子类实现：执行单步（工具调用/分析/写入黑板）。"""
