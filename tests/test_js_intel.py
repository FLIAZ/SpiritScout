import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.agents.js_reverse import _route_observations, analyze_script
from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.hypotheses import upsert
from spiritscout.core.models import AgentType, BlackboardLayer
from spiritscout.resources.session_secrets import SessionSecrets


class JSIntelTests(unittest.TestCase):
    def test_repeated_hypothesis_merges_into_stable_identity_and_keeps_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            first = upsert(
                bb, claim="接口可能存在对象级权限边界", subject="https://a.test/api/user",
                test_type="idor", asset_refs=["asset:script@1"],
                expected_signal="替换对象 ID 后返回其他用户数据",
                next_minimal_experiment="使用两个授权账号比较同一对象请求",
                stop_condition="服务端始终按会话身份约束对象归属",
                technical_basis={"technologies": "REST", "rationale": "脚本暴露 user_id 参数"},
                actor="js-reverse", agent_type=AgentType.JS_REVERSE,
            )
            second = upsert(
                bb, claim="接口可能存在对象级权限边界", subject="https://a.test/api/user",
                test_type="idor", asset_refs=["asset:endpoint@2"],
                counter_signal="跨账号请求被拒绝或只返回当前账号对象",
                technical_basis={"technologies": ["GraphQL"], "tags": ["idor"]},
                actor="js-reverse", agent_type=AgentType.JS_REVERSE,
            )

            rows = [row for row in bb.query(BlackboardLayer.TOPOLOGY)
                    if row["data"].get("hypothesis_contract") is True]
            self.assertEqual(len(rows), 1)
            self.assertEqual(first["hypothesis_ref"].split("@")[0],
                             second["hypothesis_ref"].split("@")[0])
            self.assertEqual(rows[0]["data"]["asset_refs"], [
                "asset:script@1", "asset:endpoint@2",
            ])
            self.assertIn("两个授权账号", rows[0]["data"]["next_minimal_experiment"])
            self.assertIn("对象归属", rows[0]["data"]["stop_condition"])
            self.assertEqual(rows[0]["data"]["technical_basis"]["technologies"], ["REST", "GraphQL"])
            self.assertEqual(rows[0]["data"]["technical_basis"]["tags"], ["idor"])

    def test_redaction_preserves_schema_keys_and_existing_references(self):
        from spiritscout.resources.session_secrets import protect_values

        reference = 'secret-012345abcdef'
        self.assertEqual(protect_values({'host': 'host', 'reference': reference}, {'host': reference}),
                         {'host': reference, 'reference': reference})
        self.assertEqual(protect_values(reference, {'secret': reference}), reference)

    def test_reveal_is_explicit_session_scoped_and_audited_without_plaintext(self):
        from spiritscout.resources.store import ResourceStore
        from spiritscout.web.proxies import mount_proxies

        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            reference = SessionSecrets(workspace).save('reveal-fixture-value', source='https://example.com/', kind='token')['secret_ref']
            session = SimpleNamespace(workspace=workspace, audit_path=workspace / 'audit.jsonl')
            app = FastAPI()
            mount_proxies(app, ResourceStore(workspace / 'resources'), SimpleNamespace(get=lambda key: session if key == 'fixture' else None))
            with TestClient(app) as client:
                url = f'/api/sessions/fixture/secrets/{reference}/reveal'
                self.assertEqual(client.post(url, json={'target': 'https://example.com/'}).status_code, 422)
                self.assertEqual(client.post(url, json={'confirm': True, 'target': 'https://evil.test/'}).status_code, 422)
                response = client.post(url, json={'confirm': True, 'target': 'https://example.com/'})
                self.assertEqual(response.json()['value'], 'reveal-fixture-value')
                self.assertEqual(response.headers['cache-control'], 'no-store')
            audit = session.audit_path.read_text()
            self.assertIn('secret.revealed', audit)
            self.assertNotIn('reveal-fixture-value', audit)

    def test_secret_encrypted_and_reusable_within_origin(self):
        with tempfile.TemporaryDirectory() as root:
            vault = SessionSecrets(root)
            data = analyze_script('const apiKey="private-fixture-value";fetch("/api/user?token=private-fixture-value");\n//# sourceMappingURL=app.js.map',
                                  'https://example.com/static/app.js', vault)
            self.assertNotIn('private-fixture-value', json.dumps(data))
            self.assertTrue(any(route.startswith('/api/user?token=secret-') for route in data['routes']))
            self.assertEqual(data['source_maps'], ['https://example.com/static/app.js.map'])
            ref = data['secrets'][0]['secret_ref']
            self.assertEqual(vault.resolve(ref, 'https://example.com/api/user'), 'private-fixture-value')
            with self.assertRaises(ValueError):
                vault.resolve(ref, 'https://other.example/api/user')
            self.assertNotIn(b'private-fixture-value', vault.store.path.read_bytes())

    def test_static_calls_keep_distinct_methods_for_the_same_route(self):
        with tempfile.TemporaryDirectory() as root:
            data = analyze_script(
                'fetch("/api/user"); fetch("/api/user", {method: "POST"}); '
                'axios.delete("/api/user")',
                'https://example.com/app.js', SessionSecrets(root))
            self.assertEqual({(item['route'], item['method']) for item in data['endpoint_hints']}, {
                ('/api/user', 'GET'), ('/api/user', 'POST'), ('/api/user', 'DELETE'),
            })
            self.assertEqual({(item['route'], item['method']) for item in _route_observations(data)}, {
                ('/api/user', 'GET'), ('/api/user', 'POST'), ('/api/user', 'DELETE'),
            })


