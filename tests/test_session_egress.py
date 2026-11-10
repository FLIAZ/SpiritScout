import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.resources.proxy_pool import ProxyPool
from spiritscout.resources.store import ResourceStore
from spiritscout.traffic.egress import bind, mihomo_config, session_egress, upstream
from spiritscout.traffic.proxy import tool_environment


class EgressTests(unittest.IsolatedAsyncioTestCase):
    def test_mihomo_runtime_explicit_override_and_shared_detection(self):
        from spiritscout.traffic.mihomo import executable

        with tempfile.TemporaryDirectory() as root:
            binary = Path(root) / 'mihomo'
            with patch.dict(os.environ, {'SPIRITSCOUT_MIHOMO': str(binary)}):
                self.assertIsNone(executable())
                binary.write_text('#!/bin/sh\nexit 0\n')
                binary.chmod(0o755)
                self.assertEqual(executable(), str(binary))
                self.assertTrue(ProxyPool(ResourceStore(Path(root) / 'resources')).view()['mihomo_available'])

    async def test_unavailable_egress_fails_only_network_call_and_keeps_other_tools(self):
        from spiritscout.tools.registry import ToolRegistry
        from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec
        from spiritscout.traffic.proxy import EgressUnavailable

        registry = ToolRegistry(SimpleNamespace())
        async def network(**_):
            raise EgressUnavailable('固定代理不可用，未回退直连')
        async def offline(**_):
            return ToolResult(tool='offline', ok=True)
        registry.register_builtin('network', network, ToolSpec(name='network', kind=ToolKind.BUILTIN))
        registry.register_builtin('offline', offline, ToolSpec(name='offline', kind=ToolKind.BUILTIN))
        result = await registry.call('network', {})
        self.assertFalse(result.ok)
        self.assertEqual(result.effect_status, 'none')
        self.assertTrue((await registry.call('offline', {})).ok)

    async def test_resume_egress_failure_preserves_existing_terminal_result(self):
        from spiritscout.config.settings import SystemConfig, WorkspaceConfig
        from spiritscout.web.sessions import Session, SessionManager

        with tempfile.TemporaryDirectory() as root:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=root))
            manager = SessionManager(cfg, [])
            workspace = manager.root / 'sess-resume-fixture'
            workspace.mkdir()
            session = Session(session_id='sess-resume-fixture', mode='src', target='https://example.test/',
                workspace=workspace, status='running', resume_prev_status='completed', summary={'previous': 'result'},
                request={'proxy_binding': {'id': 'proxy-fixture'}})
            await manager._run(session, cfg, None, True, [], resume=True)
            self.assertEqual(session.status, 'completed')
            self.assertEqual(session.summary, {'previous': 'result'})
            self.assertIn('续传未完成', session.error)

    async def test_fixed_exit_snapshot_survives_restart_and_changed_exit_fails(self):
        from spiritscout.traffic.proxy import executable

        if not executable():
            self.skipTest('需要本机录制网关')
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(Path(root) / 'resources')
            pool = ProxyPool(store)
            result = pool.import_content('proxies:\n- {name: fixture, type: http, server: localhost, port: 8080}', 0)
            result = pool.save_plan('fixture', [result['nodes'][0]['id']], result['version'])
            workspace = Path(root) / 'session'
            workspace.mkdir()
            session = SimpleNamespace(session_id='fixture', workspace=workspace,
                                      request={'proxy_binding': bind(store, 'fixture', result['plans'][0]['id'])})
            with patch('spiritscout.traffic.egress.verify_exit', new=AsyncMock(return_value='8.8.8.8')):
                async with session_egress(store, session):
                    self.assertEqual(session.request['proxy_binding']['exit_ip'], '8.8.8.8')
                    env = await tool_environment(workspace)
                    self.assertTrue(Path(env['SPIRITSCOUT_EGRESS_SOCKET']).is_socket())
            self.assertFalse(Path(env['SPIRITSCOUT_EGRESS_SOCKET']).exists())
            with self.assertRaisesRegex(ValueError, '未回退直连'):
                await tool_environment(workspace)
            with patch('spiritscout.traffic.egress.verify_exit', new=AsyncMock(return_value='1.1.1.1')):
                with self.assertRaisesRegex(Exception, '公网出口已改变'):
                    async with session_egress(store, session):
                        self.fail('改变出口后不能开工')

    @unittest.skipUnless(os.environ.get('SPIRITSCOUT_RUN_LOCAL_BROWSER_TEST') == '1', '按需启用本机真实浏览器验证')
    async def test_real_browser_through_isolated_stdio_and_recording_gateway(self):
        from spiritscout.config.settings import McpServerConfig
        from spiritscout.platform_support import chrome_executable
        from spiritscout.tools.mcp_client import build_client
        from spiritscout.traffic import recorder
        from spiritscout.traffic.bridge import EgressBridge
        from spiritscout.traffic.proxy import SessionProxy, executable

        candidates = list(Path('.tools/chrome-mcp/npm-cache/_npx').glob('*/node_modules/@playwright/mcp/cli.js'))
        if not candidates or not executable() or not chrome_executable({}):
            self.skipTest('本机未准备 Chrome、录制网关或 Playwright MCP 缓存')
        async def serve(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                payload = b'<html><title>SpiritScout fixture</title><body>isolated-browser-ok</body></html>'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' +
                             str(len(payload)).encode() + b'\r\nConnection: close\r\n\r\n' + payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            proxy = SessionProxy(workspace)
            proxy.task = asyncio.create_task(proxy.serve(''))
            bridge = client = None
            try:
                await asyncio.wait_for(asyncio.shield(proxy.ready), 20)
                bridge = EgressBridge(proxy.address)
                await bridge.start()
                proxy.egress_socket = bridge.path
                env = await proxy.environment()
                env.update(SPIRITSCOUT_CHROME_MODE='headless', SPIRITSCOUT_CHROME_MCP_CLI=str(candidates[0].resolve()),
                    SPIRITSCOUT_CHROME_EXECUTABLE=chrome_executable({}), SPIRITSCOUT_CHROME_OUTPUT_DIR=str(workspace / 'browser-output'))
                client = build_client(McpServerConfig(name='chrome', command='python', args=['-m', 'spiritscout.tools.chrome_mcp'], env=env))
                await client.start()
                result = await client.call('browser_navigate', {'url': f'http://127.0.0.1:{port}/'}, timeout=30)
                self.assertFalse(result['is_error'], result['text'][:1600])
                page = await client.call('browser_evaluate', {'function': '() => document.body.textContent'}, timeout=15)
                self.assertFalse(page['is_error'], page['text'][:1600])
                self.assertIn('isolated-browser-ok', page['text'])
                await client.close()
                client = None
                await proxy.stop()
                self.assertGreaterEqual(recorder.summary(workspace)['total'], 1)
            finally:
                if client:
                    await client.close()
                if bridge:
                    await bridge.close()
                await proxy.stop()
                server.close()
                await server.wait_closed()

    @unittest.skipUnless(shutil.which('bwrap'), '需要本机 bubblewrap')
    async def test_real_isolated_command_can_use_bridge_but_not_direct_socket(self):
        from spiritscout.tools.egress_isolation import isolate_command
        from spiritscout.traffic.bridge import EgressBridge

        requests = []
        async def serve(reader, writer):
            try:
                requests.append(await reader.readuntil(b'\r\n\r\n'))
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nfixture')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        bridge = EgressBridge(f'http://127.0.0.1:{port}')
        await bridge.start()
        env = {**os.environ, 'SPIRITSCOUT_EGRESS_SOCKET': bridge.path,
               'HTTP_PROXY': f'http://127.0.0.1:{port}', 'HTTPS_PROXY': f'http://127.0.0.1:{port}',
               'NO_PROXY': '', 'no_proxy': ''}
        script = '''import socket,sys,urllib.request
try:
 socket.create_connection(("127.0.0.1",int(sys.argv[1])), timeout=.3)
 raise AssertionError("direct connection escaped isolation")
except OSError:
 pass
assert urllib.request.urlopen("http://fixture.invalid/", timeout=5).read() == b"fixture"
print("isolated-proxy-ok")
'''
        process = None
        try:
            process = await asyncio.create_subprocess_exec(*isolate_command([sys.executable, '-c', script, str(port)], env),
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, error = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 0, error.decode())
            self.assertIn(b'isolated-proxy-ok', output)
            self.assertIn(b'GET http://fixture.invalid/', requests[0])
            from spiritscout.tools.sandbox import sandbox_exec

            with tempfile.TemporaryDirectory() as workspace:
                (Path(workspace) / 'traffic-ca').mkdir()
                with patch('spiritscout.traffic.proxy.tool_environment', new=AsyncMock(return_value=env)):
                    result = await sandbox_exec(script.replace('int(sys.argv[1])', str(port)),
                                                language='python', workspace_root=workspace)
                self.assertEqual(result.returncode, 0, result.error + result.stderr)
                self.assertIn('isolated-proxy-ok', result.stdout)
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            await bridge.close()
            server.close()
            await server.wait_closed()

    async def test_required_proxy_never_returns_direct_environment(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            (workspace / 'meta.json').write_text(json.dumps({'request': {'proxy_binding': {'id': 'proxy-fixture'}}}))
            with self.assertRaisesRegex(ValueError, '未回退直连'):
                await tool_environment(workspace)
            (workspace / 'meta.json').write_text('{}')
            self.assertEqual(await tool_environment(workspace), {})
            (workspace / 'meta.json').write_text('generic-workspace-fixture')
            self.assertEqual(await tool_environment(workspace), {})
            from spiritscout.traffic.proxy import _REQUIRED

            _REQUIRED.add(str(workspace.resolve()))
            try:
                with self.assertRaisesRegex(ValueError, '未回退直连'):
                    await tool_environment(workspace)
            finally:
                _REQUIRED.discard(str(workspace.resolve()))

    async def test_missing_snapshot_fails_before_entering_execution(self):
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(root)
            session = SimpleNamespace(request={'proxy_binding': {'id': 'proxy-fixture'}}, session_id='fixture', workspace=Path(root))
            with self.assertRaisesRegex(Exception, '快照缺失'):
                async with session_egress(store, session):
                    self.fail('不得执行目标任务')

    def test_binding_keeps_credentials_private_and_chain_order_fixed(self):
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(root)
            pool = ProxyPool(store)
            result = pool.import_content('proxies:\n- {name: a, type: http, server: localhost, port: 8080, password: secret-fixture}', 0)
            result = pool.save_plan('fixture', [result['nodes'][0]['id']], result['version'])
            public = bind(store, 'fixture', result['plans'][0]['id'])
            self.assertNotIn('secret-fixture', json.dumps(public))
            pool.import_content('proxies:\n- {name: a, type: http, server: localhost, port: 8080, password: updated-fixture}', result['version'])
            copied = bind(store, 'child', result['plans'][0]['id'], source_session='fixture')
            self.assertEqual(copied['id'], public['id'])
            self.assertEqual(store.setting('session_egress', 'child')['data']['nodes'][0]['password'], 'secret-fixture')
            self.assertEqual(pool.snapshot(result['plans'][0]['id'])['nodes'][0]['password'], 'updated-fixture')
            store.remove_session('fixture')
            self.assertIsNone(store.setting('session_egress', 'fixture'))
            self.assertIsNotNone(store.setting('session_egress', 'child'))
            node = pool.snapshot(result['plans'][0]['id'])['nodes'][0]
            config = mihomo_config([node, node], 12345)
            self.assertEqual(config['proxies'][1]['dialer-proxy'], 'hop-0')
            self.assertEqual(config['rules'], ['MATCH,hop-1'])
            self.assertNotIn('dialer-proxy', config['proxies'][0])
            self.assertIn('updated-fixture', upstream(node))
            self.assertIn('secret-fixture', upstream(store.setting('session_egress', 'child')['data']['nodes'][0]))
