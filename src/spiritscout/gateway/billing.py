"""调用开始时固定官方报价；订阅费与 API 调用费用保持不同口径。"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from spiritscout.config.settings import ModelEntry
from spiritscout.gateway.official_prices import PriceCatalog


def official_endpoint(entry: ModelEntry) -> bool:
    try:
        url = urlsplit(entry.base_url)
        valid_url = url.scheme == 'https' and url.port in (None, 443) and not url.username
    except ValueError:
        return False
    hosts = {'glm': {'open.bigmodel.cn'}, 'kimi': {'api.kimi.com', 'api.moonshot.cn', 'api.moonshot.ai'},
             'deepseek': {'api.deepseek.com'}}
    if not (valid_url and url.hostname in hosts.get(entry.billing_scheme.split('_')[0], set())):
        return False
    if entry.billing_scheme.startswith('kimi_'):
        return (url.hostname == 'api.kimi.com' and url.path.rstrip('/') == '/coding/v1'
                if entry.billing_scheme == 'kimi_token_plan' else url.hostname == 'api.moonshot.cn')
    if entry.billing_scheme.startswith('glm_'):
        path = url.path.rstrip('/')
        # Anthropic/Responses 地址两种 Key 共用；独立 OpenAI 套餐地址不能按普通 API 估价。
        return (path in {'/api/coding/paas/v4', '/api/anthropic', '/api/v1'}
                if entry.billing_scheme == 'glm_token_plan' else path in {'/api/paas/v4', '/api/anthropic', '/api/v1'})
    return True


async def quote_for(entry: ModelEntry, catalog: PriceCatalog) -> dict[str, Any]:
    if entry.billing_scheme == 'manual':
        return {}
    result: dict[str, Any] = {'scheme': entry.billing_scheme, 'plan': entry.billing_plan,
        'kind': 'subscription' if entry.billing_scheme.endswith('_token_plan') else 'api'}
    if entry.billing_scheme == 'other_token_plan':
        return {**result, 'status': 'subscription_price_unknown'}
    if not official_endpoint(entry):
        return {**result, 'status': 'endpoint_mismatch'}
    quote = await catalog.get(entry.billing_scheme)
    model = entry.model.removeprefix('openai/').removeprefix('anthropic/').lower()
    return {**result, **{key: quote.get(key) for key in (
        'status', 'currency', 'sources', 'fetched_at', 'version', 'schedule', 'unit', 'fee_status', 'error')},
        'rates': quote.get('models', {}).get(model, []),
        'subscription_reference': quote.get('plans', {}).get(entry.billing_plan, {})}


def estimate(entry: ModelEntry, quote: dict[str, Any], ts: float, usage: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {'billing_scheme': entry.billing_scheme, 'billing_plan': entry.billing_plan,
        'pricing_snapshot': quote, 'usd_pricing_status': 'subscription' if quote.get('kind') == 'subscription' else 'official_cny',
        'estimated_cost_usd': None, 'usd_rate_snapshot': {}, 'pricing_status': quote.get('status', 'unavailable')}
    if quote.get('kind') == 'subscription':
        # 无账期、席位及实付金额时无法合理分摊；保留 tokens 与可用的积分估算。
        result['pricing_status'] = 'subscription_not_allocated'
    if quote.get('status') != 'fresh' or not quote.get('rates'):
        if quote.get('kind') != 'subscription':
            result['pricing_status'] = 'model_price_unknown' if quote.get('status') == 'fresh' else quote.get('status', 'unavailable')
        return result
    read, write = usage.get('cached_input_tokens'), usage.get('cache_write_tokens')
    if read is None or entry.prompt_caching == 'anthropic_ephemeral' and write is None:
        result['pricing_status'] = 'unknown_cache_usage'
        return result
    inputs, outputs = usage['prompt_tokens'], usage['completion_tokens']
    if read > inputs or (write or 0) > inputs - read:
        result['pricing_status'] = 'inconsistent_cache_usage'
        return result
    local = datetime.fromtimestamp(ts, ZoneInfo('Asia/Shanghai'))
    rates = quote['rates']
    if quote.get('schedule') == 'deepseek_cn_workday_peak':
        peak = local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18)
        # 法定节假日未知：工作日高峰同时保留空闲价；不能假装有账户精确账单。
        rates = rates if peak else [r for r in rates if r.get('period') == 'off_peak']
        result['pricing_period'] = 'peak_or_public_holiday' if peak else 'off_peak'
    costs = []
    for rate in rates:
        if (read and rate.get('cached_input') is None or write and rate.get('cache_write') is None
                or rate.get('cache_storage_per_mtok_hour', 0)):
            result['pricing_status'] = 'unknown_cache_price'
            return result
        cost = ((inputs - read - (write or 0)) * rate['input'] + outputs * rate['output']
                + read * (rate.get('cached_input') or 0) + (write or 0) * (rate.get('cache_write') or 0))
        costs.append(cost)
    if not costs:
        return result
    if quote.get('kind') == 'subscription':
        if quote.get('unit') == 'credits_per_10k_tokens' and quote.get('schedule') == 'glm_weekday_afternoon_half_offpeak':
            factor = 1 if local.weekday() < 5 and 14 <= local.hour < 18 else .5
            result.update(estimated_plan_credits_min=min(costs) / 10000 * factor,
                          estimated_plan_credits_max=max(costs) / 10000 * factor)
        return result
    result.update(estimated_cost_cny_min=min(costs) / 1e6, estimated_cost_cny_max=max(costs) / 1e6,
                  pricing_status='official_estimate', pricing_version=quote.get('version') or '')
    return result
