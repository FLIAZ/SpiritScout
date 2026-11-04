"""启动预热不调用业务工具；独占移交与取消使用真实 SDK 生命周期验证。"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from spiritscout.config.settings import (
    McpServerConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.models import Authorization, RunMode, Scope
from spiritscout.runtime import _build_infra
from spiritscout.tools.mcp_client import McpError
from spiritscout.tools.mcp_preflight import McpPreflight, current_preflight
from spiritscout.web.app import create_app


def client():
    return SimpleNamespace(start=AsyncMock(return_value=[{
        'name': 'observe', 'inputSchema': {'type': 'object', 'properties': {}},
        'annotations': {'readOnlyHint': True}}]), close=AsyncMock(), connected=True,
        call=AsyncMock(return_value={'text': 'ok', 'is_error': False}))


class McpPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = McpPreflight()
        self.addAsyncCleanup(self.pool.close)
        self.spec = McpServerConfig(name='fixture', command='fixture', default_risk='readonly')

    async def warm(self, fake, spec=None):
        with patch('spiritscout.tools.mcp_preflight.build_client', return_value=fake):
            self.pool.start([spec or self.spec])
            await asyncio.gather(*(entry.task for entry in self.pool.entries.values()))

    async def test_warm_connection_is_exclusively_transferred_without_another_probe(self):
        fake = client()
        await self.warm(fake)
        fake.call.assert_not_awaited()
        first = await self.pool.take(self.spec)
        self.assertIs(first[0], fake)
        self.assertIsNone(await self.pool.take(self.spec), 'other sessions must not share the same mutable SDK session')
        await self.pool.close()
        fake.close.assert_not_awaited()
        await fake.close()  # 接管者负责关闭

    async def test_failed_preflight_is_reported_without_leaking_credentials(self):
        fake = client()
        fake.start.side_effect = RuntimeError('authentication failed SECRET-TOKEN')
        spec = self.spec.model_copy(update={'env': {'API_TOKEN': 'SECRET-TOKEN'}})
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            await self.warm(fake, spec)
        self.assertIn('启动预检失败', output.getvalue())
        self.assertNotIn('SECRET-TOKEN', output.getvalue())
        with self.assertRaises(McpError):
            await self.pool.take(spec)
        fake.start.assert_awaited_once()
        fake.close.assert_awaited_once()

    async def test_cancelling_a_waiter_does_not_cancel_preheat(self):
        fake, ready = client(), asyncio.Event()
        async def start():
            await ready.wait()
            return []
        fake.start.side_effect = start
        with patch('spiritscout.tools.mcp_preflight.build_client', return_value=fake):
            self.pool.start([self.spec])
            waiting = asyncio.create_task(self.pool.take(self.spec))
            await asyncio.sleep(0)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            ready.set()
            taken = await self.pool.take(self.spec)
        self.assertIs(taken[0], fake)
        fake.start.assert_awaited_once()
        await fake.close()

    async def test_config_change_or_disconnected_client_does_not_reuse_old_connection(self):
        fake = client()
        await self.warm(fake)
        changed = self.spec.model_copy(update={'env': {'IDENTITY': 'different'}})
        self.assertIsNone(await self.pool.take(changed))
        fake.connected = False
        self.assertIsNone(await self.pool.take(self.spec))
        fake.close.assert_awaited_once()

    async def test_browser_preflight_only_checks_catalog_and_closes_its_instance(self):
        fake = client()
        spec = self.spec.model_copy(update={'args': ['-m', 'spiritscout.tools.chrome_mcp']})
        await self.warm(fake, spec)
        self.assertEqual(self.pool.snapshot()[0]['status'], 'catalog_only')
        self.assertIsNone(await self.pool.take(spec))
        fake.call.assert_not_awaited()
        fake.close.assert_awaited_once()

    async def test_runtime_uses_preheated_client_and_releases_it_at_session_end(self):
        fake = client()
        await self.warm(fake)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp), mcp_servers=[self.spec], audit_log_path=str(root/'audit.jsonl'))
            cfg.blackboard.sqlite_path = str(root/'bb.db')
            cfg.knowledge.sqlite_path = str(root/'kb.db')
            auth = Authorization(auth_id='test', mode=RunMode.SRC, human_confirmed=True,
                                 scope=Scope(allowed_domains=['fixture.test']))
            token = current_preflight.set(self.pool)
            try:
                with patch('spiritscout.tools.mcp_client.build_client') as build:
                    infra = await _build_infra(cfg, [ModelEntry(model='fixture', provider='mock')], auth)
                build.assert_not_called()
                try:
                    result = await infra.tools.call('mcp__fixture__observe', {}, **infra.builtin_ctx)
                    self.assertTrue(result.ok)
                    fake.start.assert_awaited_once()
                finally:
                    await infra.close()
            finally:
                current_preflight.reset(token)
        await self.pool.close()
        fake.close.assert_awaited_once()

    async def test_shutdown_cancels_pending_warmup_and_closes_unclaimed_clients(self):
        fake, started = client(), asyncio.Event()
        async def start():
            started.set()
            await asyncio.Event().wait()
        fake.start.side_effect = start
        with patch('spiritscout.tools.mcp_preflight.build_client', return_value=fake):
            self.pool.start([self.spec])
            await started.wait()
            await self.pool.close()
        fake.close.assert_awaited_once()
        self.assertIsNone(await self.pool.take(self.spec))

    async def test_actual_stdio_sdk_session_survives_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp)/'server.py'
            script.write_text('import sys\nprint("startup-diagnostic",file=sys.stderr,flush=True)\n'
                              'from mcp.server.fastmcp import FastMCP\n'
                              'app=FastMCP("preflight-fixture",log_level="ERROR")\n'
                              '@app.tool()\n'
                              'def echo(value: str) -> str:\n    return value\n'
                              'app.run(transport="stdio")\n')
            spec = McpServerConfig(name='real-sdk-fixture', command=sys.executable, args=[str(script)])
            self.pool.start([spec])
            taken = await asyncio.wait_for(self.pool.take(spec), 15)
            try:
                self.assertIn('echo', [tool['name'] for tool in taken[1]])
                response = await asyncio.wait_for(taken[0].call('echo', {'value': 'handoff-ok'}), 5)
                self.assertEqual(response['text'], 'handoff-ok')
                self.assertFalse(response['is_error'])
                self.assertIn('startup-diagnostic', taken[0].diagnostic_stderr)
            finally:
                await taken[0].close()


class McpPreflightLifespanTests(unittest.TestCase):
    def test_service_start_preheats_once_exposes_status_and_closes_on_shutdown(self):
        fake = client()
        spec = McpServerConfig(name='lifespan-fixture', command='fixture')
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp), mcp_servers=[spec])
            with patch('spiritscout.tools.mcp_preflight.build_client', return_value=fake):
                app = create_app(cfg, [])
                with TestClient(app, base_url='http://127.0.0.1') as http:
                    response = http.get('/api/mcp/health')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['servers'][0]['server'], spec.name)
                    self.assertEqual(response.json()['servers'][0]['readiness'], 'tool_catalog')
                    http.portal.call(app.state.manager.start)  # 幂等启动不能重复建连接
                    fake.start.assert_awaited_once()
                    fake.call.assert_not_awaited()
                fake.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
