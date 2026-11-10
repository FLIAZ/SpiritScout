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

            infra = SimpleNamespace(cfg=SimpleNamespace(workspace=SimpleNamespace(root=root)),
                convergence=ConvergenceEngine(bb), blackboard=bb,
                authorization=SimpleNamespace(scope=Scope(allowed_domains=['*.example.com'])),
                builtin_ctx={'limiter': SimpleNamespace(acquire=AsyncMock())},
                tools=SimpleNamespace(call=AsyncMock(return_value=SimpleNamespace(ok=False, parsed={}, invocation_id='fixture', error='unreachable'))))
            with patch('spiritscout.agents.discovery.httpx.AsyncClient', side_effect=client):
                result = await discover(infra, '*.example.com')
            self.assertGreater(maximum, 1)
            self.assertEqual(result['published'], ['a.example.com'])
            requested = [call.args[1]['url'] for call in infra.tools.call.call_args_list]
            self.assertEqual(set(requested), {'http://a.example.com/', 'https://a.example.com/'})
            self.assertEqual([row['data']['value'] for row in bb.query(BlackboardLayer.ASSET)], ['a.example.com'])


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
