"""Kimi Code 设备授权；按公开客户端协议交换，令牌只在本机密钥库与后端使用。"""
from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx

from spiritscout import __version__
from spiritscout.config.settings import ModelEntry, ProviderEntry
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.web import vault

# MoonshotAI/kimi-cli src/kimi_cli/auth/oauth.py 公开的 device-flow 客户端标识，非密钥。
# 与 CC Switch 同一公开流程；不读取其他客户端的 Cookie/凭据，也不冒充其 User-Agent。
CLIENT_ID = '17e5f671-d194-4dfb-9706-5516cb48c098'
AUTH_ROOT = 'https://auth.kimi.com/api/oauth'


@asynccontextmanager
async def credential_lock():
    """刷新旋转与退出/改名互斥；跨进程锁沿用项目已有的 Windows/Unix 实现。"""
    from spiritscout.tools.run_lock import _lock, _open

    path = vault.oauth_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open(path)
    try:
        async with asyncio.timeout(20):
            while True:
                try:
                    _lock(fd)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.1)
        yield
    finally:
        os.close(fd)


def _check(provider: ProviderEntry | ModelEntry) -> None:
    if (provider.auth_method != 'kimi_oauth' or provider.base_url.rstrip('/') != 'https://api.kimi.com/coding/v1'
            or provider.billing_scheme != 'kimi_token_plan'
            or provider.provider != 'openai_compatible' or provider.api_format not in {'auto', 'chat_completions'}):
        raise SpiritScoutError('该供应商未配置支持的 Kimi OAuth 接入方式')


async def _request(path: str, form: dict[str, str], device: str) -> tuple[int, dict[str, Any]]:
    headers = {'User-Agent': f'SpiritScout/{__version__}', 'X-Msh-Platform': 'spiritscout',
               'X-Msh-Version': __version__, 'X-Msh-Device-Id': device,
               'X-Msh-Device-Name': 'SpiritScout', 'Accept': 'application/json'}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(AUTH_ROOT + path, data={'client_id': CLIENT_ID, **form}, headers=headers)
            if len(response.content) > 100_000:
                raise ValueError('oversize')
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError('unexpected response')
            return response.status_code, body
    except (httpx.HTTPError, ValueError):
        raise SpiritScoutError('Kimi 授权服务暂不可用，请重试；已有凭据未清除') from None