class SecretInjectionTests(unittest.IsolatedAsyncioTestCase):
    def test_wildcard_scope_accepts_apex_and_subdomain_for_redirects(self):
        from spiritscout.agents.scoped_assets import _within
        from spiritscout.core.models import Scope

        scope = Scope(allowed_domains=["*.example.com"])
        self.assertTrue(_within("https://example.com/", scope))
        self.assertTrue(_within("https://www.example.com/app.js", scope))

    async def test_js_role_follows_in_scope_redirect_before_parsing_scripts(self):
        from spiritscout.agents.js_reverse import JSReverseAgent
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.audit import NullAuditLogger
        from spiritscout.core.models import Scope
        from spiritscout.policy.scope import ScopeGuard

        requested = []
        async def serve(reader, writer):
            request = await reader.readuntil(b'\r\n\r\n')
            path = request.split(b' ')[1].decode()
            requested.append(path)
            if path == '/':
                response = b'HTTP/1.1 302 Found\r\nLocation: /home/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            elif path == '/home/':
                body = b'<script src="/app.js"></script>'
                response = (b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode()
                            + b'\r\nConnection: close\r\n\r\n' + body)
            else:
                body = b'fetch("/api/user")'
                response = (b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode()
                            + b'\r\nConnection: close\r\n\r\n' + body)
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        base = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
        try:
            with tempfile.TemporaryDirectory() as root:
                bb = Blackboard(Path(root) / 'bb.db')
                try:
                    guard = ScopeGuard(Scope(allowed_urls=[base]))
                    ctx = SimpleNamespace(for_agent=lambda key: ctx, shared_cache=None,
                        policy=SimpleNamespace(guard=guard), builtin_ctx={'workspace_root': root},
                        blackboard=bb, audit=NullAuditLogger())
                    agent = JSReverseAgent(ctx, enable_llm_planning=False)
                    agent._llm_chat_json = AsyncMock(return_value={})
                    result = await agent.execute_step(
                        SimpleNamespace(target=base), None, None, {'action': 'analyze'})
                    self.assertTrue(result.ok, result.note)
                    self.assertEqual(requested, ['/', '/home/', '/app.js'])
                finally:
                    bb.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_js_role_reads_scoped_scripts_and_inline_source_map_without_fetching_sources(self):
        from spiritscout.agents.js_reverse import JSReverseAgent
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.audit import NullAuditLogger
        from spiritscout.core.models import BlackboardLayer, Scope
        from spiritscout.policy.scope import ScopeGuard
        from spiritscout.tools.js_intel import js_source_read

        requested = []
        async def serve(reader, writer):
            request = await reader.readuntil(b'\r\n\r\n')
            path = request.split(b' ')[1].decode()
            requested.append(path)
            bodies = {'/': '<script src="/app.js"></script><script src="https://out-of-scope.invalid/script.js"></script>',
                      '/app.js': 'const apiKey="role-private-fixture";fetch("/api/user");\n//# sourceMappingURL=app.js.map',
                      '/app.js.map': json.dumps({'sources': ['https://out-of-scope.invalid/source.js'],
                          'sourcesContent': ['const accessToken="map-private-fixture";']})}
            body = bodies[path].encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        base = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
        try:
            with tempfile.TemporaryDirectory() as root:
                bb = Blackboard(Path(root) / 'bb.db')
                try:
                    guard = ScopeGuard(Scope(allowed_urls=[base]))
                    ctx = SimpleNamespace(for_agent=lambda key: ctx, shared_cache=None, policy=SimpleNamespace(guard=guard),
                        builtin_ctx={'workspace_root': root}, blackboard=bb, audit=NullAuditLogger())
                    agent = JSReverseAgent(ctx, enable_llm_planning=False)
                    agent._llm_chat_json = AsyncMock(side_effect=[{
                        'algorithm_summary': '静态观察到接口调用',
                        'hypotheses': [{'claim': '接口可能存在对象级权限边界', 'test_type': 'idor'}],
                        'unknowns': ['需要登录态验证'],
                    }, {}])
                    result = await agent.execute_step(SimpleNamespace(target=base), None, None, {'action': 'analyze'})
                    self.assertTrue(result.ok, result.note)
                    self.assertEqual(requested, ['/', '/app.js', '/app.js.map'])
                    self.assertEqual(bb.query(BlackboardLayer.JS_INTEL), [])
                    records = bb.query(BlackboardLayer.ASSET, limit=20)
                    self.assertEqual(len(records), 4)
                    by_value = {row['data']['value']: row for row in records}
                    script_url = base + 'app.js'
                    map_url = base + 'app.js.map'
                    endpoint_url = base + 'api/user'
                    self.assertEqual(set(by_value), {
                        script_url, endpoint_url, map_url, map_url + '#source-0',
                    })
                    script = by_value[script_url]['data']
                    endpoint = by_value[endpoint_url]['data']
                    source_map = by_value[map_url]['data']
                    inline = by_value[map_url + '#source-0']['data']
                    self.assertEqual(script['asset_type'], 'script')
                    self.assertEqual(endpoint['asset_type'], 'endpoint')
                    self.assertEqual(endpoint['attrs']['method'], 'GET')
                    self.assertEqual(source_map['attrs']['kind'], 'source_map')
                    self.assertEqual(inline['attrs']['kind'], 'source_map_source')
                    self.assertNotIn('hypotheses', script['attrs']['analysis'])

                    topology = bb.query(BlackboardLayer.TOPOLOGY, limit=20)
                    relations = [row['data'] for row in topology
                                 if row['data'].get('type') == 'asset_relation']
                    hypotheses = [row['data'] for row in topology
                                  if row['data'].get('kind') == 'exploration']
                    self.assertEqual(len(relations), 3)
                    self.assertEqual(len(hypotheses), 1)
                    refs = {value: f"asset:{row['record_id']}"
                            for value, row in by_value.items()}
                    self.assertEqual({(row['source_ref'], row['relation'], row['target_ref'])
                                      for row in relations}, {
                        (refs[script_url], 'exposes', refs[endpoint_url]),
                        (refs[script_url], 'exposes', refs[map_url]),
                        (refs[map_url], 'contains', refs[map_url + '#source-0']),
                    })
                    self.assertEqual(hypotheses[0]['node_type'], 'hypothesis')
                    self.assertEqual(hypotheses[0]['asset_refs'], [
                        f"{refs[script_url]}@{by_value[script_url]['version']}"
                    ])
                    self.assertIn('对象级权限边界', hypotheses[0]['text'])

                    public = json.dumps({'assets': records, 'topology': topology})
                    self.assertNotIn('role-private-fixture', public)
                    self.assertNotIn('map-private-fixture', public)
                    self.assertNotIn('接口可能存在对象级权限边界', json.dumps(records))
                    source = await js_source_read(script['attrs']['script_ref'], base,
                                                  guard=guard, workspace_root=root)
                    self.assertTrue(source.ok, source.error)
                    self.assertNotIn('role-private-fixture', source.stdout)
                    self.assertIn('/api/user', source.parsed['source'])
                finally:
                    bb.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_tool_injects_real_values_but_returns_and_saves_references(self):
        from urllib.parse import quote

        from spiritscout.core.models import Scope
        from spiritscout.policy.scope import ScopeGuard
        from spiritscout.tools.builtin import http_probe

        received = []
        async def serve(reader, writer):
            request = await reader.readuntil(b'\r\n\r\n')
            received.append(request)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: ' +
                         str(len(request)).encode() + b'\r\nConnection: close\r\n\r\n' + request)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        base = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/'
        try:
            with tempfile.TemporaryDirectory() as root:
                value = 'fixture+key/value'
                ref = SessionSecrets(root).save(value, source=base, kind='token')['secret_ref']
                result = await http_probe(base, guard=ScopeGuard(Scope(allowed_urls=[base])), workspace_root=root,
                    secret_headers={'X-Key': ref, 'Authorization': {'secret_ref': ref, 'prefix': 'Bearer '}},
                    secret_cookies={'sid': ref}, secret_params={'signature': ref})
                self.assertTrue(result.ok, result.error)
                self.assertIn(value.encode(), received[0])
                self.assertIn(b'Authorization: Bearer ' + value.encode(), received[0])
                self.assertIn(quote(value, safe='').encode(), received[0])
                public = json.dumps(result.parsed) + result.stdout
                self.assertNotIn(value, public)
                self.assertNotIn(quote(value, safe=''), public)
                self.assertIn(ref, public)
                self.assertNotIn(value.encode(), Path(result.parsed['body_path']).read_bytes())
        finally:
            server.close()
            await server.wait_closed()
