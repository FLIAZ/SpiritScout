"""每个账号/会话独占 MCP 浏览器上下文，登录态只在受控入口解密。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from spiritscout.resources.browser_accounts import BrowserAccounts, filter_state
from spiritscout.resources.store import ResourceStore
from spiritscout.tools.mcp_client import ChromeMcpClient


def account_spec(spec: Any, accounts: BrowserAccounts, key: str, *,
                 approved_origin: str = '', expected_version: int | None = None) -> Any:
    account = accounts.public(accounts.row(key))
    if expected_version is not None and account['version'] != expected_version:
        raise ValueError('账号已变更，本次确认已失效')
    grant = {}
    if approved_origin:
        if approved_origin not in account['origins']:
            raise ValueError('账号未配置申请的站点')
        account = {**account, 'origins': [approved_origin]}
        grant = {'SPIRITSCOUT_BROWSER_ACCOUNT_ORIGIN': approved_origin,
                 'SPIRITSCOUT_BROWSER_ACCOUNT_VERSION': str(account['version'])}
    identity_key = key + ('|' + approved_origin if approved_origin else '')
    return spec.model_copy(update={'name': spec.name + '_acct_' + hashlib.sha256(identity_key.encode()).hexdigest()[:10],
        'env': {**spec.env, 'SPIRITSCOUT_BROWSER_ACCOUNT': key,
                'SPIRITSCOUT_BROWSER_ACCOUNT_ROOT': str(accounts.store.root.resolve()),
                'SPIRITSCOUT_CHROME_ISOLATED': '1', **grant,
                'SPIRITSCOUT_BROWSER_IDENTITY': json.dumps({k: account[k] for k in ('id', 'name', 'role', 'origins')}, ensure_ascii=False)}})


def session_specs(specs: list[Any], resources: ResourceStore, session: str) -> list[Any]:
    accounts = BrowserAccounts(resources)
    selected = accounts.selected(session)
    accounts.validate_selection(selected)
    result = []
    for spec in specs:
        if list(spec.args[:2]) == ['-m', 'spiritscout.tools.chrome_mcp']:
            if selected:
                result.extend(account_spec(spec, accounts, key) for key in selected)
            else:
                result.append(spec.model_copy(update={'env': {**spec.env, 'SPIRITSCOUT_CHROME_ISOLATED': '1'}}))
        else:
            result.append(spec)
    return result


def storage_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get('is_error'):
        raise ValueError('浏览器未能读取登录态，请重新打开账号')
    text = str(result.get('text', ''))
    # 固定版本 Playwright MCP 把返回值放在 Result 节；只解析返回值，不执行其内容。
    payload = text.split('### Result', 1)[-1].lstrip()
    start = payload.find('{')
    if start < 0:
        raise ValueError('浏览器没有返回有效登录态')
    value, _ = json.JSONDecoder().raw_decode(payload[start:])
    if not isinstance(value, dict) or 'cookies' not in value or 'origins' not in value:
        raise ValueError('浏览器登录态格式无效')
    return value


class AccountChromeMcpClient(ChromeMcpClient):
    def __init__(self, *args: Any, **kwargs: Any):
        env = dict(kwargs['env'])
        self.account_id = env['SPIRITSCOUT_BROWSER_ACCOUNT']
        self.accounts = BrowserAccounts(ResourceStore(env['SPIRITSCOUT_BROWSER_ACCOUNT_ROOT']))
        self.identity, state = self.accounts.snapshot(self.account_id)
        self.approved_origin = env.get('SPIRITSCOUT_BROWSER_ACCOUNT_ORIGIN', '')
        expected = env.get('SPIRITSCOUT_BROWSER_ACCOUNT_VERSION')
        if expected is not None and str(self.identity['version']) != expected:
            raise ValueError('账号已变更，本次确认已失效')
        if self.approved_origin:
            if self.approved_origin not in self.identity['origins']:
                raise ValueError('账号未配置批准的站点')
            self.identity = {**self.identity, 'origins': [self.approved_origin]}
            state = filter_state(state, self.identity['origins'])
        data = self.accounts.row(self.account_id)['data']
        self._secrets = {data[k] for k in ('username', 'password') if data.get(k)}
        self._state_file: Path | None = None
        self._state_fd: int | None = None
        if hasattr(os, 'memfd_create') and Path('/proc/self/fd').is_dir():
            # Linux 使用匿名内存文件：异常退出也不会遗留明文 Cookie 文件。
            fd = os.memfd_create('spiritscout-browser', os.MFD_CLOEXEC)
            self._state_fd = fd
            name = f'/proc/{os.getpid()}/fd/{fd}'
        else:
            fd, name = tempfile.mkstemp(prefix='spiritscout-browser-', suffix='.json')
            os.close(fd)
            self._state_file = Path(name)
        try:
            self._write_state(state)
        except BaseException:
            self._remove_state_file()
            raise
        env['SPIRITSCOUT_CHROME_STORAGE_STATE'] = name
        env['SPIRITSCOUT_CHROME_ISOLATED'] = '1'
        kwargs['env'] = env
        self._used = False
        self.cache_error = ''
        self.code_tool = 'browser_run_code'
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            self._remove_state_file()
            raise

    async def start(self) -> list[dict[str, Any]]:
        try:
            tools = await super().start()
            self.code_tool = next((t['name'] for t in tools if t['name'] in {
                'browser_run_code', 'browser_run_code_unsafe'}), '')
            if not self.code_tool:
                await super().close()
                raise ValueError('当前浏览器 MCP 不支持受控账号填写与缓存')
        except BaseException:
            self._remove_state_file()
            raise
        label = f"账号 {self.identity['name']}（{self.account_id}），身份说明：{self.identity['role'] or '未设置'}。"
        return [{**tool, 'description': label + '独立浏览器上下文；缓存不证明登录有效，先检查当前身份。'
                 '账号网站不构成测试授权。' + tool.get('description', '')} for tool in tools]

    def _remove_state_file(self) -> None:
        if self._state_fd is not None:
            os.close(self._state_fd)
            self._state_fd = None
        if self._state_file:
            self._state_file.unlink(missing_ok=True)

    def _write_state(self, state: dict[str, Any]) -> None:
        # 下一次 browser_close 后重建上下文应读取刚保存的状态，而非启动前的旧副本。
        if self._state_fd is not None:
            os.lseek(self._state_fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(self._state_fd), 'w') as target:
                target.truncate()
                json.dump(state, target)
        elif self._state_file is not None:
            self._state_file.write_text(json.dumps(state))

    def protect(self, values: list[str]) -> None:
        self._secrets.update(value for value in values if value)

    def redact(self, text: str) -> str:
        for value in sorted(self._secrets, key=len, reverse=True):
            for encoded in {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}:
                if len(encoded) < 4:
                    text = re.sub(r'(?<!\w)' + re.escape(encoded) + r'(?!\w)', '[账号私密内容]', text)
                else:
                    text = text.replace(encoded, '[账号私密内容]')
        return text

    async def call(self, tool: str, arguments: dict[str, Any] | None = None,
                   *, timeout: float | None = None) -> dict[str, Any]:
        if self.accounts.row(self.account_id)['version'] != self.identity['version']:
            raise ValueError('账号或缓存已更新，请重新打开账号浏览器或会话')
        if tool == 'browser_close' and self._used and self.identity['retain']:
            await self._save_cache()
        try:
            result = await super().call(tool, arguments, timeout=timeout)
        except Exception:
            raise ValueError('账号浏览器调用失败，请检查浏览器连接与页面状态') from None
        if not result.get('is_error'):
            self._used = tool != 'browser_close'
        # isolated 上下文已经创建；保留路径到 close，支持 MCP 关闭页面后重新建上下文。
        return {**result, 'text': self.redact(str(result.get('text', '')))}

    async def capture(self) -> dict[str, Any]:
        if not self._used:
            raise ValueError('请先打开账号浏览器并完成登录')
        result = await super().call(self.code_tool,
            {'code': 'async (page) => await page.context().storageState()'}, timeout=20)
        state = storage_result(result)
        if self.approved_origin:
            previous, saved = self.accounts.snapshot(self.account_id)
            # 只更新本次获准站点，保留同一账号其他站点的缓存。
            retained = filter_state(saved, [site for site in previous['origins'] if site != self.approved_origin])
            state = filter_state(state, [self.approved_origin])
            cookies = {(c['domain'], c['path'], c['name'], c.get('partitionKey')): c
                       for c in retained['cookies'] + state['cookies']}
            state = {'cookies': list(cookies.values()), 'origins': retained['origins'] + state['origins']}
        self.identity = self.accounts.capture(self.account_id, state, self.identity['version'])
        if self.approved_origin:
            self.identity = {**self.identity, 'origins': [self.approved_origin]}
        if self.identity['retain']:
            self._write_state(filter_state(state, self.identity['origins']))
        return self.identity

    async def _save_cache(self) -> None:
        try:
            await self.capture()
        except Exception:  # noqa: BLE001 —— 绝不把 Cookie 或 MCP 返回正文放入日志
            self.cache_error = '登录态未保存：账号设置已变化或浏览器不可用'
            if getattr(self, 'on_cache_error', None):
                self.on_cache_error(self.cache_error)

    async def close(self, *, save: bool = True) -> None:
        try:
            if save and self._used and self.identity['retain']:
                await self._save_cache()
        finally:
            self._used = False
            try:
                await super().close()
            finally:
                self._remove_state_file()
