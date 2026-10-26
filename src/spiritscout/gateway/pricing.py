"""官方 DeepSeek 人民币价格快照；独立于美元账本，不冒充平台结算金额。"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from spiritscout.config.settings import ModelEntry

# 来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/，2026-10-23 核对。
# 工作日高峰可能遇到法定节假日；缺少节假日日历时如实保留上下界。
_PRICE_VERSION = "deepseek-cny-2026-10-23"
_OFF_PEAK = {"deepseek-flash": (0.02, 1.0, 4.0), "deepseek-v4-pro": (0.15, 4.5, 13.5)}


def estimate_deepseek_cny(entry: ModelEntry, ts: float, input_tokens: int,
                          cached_tokens: int | None, output_tokens: int) -> dict[str, float | str]:
    """只采用官方域名、已知模型和实测缓存量；未知用量不伪造未命中。"""
    name = entry.model.removeprefix("openai/")
    if (urlsplit(entry.base_url).hostname != "api.deepseek.com" or name not in _OFF_PEAK
            or cached_tokens is None):
        return {}
    cached = min(max(0, cached_tokens), input_tokens)
    read, miss, output = _OFF_PEAK[name]
    base = (cached * read + (input_tokens - cached) * miss + output_tokens * output) / 1e6
    local = datetime.fromtimestamp(ts, ZoneInfo("Asia/Shanghai"))
    weekday_peak = local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18)
    return {"estimated_cost_cny_min": round(base, 8),
            "estimated_cost_cny_max": round(base * (2 if weekday_peak else 1), 8),
            "pricing_version": _PRICE_VERSION,
            "pricing_period": "peak_or_public_holiday" if weekday_peak else "off_peak"}
