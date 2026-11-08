"""账号管理页：凭据只写不回显，手动登录在独立浏览器上下文中进行。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.resources.browser_accounts import MAX_STATE, BrowserAccounts
from spiritscout.tools.browser_accounts import account_spec
from spiritscout.tools.browser_handoff import window_state
from spiritscout.tools.mcp_client import build_client


class AccountBrowsers:
    def __init__(self, cfg: Any, accounts: BrowserAccounts):
        self.cfg, self.accounts = cfg, accounts
        self.clients: dict[str, Any] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    async def open(self, key: str) -> dict[str, Any]:
        row = self.accounts.row(key)
        client = self.clients.get(key)
        if client and client.identity['version'] != row['version']:
            await self.close(key)
            client = None
        if client is None:
            base = next((s for s in self.accounts.store.effective_mcp(self.cfg.mcp_servers)
                         if s.enabled and list(s.args[:2]) == ['-m', 'spiritscout.tools.chrome_mcp']), None)
            if base is None:
                raise ValueError('请先启用内置 Chrome MCP')
            spec = account_spec(base, self.accounts, key)
            spec.env.update(SPIRITSCOUT_CHROME_MODE='cdp',
                SPIRITSCOUT_CHROME_OUTPUT_DIR=str(Path(self.accounts.store.root) / 'browser-output' / key),
                SPIRITSCOUT_CHROME_PROFILE_DIR=str(Path(self.cfg.workspace.root) / 'browser-profile'))
            client = build_client(spec)
            try:
                await client.start()
                result = await client.call('browser_navigate', {'url': row['data']['origins'][0]}, timeout=30)
                if result.get('is_error'):
                    raise ValueError('账号网站未能打开')
            except BaseException:
                await client.close(save=False)
                raise
            self.clients[key] = client
        await window_state(client, client.code_tool, 'normal')
        return {'opened': True, 'login_verified': False}

    async def close(self, key: str) -> None:
        client = self.clients.pop(key, None)
        if client:
            await client.close(save=False)

    async def aclose(self) -> None:
        await asyncio.gather(*(self.close(key) for key in list(self.clients)), return_exceptions=True)


def mount_browser_accounts(app: FastAPI, cfg: Any, store: Any) -> AccountBrowsers:
    accounts = BrowserAccounts(store)
    browsers = AccountBrowsers(cfg, accounts)
    app.state.browser_accounts = browsers

    async def payload(request: Request) -> tuple[dict[str, Any], int]:
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_STATE + 20000:
                raise HTTPException(413, '账号配置过大')
        try:
            value = json.loads(content)
            if not isinstance(value, dict) or set(value) - {'data', 'expected_version'}:
                raise ValueError()
            expected = value.get('expected_version', 0)
            if type(expected) is not int or expected < 0 or not isinstance(value.get('data', {}), dict):
                raise ValueError()
            return value.get('data', {}), expected
        except (ValueError, TypeError):
            raise HTTPException(422, '账号请求格式无效') from None

    def failure(exc: Exception) -> HTTPException:
        if isinstance(exc, KeyError):
            return HTTPException(404, '浏览器账号不存在或已删除')
        if isinstance(exc, SpiritScoutError):
            return HTTPException(409, '账号已被更新，请刷新后重试')
        # 外部浏览器异常可能携带代码/凭据，统一屏蔽错误正文。
        return HTTPException(422, '操作未完成，请检查账号设置、网站地址和 Chrome MCP 连接')

    @app.get('/api/browser-accounts')
    def catalog() -> dict[str, Any]:
        return {'items': [{**item, 'opened': item['id'] in browsers.clients} for item in accounts.list()]}

    @app.put('/api/browser-accounts/{key}')
    async def save(key: str, request: Request) -> dict[str, Any]:
        data, expected = await payload(request)
        async with browsers.lock(key):
            try:
                view = accounts.save(key, data, expected)
                await browsers.close(key)
                return view
            except (ValueError, KeyError, SpiritScoutError) as exc:
                raise failure(exc) from None

    @app.post('/api/browser-accounts/{key}/{action}')
    async def act(key: str, action: str, request: Request) -> dict[str, Any]:
        _, expected = await payload(request)
        async with browsers.lock(key):
            try:
                if accounts.row(key)['version'] != expected:
                    raise SpiritScoutError('stale')
                if action == 'open':
                    return await browsers.open(key)
                if action == 'capture':
                    client = browsers.clients.get(key)
                    if client is None:
                        raise ValueError('请先打开账号浏览器')
                    return await client.capture()
                if action == 'close':
                    await browsers.close(key)
                    return {'closed': True}
                if action in {'clear', 'delete'}:
                    accounts.clear(key, expected, delete=action == 'delete')
                    await browsers.close(key)
                    return {'cleared': True}
                raise HTTPException(404, '未知账号操作')
            except (KeyError, SpiritScoutError) as exc:
                raise failure(exc) from None
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 —— 不向客户端泄露浏览器原始错误
                raise failure(exc) from None

    return browsers
