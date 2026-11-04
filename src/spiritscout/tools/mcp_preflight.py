"""服务启动时预热 MCP 目录；连接独占移交，工具调用不增加健康探测。"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from spiritscout.config.settings import McpServerConfig
from spiritscout.tools.mcp_client import McpError, McpStdioClient, build_client

current_preflight: ContextVar[McpPreflight | None] = ContextVar('mcp_preflight', default=None)


def _key(spec: McpServerConfig) -> str:
    # 凭据和环境参与身份，但只保存摘要；配置变化不能接管旧连接。
    raw = json.dumps(spec.model_dump(mode='json'), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class _Entry:
    name: str
    task: asyncio.Task | None = None
    client: Any = None
    tools: list[dict[str, Any]] | None = None
    claimed: bool = False
    status: str = 'starting'
    error: str = ''
    finished: float = 0


class McpPreflight:
    def __init__(self) -> None:
        self.entries: dict[str, _Entry] = {}
        self.closed = False
        self._slots = asyncio.Semaphore(4)

    def start(self, specs: list[McpServerConfig]) -> None:
        if self.closed:
            return
        for spec in specs:
            key = _key(spec)
            if not spec.enabled or key in self.entries:
                continue
            entry = self.entries[key] = _Entry(spec.name)
            entry.task = asyncio.create_task(self._warm(spec, entry), name=f'mcp-preflight[{spec.name}]')

    async def _warm(self, spec: McpServerConfig, entry: _Entry) -> None:
        async with self._slots:
            await self._connect(spec, entry)

    async def _connect(self, spec: McpServerConfig, entry: _Entry) -> None:
        try:
            entry.client = build_client(spec)
            if isinstance(entry.client, McpStdioClient):
                entry.client.capture_stderr = True
            entry.tools = await entry.client.start()
            # 浏览器依赖模式、输出目录和登录现场；只预检目录，不跨会话复用实例。
            if list(spec.args[:2]) == ['-m', 'spiritscout.tools.chrome_mcp']:
                await entry.client.close()
                entry.client = None
                entry.status = 'catalog_only'
            else:
                entry.status = 'ready'
        except asyncio.CancelledError:
            entry.status = 'closed'
            raise
        except Exception as exc:  # noqa: BLE001 —— 可选 MCP 故障不影响工作台开工
            causes = [exc]
            while any(isinstance(cause, BaseExceptionGroup) for cause in causes):
                causes = [leaf for cause in causes for leaf in (cause.exceptions if isinstance(cause, BaseExceptionGroup) else [cause])]
            error = '; '.join(f'{type(cause).__name__}: {cause}' for cause in causes[:4])
            stderr = getattr(entry.client, 'diagnostic_stderr', '')
            if isinstance(stderr, str) and stderr:
                error += '; stderr: ' + stderr[-300:]
            secrets = [*spec.headers.values(), *(value for key, value in spec.env.items()
                       if any(word in key.casefold() for word in ('key', 'token', 'secret', 'password', 'cookie')))]
            for value in sorted((v for v in secrets if v), key=len, reverse=True):
                error = error.replace(value, '[credential]')
            entry.error, entry.status = error[:500], 'failed'
            print(f'[spiritscout] MCP[{spec.name}] 启动预检失败：{entry.error}；请检查启动命令、服务地址及凭据。', file=sys.stderr)
        finally:
            entry.finished = time.monotonic()
            if entry.status != 'ready' and entry.client is not None:
                with contextlib.suppress(Exception):
                    await entry.client.close()
                entry.client = None

    async def take(self, spec: McpServerConfig) -> tuple[Any, list[dict[str, Any]]] | None:
        """一次性移交所有权；取消等待不取消服务预热，也不占住下一会话的连接。"""
        entry = self.entries.get(_key(spec))
        if self.closed or entry is None or entry.claimed:
            return None
        entry.claimed = True
        try:
            await asyncio.shield(entry.task)
            if entry.status == 'failed' and time.monotonic() - entry.finished < 10:
                raise McpError(f'MCP[{spec.name}] 启动预检失败：{entry.error}')
            if entry.status != 'ready' or entry.client is None:
                return None
            if not getattr(entry.client, 'connected', True):
                await entry.client.close()
                entry.client = None
                entry.status = 'disconnected'
                return None
            client, entry.client = entry.client, None
            entry.status = 'leased'
            return client, copy.deepcopy(entry.tools or [])
        finally:
            if entry.status != 'leased':
                entry.claimed = False

    def snapshot(self) -> list[dict[str, Any]]:
        return [{'server': entry.name, 'status': entry.status, 'readiness': 'tool_catalog',
                 'tools': len(entry.tools or []), **({'error': entry.error} if entry.error else {})}
                for entry in self.entries.values()]

    async def close(self) -> None:
        self.closed = True
        tasks = [e.task for e in self.entries.values() if e.task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        clients = [e.client for e in self.entries.values() if e.client is not None]
        await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
        for entry in self.entries.values():
            entry.client = None
