"""官方账户余额/套餐额度只读查询；失败保留旧值的时间，不把缺字段推成零。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from spiritscout.config.settings import ProviderEntry
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.web import vault

ENDPOINTS = {
    'deepseek_balance': 'https://api.deepseek.com/user/balance',
    'kimi_plan': 'https://api.kimi.com/coding/v1/usages',
    'glm_plan': 'https://open.bigmodel.cn/api/monitor/usage/quota/limit',
    'glm_balance': 'https://open.bigmodel.cn/api/biz/account/query-customer-account-report',
    'glm_plan_global': 'https://api.z.ai/api/monitor/usage/quota/limit',
    'kimi_plan_global': 'https://api.kimi.ai/coding/v1/usages',
    'minimax_plan': 'https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains',
    'minimax_plan_global': 'https://api.minimax.io/v1/api/openplatform/coding_plan/remains',
    'siliconflow_balance': 'https://api.siliconflow.cn/v1/user/info',
    'siliconflow_balance_global': 'https://api.siliconflow.com/v1/user/info',
    'stepfun_balance': 'https://api.stepfun.com/v1/accounts',
    'stepfun_balance_global': 'https://api.stepfun.ai/v1/accounts',
    'openrouter_balance': 'https://openrouter.ai/api/v1/credits',
    'moonshot_balance': 'https://api.moonshot.cn/v1/users/me/balance',
    'moonshot_balance_global': 'https://api.moonshot.ai/v1/users/me/balance',
    'opencode_plan': 'https://opencode.ai/zen/go/v1/usage',
    'ark_plan': 'https://ark.cn-beijing.volcengineapi.com/',
}
GLOBAL_HOSTS = {'api.z.ai', 'api.kimi.ai', 'api.minimax.io', 'api.siliconflow.com',
                'api.stepfun.ai', 'api.moonshot.ai'}


def endpoint_for(provider: ProviderEntry, kind: str) -> str:
    suffix = '_global' if urlsplit(provider.base_url).hostname in GLOBAL_HOSTS else ''
    return ENDPOINTS[kind + suffix]


def query_kind(provider: ProviderEntry) -> str:
    if provider.quota_query == 'disabled':
        return 'disabled'
    try:
        url = urlsplit(provider.base_url)
        valid = url.scheme == 'https' and not url.username and url.port in (None, 443)
    except ValueError:
        valid = False
    if not valid:
        return 'unsupported'
    supported = {'api.deepseek.com': 'deepseek_balance', 'open.bigmodel.cn': 'glm_plan', 'api.z.ai': 'glm_plan',
        'api.kimi.com': 'kimi_plan', 'api.kimi.ai': 'kimi_plan',
        'api.minimax.cn': 'minimax_plan', 'api.minimaxi.com': 'minimax_plan', 'api.minimax.io': 'minimax_plan',
        'api.siliconflow.cn': 'siliconflow_balance', 'api.siliconflow.com': 'siliconflow_balance',
        'api.stepfun.com': 'stepfun_balance', 'api.stepfun.ai': 'stepfun_balance',
        'api.moonshot.cn': 'moonshot_balance', 'api.moonshot.ai': 'moonshot_balance',
        'openrouter.ai': 'openrouter_balance', 'opencode.ai': 'opencode_plan',
        'ark.cn-beijing.volces.com': 'ark_plan'}.get(url.hostname, 'unsupported')
    if supported == 'ark_plan' and not url.path.startswith(('/api/plan', '/api/coding')):
        return 'unsupported'
    if supported == 'opencode_plan' and not url.path.startswith(('/zen/go', '/inference/go')):
        return 'unsupported'
    if url.hostname == 'open.bigmodel.cn' and (provider.quota_query == 'glm_balance'
                                              or provider.quota_query == 'auto' and provider.billing_scheme == 'glm_api'):
        return 'glm_balance'
    if provider.quota_query != 'auto':
        return supported if supported == provider.quota_query else 'unsupported'
    if supported == 'minimax_plan' and not provider.billing_scheme.endswith('_token_plan') and '/coding/' not in url.path:
        return 'unsupported'
    if supported == 'kimi_plan' and not url.path.startswith('/coding'):
        return 'unsupported'
    return supported


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _reset(value: Any) -> str | None:
    try:
        if isinstance(value, str) and 'T' in value:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).isoformat()
        number = _number(value)
        if number is not None and number > 0:
            return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    return None


def parse_quota(kind: str, body: dict[str, Any], *, currency: str = 'CNY') -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError('响应结构不可识别')
    if kind == 'deepseek_balance':
        balances = []
        for row in body.get('balance_infos') or []:
            total = _number(row.get('total_balance'))
            currency = row.get('currency')
            if total is not None and currency in {'CNY', 'USD'}:
                balances.append({'currency': currency, 'remaining': total,
                    'granted': _number(row.get('granted_balance')), 'topped_up': _number(row.get('topped_up_balance'))})
        if not balances:
            raise ValueError('余额字段缺失')
        return {'balances': balances, 'is_available': body.get('is_available'), 'windows': []}
    if kind in {'siliconflow_balance', 'stepfun_balance', 'openrouter_balance', 'moonshot_balance', 'glm_balance'}:
        data = body.get('data') or body
        if body.get('success') is False or body.get('code') not in (None, 0, 200, '0', '200'):
            raise ValueError('余额查询失败')
        if kind == 'openrouter_balance':
            total, used = _number(data.get('total_credits')), _number(data.get('total_usage'))
            remaining = total - used if total is not None and used is not None else None
            currency = 'USD'
        elif kind == 'glm_balance':
            remaining = _number(data.get('availableBalance'))
            if remaining is None:
                remaining = _number(data.get('balance'))
        else:
            field = {'siliconflow_balance': 'totalBalance', 'stepfun_balance': 'balance',
                     'moonshot_balance': 'available_balance'}[kind]
            remaining = _number(data.get(field))
        if remaining is None or not math.isfinite(remaining):
            raise ValueError('余额字段缺失')
        return {'windows': [], 'balances': [{'remaining': remaining, 'currency': currency}]}
    windows = []
    if kind == 'kimi_plan':
        items = [(f'短周期额度 {i + 1}', row.get('detail')) for i, row in enumerate(body.get('limits') or [])]
        if isinstance(body.get('usage'), dict):
            items.append(('套餐总额度', body['usage']))
        for label, detail in items:
            if not isinstance(detail, dict):
                continue
            limit, remaining = _number(detail.get('limit')), _number(detail.get('remaining'))
            if limit is None or remaining is None or limit <= 0 or remaining < 0 or remaining > limit:
                continue
            windows.append({'label': label, 'remaining': remaining, 'limit': limit, 'unit': '额度',
                'remaining_percent': remaining / limit * 100, 'resets_at': _reset(detail.get('resetTime'))})
    elif kind == 'glm_plan':
        if body.get('success') is False or body.get('code') not in (None, 0, 200, '0', '200'):
            raise ValueError('套餐额度查询未成功')
        data = body.get('data') or {}
        for row in data.get('limits') or []:
            if str(row.get('type', '')).upper() not in {'TOKENS_LIMIT', 'CREDIT_LIMIT'}:
                continue
            used = _number(row.get('percentage'))
            if used is None or not 0 <= used <= 100:
                continue
            unit, number = row.get('unit'), row.get('number')
            label = f'{number} 小时额度' if unit == 3 and number else '周额度' if unit == 6 else '套餐额度'
            windows.append({'label': label, 'remaining_percent': 100 - used,
                'resets_at': _reset(row.get('nextResetTime'))})
    elif kind == 'minimax_plan':
        if (body.get('base_resp') or {}).get('status_code') not in (None, 0):
            raise ValueError('套餐查询失败')
        rows = body.get('model_remains') or []
        row = next((r for r in rows if isinstance(r, dict) and r.get('model_name') == 'general'), {})
        for field, label, reset in [('current_interval_remaining_percent', '5 小时额度', 'end_time'),
                                     ('current_weekly_remaining_percent', '周额度', 'weekly_end_time')]:
            if 'weekly' in field and row.get('current_weekly_status') not in (1, 2):
                continue
            remain = _number(row.get(field))
            if remain is not None and 0 <= remain <= 100:
                windows.append({'label': label, 'remaining_percent': remain, 'resets_at': _reset(row.get(reset))})
    elif kind == 'opencode_plan':
        for key, label in [('rolling', '5 小时额度'), ('weekly', '周额度'), ('monthly', '月额度')]:
            row = (body.get('usage') or {}).get(key) or {}
            used = _number(row.get('percent'))
            if used is not None and 0 <= used <= 100:
                windows.append({'label': label, 'remaining_percent': 100 - used,
                                'resets_at': _reset(row.get('resetsAt')) if used > 0 else None})
    elif kind == 'ark_plan':
        if (body.get('ResponseMetadata') or {}).get('Error'):
            raise ValueError('火山用量查询失败')
        data = body.get('Result') or {}
        for key, label in [('AFPFiveHour', '5 小时额度'), ('AFPWeekly', '周额度'), ('AFPMonthly', '月额度')]:
            row = data.get(key) or {}
            limit, used = _number(row.get('Quota')), _number(row.get('Used'))
            if limit is not None and limit > 0 and used is not None and used >= 0:
                windows.append({'label': label, 'remaining_percent': max(0, 100 - used / limit * 100),
                    'remaining': max(0, limit - used), 'limit': limit, 'unit': 'AFP', 'resets_at': _reset(row.get('ResetTime'))})
        for row in data.get('QuotaUsage') or []:
            label = {'session': '5 小时额度', 'weekly': '周额度', 'monthly': '月额度'}.get(row.get('Level'))
            used = _number(row.get('Percent'))
            if label and used is not None and 0 <= used <= 100:
                windows.append({'label': label, 'remaining_percent': 100 - used, 'resets_at': _reset(row.get('ResetTime'))})
    if not windows:
        raise ValueError('未返回可识别的套餐额度，请核对套餐或稍后重试')
    return {'windows': windows, 'balances': []}


class QuotaService:
    def __init__(self):
        self.cache: dict[str, dict[str, Any]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def invalidate(self, name: str) -> None:
        self.cache.pop(name, None)

    async def get(self, provider: ProviderEntry, *, force: bool = False) -> dict[str, Any]:
        kind = query_kind(provider)
        if kind in {'disabled', 'unsupported'}:
            return {'status': kind, 'windows': [], 'balances': [],
                    'message': '额度查询已关闭' if kind == 'disabled' else '此接入方式暂不支持官方额度查询'}
        async with self.locks.setdefault(provider.name, asyncio.Lock()):
            credential = (vault.get_oauth(provider.name).get('device_id', '') if provider.auth_method == 'kimi_oauth'
                          else repr(vault.get_quota_credentials(provider.name)) if kind == 'ark_plan' else vault.get_key(provider.name))
            fingerprint = hashlib.sha256(f'{kind}|{provider.base_url}|{provider.auth_method}|{credential}'.encode()).hexdigest()
            old = self.cache.get(provider.name, {})
            if old.get('_fingerprint') != fingerprint:
                old = {}
            if old and time.time() - old.get('attempted_at', 0) < (10 if force else 60):
                return self._public(old)
            endpoint = endpoint_for(provider, kind)
            result = {**old, '_fingerprint': fingerprint, 'attempted_at': time.time(), 'source': endpoint}
            try:
                if provider.auth_method == 'kimi_oauth':
                    from spiritscout.web.provider_auth import access_token
                    credential = await access_token(provider)
                if not credential:
                    raise SpiritScoutError('请先配置 API Key 或完成供应商登录')
                headers = {'Authorization': credential if kind in {'glm_plan', 'glm_balance'} else f'Bearer {credential}',
                           'Accept': 'application/json', 'User-Agent': 'SpiritScout-quota'}
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    if kind == 'ark_plan':
                        from spiritscout.web.provider_quota_ark import fetch_ark
                        response = await fetch_ark(client, endpoint, provider)
                    else:
                        response = await client.get(endpoint, headers=headers)
                parsed_kind = kind
                # GLM 两类 Key 共用部分推理地址；自动查询没有套餐数据时尝试同账户余额。
                # 显式选择套餐查询、鉴权/网络失败时均不改换口径。
                if (kind == 'glm_plan' and provider.quota_query == 'auto'
                        and urlsplit(provider.base_url).hostname == 'open.bigmodel.cn'
                        and response.is_success and len(response.content) <= 1_000_000):
                    try:
                        parse_quota(kind, response.json())
                    except (ValueError, TypeError, AttributeError):
                        parsed_kind = 'glm_balance'
                        result['source'] = ENDPOINTS[parsed_kind]
                        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                            response = await client.get(ENDPOINTS[parsed_kind], headers=headers)
                if response.status_code in {401, 403}:
                    result.update(status='auth_required', message='凭据无效或无查询权限，请检查 Key 或重新登录')
                else:
                    response.raise_for_status()
                    if len(response.content) > 1_000_000:
                        raise ValueError('响应过大')
                    currency = 'USD' if urlsplit(provider.base_url).hostname in GLOBAL_HOSTS else 'CNY'
                    data = parse_quota(parsed_kind, response.json(), currency=currency)
                    result.update(**data, status='ok', fetched_at=time.time(), message='')
            except SpiritScoutError as exc:
                result.update(status='auth_required', message=str(exc))
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                result.update(status='unavailable', message='额度暂未查到；未将未知余额记为 0')
            result['stale'] = bool(result.get('fetched_at')) and result['status'] != 'ok'
            self.cache[provider.name] = result
            return self._public(result)

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy({k: v for k, v in row.items() if not k.startswith('_')})
