"""工具注册表：统一调度内置工具与 CLI/Docker 封装工具。

- 所有调用先经 PolicyEngine 风险门（敏感操作拦截）与 ScopeGuard（目标授权）；
- 失败自动重试与降级替代（4.3.1：fallback_tool 链）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import (
    PolicyViolation,
    SpiritScoutError,
    ToolExecutionError,
)
from spiritscout.tools.executor import ToolExecutor
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec

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
    ):
        self.executor = executor
        self.policy = policy
        self.audit = audit or NullAuditLogger()
        self.max_retries = max_retries
        self._specs: dict[str, ToolSpec] = {}
        self._builtins: dict[str, BuiltinFn] = {}

    # ------------------------------------------------------------ 注册

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def register_builtin(self, name: str, fn: BuiltinFn, spec: ToolSpec | None = None) -> None:
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
        """统一调用入口：策略风险门 → 重试 → 降级替代 → 结构化结果。"""
        spec = self.get(name)
        if self.policy is not None:
            try:
                await self.policy.ensure_tool_allowed(
                    spec, action=str(params.get("action", "")) or None)
            except PolicyViolation as exc:
                self.audit.log("tool.denied", "registry", {"tool": name, "reason": str(exc)})
                return ToolResult(tool=name, ok=False, error=str(exc), returncode=-3)
        attempts = 0
        last: ToolResult | None = None
        while attempts <= self.max_retries:
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
            if last.ok or last.timed_out:
                break
        if last is not None and not last.ok and spec.fallback_tool:
            self.audit.log("tool.fallback", "registry",
                           {"tool": name, "fallback": spec.fallback_tool, "reason": last.error})
            fb = await self.call(spec.fallback_tool, params, **builtin_ctx)
            fb.attempts = attempts + fb.attempts
            return fb
        assert last is not None
        return last
