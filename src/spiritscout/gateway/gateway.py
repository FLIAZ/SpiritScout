"""多模型网关主类（4.9 / 6.10）。

 实现：路由模式（默认）+ 主备切换 + 预算治理 + token 计量。
 补齐：模式预算硬顶与用量降级（4.9.4）；投票 / 辩论 / 级联 / 异构角色
完整协作见 gateway.collab。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from spiritscout.config.settings import GatewayConfig, ModelEntry
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import BudgetExceeded, SpiritScoutError
from spiritscout.gateway.backends import (
    ChatBackend,
    DeltaCallback,
    MockBackend,
    backend_for,
    emit_delta,
)
from spiritscout.gateway.registry import ModelRegistry


@dataclass
class ChatResponse:
    content: str
    model_alias: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float
    fallback_used: bool = False
    reasoning: str = ""          # 推理模型思考内容（reasoning_content/thinking）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""


@dataclass
class BudgetTracker:
    """预算治理（4.9.4 成本预算 / 9.1 模型成本失控）。

    双层控制：全局 token/美元硬顶（超顶熔断）+ 按运行模式 token 硬顶；
    用量比例（pressure）供网关做「超预算自动降级到低档模型」的软控制。
    """

    budget_tokens: int
    budget_usd: float
    mode_budgets: dict[str, int] = field(default_factory=dict)
    used_tokens: int = 0
    used_usd: float = 0.0
    per_model_tokens: dict[str, int] = field(default_factory=dict)
    per_mode_tokens: dict[str, int] = field(default_factory=dict)
    mode: str = ""

    def check(self) -> None:
        if self.used_tokens >= self.budget_tokens or self.used_usd >= self.budget_usd:
            raise BudgetExceeded(
                f"模型预算耗尽: tokens {self.used_tokens}/{self.budget_tokens}, "
                f"usd {self.used_usd:.4f}/{self.budget_usd}"
            )
        if self.mode and self.mode in self.mode_budgets:
            cap = self.mode_budgets[self.mode]
            if self.per_mode_tokens.get(self.mode, 0) >= cap:
                raise BudgetExceeded(
                    f"模式 {self.mode} 预算熔断: tokens "
                    f"{self.per_mode_tokens[self.mode]}/{cap}（9.1 预算硬顶）"
                )

    def pressure(self) -> float:
        """用量压力 0~1：全局与当前模式预算中的最大占用比（降级阈值判定依据）。"""
        ratios = [self.used_tokens / max(self.budget_tokens, 1),
                  self.used_usd / max(self.budget_usd, 1e-9)]
        if self.mode and self.mode in self.mode_budgets:
            ratios.append(self.per_mode_tokens.get(self.mode, 0)
                          / max(self.mode_budgets[self.mode], 1))
        return min(max(ratios), 1.0)

    def record(self, alias: str, tokens: int, usd: float) -> None:
        self.used_tokens += tokens
        self.used_usd += usd
        self.per_model_tokens[alias] = self.per_model_tokens.get(alias, 0) + tokens
        if self.mode:
            self.per_mode_tokens[self.mode] = self.per_mode_tokens.get(self.mode, 0) + tokens


class ModelGateway:
    def __init__(
        self,
        registry: ModelRegistry,
        config: GatewayConfig | None = None,
        audit: AuditLogger | None = None,
        mock_backend: MockBackend | None = None,
    ):
        self.registry = registry
        self.config = config or GatewayConfig()
        self.audit = audit or NullAuditLogger()
        self.budget = BudgetTracker(
            self.config.budget_tokens, self.config.budget_usd,
            mode_budgets=dict(self.config.mode_budget_tokens),
        )
        self._mock = mock_backend or MockBackend()
        self._rate_state: dict[str, list[float]] = {}

    def set_mode(self, mode: str) -> None:
        """设置当前运行模式（src/ctf/competition）：模式预算计量与熔断生效。"""
        self.budget.mode = mode

    # ------------------------------------------------------------ 主入口

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        task_type: str = "execution",
        model_alias: str | None = None,
        required_capability: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: str | None = None,
        timeout_seconds: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> ChatResponse:
        """统一对话入口：路由（预算降级）→ 限流 → 调用（主备切换）→ 计量 → 审计。

        timeout_seconds：单次后端调用硬超时；缺省用网关配置。重推理角色
        （如解题环）可放宽，避免长链推理被全局默认值中途掐死。
        tools/on_delta：原生工具调用与流式增量（主备切换时若已吐出增量，
        切换前投递 reset 让消费端清屏，避免两次尝试的文本叠加）。"""
        self.budget.check()
        degraded = (model_alias is None
                    and self.config.degrade_at_ratio < 1.0
                    and self.budget.pressure() >= self.config.degrade_at_ratio)
        if degraded:
            # 预算压力超阈值：自动降级到低档模型（4.9.4），硬顶仍在（BudgetExceeded）
            entry = self.registry.route_degraded(
                task_type, required_capability=required_capability)
            self.audit.log("model.degraded", "gateway", {
                "task_type": task_type, "to_alias": entry.alias, "to_tier": entry.tier,
                "pressure": round(self.budget.pressure(), 3)})
        else:
            entry = self.registry.route(
                task_type, prefer_alias=model_alias,
                required_capability=required_capability,
            )
        chain = [entry, *self.registry.fallback_of(entry.alias)]

        # 流式增量与主备切换的衔接：已吐出的增量在切换前用 reset 清屏
        emitted = False

        async def _relay(kind: str, text: str) -> None:
            nonlocal emitted
            emitted = True
            await emit_delta(on_delta, kind, text)

        last_exc: Exception | None = None
        for idx, candidate in enumerate(chain):
            try:
                await self._rate_limit(candidate)
                if idx > 0 and emitted:
                    await emit_delta(on_delta, "reset", "")
                    emitted = False
                resp = await self._invoke(
                    candidate, messages,
                    temperature=temperature, max_tokens=max_tokens,
                    response_format=response_format,
                    timeout_seconds=timeout_seconds,
                    tools=tools, tool_choice=tool_choice,
                    on_delta=_relay if on_delta is not None else None,
                )
                if idx > 0:
                    resp.fallback_used = True
                    self.audit.log(
                        "model.fallback", "gateway",
                        {"from": entry.alias, "to": candidate.alias, "reason": str(last_exc)},
                    )
                return resp
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 —— 主备切换语义（4.9.3-5）
                last_exc = exc
                continue
        raise SpiritScoutError(f"模型调用全链失败（主备均不可用）: {last_exc}")

    # ------------------------------------------------------------ 内部

    async def _invoke(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int,
        response_format: str | None,
        timeout_seconds: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> ChatResponse:
        backend: ChatBackend = backend_for(entry, self._mock)
        started = time.perf_counter()
        raw = await backend.complete(
            entry, messages,
            temperature=temperature, max_tokens=max_tokens,
            timeout=timeout_seconds or self.config.request_timeout_seconds,
            response_format=response_format,
            tools=tools, tool_choice=tool_choice, on_delta=on_delta,
        )
        latency = (time.perf_counter() - started) * 1000
        total_tokens = raw["prompt_tokens"] + raw["completion_tokens"]
        cost = (
            raw["prompt_tokens"] / 1e6 * entry.cost_input_per_mtok
            + raw["completion_tokens"] / 1e6 * entry.cost_output_per_mtok
        )
        self.budget.record(entry.alias, total_tokens, cost)
        self.audit.log(
            "model.call", "gateway",
            {"alias": entry.alias, "tier": entry.tier, "tokens": total_tokens,
             "cost_usd": round(cost, 6), "latency_ms": round(latency, 1),
             "tool_calls": [c.get("name") for c in raw.get("tool_calls", [])]},
        )
        return ChatResponse(
            content=raw["content"],
            model_alias=entry.alias,
            prompt_tokens=raw["prompt_tokens"],
            completion_tokens=raw["completion_tokens"],
            cost_usd=cost,
            latency_ms=latency,
            reasoning=str(raw.get("reasoning", "") or ""),
            tool_calls=list(raw.get("tool_calls", []) or []),
            finish_reason=str(raw.get("finish_reason", "") or ""),
        )

    async def _rate_limit(self, entry: ModelEntry) -> None:
        """按模型 RPM 限流（注册表 rate_limit_rpm）。"""
        rpm = max(entry.rate_limit_rpm, 1)
        window = self._rate_state.setdefault(entry.alias, [])
        now = time.monotonic()
        while True:
            window[:] = [t for t in window if now - t < 60.0]
            if len(window) < rpm:
                window.append(now)
                return
            await asyncio.sleep(max(60.0 - (now - window[0]), 0.05))
            now = time.monotonic()

    # ------------------------------------------------------------ 状态

    def usage_report(self) -> dict:
        return {
            "used_tokens": self.budget.used_tokens,
            "used_usd": round(self.budget.used_usd, 6),
            "budget_tokens": self.budget.budget_tokens,
            "budget_usd": self.budget.budget_usd,
            "per_model_tokens": dict(self.budget.per_model_tokens),
            "per_mode_tokens": dict(self.budget.per_mode_tokens),
            "mode": self.budget.mode,
            "pressure": round(self.budget.pressure(), 3),
        }


class AgentBoundGateway:
    """按 Agent 绑定模型的网关包装（Web 编队页分配）。

    chat 未显式给 model_alias 时注入该 Agent 的分配别名；别名为空 = 不干预
    （维持档位路由现状）。其余属性（budget/usage_report/...）全部透传内层网关，
    预算计量、限流、主备切换、降级语义不变——本包装只改「谁来答」，不改治理。
    """

    def __init__(self, inner: ModelGateway, alias: str = "") -> None:
        self._inner = inner
        self._alias = alias

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> ChatResponse:
        if self._alias and not kwargs.get("model_alias"):
            kwargs["model_alias"] = self._alias
        return await self._inner.chat(messages, **kwargs)
