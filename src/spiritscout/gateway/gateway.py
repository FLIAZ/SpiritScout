"""多模型网关主类（4.9 / 6.10）。

 实现：路由模式（默认）+ 主备切换 + 预算治理 + token 计量。
 补齐：模式预算硬顶与用量降级（4.9.4）；投票 / 辩论 / 级联 / 异构角色
完整协作见 gateway.collab。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from spiritscout.config.settings import GatewayConfig, ModelEntry
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.gateway.backends import (
    ChatBackend,
    DeltaCallback,
    MockBackend,
    backend_for,
    emit_delta,
)
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope


@dataclass
class ChatResponse:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float
    fallback_used: bool = False
    reasoning: str = ""          # 推理模型思考内容（reasoning_content/thinking）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""


def output_lost(resp: ChatResponse) -> bool:
    """正文为空或被端点掐断：思考型模型可能把输出额度耗在思考上，正文
    只剩半截 JSON——决策通道据此加大余量重试，而不是把残缺当决策缺席
    静默降级（空报告的代价远大于重试一次的 token）。"""
    return (not str(resp.content or "").strip()) or resp.finish_reason in ("length", "max_tokens")


async def chat_decision(
    gateway: ModelGateway,
    messages: list[dict[str, Any]],
    *,
    audit=None,
    actor: str = "",
    **kwargs: Any,
) -> ChatResponse:
    """决策 JSON 通道：输出为空或被端点截断时加大输出余量重试一次。

    思考型模型可能把输出额度耗在思考上，正文为空或只剩半截 JSON——解析
    失败会被上层当成决策缺席走规则回退，一次重试远比误判便宜。重试失败
    沿用原响应交由上层解析兜底，不掩盖原始结果。"""
    resp = await gateway.chat(messages, **kwargs)
    if output_lost(resp):
        if audit is not None:
            audit.log("llm.output_retry", actor, {
                "task_type": kwargs.get("task_type", ""),
                "finish_reason": resp.finish_reason,
                "content_chars": len(resp.content or "")})
        try:
            retried = await gateway.chat(messages, max_tokens=32768, **kwargs)
            if not output_lost(retried):
                resp = retried
        except Exception:  # noqa: BLE001, S110 —— 重试失败沿用原响应，解析端兜底
            pass
    return resp


@dataclass
class BudgetTracker:
    """用量计量（token/美元/按模型/按模式）。

    只计量不拦截：进度性的杀开关（预算耗尽/模式熔断）已移除——调查正酣时
    被后端规则掐断对用户是灾难；用量数据仅供控制台展示与成本复盘。
    """

    budget_tokens: int
    budget_usd: float
    mode_budgets: dict[str, int] = field(default_factory=dict)
    used_tokens: int = 0
    used_usd: float = 0.0
    per_model_tokens: dict[str, int] = field(default_factory=dict)
    per_mode_tokens: dict[str, int] = field(default_factory=dict)
    mode: str = ""
    call_log: list[dict] = field(default_factory=list)
    # 记账钩子：每笔调用后把用量快照外发（控制台实时展示，不等终场汇总）
    on_record: Any = None

    def record(self, model: str, tokens: int, usd: float, ts: float = 0) -> None:
        self.used_tokens += tokens
        self.used_usd += usd
        self.per_model_tokens[model] = self.per_model_tokens.get(model, 0) + tokens
        if self.mode:
            self.per_mode_tokens[self.mode] = self.per_mode_tokens.get(self.mode, 0) + tokens
        import time as _t
        self.call_log.append({
            "ts": ts or _t.time(), "model": model,
            "tokens": tokens, "cost_usd": usd, "mode": self.mode})
        if self.on_record is not None:
            # 展示面失败不拖累记账主链（sink 自身须可容错，如落盘瞬时失败）
            with contextlib.suppress(Exception):
                self.on_record(self.persist_snapshot())

    def persist_snapshot(self) -> dict:
        """落盘快照：实时计数 + 逐笔时序（进程续传时回灌 BudgetTracker）。"""
        snap = self.live_snapshot()
        snap.pop("live", None)
        snap["call_series"] = self.call_series()
        return snap

    def restore(self, snap: dict) -> None:
        """回灌上一进程的累计用量与逐笔时序（续传不重新从零计起）。"""
        self.used_tokens += int(snap.get("used_tokens", 0) or 0)
        self.used_usd += float(snap.get("used_usd", 0.0) or 0.0)
        for model, tokens in (snap.get("per_model_tokens") or {}).items():
            self.per_model_tokens[model] = self.per_model_tokens.get(model, 0) + int(tokens or 0)
        for mode, tokens in (snap.get("per_mode_tokens") or {}).items():
            self.per_mode_tokens[mode] = self.per_mode_tokens.get(mode, 0) + int(tokens or 0)
        self.call_log.extend(
            e for e in (snap.get("call_series") or []) if isinstance(e, dict))

    def live_snapshot(self) -> dict:
        """轻量实时快照（不含逐笔时序——文件随写随读，保持小体积）。"""
        return {
            "used_tokens": self.used_tokens,
            "used_usd": round(self.used_usd, 6),
            "per_model_tokens": dict(self.per_model_tokens),
            "per_mode_tokens": dict(self.per_mode_tokens),
            "mode": self.mode,
            "calls": len(self.call_log),
            "live": True,
        }

    def call_series(self) -> list[dict]:
        """逐笔调用记录（时序图表数据源）。"""
        return list(self.call_log)


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
        """设置当前运行模式（src/ctf/competition）：按模式分开计量用量。"""
        self.budget.mode = mode

    # ------------------------------------------------------------ 主入口

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        task_type: str = "execution",
        model: str | None = None,
        required_capability: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: str | None = None,
        timeout_seconds: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_delta: DeltaCallback | None = None,
        reasoning_effort: str = "",
    ) -> ChatResponse:
        """统一对话入口：路由 → 限流 → 调用（主备切换）→ 计量 → 审计。

        timeout_seconds：单次后端调用硬超时；缺省用网关配置。重推理角色
        （如解题环）可放宽，避免长链推理被全局默认值中途掐死。
        tools/on_delta：原生工具调用与流式增量（主备切换时若已吐出增量，
        切换前投递 reset 让消费端清屏，避免两次尝试的文本叠加）。
        reasoning_effort：思考强度取值，按候选模型各自声明的档位把关
        （未声明的取值不下发，见 _resolve_effort）。
        max_tokens：单次输出上限。None（默认）= 不设硬顶——是否限、限多少
        由模型档案声明（ModelEntry.max_output_tokens）或端点自定；上限只影响
        生成在哪停，设小了思考型模型会把额度耗在思考上、正文被截成半截。
        小通道（路由/连通性探测）可传定值收紧。"""
        entry = self.registry.route(
            task_type, prefer_model=model,
            required_capability=required_capability,
        )
        chain = [entry, *self.registry.fallback_of(entry.model)]

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
                    # 思考强度按每个候选模型各自声明的档位把关（主备档位可能不同）
                    reasoning_effort=self._resolve_effort(candidate, reasoning_effort),
                    task_type=task_type,
                )
                if idx > 0:
                    resp.fallback_used = True
                    self.audit.log(
                        "model.fallback", "gateway",
                        {"from": entry.model, "to": candidate.model, "reason": str(last_exc)},
                    )
                return resp
            except Exception as exc:  # noqa: BLE001 —— 主备切换语义（4.9.3-5）
                last_exc = exc
                continue
        raise SpiritScoutError(f"模型调用全链失败（主备均不可用）: {last_exc}")

    # ------------------------------------------------------------ 内部

    def _resolve_effort(self, entry: ModelEntry, requested: str) -> str:
        """思考强度把关：只下发该模型自己声明过的取值。

        各模型支持的思考强度并不一致（有的 low/medium/high，有的另有 ultra），
        所以档位由每个模型条目自行声明，后端不设固定候选；请求值不在声明内
        时不下发并留痕（不猜、不代填、不静默改档）。
        """
        want = (requested or "").strip()
        if not want:
            return ""
        if want in entry.reasoning_effort_levels:
            return want
        self.audit.log("model.effort_ignored", "gateway", {
            "model": entry.model, "requested": want,
            "declared": list(entry.reasoning_effort_levels),
            "reason": ("该模型未声明此思考强度取值" if entry.reasoning_effort_levels
                       else "该模型未声明思考强度"),
        })
        return ""

    async def _invoke(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int | None,
        response_format: str | None,
        timeout_seconds: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_delta: DeltaCallback | None = None,
        reasoning_effort: str = "",
        task_type: str = "execution",
    ) -> ChatResponse:
        backend: ChatBackend = backend_for(entry, self._mock)
        # 输出上限落点：调用点显式给定值优先（小通道定值）；否则按模型档案
        # 声明下发；档案也没声明就 None（请求不带该字段，端点自定）
        if max_tokens is None and entry.max_output_tokens > 0:
            max_tokens = entry.max_output_tokens
        started = time.perf_counter()
        wall = time.time()                      # 记账用墙钟（时序图按日聚合）
        scope = current_execution_scope.get() or ExecutionScope()
        actor = scope.agent_id or "gateway"
        call = {"call_id": f"model-{uuid.uuid4().hex}", "model": entry.model,
                "task_type": task_type, "task_id": scope.task_id, "route_id": scope.route_id}
        self.audit.log("model.started", actor, call)
        try:
            raw = await backend.complete(
                entry, messages,
                temperature=temperature, max_tokens=max_tokens,
                timeout=timeout_seconds or self.config.request_timeout_seconds,
                response_format=response_format,
                tools=tools, tool_choice=tool_choice, on_delta=on_delta,
                reasoning_effort=reasoning_effort,
            )
        except BaseException as exc:
            self.audit.log("model.cancelled" if isinstance(exc, asyncio.CancelledError)
                           else "model.failed", actor,
                           {**call, "error": str(exc)[:300],
                            "latency_ms": round((time.perf_counter() - started) * 1000, 1)})
            raise
        latency = (time.perf_counter() - started) * 1000
        total_tokens = raw["prompt_tokens"] + raw["completion_tokens"]
        cost = (
            raw["prompt_tokens"] / 1e6 * entry.cost_input_per_mtok
            + raw["completion_tokens"] / 1e6 * entry.cost_output_per_mtok
        )
        self.budget.record(entry.model, total_tokens, cost, ts=wall)
        self.audit.log(
            "model.call", actor,
            {**call, "tokens": total_tokens,
             "input_tokens": raw["prompt_tokens"], "output_tokens": raw["completion_tokens"],
             "cost_usd": round(cost, 6), "latency_ms": round(latency, 1),
             "effort": reasoning_effort,
             "tool_calls": [c.get("name") for c in raw.get("tool_calls", [])]},
        )
        return ChatResponse(
            content=raw["content"],
            model=entry.model,
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
        window = self._rate_state.setdefault(entry.model, [])
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
            "per_model_tokens": dict(self.budget.per_model_tokens),
            "per_mode_tokens": dict(self.budget.per_mode_tokens),
            "mode": self.budget.mode,
            "call_series": self.budget.call_series(),
        }


class AgentBoundGateway:
    """按 Agent 绑定模型的网关包装（Web 编队页分配）。

    chat 未显式给 model 时注入该 Agent 被指派的模型；模型为空 = 不干预
    （维持档位路由现状）。其余属性（budget/usage_report/...）全部透传内层网关，
    预算计量、限流、主备切换、降级语义不变——本包装只改「谁来答」，不改治理。
    """

    def __init__(self, inner: ModelGateway, model: str = "") -> None:
        self._inner = inner
        self._model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> ChatResponse:
        if self._model and not kwargs.get("model"):
            kwargs["model"] = self._model
        return await self._inner.chat(messages, **kwargs)
