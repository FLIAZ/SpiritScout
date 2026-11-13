import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from spiritscout.agents.scoped_assets import ScopedAssetPublisher, _within
from spiritscout.core.models import Asset, Scope


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fofa_claims_require_scope_and_local_verification(self):
        import httpx

        from spiritscout.agents.discovery import discover
        from spiritscout.assets.providers.base import ProviderCandidate, ProviderResult
        from spiritscout.blackboard.convergence import ConvergenceEngine
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.config.settings import AssetIntelligenceConfig
        from spiritscout.core.models import BlackboardLayer
        from spiritscout.tools.spec import ToolResult

        async def search(*args, **kwargs):
            return ProviderResult("fofa", "ok", candidates=[
                ProviderCandidate("api.example.com", observation={
                    "provider_claim": {"ip": "203.0.113.9", "port": "8443"},
                    "verification_status": "unverified",
                }),
                ProviderCandidate("evil.test"),
            ], returned=2)

        async def tool_call(name, args, **kwargs):
            if name == "dns_resolve" and args["host"] == "api.example.com":
                return ToolResult(tool=name, ok=True, invocation_id="dns-api",
                                  parsed={"ips": ["203.0.113.10"], "proxy_virtual": False})
            return ToolResult(tool=name, ok=False, invocation_id="failed", error="unreachable")

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"{}"

        async def handle(request):
            return httpx.Response(200, stream=Body())

        original = httpx.AsyncClient
        def client(**options):
            return original(**options, transport=httpx.MockTransport(handle))

        with tempfile.TemporaryDirectory() as root:
            bb = Blackboard(Path(root) / "bb.db")
            self.addCleanup(bb.close)
            from unittest.mock import AsyncMock

            config = AssetIntelligenceConfig()
            config.fofa.enabled = True
            infra = SimpleNamespace(
                cfg=SimpleNamespace(workspace=SimpleNamespace(root=root),
                                    asset_intelligence=config),
                convergence=ConvergenceEngine(bb), blackboard=bb, resources=None,
                authorization=SimpleNamespace(scope=Scope(allowed_domains=["*.example.com"])),
                builtin_ctx={"limiter": SimpleNamespace(acquire=AsyncMock())},
                tools=SimpleNamespace(call=tool_call),
            )
            with patch("spiritscout.agents.discovery.tool_environment", AsyncMock(return_value={})), \
                    patch("spiritscout.agents.discovery.FofaProvider.search_domain", side_effect=search), \
                    patch("spiritscout.agents.discovery.httpx.AsyncClient", side_effect=client):
                result = await discover(infra, "*.example.com")

            self.assertIn("api.example.com", result["published"])
            assets = [row["data"] for row in bb.query(BlackboardLayer.ASSET)]
            self.assertNotIn("evil.test", {row["value"] for row in assets})
            self.assertNotIn("203.0.113.9", {row["value"] for row in assets})
            self.assertFalse(any(row.get("port") == 8443 for row in assets))

    async def test_parallel_dns_and_only_scoped_passive_assets(self):
        import httpx

        from spiritscout.agents.discovery import discover
        from spiritscout.blackboard.convergence import ConvergenceEngine
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.models import BlackboardLayer

        active, maximum = 0, 0
        class Body(httpx.AsyncByteStream):
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()
            async def __aiter__(self):
                yield self.payload

        async def handle(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            payload = ([{'name_value': 'a.example.com\nevil.test'}] if request.url.host == 'crt.sh'
                       else {'Answer': [{'type': 1, 'data': '203.0.113.1'}]})
            active -= 1
            return httpx.Response(200, stream=Body(payload))

        original = httpx.AsyncClient
        def client(**options):
            return original(**options, transport=httpx.MockTransport(handle))
        with tempfile.TemporaryDirectory() as root:
            bb = Blackboard(Path(root) / 'bb.db')
            self.addCleanup(bb.close)
            from unittest.mock import AsyncMock

            from spiritscout.config.settings import AssetIntelligenceConfig

            infra = SimpleNamespace(cfg=SimpleNamespace(workspace=SimpleNamespace(root=root),
                asset_intelligence=AssetIntelligenceConfig()),
                convergence=ConvergenceEngine(bb), blackboard=bb,
                authorization=SimpleNamespace(scope=Scope(allowed_domains=['*.example.com'])),
                builtin_ctx={'limiter': SimpleNamespace(acquire=AsyncMock())},
                tools=SimpleNamespace(call=AsyncMock(return_value=SimpleNamespace(ok=False, parsed={}, invocation_id='fixture', error='unreachable'))))
            with patch('spiritscout.agents.discovery.httpx.AsyncClient', side_effect=client):
                result = await discover(infra, '*.example.com')
            self.assertGreater(maximum, 1)
            self.assertEqual(result['published'], [])
            requested = [(call.args[0], call.args[1])
                         for call in infra.tools.call.call_args_list]
            self.assertEqual(requested, [
                ('dns_resolve', {'host': 'example.com'}),
                ('dns_resolve', {'host': 'a.example.com'}),
            ])
            self.assertEqual(bb.query(BlackboardLayer.ASSET), [])
            candidate = next(row['data'] for row in bb.query(BlackboardLayer.RECON_CLUE)
                             if row['data'].get('clue_type') == 'asset_candidate'
                             and row['data'].get('value') == 'a.example.com')
            self.assertEqual(candidate['value'], 'a.example.com')
            self.assertEqual(candidate['stage'], 'in_scope')

    async def test_dns_verified_candidate_is_promoted_and_http_probed(self):
        import httpx

        from spiritscout.agents.discovery import discover
        from spiritscout.blackboard.convergence import ConvergenceEngine
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.config.settings import AssetIntelligenceConfig
        from spiritscout.core.models import BlackboardLayer
        from spiritscout.tools.spec import ToolResult

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield json.dumps([{'name_value': 'a.example.com'}]).encode()

        async def handle(request):
            payload = Body() if request.url.host == 'crt.sh' else Body()
            if request.url.host != 'crt.sh':
                payload = type('DnsBody', (httpx.AsyncByteStream,), {
                    '__aiter__': lambda self: _bytes_iter(b'{"Answer": []}')})()
            return httpx.Response(200, stream=payload)

        async def _bytes_iter(value):
            yield value

        calls = []
        async def tool_call(name, args, **kwargs):
            calls.append((name, args))
            if name == 'dns_resolve':
                return ToolResult(tool=name, ok=True, invocation_id='inv-dns',
                                  parsed={'ips': ['203.0.113.10'], 'proxy_virtual': False})
            return ToolResult(tool=name, ok=True, invocation_id='inv-http',
                              parsed={'status_code': 200, 'headers': {},
                                      'fingerprint': {'server': 'nginx'}})

        original = httpx.AsyncClient
        def client(**options):
            return original(**options, transport=httpx.MockTransport(handle))
        with tempfile.TemporaryDirectory() as root:
            bb = Blackboard(Path(root) / 'bb.db')
            self.addCleanup(bb.close)
            from unittest.mock import AsyncMock

            infra = SimpleNamespace(cfg=SimpleNamespace(workspace=SimpleNamespace(root=root),
                asset_intelligence=AssetIntelligenceConfig()),
                convergence=ConvergenceEngine(bb), blackboard=bb,
                authorization=SimpleNamespace(scope=Scope(allowed_domains=['*.example.com'])),
                builtin_ctx={'limiter': SimpleNamespace(acquire=AsyncMock())},
                tools=SimpleNamespace(call=tool_call))
            with patch('spiritscout.agents.discovery.httpx.AsyncClient', side_effect=client):
                result = await discover(infra, '*.example.com')

            self.assertEqual(result['published'], ['a.example.com', 'example.com'])
            self.assertEqual([call for call in calls if call[0] == 'dns_resolve'], [
                ('dns_resolve', {'host': 'example.com'}),
                ('dns_resolve', {'host': 'a.example.com'}),
            ])
            probed = {call[1]['url'] for call in calls if call[0] == 'http_probe'}
            self.assertEqual(probed, {
                'https://example.com/', 'http://example.com/',
                'https://a.example.com/', 'http://a.example.com/',
            })
            self.assertEqual({row['data']['asset_type'] for row in bb.query(BlackboardLayer.ASSET)},
                             {'domain', 'url'})

    async def test_passive_candidate_is_verified_before_slower_provider_finishes(self):
        import httpx

        from spiritscout.agents.discovery import discover
        from spiritscout.assets.providers.base import ProviderResult
        from spiritscout.blackboard.convergence import ConvergenceEngine
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.config.settings import AssetIntelligenceConfig
        from spiritscout.core.models import BlackboardLayer
        from spiritscout.tools.spec import ToolResult

        fofa_release = asyncio.Event()
        fofa_finished = asyncio.Event()
        fast_verified = asyncio.Event()

        async def search(*args, **kwargs):
            await fofa_release.wait()
            fofa_finished.set()
            return ProviderResult('fofa', 'ok')

        class Body(httpx.AsyncByteStream):
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()
            async def __aiter__(self):
                yield self.payload

        async def handle(request):
            payload = ([{'name_value': 'fast.example.com'}]
                       if request.url.host == 'crt.sh' else {'Answer': []})
            return httpx.Response(200, stream=Body(payload))

        async def tool_call(name, args, **kwargs):
            if name == 'dns_resolve':
                if args['host'] == 'fast.example.com':
                    fast_verified.set()
                return ToolResult(tool=name, ok=True, invocation_id='dns-' + args['host'],
                                  parsed={'ips': ['203.0.113.10']})
            return ToolResult(tool=name, ok=False, invocation_id='http-failed',
                              error='unreachable')

        original = httpx.AsyncClient
        def client(**options):
            return original(**options, transport=httpx.MockTransport(handle))

        with tempfile.TemporaryDirectory() as root:
            bb = Blackboard(Path(root) / 'bb.db')
            self.addCleanup(bb.close)
            from unittest.mock import AsyncMock

            config = AssetIntelligenceConfig()
            config.fofa.enabled = True
            infra = SimpleNamespace(
                cfg=SimpleNamespace(workspace=SimpleNamespace(root=root),
                                    asset_intelligence=config),
                convergence=ConvergenceEngine(bb), blackboard=bb, resources=None,
                authorization=SimpleNamespace(
                    scope=Scope(allowed_domains=['*.example.com'])),
                builtin_ctx={'limiter': SimpleNamespace(acquire=AsyncMock())},
                tools=SimpleNamespace(call=tool_call),
            )
            with patch('spiritscout.agents.discovery.tool_environment',
                       AsyncMock(return_value={})), \
                    patch('spiritscout.agents.discovery.FofaProvider.search_domain',
                          side_effect=search), \
                    patch('spiritscout.agents.discovery.httpx.AsyncClient',
                          side_effect=client):
                task = asyncio.create_task(discover(infra, '*.example.com'))
                try:
                    await asyncio.wait_for(fast_verified.wait(), timeout=2)
                    self.assertFalse(fofa_finished.is_set())
                    assets = [row['data']['value']
                              for row in bb.query(BlackboardLayer.ASSET)]
                    self.assertIn('fast.example.com', assets)
                finally:
                    fofa_release.set()
                result = await task

            self.assertIn('fast.example.com', result['published'])

    async def test_exact_url_only_probes_delegated_path(self):
        import httpx

        from spiritscout.agents.discovery import discover
        from spiritscout.blackboard.convergence import ConvergenceEngine
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.config.settings import AssetIntelligenceConfig
        from spiritscout.tools.spec import ToolResult

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"Answer": []}'

        external_requests = []
        async def handle(request):
            external_requests.append(str(request.url))
            return httpx.Response(200, stream=Body())

        calls = []
        async def tool_call(name, args, **kwargs):
            calls.append((name, args))
            return ToolResult(tool=name, ok=True, invocation_id='inv-http',
                              parsed={'status_code': 302, 'headers': {'location': '/login'},
                                      'final_url': args['url'],
                                      'redirect_not_followed': True,
                                      'fingerprint': {}})

        original = httpx.AsyncClient
        def client(**options):
            return original(**options, transport=httpx.MockTransport(handle))

        target = 'https://example.com/app/login'
        with tempfile.TemporaryDirectory() as root:
            bb = Blackboard(Path(root) / 'bb.db')
            self.addCleanup(bb.close)
            from unittest.mock import AsyncMock

            infra = SimpleNamespace(
                cfg=SimpleNamespace(workspace=SimpleNamespace(root=root),
                                    asset_intelligence=AssetIntelligenceConfig()),
                convergence=ConvergenceEngine(bb), blackboard=bb,
                authorization=SimpleNamespace(scope=Scope(allowed_urls=[target])),
                builtin_ctx={'limiter': SimpleNamespace(acquire=AsyncMock())},
                tools=SimpleNamespace(call=tool_call),
            )
            with patch('spiritscout.agents.discovery.tool_environment',
                       AsyncMock(return_value={})), \
                    patch('spiritscout.agents.discovery.httpx.AsyncClient',
                          side_effect=client):
                result = await discover(infra, target)

            self.assertEqual(calls, [
                ('http_probe', {'url': target, 'max_redirects': 0}),
            ])
            self.assertEqual(external_requests, [])
            self.assertEqual(result['published'], [target])
            self.assertTrue(result['observations']['http']['ok'])


