"""SDK 请求尝试审计：重试和取消单列，用量未知不冒充零消费。"""
from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

from spiritscout.gateway.metering import merge_usage, normalize_usage

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
        merge_usage(row, normalize_usage(usage))
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

    def accounted_usage(self, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        """每次尝试内合并，尝试间求和；最终响应只用于无 SDK trace 的后端。"""
        if not self.attempts:
            return dict(fallback or {"prompt_tokens": 0, "completion_tokens": 0, "usage_status": "unknown"})
        result: dict[str, Any] = {}
        for source, target in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                               ("cached_input_tokens", "cached_input_tokens"), ("cache_write_tokens", "cache_write_tokens")):
            values = [row[source] for row in self.attempts if source in row]
            result[target] = sum(values) if values else (0 if source in {"input_tokens", "output_tokens"} else None)
        # 任一次输入缺缓存计量，都不能把那部分输入当作零命中参与命中率。
        if any(row.get("input_tokens", 0) > 0 and "cached_input_tokens" not in row for row in self.attempts):
            result["cached_input_tokens"] = None
        result["usage_status"] = ("reported" if all(row["usage_status"] == "reported" for row in self.attempts)
                                  else "partial" if any("input_tokens" in row or "output_tokens" in row for row in self.attempts)
                                  else "unknown")
        return result

    def metrics(self) -> dict[str, int | None]:
        """已报告尝试合计供审计核对；未知尝试保持未知。"""
        result: dict[str, int | None] = {
            "sdk_attempts": len(self.attempts),
            "unreported_attempts": sum(row["usage_status"] != "reported" for row in self.attempts),
        }
        for field in ("input_tokens", "output_tokens"):
            values = [row[field] for row in self.attempts if field in row]
            result["reported_attempt_" + field] = sum(values) if values else None
        return result


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