def _token(body: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        expires = float(body.get('expires_in') or 0)
    except (TypeError, ValueError):
        raise SpiritScoutError('Kimi 授权响应不完整，未覆盖已有凭据') from None
    access = body.get('access_token')
    refresh = body.get('refresh_token') or (previous or {}).get('refresh_token')
    if (not math.isfinite(expires) or expires <= 0 or not isinstance(access, str) or not access
            or not isinstance(refresh, str) or not refresh or str(body.get('token_type', 'bearer')).lower() != 'bearer'):
        raise SpiritScoutError('Kimi 授权响应不完整，未覆盖已有凭据')
    return {'access_token': access, 'refresh_token': refresh, 'expires_at': time.time() + expires}


def _flow_view(flow: dict[str, Any]) -> dict[str, Any]:
    return {k: flow[k] for k in ('flow_id', 'status', 'verification_uri', 'user_code', 'expires_at', 'interval') if k in flow}


async def start(provider: ProviderEntry) -> dict[str, Any]:
    _check(provider)
    async with credential_lock():
        stored = vault.get_oauth(provider.name)
        device = stored.get('device_id') or str(uuid.uuid4())
        status, body = await _request('/device_authorization', {}, device)
        if status != 200:
            raise SpiritScoutError(f'Kimi 拒绝设备授权（HTTP {status}）')
        uri = body.get('verification_uri_complete') or body.get('verification_uri', '')
        try:
            parsed = urlsplit(uri)
            expires = float(body.get('expires_in') or 0)
            interval = max(5, min(60, int(body.get('interval') or 5)))
            port = parsed.port
        except (TypeError, ValueError, OverflowError):
            raise SpiritScoutError('Kimi 返回的设备授权信息不完整') from None
        if (parsed.scheme != 'https' or parsed.hostname not in {'auth.kimi.com', 'www.kimi.com', 'kimi.com'}
                or parsed.username or port not in (None, 443)):
            raise SpiritScoutError('Kimi 返回了无法确认的授权页面，未启动登录')
        if not math.isfinite(expires) or not 0 < expires <= 3600 or not body.get('device_code') or not body.get('user_code'):
            raise SpiritScoutError('Kimi 返回的设备授权信息不完整')
        flow = {'flow_id': uuid.uuid4().hex, 'status': 'pending', 'device_code': body['device_code'],
            'verification_uri': uri, 'user_code': str(body['user_code']), 'expires_at': time.time() + expires,
            'interval': interval, 'next_poll': 0}
        vault.set_oauth(provider.name, {**stored, 'device_id': device, 'pending': flow})
        return _flow_view(flow)


async def poll(provider: ProviderEntry, flow_id: str) -> dict[str, Any]:
    _check(provider)
    async with credential_lock():
        stored = vault.get_oauth(provider.name)
        flow = stored.get('pending') or {}
        if not flow or flow.get('flow_id') != flow_id:
            raise SpiritScoutError('登录流程不存在或已取消，请重新登录')
        now = time.time()
        if now >= flow['expires_at']:
            flow['status'] = 'expired'
        if flow['status'] != 'pending' or now < flow.get('next_poll', 0):
            return _flow_view(flow)
        status, body = await _request('/token', {'device_code': flow['device_code'],
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'}, stored['device_id'])
        if status == 200:
            stored['token'] = _token(body)
            flow['status'] = 'authenticated'
            flow.pop('device_code', None)
        elif body.get('error') == 'slow_down':
            flow['interval'] = min(60, flow['interval'] + 5)
        elif body.get('error') in {'expired_token', 'access_denied'}:
            flow['status'] = 'expired' if body['error'] == 'expired_token' else 'denied'
        elif body.get('error') != 'authorization_pending':
            raise SpiritScoutError(f'Kimi 登录查询失败（HTTP {status}），可重试')
        flow['next_poll'] = now + flow['interval']
        vault.set_oauth(provider.name, stored)
        return _flow_view(flow)


async def logout(name: str, *, cancel_only: bool = False) -> None:
    async with credential_lock():
        stored = vault.get_oauth(name)
        if cancel_only:
            stored.pop('pending', None)
        else:
            stored = {}
        vault.set_oauth(name, stored)


async def access_token(provider: ProviderEntry | ModelEntry) -> str:
    _check(provider)
    name = provider.name if isinstance(provider, ProviderEntry) else provider.provider_ref
    async with credential_lock():
        stored = vault.get_oauth(name)
        token = stored.get('token') or {}
        if not token.get('access_token') or token.get('rejected'):
            raise SpiritScoutError('Kimi 尚未登录或登录已失效，请在供应商面板重新登录')
        if token['expires_at'] - time.time() > 90:
            return token['access_token']
        if token.get('retry_after', 0) > time.time():
            if token['expires_at'] > time.time() + 5:
                return token['access_token']
            raise SpiritScoutError('Kimi 令牌刷新暂不可用，稍后重试')
        try:
            status, body = await _request('/token', {'grant_type': 'refresh_token',
                'refresh_token': token['refresh_token']}, stored['device_id'])
            if status == 200:
                stored['token'] = _token(body, token)
                vault.set_oauth(name, stored)
                return stored['token']['access_token']
            if status in {401, 403} or body.get('error') in {'invalid_grant', 'invalid_token'}:
                token['rejected'] = True
                vault.set_oauth(name, stored)
                raise SpiritScoutError('Kimi 登录已失效，请重新登录')
            raise SpiritScoutError(f'Kimi 令牌刷新失败（HTTP {status}）')
        except SpiritScoutError:
            token['retry_after'] = time.time() + 60
            vault.set_oauth(name, stored)
            if not token.get('rejected') and token['expires_at'] > time.time() + 5:
                return token['access_token']
            raise
