"""可恢复的模型服务故障与重试间隔；不重试配置、鉴权或本地编程错误。"""
from __future__ import annotations

import math
import time
from email.utils import parsedate_to_datetime

import httpx

from spiritscout.config.settings import GatewayConfig


def status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    if value is None:
        value = getattr(getattr(error, "response", None), "status_code", None)
    return value if isinstance(value, int) else None


def recoverable(error: Exception) -> bool:
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else {}
    code = str(getattr(error, "code", "") or (detail.get("code", "") if isinstance(detail, dict) else ""))
    if code in {"insufficient_quota", "billing_hard_limit_reached", "invalid_api_key", "context_length_exceeded"}:
        return False
    status = status_code(error)
    if status is not None:
        return status in {408, 429, 500, 502, 503, 504, 529}
    return isinstance(error, (TimeoutError, ConnectionError, httpx.TimeoutException,
                              httpx.NetworkError, httpx.RemoteProtocolError)) or type(error).__name__ in {
                                  "APIConnectionError", "APITimeoutError"}


def _retry_after(error: Exception) -> float | None:
    # LiteLLM 的协议异常包装可能丢掉响应头，原始 HTTP 异常仍在异常链上。
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        headers = getattr(response, "headers", None) or getattr(current, "headers", None) or {}
        raw = headers.get("retry-after") or headers.get("Retry-After")
        milliseconds = headers.get("retry-after-ms")
        if raw is not None or milliseconds is not None:
            break
        current = current.__cause__ or current.__context__
    else:
        return None
    try:
        value = float(raw) if raw is not None else float(milliseconds) / 1000
    except (ValueError, TypeError):
        try:
            value = parsedate_to_datetime(str(raw)).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, value) if math.isfinite(value) else None


def retry_delay(error: Exception, retry_index: int, config: GatewayConfig) -> float:
    requested = _retry_after(error)
    if requested is not None:
        return requested
    return min(config.retry_max_seconds, config.retry_initial_seconds * 2 ** min(retry_index, 30))
