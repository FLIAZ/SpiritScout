"""费用覆盖率：已知金额与完整费用分开，币种不混加，缺配置不当作免费。"""
from __future__ import annotations

import math
from typing import Any

from spiritscout.config.settings import ModelEntry


def configured_usd(entry: ModelEntry, usage: dict[str, Any]) -> dict[str, Any]:
    rates = {"input": entry.cost_input_per_mtok, "output": entry.cost_output_per_mtok,
             "cached_input": entry.cost_cached_input_per_mtok,
             "cache_write": entry.cost_cache_write_per_mtok}
    result: dict[str, Any] = {"usd_rate_snapshot": rates, "estimated_cost_usd": None,
                              "usd_pricing_status": "unknown_prices"}
    # 现有配置约定 input/output=0 表示未配置，不推断免费或套餐的边际价格。
    if not rates["input"] or not rates["output"]:
        return result
    read, write = usage.get("cached_input_tokens"), usage.get("cache_write_tokens")
    if read is None or (entry.prompt_caching == "anthropic_ephemeral" and write is None):
        return {**result, "usd_pricing_status": "unknown_cache_usage"}
    if read and rates["cached_input"] is None or write and rates["cache_write"] is None:
        return {**result, "usd_pricing_status": "unknown_cache_price"}
    inputs, outputs = usage["prompt_tokens"], usage["completion_tokens"]
    read = min(read, inputs)
    write = min(write or 0, inputs - read)
    cost = ((inputs - read - write) * rates["input"] + outputs * rates["output"]
            + read * (rates["cached_input"] or 0) + write * (rates["cache_write"] or 0)) / 1e6
    return {**result, "estimated_cost_usd": cost, "usd_pricing_status": "configured"}


def _amount(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def cost_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """只用调用时的价格快照；部分已报告费用保留，但完整均摊指标须全覆盖。"""
    def currency(name: str) -> dict[str, Any]:
        amounts = []
        legacy = 0
        for row in rows:
            if name == "usd":
                low = high = row.get("estimated_cost_usd")
                # 旧账本正金额保留为历史估算；不重写或按当前配置重算。
                if not row.get("usd_pricing_status") and low is None and (row.get("cost_usd") or 0) > 0:
                    low = high = row["cost_usd"]
                    legacy += 1
            else:
                low, high = row.get("estimated_cost_cny_min"), row.get("estimated_cost_cny_max")
            if _amount(low) and _amount(high) and low <= high:
                amounts.append((low, high))
        incomplete = sum(row.get("usage_status") != "reported" or bool(row.get("unreported_attempts"))
                         for row in rows)
        return {"known_min": sum(a for a, _ in amounts) if amounts else None,
                "known_max": sum(b for _, b in amounts) if amounts else None,
                "priced_calls": len(amounts), "unpriced_calls": len(rows) - len(amounts),
                "incomplete_usage_calls": incomplete,
                "legacy_estimate_calls": legacy,
                "complete": bool(rows) and len(amounts) == len(rows) and incomplete == 0 and legacy == 0}

    plans: dict[str, Any] = {}
    for row in rows:
        if not row.get('billing_scheme', '').endswith('_token_plan'):
            continue
        key = row.get('provider_ref') or row['billing_scheme']
        group = plans.setdefault(key, {'scheme': row['billing_scheme'], 'calls': 0, 'tokens': 0,
            'credits_min': 0.0, 'credits_max': 0.0, 'credit_priced_calls': 0, 'allocated_cost': None})
        group['calls'] += 1
        group['tokens'] += row.get('tokens', row.get('input_tokens', 0) + row.get('output_tokens', 0))
        if _amount(row.get('estimated_plan_credits_min')) and _amount(row.get('estimated_plan_credits_max')):
            group['credits_min'] += row['estimated_plan_credits_min']
            group['credits_max'] += row['estimated_plan_credits_max']
            group['credit_priced_calls'] += 1
    for group in plans.values():
        if not group['credit_priced_calls']:
            group['credits_min'] = group['credits_max'] = None
    return {"usd": currency("usd"), "cny": currency("cny"), "kind": "estimate", 'subscriptions': plans}


def cost_per_result(costs: dict[str, Any], count: int) -> dict[str, Any]:
    return {name: ({"min": costs[name]["known_min"] / count,
                    "max": costs[name]["known_max"] / count}
                   if count > 0 and costs[name]["complete"] else None)
            for name in ("usd", "cny")}
