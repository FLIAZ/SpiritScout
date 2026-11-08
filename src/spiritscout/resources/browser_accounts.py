"""浏览器账号元数据与加密登录态；缓存不是登录验证，也不构成目标授权。"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Any
from urllib.parse import urlsplit

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.resources.store import ResourceStore

KIND = 'browser_account'
MAX_STATE = 2 * 1024 * 1024
MFA_METHODS = {'none': '无二次验证', 'sms': '短信验证码', 'email': '邮箱验证码',
               'authenticator': '验证器动态码'}


def mask(value: str) -> str:
    return (value[:2] + '…' + value[-2:]) if len(value) > 4 else '…' if value else ''


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('网站须为不含凭据的 HTTP(S) 地址')
    host = parsed.hostname.encode('idna').decode().lower()
    host = f'[{host}]' if ':' in host else host
    port = parsed.port
    return f'{parsed.scheme}://{host}' + (f':{port}' if port and port != {'http': 80, 'https': 443}[parsed.scheme] else '')


def filter_state(raw: Any, origins: list[str]) -> dict[str, Any]:
    """只留声明网站的 Cookie/localStorage；不保存网页代码、密码表单或未知字段。"""
    if not isinstance(raw, dict) or len(json.dumps(raw, ensure_ascii=False).encode()) > MAX_STATE:
        raise ValueError('登录态须为不超过 2 MiB 的 JSON 对象')
    cookies, storage = raw.get('cookies', []), raw.get('origins', [])
    if (not isinstance(cookies, list) or not isinstance(storage, list)
            or len(cookies) > 2000 or len(storage) > 100):
        raise ValueError('登录态 cookies/origins 格式或数量无效')
    hosts = {urlsplit(value).hostname for value in origins}
    kept = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise ValueError('Cookie 格式无效')
        domain = cookie.get('domain', '')
        if not isinstance(domain, str) or not domain:
            raise ValueError('Cookie 缺少 domain')
        host = domain.lstrip('.').lower().encode('idna').decode()
        # 父域 Cookie 必须明确声明父域网站，不猜测公共后缀或跨站登录范围。
        if host not in hosts:
            continue
        if any(not isinstance(cookie.get(k), str) for k in ('name', 'value', 'path')):
            raise ValueError('Cookie name/value/path 须为文本')
        if not cookie['name'] or not cookie['path'].startswith('/'):
            raise ValueError('Cookie 名称或路径无效')
        expires = cookie.get('expires', -1)
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ValueError('Cookie 到期时间无效')
        if expires != -1 and expires <= time.time():
            continue
        if any(type(cookie.get(k, False)) is not bool for k in ('httpOnly', 'secure')):
            raise ValueError('Cookie 安全标记须为布尔值')
        same_site = cookie.get('sameSite', 'Lax')
        if not isinstance(same_site, str) or same_site not in {'Strict', 'Lax', 'None'}:
            raise ValueError('Cookie sameSite 无效')
        partition = cookie.get('partitionKey')
        if partition is not None:
            if not isinstance(partition, str) or not cookie.get('secure'):
                raise ValueError('分区 Cookie 需要有效 partitionKey 和 secure 标记')
            if origin(partition) not in origins:
                continue
        kept.append({k: cookie[k] for k in ('name', 'value', 'domain', 'path')})
        kept[-1].update(expires=expires, httpOnly=cookie.get('httpOnly', False),
                        secure=cookie.get('secure', False), sameSite=same_site)
        if partition is not None:
            kept[-1]['partitionKey'] = partition
    saved = []
    for entry in storage:
        if not isinstance(entry, dict) or not isinstance(entry.get('origin'), str):
            raise ValueError('站点存储格式无效')
        if origin(entry['origin']) not in origins:
            continue
        values = entry.get('localStorage', [])
        if not isinstance(values, list) or any(not isinstance(v, dict) or any(
                not isinstance(v.get(k), str) for k in ('name', 'value')) for v in values):
            raise ValueError('localStorage 格式无效')
        saved.append({'origin': origin(entry['origin']),
                      'localStorage': [{'name': v['name'], 'value': v['value']} for v in values]})
    return {'cookies': kept, 'origins': saved}


class BrowserAccounts:
    def __init__(self, store: ResourceStore):
        self.store = store

    def row(self, key: str) -> dict[str, Any]:
        row = next((r for r in self.store.settings(KIND) if r['id'] == key and not r['data'].get('deleted')), None)
        if row is None:
            raise KeyError('浏览器账号不存在或已删除')
        return row

    @staticmethod
    def public(row: dict[str, Any]) -> dict[str, Any]:
        data = row['data']
        state = data.get('state') or {}
        expires = data.get('expires_at', 0)
        valid = bool(data.get('retain') and expires > time.time() and state)
        return {'id': row['id'], 'version': row['version'],
                **{k: data.get(k) for k in ('name', 'role', 'origins', 'retain', 'ttl_days')},
                'saved_at': data.get('saved_at'), 'expires_at': expires or None,
                'cache_status': 'available' if valid else 'expired' if state else 'empty',
                'cookie_count': len(state.get('cookies', [])) if valid else 0,
                'origin_count': len(state.get('origins', [])) if valid else 0,
                'username_hint': mask(data.get('username', '')),
                'credentials_configured': bool(data.get('username') and data.get('password')),
                'mfa_method': data.get('mfa_method', 'none'),
                'mfa_destination_hint': mask(data.get('mfa_destination', '')),
                'login_verified': False}

    def list(self) -> list[dict[str, Any]]:
        return [self.public(r) for r in self.store.settings(KIND) if not r['data'].get('deleted')]

    def save(self, key: str, change: dict[str, Any], expected: int) -> dict[str, Any]:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', key):
            raise ValueError('账号标识须为 1–64 位字母、数字、下划线或短横线')
        if set(change) - {'name', 'role', 'origins', 'retain', 'ttl_days', 'storage_state',
                          'username', 'password', 'clear_credentials', 'mfa_method', 'mfa_destination'}:
            raise ValueError('未知账号设置字段')
        try:
            old = self.row(key)
        except KeyError:
            old = {'data': {}, 'version': 0}
        data = {**old['data'], **{k: v for k, v in change.items() if k not in {'storage_state', 'clear_credentials'}}}
        if type(change.get('clear_credentials', False)) is not bool:
            raise ValueError('clear_credentials 须为布尔值')
        for key_name in ('username', 'password', 'mfa_destination'):
            if not isinstance(data.get(key_name, ''), str) or len(data.get(key_name, '')) > 4096:
                raise ValueError('账号凭据字段格式无效')
        if change.get('clear_credentials'):
            if change.get('username') or change.get('password'):
                raise ValueError('清除凭据时不能同时填写新凭据')
            data.pop('username', None)
            data.pop('password', None)
        if not isinstance(data.get('mfa_method', 'none'), str) or data.get('mfa_method', 'none') not in MFA_METHODS:
            raise ValueError('二次验证方式无效')
        if change.get('clear_credentials') or any(k in change and change[k] != old['data'].get(k)
                                                   for k in ('username', 'password')):
            for field in ('state', 'saved_at', 'expires_at'):
                data.pop(field, None)
        for field in ('name', 'role'):
            value = data.get(field, '')
            if not isinstance(value, str) or len(value) > 100 or (field == 'name' and not value.strip()):
                raise ValueError('名称不能为空，名称与身份说明最多 100 字符')
            data[field] = value.strip()
        sites = data.get('origins', [])
        if not isinstance(sites, list) or not 1 <= len(sites) <= 20 or any(not isinstance(s, str) for s in sites):
            raise ValueError('请提供 1–20 个账号网站地址')
        data['origins'] = list(dict.fromkeys(origin(s) for s in sites))
        data.setdefault('retain', True)
        data.setdefault('ttl_days', 30)
        if type(data['retain']) is not bool or type(data['ttl_days']) is not int or not 1 <= data['ttl_days'] <= 365:
            raise ValueError('保留登录态须为布尔值，缓存期限为 1–365 天')
        if 'storage_state' in change:
            data['state'] = filter_state(change['storage_state'], data['origins'])
            data['saved_at'] = time.time()
        elif data.get('state'):
            data['state'] = filter_state(data['state'], data['origins'])
        if data.get('saved_at'):
            data['expires_at'] = data['saved_at'] + data['ttl_days'] * 86400
        if not data['retain']:
            for field in ('state', 'saved_at', 'expires_at'):
                data.pop(field, None)
        version = self.store.save(KIND, key, data, expected)
        return self.public({'id': key, 'version': version, 'data': data})

    def clear(self, key: str, expected: int, *, delete: bool = False) -> None:
        data = dict(self.row(key)['data'])
        for field in ('state', 'saved_at', 'expires_at'):
            data.pop(field, None)
        if delete:
            data = {'deleted': True}
        self.store.save(KIND, key, data, expected)

    def snapshot(self, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
        row = self.row(key)
        view = self.public(row)
        state = filter_state(row['data']['state'], row['data']['origins']) if view['cache_status'] == 'available' else {'cookies': [], 'origins': []}
        return view, state

    def capture(self, key: str, raw: dict[str, Any], expected: int) -> dict[str, Any]:
        row = self.row(key)
        if row['version'] != expected:
            raise SpiritScoutError('账号设置或缓存已改变，本次旧登录态未覆盖新设置；请重新打开账号')
        if not row['data']['retain']:
            return self.public(row)
        return self.save(key, {'storage_state': raw}, expected)

    def validate_selection(self, ids: list[str]) -> None:
        for key in ids:
            self.row(key)

    def bind(self, session: str, ids: list[str]) -> None:
        if ids:
            self.validate_selection(ids)
            self.store.save('browser_binding', session, {'account_ids': list(dict.fromkeys(ids))}, 0)

    def selected(self, session: str) -> list[str]:
        row = next((r for r in self.store.settings('browser_binding') if r['id'] == session), None)
        return row['data'].get('account_ids', []) if row else []
