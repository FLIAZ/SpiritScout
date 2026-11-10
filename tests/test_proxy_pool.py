"""代理池公开契约：加密、固定快照、乐观锁和导入边界。"""
import asyncio
import base64
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.resources.proxy_pool import (
    ProxyPool,
    SubscriptionContentError,
    SubscriptionPayload,
    fetch_subscription,
    parse_nodes,
    parse_subscription,
    parse_subscription_usage,
)
from spiritscout.resources.store import ResourceStore
from spiritscout.web.proxies import mount_proxies

CONFIG = 'proxies:\n- {name: fixture, type: http, server: proxy.example, port: 8080, password: fixture-secret}\n'


class ProxyPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ResourceStore(self.temp.name)
        self.pool = ProxyPool(self.store)

    def test_encrypted_public_view_and_snapshot(self):
        result = self.pool.import_content(CONFIG, 0)
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertNotIn(b'fixture-secret', self.store.path.read_bytes())
        node = result['nodes'][0]['id']
        result = self.pool.save_plan('固定出口', [node], result['version'])
        snapshot = self.pool.snapshot(result['plans'][0]['id'])
        self.pool.import_content(CONFIG.replace('fixture-secret', 'new-secret'), result['version'])
        self.assertEqual(snapshot['nodes'][0]['password'], 'fixture-secret')
        snapshot['nodes'][0]['password'] = 'changed'
        self.assertEqual(self.pool.snapshot(result['plans'][0]['id'])['nodes'][0]['password'], 'new-secret')
        self.assertEqual(len(self.pool.view()['nodes']), 1)
        with self.assertRaises(SpiritScoutError):
            self.pool.import_content(CONFIG, 0)
        with self.assertRaises(ValueError):
            self.pool.delete('nodes', node, self.pool.view()['version'])

    def test_multiple_plans_are_kept_in_public_view(self):
        result = self.pool.import_content(CONFIG, 0)
        node = result['nodes'][0]['id']
        result = self.pool.save_plan('出口一', [node], result['version'])
        result = self.pool.save_plan('出口二', [node], result['version'])
        self.assertEqual([plan['name'] for plan in result['plans']], ['出口一', '出口二'])
        self.assertEqual(len({plan['id'] for plan in result['plans']}), 2)

    def test_unsafe_configuration_and_base64_list(self):
        for suffix in ('tun: {}', 'proxy-providers: {}', 'script: {}', 'external-controller: localhost:9090'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                parse_nodes(CONFIG + suffix)
        with self.assertRaises(ValueError):
            parse_nodes(CONFIG.replace('password: fixture-secret', 'client-cert: /etc/passwd'))
        encoded = base64.b64encode(b'socks5://user:password@proxy.example:1080#fixture').decode()
        self.assertEqual(parse_nodes(encoded)[0]['password'], 'password')

    def test_common_subscription_uri_transports_and_legacy_ss(self):
        for uri, protocol, network in [
            ('vless://fixture-uuid@proxy.example:443?type=ws&security=tls&sni=front.example&path=%2Fsocket#fixture', 'vless', 'ws'),
            ('trojan://fixture-password@proxy.example:443?type=grpc&serviceName=fixture&security=tls', 'trojan', 'grpc')]:
            with self.subTest(protocol=protocol):
                node = parse_nodes(uri)[0]
                self.assertEqual(node['type'], protocol)
                self.assertEqual(node['network'], network)
                self.assertIs(node['tls'], True)
        legacy = base64.b64encode(b'aes-256-gcm:fixture-password@proxy.example:443').decode()
        self.assertEqual(parse_nodes('ss://' + legacy + '#fixture')[0]['password'], 'fixture-password')
        with self.assertRaises(ValueError):
            parse_nodes('vless://id@proxy.example:443?type=tcp&type=ws')

    def test_modern_protocols_and_optional_subscription_fields(self):
        for protocol, uri in [
            ('anytls', 'anytls://password@proxy.example:443?sni=proxy.example'),
            ('hysteria2', 'hy2://password@proxy.example:443?obfs=salamander&obfs-password=fixture'),
            ('tuic', 'tuic://uuid:password@proxy.example:443?congestion_control=bbr')]:
            with self.subTest(protocol=protocol):
                node = parse_nodes(uri)[0]
                self.assertEqual(node['type'], protocol)
                self.assertEqual(node['password'], 'password')
        result = self.pool.import_content(CONFIG.replace('password: fixture-secret', 'flow: null, skip-cert-verify: true'), 0)
        self.assertEqual(result['nodes'][0]['tls_verification'], 'disabled')
        self.assertNotIn('flow', self.pool._read()[0]['nodes'][result['nodes'][0]['id']])

    def test_subscription_extracts_only_inline_nodes(self):
        content = CONFIG + 'external-controller: 127.0.0.1:9090\nrule-anchor: &rule {type: http}\nrule-providers: {example: *rule}\ntun: {enable: true}\n'
        self.assertEqual(parse_subscription(content), parse_nodes(CONFIG))
        with self.assertRaises(SubscriptionContentError):
            parse_subscription('proxy-providers: {remote: {url: https://example.test}}')
        with self.assertRaises(SubscriptionContentError):
            parse_subscription('proxies: [&node {name: fixture, type: http, server: proxy.example, port: 8080}, *node]')

    def test_old_catalog_notice_diagnosis_and_unselectable_plan(self):
        from spiritscout.resources.proxy_pool import _node_id

        node = {'name': '客户端不支持或版本太旧', 'type': 'ss', 'server': '127.0.0.1', 'port': 999}
        key = _node_id(node, 'sub-old')
        self.store.save('proxy_pool', 'pool', {'nodes': {key: node}, 'plans': {},
                        'subscriptions': {'sub-old': {'name': '旧订阅', 'url': 'https://example.test/private'}}}, 0)
        result = self.pool.view()
        self.assertFalse(result['nodes'][0]['selectable'])
        self.assertEqual(result['nodes'][0]['source_id'], 'sub-old')
        self.assertEqual(result['subscriptions'][0]['node_count'], 0)
        self.assertTrue(result['subscriptions'][0]['diagnostic'])
        with self.assertRaises(SubscriptionContentError):
            self.pool.save_plan('bad', [key], result['version'])
        ordinary = self.pool.import_content(CONFIG.replace('proxy.example', 'localhost'), result['version'])
        self.assertTrue(next(n for n in ordinary['nodes'] if n['name'] == 'fixture')['selectable'])

    def test_api_redacts_invalid_input(self):
        app = FastAPI()
        mount_proxies(app, self.store)
        with TestClient(app) as client:
            response = client.post('/api/proxies/import', json={'version': 0, 'content': 'password: api-fixture-secret'})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('api-fixture-secret', response.text)
            response = client.post('/api/proxies/import', json={'version': 0, 'content': CONFIG})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('fixture-secret', response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_headers_negotiate_mihomo_without_leaking_url(self):
        import httpx

        seen = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield CONFIG.encode()

        def respond(request):
            seen.append(request)
            return httpx.Response(200, headers={
                'x-amz-meta-subscription-userinfo': 'upload=1024; download=2048; total=8192; expire=1893456000',
            }, stream=Stream())
        with patch('spiritscout.resources.proxy_pool.PublicSubscriptionTransport', return_value=httpx.MockTransport(respond)):
            response = await fetch_subscription('https://subscription.example/?token=private')
        self.assertEqual(response, CONFIG)
        self.assertEqual(response.usage, {'upload': 1024, 'download': 2048, 'total': 8192,
                                          'expire': 1893456000, 'used': 3072, 'remaining': 5120})
        self.assertTrue(seen[0].headers['user-agent'].startswith('mihomo SpiritScout/'))
        self.assertIn('application/yaml', seen[0].headers['accept'])

    def test_subscription_usage_header_is_optional_and_clamps_remaining(self):
        self.assertIsNone(parse_subscription_usage({}))
        self.assertEqual(parse_subscription_usage({'subscription-userinfo':
            'upload=40; download=70; total=100; expire=0'})['remaining'], 0)

    async def test_refresh_replaces_owned_nodes_but_preserves_plans_and_invalid_response(self):
        with tempfile.TemporaryDirectory() as root:
            pool = ProxyPool(ResourceStore(root))
            first_payload = SubscriptionPayload(CONFIG, {'upload': 10, 'download': 20, 'total': 100,
                'expire': 1893456000, 'used': 30, 'remaining': 70})
            with patch('spiritscout.resources.proxy_pool.fetch_subscription', new=AsyncMock(return_value=first_payload)) as fetch:
                first = await pool.subscribe('fixture', 'https://example.test/?token=private', 0)
                sub = first['subscriptions'][0]['id']
                node = first['nodes'][0]['id']
                self.assertEqual(first['subscriptions'][0]['usage']['remaining'], 70)
                saved = pool.save_plan('fixed', [node], first['version'])
                fetch.return_value = SubscriptionPayload(CONFIG.replace('name: fixture', 'name: replacement'),
                    {'upload': 20, 'download': 30, 'total': 100, 'expire': 1893456000,
                     'used': 50, 'remaining': 50})
                updated = await pool.refresh(sub, saved['version'])
                self.assertEqual(updated['subscriptions'][0]['node_count'], 1)
                self.assertEqual(updated['subscriptions'][0]['usage']['remaining'], 50)
                self.assertEqual(next(n for n in updated['nodes'] if n['id'] == node)['source_id'], 'legacy')
                self.assertEqual(pool.snapshot(updated['plans'][0]['id'])['nodes'][0]['name'], 'fixture')
                fetch.return_value = CONFIG.replace('fixture,', '客户端不支持或版本太旧,').replace('proxy.example', '127.0.0.1')
                with self.assertRaises(SubscriptionContentError):
                    await pool.refresh(sub, updated['version'])
                self.assertEqual(pool.view(), updated)

    async def test_subscription_secret_and_refresh(self):
        with tempfile.TemporaryDirectory() as root:
            pool = ProxyPool(ResourceStore(root))
            with patch('spiritscout.resources.proxy_pool.fetch_subscription', new=AsyncMock(return_value=CONFIG)):
                result = await pool.subscribe('fixture', 'https://subscription.example/?token=private-fixture', 0)
                self.assertNotIn('private-fixture', json.dumps(result))
                refreshed = await pool.refresh(result['subscriptions'][0]['id'], result['version'])
                self.assertEqual(len(refreshed['nodes']), 1)

    async def test_private_destination_rejected_before_http(self):
        import httpx

        from spiritscout.resources.proxy_pool import PublicSubscriptionTransport

        transport = PublicSubscriptionTransport()
        try:
            with patch('asyncio.BaseEventLoop.getaddrinfo', new=AsyncMock(return_value=[(2, 1, 6, '', ('127.0.0.1', 443))])):
                with self.assertRaises(ValueError):
                    await transport.handle_async_request(httpx.Request('GET', 'https://localhost/'))
        finally:
            await transport.aclose()


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_proxy_samples_and_download_units(self):
        from spiritscout.traffic.benchmark import measure

        requested = []
        async def serve(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                target = request.split(b' ', 2)[1].decode()
                size = int(parse_qs(urlsplit(target).query)['bytes'][0])
                requested.append((target, size))
                await asyncio.sleep(.02)
                writer.write(f'HTTP/1.1 200 OK\r\nContent-Length: {size}\r\nConnection: close\r\n\r\n'.encode() + b'x' * size)
                await writer.drain()
            finally:
                writer.close()
        async with await asyncio.start_server(serve, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            with patch('spiritscout.traffic.benchmark.DOWNLOAD_BYTES', 131072):
                result = await measure(f'http://127.0.0.1:{port}', 'download', 'http://benchmark.invalid/__down')
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['sample_count'], 3)
        self.assertGreaterEqual(result['latency_ms'], 20)
        self.assertEqual(result['download_bytes'], 131072)
        self.assertTrue(result['download_complete'])
        self.assertAlmostEqual(result['speed_mb_s'] * result['download_seconds'], 131072 / 1_000_000, delta=.01)
        self.assertEqual([size for _, size in requested], [1, 1, 1, 131072])
        self.assertTrue(all(target.startswith('http://benchmark.invalid/') for target, _ in requested))

    async def test_unreachable_proxy_never_returns_fake_zero_measurement(self):
        from spiritscout.traffic.benchmark import measure

        server = await asyncio.start_server(lambda *_: None, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        result = await measure(f'http://127.0.0.1:{port}', 'download', 'http://benchmark.invalid/')
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['latency_ms'])
        self.assertIsNone(result['speed_mb_s'])
        self.assertEqual(result['sample_count'], 0)

    async def test_job_persistence_configuration_invalidation_and_cancel(self):
        from spiritscout.traffic.benchmark import ProxyBenchmarks

        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(root)
            pool = ProxyPool(store)
            view = pool.import_content(CONFIG, 0)
            key = view['nodes'][0]['id']
            benchmarks = ProxyBenchmarks(store)
            cleaned = asyncio.Event()
            @asynccontextmanager
            async def proxy(_):
                try:
                    yield 'http://fixture.invalid:80'
                finally:
                    cleaned.set()
            measurement = {'status':'available', 'latency_ms': 125.5, 'speed_mb_s': 2.5, 'tested_at': 1234}
            with patch('spiritscout.traffic.benchmark.executable', return_value='/fixture'), \
                 patch('spiritscout.traffic.benchmark.node_proxy', proxy), \
                 patch('spiritscout.traffic.benchmark.measure', new=AsyncMock(return_value=measurement)):
                job = benchmarks.start([key], 'download', view['version'])
                self.assertNotIn('fixture-secret', json.dumps(job))
                with self.assertRaises(SpiritScoutError):
                    benchmarks.start([key], 'latency', view['version'])
                await benchmarks.task
                self.assertTrue(cleaned.is_set())
                self.assertEqual(benchmarks.view()['job']['status'], 'completed')
                self.assertEqual(pool.view()['nodes'][0]['benchmark']['speed_mb_s'], 2.5)
                self.assertEqual(pool.view()['version'], view['version'])
                changed = pool.import_content(CONFIG.replace('fixture-secret', 'changed-secret'), view['version'])
                self.assertIsNone(changed['nodes'][0]['benchmark'])
                self.assertEqual(benchmarks.view()['job']['results'][key]['status'], 'stale')
            started = asyncio.Event()
            async def slow(*_):
                started.set()
                await asyncio.Future()
            cleaned.clear()
            with patch('spiritscout.traffic.benchmark.executable', return_value='/fixture'), \
                 patch('spiritscout.traffic.benchmark.node_proxy', proxy), \
                 patch('spiritscout.traffic.benchmark.measure', side_effect=slow):
                benchmarks.start([key], 'latency', changed['version'])
                await started.wait()
                await benchmarks.close()
                self.assertTrue(cleaned.is_set())
                self.assertEqual(benchmarks.view()['job']['status'], 'cancelled')
                benchmarks.start([key], 'latency', changed['version'])
                await benchmarks.close()
                self.assertEqual(benchmarks.view()['job']['status'], 'cancelled')

    async def test_all_nodes_start_concurrently_and_separate_jobs_keep_results(self):
        from spiritscout.traffic.benchmark import ProxyBenchmarks

        config = 'proxies:\n' + '\n'.join(
            f'- {{name: node-{index}, type: http, server: proxy{index}.example, port: 8080}}'
            for index in range(3)) + '\n'
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(root)
            pool = ProxyPool(store)
            view = pool.import_content(config, 0)
            keys = [node['id'] for node in view['nodes']]
            benchmarks = ProxyBenchmarks(store)
            started, release = set(), asyncio.Event()

            @asynccontextmanager
            async def proxy(node):
                yield f"http://{node['name']}.invalid:80"

            async def measure(proxy_url, _mode):
                started.add(proxy_url)
                if len(started) == len(keys):
                    release.set()
                await asyncio.wait_for(release.wait(), 1)
                index = int(proxy_url.split('node-', 1)[1].split('.', 1)[0])
                return {'status': 'available', 'latency_ms': 100 + index,
                        'speed_mb_s': None, 'tested_at': 1000 + index}

            with patch('spiritscout.traffic.benchmark.executable', return_value='/fixture'), \
                 patch('spiritscout.traffic.benchmark.node_proxy', proxy), \
                 patch('spiritscout.traffic.benchmark.measure', side_effect=measure):
                benchmarks.start(keys, 'latency', view['version'])
                await asyncio.wait_for(release.wait(), 1)
                self.assertEqual(len(started), 3)
                await benchmarks.task

                first_result = pool.view()['nodes'][0]['benchmark']
                benchmarks.start([keys[1]], 'latency', view['version'])
                await benchmarks.task
                saved = {node['id']: node['benchmark'] for node in pool.view()['nodes']}
                self.assertEqual(saved[keys[0]], first_result)
                self.assertEqual(saved[keys[1]]['latency_ms'], 101)

    async def test_latency_results_finish_per_node_and_completed_node_can_restart(self):
        from spiritscout.traffic.benchmark import ProxyBenchmarks

        config = ('proxies:\n'
                  '- {name: fast, type: http, server: fast.example, port: 8080}\n'
                  '- {name: slow, type: http, server: slow.example, port: 8080}\n')
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(root)
            pool = ProxyPool(store)
            view = pool.import_content(config, 0)
            keys = {node['name']: node['id'] for node in view['nodes']}
            benchmarks = ProxyBenchmarks(store)
            slow_started, release_slow = asyncio.Event(), asyncio.Event()
            fast_runs = 0

            @asynccontextmanager
            async def proxy(node):
                yield f"http://{node['name']}.invalid:80"

            async def measure(proxy_url, _mode):
                nonlocal fast_runs
                if 'slow.invalid' in proxy_url:
                    slow_started.set()
                    await release_slow.wait()
                    return {'status': 'available', 'latency_ms': 200,
                            'speed_mb_s': None, 'tested_at': 2000}
                fast_runs += 1
                return {'status': 'available', 'latency_ms': 100 + fast_runs,
                        'speed_mb_s': None, 'tested_at': 1000 + fast_runs}

            async def wait_for_result(key, latency):
                for _ in range(100):
                    result = benchmarks.view()['job']['results'][key]
                    if result.get('latency_ms') == latency:
                        return result
                    await asyncio.sleep(.01)
                self.fail(f'节点 {key} 未及时返回 {latency}ms 结果')

            with patch('spiritscout.traffic.benchmark.executable', return_value='/fixture'), \
                 patch('spiritscout.traffic.benchmark.node_proxy', proxy), \
                 patch('spiritscout.traffic.benchmark.measure', side_effect=measure):
                benchmarks.start([keys['fast'], keys['slow']], 'latency', view['version'])
                await slow_started.wait()
                await wait_for_result(keys['fast'], 101)
                self.assertFalse(benchmarks.task.done())
                self.assertEqual(benchmarks.view()['job']['results'][keys['slow']]['status'], 'testing')
                saved = {node['id']: node['benchmark'] for node in pool.view()['nodes']}
                self.assertEqual(saved[keys['fast']]['latency_ms'], 101)
                self.assertIsNone(saved[keys['slow']])

                benchmarks.start([keys['fast']], 'latency', view['version'])
                await wait_for_result(keys['fast'], 102)
                self.assertFalse(benchmarks.task.done())
                self.assertEqual(benchmarks.view()['job']['results'][keys['slow']]['status'], 'testing')

                release_slow.set()
                await benchmarks.task
                self.assertEqual(benchmarks.view()['job']['status'], 'completed')
