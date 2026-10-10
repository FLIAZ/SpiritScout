"""子 Agent 基座：路线生成 → 并发执行 → 健康度剪枝 → 局部反思 → 结果上报。

硬约束 C2 落地：
- 默认 3 条差异化路线（route_templates 按漏洞类/策略绑定流水线，
  LLM 仅在模板槽位内做参数级增强，不自由发散）；
- 路线间经 LocalSharedCache 共享中间结果；
- BranchHealthManager 每步评估、自动剪枝/合并；
- 局部反思：每 3 步复盘，连续失败自动调整（毫秒级本地决策，6.4）。
"""

from __future__ import annotations

import abc
import asyncio
import json
import re
import time
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
from spiritscout.gateway import ModelGateway
from spiritscout.gateway.backends import extract_json
from spiritscout.policy.engine import PolicyEngine
from spiritscout.tools.registry import ToolRegistry


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


@dataclass
class SubAgentContext:
    """子 Agent 运行上下文（依赖注入）。"""

    bus: EventBus
    blackboard: Blackboard
    convergence: ConvergenceEngine
    gateway: ModelGateway
    tools: ToolRegistry
    policy: PolicyEngine
    audit: AuditLogger | None = None
    builtin_ctx: dict[str, Any] = field(default_factory=dict)  # guard/limiter 等
    knowledge: Any = None                  # KnowledgeStore（经验库检索接线，None=不接）
    agent_models: dict[str, str] = field(default_factory=dict)  # 按 Agent 绑定模型
    # 环形协同台账：已尝试登记 + 灵枢督导（会话内所有 Agent 共持一份；
    # for_agent 的 dataclasses.replace 共享同一引用）
    ring: RingLedger | None = None

    def __post_init__(self) -> None:
        if self.audit is None:
            self.audit = NullAuditLogger()
        if self.ring is None:
            self.ring = RingLedger(self.audit)

    def for_agent(self, agent_key: str) -> SubAgentContext:
        """按 Agent 绑定模型：该 Agent 有分配 → 网关包装注入别名；
        无分配 → 原样返回（共享上下文零开销，维持档位路由）。"""
        alias = self.agent_models.get(agent_key, "")
        if not alias:
            return self
        import dataclasses

        from spiritscout.gateway import AgentBoundGateway
        return dataclasses.replace(
            self, gateway=AgentBoundGateway(self.gateway, alias))


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
        self.cache = LocalSharedCache(default_ttl_seconds=3600)
        self.agent_id = new_id(self.agent_type.value.replace("_", "-"))

    # ------------------------------------------------------------ 主流程

    async def run(self, task: Task) -> dict[str, Any]:
        """执行一个任务包：路线生成 → 并发执行 → 汇总上报。"""
        task.status = TaskStatus.RUNNING
        self.ctx.audit.log("task.start", self.agent_id,
                           {"task_id": task.task_id, "type": task.task_type, "target": task.target})
        plans = await self.plan_routes(task)
        routes = [RouteState(route_id=new_id("route"), task_id=task.task_id, name=p.name)
                  for p in plans]
        # 并发执行各路线（对等并行，无阶段瓶颈）
        await asyncio.gather(
            *(self._run_route(task, plan, route, routes)
              for plan, route in zip(plans, routes))
        )
        summary = self._summarize(task, routes)
        task.status = TaskStatus.DONE
        task.result_summary = summary
        await self._report(task, routes, summary)
        return summary

    # ------------------------------------------------------------ 路线生成

    async def plan_routes(self, task: Task) -> list[RoutePlan]:
        """生成差异化路线：模板为骨架，主 Agent 预算裁决条数，LLM 增广补缺。

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
            self._attach_kb_guidance(task, plans)   # 经验库检索接线（3.3.6 联动）
        if self.enable_llm_planning:
            plans = await self._llm_enrich(task, plans)
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
                        "existing_routes": [
                            {"name": p.name, "strategy": p.strategy} for p in plans],
                        "allowed_actions": {a: {"required_fields": f}
                                            for a, f in
                                            self.llm_route_actions.items()},
                        "slots": slots,
                        "constraints": (
                            "返回 JSON：{\"new_routes\": [{\"name\": 英文短名, "
                            "\"strategy\": 一句话策略, \"steps\": [{\"action\": "
                            "白名单动作之一, ...参数字段}]}]}。新路线必须与既有路线"
                            "思路不同；每条 1~6 步；最多 slots 条。"),
                    }, ensure_ascii=False)},
                ],
                task_type="execution", response_format="json",
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
        """按战术/技术 + 漏洞类检索红队/SRC 经验库，注入路线 meta（3.3.6）。

        - 红队经验库：按 DAG 下发的 tactic/technique 取 checklist 前置条件与步骤；
        - SRC 经验库：按任务 vuln_classes 取挖洞模板（步骤 + 合规红线）；
         guidance 是执行参考而非强制流程：路线仍以模板绑定为主。
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
                 "steps": t.get("step_sequence", []),
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

    async def _llm_enrich(self, task: Task, plans: list[RoutePlan]) -> list[RoutePlan]:
        """LLM 在模板槽位内做参数级增强；失败静默回退模板（不影响主流程）。"""
        try:
            resp = await self.ctx.gateway.chat(
                [
                    {"role": "system", "content": self._system_prompt() + "\nROLE: route_planner"},
                    {"role": "user", "content": json.dumps({
                        "task": task.task_type, "target": task.target,
                        "templates": [{"name": p.name, "strategy": p.strategy} for p in plans],
                    }, ensure_ascii=False)},
                ],
                task_type="execution", response_format="json",
            )
            data = extract_json(resp.content)
            hints = {r.get("name"): r for r in data.get("routes", []) if isinstance(r, dict)}
            for plan in plans:
                hint = hints.get(plan.name)
                if hint:
                    plan.meta["llm_hint"] = {k: v for k, v in hint.items() if k != "name"}
        except Exception:  # noqa: BLE001, S110 —— LLM 增强失败不阻断（规则引擎裁决原则）
            pass
        return plans

    # ------------------------------------------------------------ 路线执行

    async def _run_route(
        self, task: Task, plan: RoutePlan, route: RouteState,
        routes: list[RouteState] | None = None,
    ) -> None:
        total_steps = max(len(plan.steps), 1)
        for step in plan.steps:
            # 健康度剪枝门（每步评估；宽限期内不剪，见 BranchHealthManager）
            if self.health_prune_enabled and self.health_mgr.should_prune(route):
                self.health_mgr.prune(route, f"健康度 {route.health:.2f} 低于阈值")
                self.ctx.audit.log("route.pruned", self.agent_id,
                                   {"route": route.name, "health": round(route.health, 3)})
                break
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
                # 已尝试登记：结果价值供同伴避让参考（高价值先写不被低价值覆盖）
                if akey and self.ctx.ring is not None:
                    self.ctx.ring.record_attempt(
                        akey, agent_id=self.agent_id,
                        agent_name=self.agent_type.value, action=str(step.get("action", "")),
                        outcome=result.value, note=result.note)
            self._update_route(route, result, total_steps)
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
            route.status = TaskStatus.DONE
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
            route.consecutive_failures = 0
            route.value_score = min(route.value_score + result.value, 1.0)
            route.progress = min(route.progress + (result.progress_delta or 1.0 / total_steps), 1.0)
        else:
            route.consecutive_failures += 1
        route.risk_score = min(route.risk_score + result.risk_delta, 1.0)
        if result.note:
            route.notes.append(result.note[:300])
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
        self.ctx.audit.log("task.done", self.agent_id,
                           {"task_id": task.task_id, "summary": summary})
        await self.ctx.bus.publish(Event(
            topic="task.result", data_id=task.task_id, source=self.agent_id,
            payload={"agent_type": self.agent_type.value, "summary": summary},
        ))

    # ------------------------------------------------------------ LLM 步骤决策

    async def _llm_chat_json(self, role: str, user_obj: dict[str, Any],
                             *, max_user_chars: int = 6000,
                             timeout_seconds: int | None = None) -> dict[str, Any] | None:
        """云端 LLM 决策调用（JSON 应答）；不可用/解析失败 → None（调用方回退规则）。

        统一的降级语义：LLM 是操作建议的主路径，规则模板是骨架与兜底；
        任何一步 LLM 失败都不阻断任务（规则引擎裁决原则 4.9.5 不动）。
        timeout_seconds 透传网关：重推理角色（如解题环）须放宽单次调用硬顶。
        """
        if not self.enable_llm_planning:
            return None
        assert self.ctx.audit is not None
        # 灵枢督导：主 Agent 按全局实时进度给出的方向建议（上下文而非指令）
        steering = (self.ctx.ring.steering_for(self.agent_type.value)
                    if self.ctx.ring is not None else None)
        if steering:
            user_obj = {**user_obj, "灵枢督导": {
                "hint": steering["hint"], "rationale": steering.get("rationale", "")}}
        try:
            resp = await self.ctx.gateway.chat(
                [
                    {"role": "system", "content": self._system_prompt() + f"\nROLE: {role}"},
                    {"role": "user", "content":
                        json.dumps(user_obj, ensure_ascii=False)[:max_user_chars]},
                ],
                task_type="execution", response_format="json",
                timeout_seconds=timeout_seconds,
            )
            data = extract_json(resp.content)
            return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001 —— 降级为规则骨架，不阻断（留痕可见）
            self.ctx.audit.log("llm.step_fallback", self.agent_id,
                               {"role": role, "error": str(exc)[:200]})
            return None

    # ------------------------------------------------------------ 提示词

    def _system_prompt(self) -> str:
        """分模式 system prompt（显式授权边界，防拒答亦防越界）。"""
        scope = self.ctx.policy.guard.scope
        allowed = scope.allowed_domains + scope.allowed_cidrs + scope.allowed_urls
        return (
            f"你是灵探 SpiritScout 的 {self.agent_type.value} 子 Agent，执行已获授权的"
            f"安全测试任务。授权范围严格限于: {allowed}。禁止访问范围外任何目标；"
            f"禁止破坏性操作；所有发现必须有可复现证据。"
        )

    @abc.abstractmethod
    async def execute_step(
        self, task: Task, route: RouteState, plan: RoutePlan, step: dict[str, Any]
    ) -> StepResult:
        """子类实现：执行单步（工具调用/分析/写入黑板）。"""