class ScopedAssetTests(unittest.TestCase):
    def test_domains_urls_and_resolved_ips(self):
        exact = Scope(allowed_domains=['example.com'])
        wildcard = Scope(allowed_domains=['*.example.com'])
        self.assertFalse(_within('other.example.com', exact))
        self.assertTrue(_within('other.example.com', wildcard))
        self.assertFalse(_within('example.com.evil.test', wildcard))
        self.assertFalse(_within('203.0.113.1', wildcard))
        self.assertFalse(_within('other.exam\nple.com', wildcard))
        path = Scope(allowed_urls=['https://example.com/app'])
        for value in ['https://example.com/application', 'http://example.com/app',
                      'https://example.com/app/../private', 'https://example.com/app/%252e%252e/private']:
            self.assertFalse(_within(value, path), value)
        self.assertTrue(_within('https://example.com/app/login', path))

    def test_publication_requires_both_scopes(self):
        convergence = Mock()
        publisher = ScopedAssetPublisher(convergence, Scope(allowed_domains=['*.example.com']),
                                         Scope(allowed_domains=['a.example.com']))
        self.assertIsNone(publisher.publish(Asset(asset_id='b', asset_type='domain', value='b.example.com')))
        convergence.merge_asset.assert_not_called()
        self.assertIsNone(publisher.publish(Asset(asset_id='invalid', asset_type='domain', value='a.example.com/path')))
        convergence.merge_asset.assert_not_called()
        publisher.publish(Asset(asset_id='a', asset_type='domain', value='a.example.com'))
        convergence.merge_asset.assert_called_once()
