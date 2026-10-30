"""工具注册表：统一调度内置工具与 CLI/Docker 封装工具。

- 所有调用先经 PolicyEngine 风险门（危险动作即时审批）；测试边界由模型判断；
- 失败自动重试与降级替代（4.3.1：fallback_tool 链）。
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from spiritscout.agents.profiles import AgentProfile, get_profile
from spiritscout.core.attempts import AttemptLedger, exact_key
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import (
    PolicyViolation,
    SpiritScoutError,
    ToolExecutionError,
)
from spiritscout.core.models import AgentType, RunMode
from spiritscout.tools.executor import ToolExecutor
from spiritscout.tools.invocation import (
    ExecutionScope,
    InvocationContext,
    current_execution_scope,
    current_invocation,
)
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.tools.search_policy import is_search_tool
from spiritscout.tools.spec import (
    ActionCategory,
    RiskLevel,
    ToolKind,
    ToolResult,
    ToolSpec,
)

if False:  # TYPE_CHECKING 等价：避免运行时环（policy.engine 依赖 tools.spec）
    from spiritscout.policy.engine import PolicyEngine

# 内置工具函数签名：async fn(guard=..., limiter=..., **params) -> ToolResult
BuiltinFn = Callable[..., Awaitable[ToolResult]]


class ToolRegistry:
    def __init__(
        self,
        executor: ToolExecutor,
        *,
        policy: PolicyEngine | None = None,
        audit: AuditLogger | None = None,
        max_retries: int = 1,
        journal: ExecutionJournal | None = None,
        attempts: AttemptLedger | None = None,
    ):
        self.executor = executor
        self.policy = policy
        self.audit = audit or NullAuditLogger()
        self.max_retries = max_retries
        self.journal = journal
        self.attempts = attempts or AttemptLedger()
        self._generation = 0
        self._mutations = 0
        self._specs: dict[str, ToolSpec] = {}
        self._builtins: dict[str, BuiltinFn] = {}

    # ------------------------------------------------------------ 注册

    def register(self, spec: ToolSpec) -> None:
        self._generation += 1
        self._specs[spec.name] = spec

    def register_builtin(self, name: str, fn: BuiltinFn, spec: ToolSpec | None = None) -> None:
        self._generation += 1
        self._builtins[name] = fn
        if spec:
            self._specs[name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolExecutionError(f"未注册的工具: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._specs)

    # ------------------------------------------------------------ 调用

    async def call(self, name: str, params: dict[str, Any], **builtin_ctx) -> ToolResult:
        """每次调用独立编号，参数快照绑定审批；未决动作只留痕、不自动重放。"""
        spec = self.get(name)
        params = copy.deepcopy(params)
        refresh = bool(builtin_ctx.pop("_fresh", False))
        if name == "http_probe":
            refresh = bool(params.pop("refresh", False)) or refresh
        bypass = bool(builtin_ctx.pop("_dedup_bypass", False))
        environment_id = str(builtin_ctx.pop("_environment_id", "") or "")
        scope = current_execution_scope.get()
        bypass = bypass or bool(scope and (scope.agent_id == "verifier" or scope.task_id.startswith("verify-")))
        reusable = self._replay_safe(spec, params) and spec.reuse_ttl_seconds > 0 and not bypass
        mutates = not self._replay_safe(spec, params)
        if refresh or mutates:
            self._generation += 1
        digest = hashlib.sha256(json.dumps(
            params, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        invocation_id = f"inv-{uuid.uuid4().hex}"
        replay_safe = self._replay_safe(spec, params)
        context = InvocationContext(invocation_id, digest,
            (lambda action, allow: self.journal.approval(invocation_id, action, allow))
            if self.journal is not None else None)
        token = current_invocation.set(context)
        phase = "not_started"
        registered = False
        if mutates:
            self._mutations += 1

        def transition(status: str) -> None:
            nonlocal phase
            phase = status
            if self.journal is not None:
                self.journal.transition(invocation_id, status,
                                        effect="unknown" if status == "running"
                                        and not replay_safe else "none")

        try:
            if self.journal is not None:
                self.journal.begin(invocation_id, name, digest,
                                   "safe" if replay_safe else "never_automatic",
                                   params=params, scope=(dataclasses.replace(scope or ExecutionScope(), environment_id=environment_id)
                                                        if environment_id else scope))
                registered = True
            async def perform() -> ToolResult:
                result = await self._call(name, params, transition=transition,
                                          preauthorized=reusable, **builtin_ctx)
                if result.invocation_id and result.invocation_id != invocation_id:
                    result.source_invocation_id = result.source_invocation_id or result.invocation_id
                result.invocation_id = invocation_id
                return result

            denied = await self._authorize(name, params, transition) if reusable else None
            if denied is not None:
                result = denied
            elif reusable and not refresh and not self._mutations:
                guard = builtin_ctx.get("guard") or getattr(self.policy, "guard", None)
                scope_model = getattr(guard, "scope", None)
                auth = scope_model.model_dump(mode="json") if callable(getattr(scope_model, "model_dump", None)) else {}
                approved = guard.approved_targets() if callable(getattr(guard, "approved_targets", None)) else []
                auth = {**auth, "approved": sorted(approved)} if isinstance(auth, dict) and isinstance(approved, (set, list, tuple)) else {}
                key_parameters = {**params, "method": str(params.get("method", "GET")).upper()} if name == "http_probe" else params
                key = exact_key(name, "", key_parameters, {
                    "identity": scope.identity_id if scope else "",
                    "environment": environment_id or (scope.environment_id if scope else ""),
                    "workspace": str(builtin_ctx.get("workspace_scope") or ""),
                    "generation": self._generation, "authorization": auth,
                    "run": self.journal.run_id if self.journal is not None else id(self)})
                result, reuse = await self.attempts.execute(key, perform, ttl=spec.reuse_ttl_seconds)
                if reuse != "executed":
                    result.source_invocation_id = result.source_invocation_id or result.invocation_id
                    result.reuse = reuse
                    transition("running")
                    self.audit.log("attempt.reused", scope.agent_id if scope else "registry", {
                        "key": key, "invocation_id": invocation_id, "source_invocation_id": result.source_invocation_id,
                        "kind": reuse})
            else:
                result = await perform()
            result.invocation_id = invocation_id
            result.execution_status = ("denied" if phase != "running" else
                                       "succeeded" if result.ok else "failed")
            result.effect_status = ("none" if replay_safe or phase != "running" else
                                    "applied" if result.ok else "unknown")
            if self.journal is not None:
                self.journal.transition(invocation_id, result.execution_status,
                                        effect=result.effect_status, result=result)
            return result
        except (Exception, asyncio.CancelledError) as exc:
            if self.journal is not None and registered:
                status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "unknown"
                self.journal.transition(invocation_id, status,
                                        effect="unknown" if phase == "running" else "none")
            raise
        finally:
            current_invocation.reset(token)
            if mutates:
                self._mutations -= 1
                self._generation += 1

    @staticmethod
    def _replay_safe(spec: ToolSpec, params: dict[str, Any]) -> bool:
        return (spec.risk == RiskLevel.READONLY
                and spec.automatic_replay
                and params.get(spec.action_param) in (None, ActionCategory.ANALYSIS.value)
                and spec.name != "sandbox_exec"
                and (spec.name != "http_probe" or str(params.get("method", "GET")).upper()
                     in {"GET", "HEAD", "OPTIONS"}))

    async def _authorize(self, name: str, params: dict[str, Any], transition: Callable[[str], None]) -> ToolResult | None:
        spec = self.get(name)
        # 行为声明在统一入口校验，不能依赖某个 Agent 的包装函数。
        # MCP 的 action 可能是远端业务参数；其风险仍由工具规格门把关。
        raw_action = params.get(spec.action_param)
        if spec.kind == ToolKind.MCP and spec.action_param == "action" and (
            not isinstance(raw_action, str) or raw_action not in tuple(a.value for a in ActionCategory)
        ):
            raw_action = None
        action = None
        required = spec.action_required or (
            name == "http_probe"
            and str(params.get("method", "GET")).upper() not in {"GET", "HEAD", "OPTIONS"}
        )
        if raw_action is not None:
            try:
                action = ActionCategory(raw_action).value
            except (ValueError, TypeError):
                return ToolResult(tool=name, ok=False, returncode=-4,
                                  error="action 类别未知，请如实声明："
                                  + ", ".join(a.value for a in ActionCategory))
        if required and action is None:
            return ToolResult(tool=name, ok=False, returncode=-4,
                              error=f"缺少 {spec.action_param} 行为声明，请根据本次实际操作补充后重试")
        if action not in (None, ActionCategory.ANALYSIS.value) and self.policy is None:
            return ToolResult(tool=name, ok=False, returncode=-3,
                              error="敏感动作缺少策略与即时审批通道，未执行")
        if spec.allowed_params:
            unknown = set(params) - spec.allowed_params
            if unknown:
                reason = f"工具 {name} 非法参数: {sorted(unknown)}"
                self.audit.log("tool.denied", "registry", {"tool": name, "reason": reason})
                return ToolResult(tool=name, ok=False, error=reason, returncode=-4)
        if self.policy is not None:
            try:
                if (not self.policy.automatic_approval
                        and self.policy.requires_approval(spec, action)):
                    transition("awaiting_approval")
                await self.policy.ensure_tool_allowed(
                    spec, action=action)
            except PolicyViolation as exc:
                self.audit.log("tool.denied", "registry", {"tool": name, "reason": str(exc)})
                return ToolResult(tool=name, ok=False, error=str(exc), returncode=-3)
        return None

    async def _call(self, name: str, params: dict[str, Any], *,
                    transition: Callable[[str], None], preauthorized: bool = False, **builtin_ctx) -> ToolResult:
        """统一调用入口：工具权限与危险动作审批 → 执行/重试 → 结构化结果。"""
        # 可复用调用已在查缓存前逐次授权；实际执行不能再次写同一调用的审批记录。
        denied = None if preauthorized else await self._authorize(name, params, transition)
        if denied is not None:
            return denied
        spec = self.get(name)
        attempts = 0
        last: ToolResult | None = None
        # 侵入性工具不自动重放，避免网络抖动把一次有副作用的调用变成两次。
        replay_safe = self._replay_safe(spec, params)
        retry_limit = self.max_retries if replay_safe else 0
        transition("running")
        while attempts <= retry_limit:
            attempts += 1
            if name in self._builtins:
                last = await self._builtins[name](**params, **builtin_ctx)
            elif spec.kind in (ToolKind.CLI, ToolKind.DOCKER):
                last = await self.executor.run(spec, params)
            elif spec.kind == ToolKind.MCP:
                raise ToolExecutionError("MCP 工具执行器预留，后续启用")
            else:  # pragma: no cover
                raise SpiritScoutError(f"工具 {name} 类型无法调度: {spec.kind}")
            last.attempts = attempts
            if last.ok or last.timed_out or last.parsed.get("retryable") is False:
                break
        if (last is not None and not last.ok and spec.fallback_tool and replay_safe
                and last.parsed.get("retryable") is not False):
            self.audit.log("tool.fallback", "registry",
                           {"tool": name, "fallback": spec.fallback_tool, "reason": last.error})
            fb = await self.call(spec.fallback_tool, params, **builtin_ctx)
            fb.attempts = attempts + fb.attempts
            return fb
        assert last is not None
        return last



class AgentToolView:
    """按角色收窄后的工具视图。

    角色提示词中的工具边界同时落实到调用层。即使模型生成了越权工具名，
    也会在进入策略与执行器前被拒绝并留下审计记录。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        agent: AgentType | str,
        *,
        audit: AuditLogger | None = None,
    ) -> None:
        self._registry = registry
        self.profile: AgentProfile = get_profile(agent)
        self.audit = audit or registry.audit

    def allows(self, name: str) -> bool:
        if getattr(self._registry.policy, "mode", None) == RunMode.COMPETITION:
            spec = self._registry._specs.get(name)
            if is_search_tool(name) or (spec is not None and spec.internet_search):
                return False
        if self.profile.allows_tool(name):
            return True
        if self.profile.key in {AgentType.MASTER, AgentType.SECRETARY, AgentType.REPORT}:
            return False
        try:
            spec = self._registry.get(name)
        except ToolExecutionError:
            return False
        return spec.kind == ToolKind.MCP and self.profile.key.value in spec.allowed_agents

    def _allowed(self, name: str) -> bool:
        return self.allows(name)

    def get(self, name: str) -> ToolSpec:
        if not self._allowed(name):
            raise ToolExecutionError(
                f"Agent {self.profile.key.value} 无权使用工具 {name}")
        return self._registry.get(name)

    def names(self) -> list[str]:
        return [name for name in self._registry.names() if self._allowed(name)]

    async def call(self, name: str, params: dict[str, Any], **builtin_ctx) -> ToolResult:
        if not self._allowed(name):
            reason = f"Agent {self.profile.key.value} 无权使用工具 {name}"
            self.audit.log("tool.denied", self.profile.key.value, {
                "tool": name,
                "reason": reason,
            })
            return ToolResult(tool=name, ok=False, error=reason, returncode=-5)
        if self.profile.key == AgentType.VERIFIER:
            builtin_ctx["_dedup_bypass"] = True
        return await self._registry.call(name, params, **builtin_ctx)
