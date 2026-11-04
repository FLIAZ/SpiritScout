"""端点用量归一化；流式 usage 是累计快照，不是可相加的增量。"""
from __future__ import annotations

from typing import Any


def get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def normalize_usage(usage: Any) -> dict[str, int]:
    def number(*values: Any) -> int | None:
        return next((v for v in values if type(v) is int and v >= 0), None)

    details = get(usage, "prompt_tokens_details") or get(usage, "input_tokens_details")
    cached = number(get(details, "cached_tokens"), get(usage, "cache_read_input_tokens"),
                    get(usage, "prompt_cache_hit_tokens"))
    write = number(get(usage, "cache_creation_input_tokens"), get(details, "cache_creation_tokens"))
    prompt = number(get(usage, "prompt_tokens"))
    inputs = prompt if prompt is not None else number(get(usage, "input_tokens"))
    # LiteLLM 的 prompt_tokens 已含缓存；原生 Claude 的 input_tokens 不含读写缓存。
    if prompt is None and inputs is not None and any(
            get(usage, key) is not None for key in ("cache_read_input_tokens", "cache_creation_input_tokens")):
        inputs += (cached or 0) + (write or 0)
    values = {"input_tokens": inputs,
              "output_tokens": number(get(usage, "completion_tokens"), get(usage, "output_tokens")),
              "cached_input_tokens": cached, "cache_write_tokens": write}
    return {key: value for key, value in values.items() if value is not None}


def merge_usage(current: dict[str, int], incoming: dict[str, int]) -> None:
    """后续非零字段覆盖，缺失字段及 SDK 补的零值不抹掉先前计量。"""
    for key, value in incoming.items():
        if value or key not in current:
            current[key] = value


def backend_usage(usage: dict[str, int]) -> dict[str, Any]:
    return {"prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
            "usage_status": "reported" if all(k in usage for k in ("input_tokens", "output_tokens"))
            else "partial" if usage else "unknown"}
