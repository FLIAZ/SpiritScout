"""模型档案只读核对；声明、官方参考与实际请求分列，不探测付费端点。"""
from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from spiritscout.config.settings import ModelEntry


def model_diagnostics(entries: list[ModelEntry], calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    reports = []
    for entry in entries:
        if entry.provider in {"ollama", "mock"}:
            continue
        issues = []
        reference: dict[str, Any] = {}
        host = urlsplit(entry.base_url).hostname
        if host == "open.bigmodel.cn" and entry.model == "glm-5.3":
            reference = {"source": "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
                         "checked_on": "2026-11-06", "context_window": "1M",
                         "max_output_tokens": "128K",
                         "reasoning_effort_levels": ["low", "high", "max"],
                         "note": "官方声明；具体兼容协议及账号仍需验证，不代表已调用端点。"}
            if set(entry.reasoning_effort_levels) - set(reference["reasoning_effort_levels"]):
                issues.append("reasoning_levels_differ_from_reference")
        elif host == "api.deepseek.com" and entry.model in {"deepseek-flash", "deepseek-v4-pro"}:
            reference = {"source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                         "checked_on": "2026-11-06", "context_window": "1M",
                         "max_output_tokens": "384K",
                         "pricing": "已有独立人民币分时估算，不代填美元价格。"}
        if not entry.context_window:
            issues.append("context_window_unknown")
        if not entry.reasoning_effort_levels:
            issues.append("reasoning_effort_undeclared")
        if entry.billing_scheme == 'manual' and (not entry.cost_input_per_mtok or not entry.cost_output_per_mtok):
            issues.append("usd_prices_unconfigured")
        if entry.billing_scheme == 'manual' and entry.cost_cached_input_per_mtok is None:
            issues.append("cache_read_price_unknown")
        if entry.billing_scheme == 'manual' and entry.prompt_caching == "anthropic_ephemeral" and entry.cost_cache_write_per_mtok is None:
            issues.append("cache_write_price_unknown")
        if entry.api_format == "anthropic_messages" or (
                entry.api_format == "auto" and entry.provider == "anthropic_compatible"):
            issues.append("anthropic_effort_transport_not_live_verified")
        observed = [row for row in calls or [] if (row.get("model_key") or row.get("model")) == entry.key]
        reports.append({"model_key": entry.key, "model": entry.model, "provider_ref": entry.provider_ref,
                        "billing_scheme": entry.billing_scheme, "billing_plan": entry.billing_plan,
                        "api_format": entry.api_format, "provider": entry.provider,
                        "context_window": entry.context_window,
                        "max_output_tokens": entry.max_output_tokens, "capabilities": entry.capabilities,
                        "usd_rates_per_mtok": {"input": entry.cost_input_per_mtok,
                            "output": entry.cost_output_per_mtok, "cached_input": entry.cost_cached_input_per_mtok,
                            "cache_write": entry.cost_cache_write_per_mtok},
                        "reasoning_effort_levels": entry.reasoning_effort_levels,
                        "issues": issues, "reference": reference,
                        "observed_calls": len(observed),
                        "sent_effort_counts": dict(Counter(row["sent_reasoning_effort"] or "endpoint_default"
                            for row in observed if row.get("sent_reasoning_effort") is not None)),
                        "effort_unknown_calls": sum(row.get("sent_reasoning_effort") is None for row in observed)})
    return {"models": reports, "live_verified": False,
            "note": "只读核对，不改配置、不发模型请求；空档位不等于关闭思考，零单价不等于免费。"
                    "标准 API 单价不能代替套餐、资源包或代理报价。窗口与输出上限保留官网标示单位，"
                    "不自动换算 K/M 或覆盖配置。"}
