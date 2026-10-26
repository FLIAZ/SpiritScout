"""SDK 请求尝试审计：重试和取消单列，用量未知不冒充零消费。"""
from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

current_attempt_trace: ContextVar[AttemptTrace | None] = ContextVar("model_attempt_trace", default=None)


def _get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


class AttemptTrace:
    def __init__(self, audit: Any, actor: str, call: dict[str, Any]) -> None:
        self.audit, self.actor, self.call = audit, actor, call
        self.attempts: list[dict[str, Any]] = []

    def start(self, *, streaming: bool) -> None:
        self.finish("replaced_by_retry")
        row = {"attempt_id": f"{self.call['call_id']}:{len(self.attempts) + 1}",
               "attempt_index": len(self.attempts) + 1, "streaming": streaming,
               "usage_status": "unknown", "started": time.perf_counter(), "finished": False}
        self.attempts.append(row)
        self.audit.log("model.attempt_started", self.actor, {
            **self.call, "attempt_id": row["attempt_id"], "attempt_index": row["attempt_index"],
            "streaming": streaming, "usage_status": "unknown"})

    def usage(self, usage: Any) -> None:
        if usage is None or not self.attempts:
            return
        row = self.attempts[-1]
        for target, names in {"input_tokens": ("prompt_tokens", "input_tokens"),
                              "output_tokens": ("completion_tokens", "output_tokens")}.items():
            for name in names:
                value = _get(usage, name)
                if type(value) is int and value >= 0:
                    row[target] = value
                    break
        details = _get(usage, "prompt_tokens_details") or _get(usage, "input_tokens_details")
        cached = _get(details, "cached_tokens")
        if cached is None:
            cached = _get(usage, "cache_read_input_tokens")
        if cached is None:
            cached = _get(usage, "prompt_cache_hit_tokens")
        if type(cached) is int and cached >= 0:
            row["cached_input_tokens"] = cached
        if "input_tokens" in row and "output_tokens" in row:
            row["usage_status"] = "reported"
        elif "input_tokens" in row or "output_tokens" in row:
            row["usage_status"] = "partial"

    def finish(self, status: str) -> None:
        if not self.attempts or self.attempts[-1]["finished"]:
            return
        row = self.attempts[-1]
        row["finished"] = True
        self.audit.log("model.attempt_finished", self.actor, {
            **self.call, **{k: v for k, v in row.items() if k not in {"started", "finished"}},
            "status": status, "latency_ms": round((time.perf_counter() - row["started"]) * 1000, 1)})


async def tracked_request(request: Any, **kwargs: Any) -> Any:
    """记录框架发起的每次 SDK 请求，不宣称覆盖 SDK 内部不可见的 HTTP 重试。"""
    trace = current_attempt_trace.get()
    if trace is None:
        return await request(**kwargs)
    streaming = bool(kwargs.get("stream"))
    trace.start(streaming=streaming)
    try:
        response = await request(**kwargs)
    except BaseException as exc:
        import asyncio

        trace.finish("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
        raise
    trace.usage(_get(response, "usage"))
    if not streaming:
        trace.finish("completed")
    return response
