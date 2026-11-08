"""供应商认证/额度：本地真实 HTTP 协议、密钥库与 Web API，绝不请求付费模型。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.config.settings import ProviderEntry, SystemConfig, WorkspaceConfig
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.web import modelcfg, provider_auth, vault
from spiritscout.web.model_routes import mount_model_routes
from spiritscout.web.provider_quota import QuotaService, parse_quota, query_kind


def kimi(name='kimi'):
    return ProviderEntry(name=name, base_url='https://api.kimi.com/coding/v1',
                         billing_scheme='kimi_token_plan', auth_method='kimi_oauth')


class AccountHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(vault, '_root', self.root).start()
        patch.object(vault, '_fernet', None).start()
        self.responses = deque()
        self.requests = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.delay = False

        async def handle(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                lines = head.decode().split('\r\n')
                headers = dict(line.lower().split(':', 1) for line in lines[1:] if ':' in line)
                body = (await reader.readexactly(int(headers.get('content-length', '0')))).decode()
                self.requests.append({'target': lines[0], 'headers': headers, 'form': parse_qs(body)})
                if self.delay:
                    self.entered.set()
                    await self.release.wait()
                status, response = self.responses.popleft()
                data = json.dumps(response).encode()
                writer.write(f'HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: {len(data)}\r\n\r\n'.encode() + data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        self.server = await asyncio.start_server(handle, '127.0.0.1', 0)
        self.addAsyncCleanup(self.server.wait_closed)
        self.addCleanup(self.server.close)
        self.base = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
        patch.object(provider_auth, 'AUTH_ROOT', self.base + '/oauth').start()

    async def test_device_flow_pending_slowdown_rotation_and_logout(self):
        provider = kimi()
        self.responses.extend([
            (200, {'device_code': 'fixture-private-device', 'user_code': 'ABCD',
                   'verification_uri_complete': 'https://auth.kimi.com/activate?user_code=ABCD', 'expires_in': 600, 'interval': 5}),
            (400, {'error': 'authorization_pending'}), (400, {'error': 'slow_down'}),
            (200, {'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh', 'expires_in': 3600}),
            (200, {'access_token': 'fixture-rotated', 'refresh_token': 'fixture-refresh-rotated', 'expires_in': 3600}),
        ])
        flow = await provider_auth.start(provider)
        self.assertNotIn('device_code', flow)
        self.assertNotIn('fixture-private-device', json.dumps(flow))
        pending = await provider_auth.poll(provider, flow['flow_id'])
        self.assertEqual(pending['status'], 'pending')
        await provider_auth.poll(provider, flow['flow_id'])
        self.assertEqual(len(self.requests), 2)  # 轮询间隔在服务端强制执行。
        saved = vault.get_oauth(provider.name)
        saved['pending']['next_poll'] = 0
        vault.set_oauth(provider.name, saved)
        slower = await provider_auth.poll(provider, flow['flow_id'])
        self.assertEqual(slower['interval'], 10)
        saved = vault.get_oauth(provider.name)
        saved['pending']['next_poll'] = 0
        vault.set_oauth(provider.name, saved)
        success = await provider_auth.poll(provider, flow['flow_id'])
        self.assertEqual(success['status'], 'authenticated')
        self.assertNotIn('fixture-access', json.dumps(success))
        self.assertNotIn(b'fixture-access', (self.root / 'vault.db').read_bytes())
        self.assertEqual(await provider_auth.access_token(provider), 'fixture-access')
        saved = vault.get_oauth(provider.name)
        saved['token']['expires_at'] = time.time() - 1
        vault.set_oauth(provider.name, saved)
        tokens = await asyncio.gather(*(provider_auth.access_token(provider) for _ in range(3)))
        self.assertEqual(tokens, ['fixture-rotated'] * 3)
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(self.requests[-1]['form']['refresh_token'], ['fixture-refresh'])
        self.assertIn('spiritscout', self.requests[-1]['headers']['user-agent'])
        await provider_auth.logout(provider.name)
        with self.assertRaises(SpiritScoutError):
            await provider_auth.access_token(provider)

    async def test_ark_uses_independent_signed_credentials_and_single_read(self):
        provider = ProviderEntry(name='ark', base_url='https://ark.cn-beijing.volces.com/api/plan/v3',
                                 billing_scheme='other_token_plan')
        vault.set_key('ark', 'inference-only-fixture')
        service = QuotaService()
        with patch.dict('spiritscout.web.provider_quota.ENDPOINTS', {'ark_plan': self.base + '/'}):
            missing = await service.get(provider)
            self.assertEqual(missing['status'], 'auth_required')
            self.assertEqual(self.requests, [])
            vault.set_quota_credentials('ark', {'access_key_id': 'quota-ak', 'secret_access_key': 'quota-sk-private'})
            self.responses.append((200, {'Result': {'AFPFiveHour': {'Quota': '1000', 'Used': '250', 'ResetTime': 1800000000000}}}))
            result = await service.get(provider)
            self.assertEqual(result['windows'][0]['remaining_percent'], 75)
            self.assertIn('Action=GetAFPUsage', self.requests[0]['target'])
            auth = self.requests[0]['headers']['authorization']
            self.assertIn('hmac-sha256 credential=quota-ak/', auth)
            self.assertNotIn('quota-sk-private', str(self.requests))
            self.assertNotIn('inference-only-fixture', str(self.requests))
            self.assertEqual((await service.get(provider))['fetched_at'], result['fetched_at'])
            self.assertEqual(len(self.requests), 1)

    async def test_bad_oauth_expiry_is_a_controlled_error(self):
        for bad in ('not-a-number', {}, float('inf')):
            with self.assertRaises(SpiritScoutError):
                provider_auth._token({'expires_in': bad, 'access_token': 'a', 'refresh_token': 'r'})

    async def test_glm_auto_queries_balance_only_when_plan_data_is_absent(self):
        provider = ProviderEntry(name='glm', base_url='https://open.bigmodel.cn/api/anthropic')
        vault.set_key('glm', 'fixture-glm-key')
        self.responses.extend([(200, {'success': True, 'data': {'limits': []}}),
                               (200, {'success': True, 'data': {'availableBalance': '12.5'}})])
        with patch.dict('spiritscout.web.provider_quota.ENDPOINTS',
                        {'glm_plan': self.base + '/plan', 'glm_balance': self.base + '/balance'}):
            result = await QuotaService().get(provider)
            self.assertEqual(result['balances'][0]['remaining'], 12.5)
            self.assertEqual(result['source'], self.base + '/balance')
            self.assertNotIn('bearer', self.requests[-1]['headers']['authorization'])
            self.responses.append((401, {}))
            self.assertEqual((await QuotaService().get(provider))['status'], 'auth_required')
            self.assertEqual(len(self.requests), 3)

    async def test_refresh_failure_backoff_and_rejection_never_falls_back_to_api_key(self):
        provider = kimi()
        vault.set_key(provider.name, 'fixture-paygo-key')
        vault.set_oauth(provider.name, {'device_id': 'device', 'token': {
            'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh', 'expires_at': time.time() - 1}})
        self.responses.extend([(503, {'error': 'unavailable'}), (400, {'error': 'invalid_grant'})])
        for _ in range(2):
            with self.assertRaises(SpiritScoutError):
                await provider_auth.access_token(provider)
        self.assertEqual(len(self.requests), 1)
        saved = vault.get_oauth(provider.name)
        self.assertEqual(saved['token']['refresh_token'], 'fixture-refresh')
        saved['token']['retry_after'] = 0
        vault.set_oauth(provider.name, saved)
        with self.assertRaisesRegex(SpiritScoutError, '重新登录'):
            await provider_auth.access_token(provider)
        self.assertTrue(vault.get_oauth(provider.name)['token']['rejected'])

    async def test_logout_waits_for_inflight_refresh_and_clears_rotated_token(self):
        provider = kimi()
        vault.set_oauth(provider.name, {'device_id': 'device', 'token': {
            'access_token': 'fixture-old', 'refresh_token': 'refresh-old', 'expires_at': 1}})
        self.responses.append((200, {'access_token': 'fixture-new', 'refresh_token': 'refresh-new', 'expires_in': 3600}))
        self.delay = True
        refresh = asyncio.create_task(provider_auth.access_token(provider))
        await asyncio.wait_for(self.entered.wait(), 2)
        logout = asyncio.create_task(provider_auth.logout(provider.name))
        await asyncio.sleep(.02)
        self.release.set()
        await asyncio.gather(refresh, logout)
        self.assertEqual(vault.get_oauth(provider.name), {})

    async def test_quota_auth_cache_stale_unknown_and_no_key_leak(self):
        from spiritscout.web import provider_quota
        patch.dict(provider_quota.ENDPOINTS, {'deepseek_balance': self.base + '/balance'}).start()
        provider = ProviderEntry(name='deepseek', base_url='https://api.deepseek.com')
        vault.set_key(provider.name, 'fixture-deepseek')
        self.responses.extend([(200, {'is_available': True, 'balance_infos': [{'currency': 'CNY', 'total_balance': '42.50', 'granted_balance': '2.5', 'topped_up_balance': '40'}]}),
                               (503, {}), (200, {'balance_infos': []}), (401, {'error': 'fixture-deepseek'})])
        service = QuotaService()
        first = await service.get(provider)
        self.assertEqual(first['balances'][0]['remaining'], 42.5)
        self.assertEqual(self.requests[0]['headers']['authorization'].strip(), 'bearer fixture-deepseek')
        self.assertNotIn('fixture-deepseek', json.dumps(first))
        self.assertEqual(await service.get(provider), first)
        self.assertEqual(len(self.requests), 1)
        for expected in ('unavailable', 'unavailable', 'auth_required'):
            service.cache[provider.name]['attempted_at'] = 0
            result = await service.get(provider)
            self.assertEqual(result['status'], expected)
            self.assertTrue(result['stale'])
            self.assertEqual(result['balances'], first['balances'])
        vault.set_key(provider.name, 'new-account-key')
        self.responses.append((200, {}))
        result = await service.get(provider)
        self.assertNotIn('balances', result)  # 换账号不能沿用前一个账号余额。


class QuotaParsingTests(unittest.TestCase):
    def test_glm_window_unit_not_reset_order_and_unknown_is_not_zero(self):
        q = parse_quota('glm_plan', {'data': {'limits': [
            {'type': 'TOKENS_LIMIT', 'unit': 6, 'number': 7, 'percentage': 40, 'nextResetTime': 1800000000000},
            {'type': 'CREDIT_LIMIT', 'unit': 3, 'number': 5, 'percentage': 60, 'nextResetTime': 1900000000000},
            {'type': 'TOKENS_LIMIT', 'percentage': None}]}})
        self.assertEqual([row['label'] for row in q['windows']], ['周额度', '5 小时额度'])
        self.assertEqual([row['remaining_percent'] for row in q['windows']], [60, 40])
        with self.assertRaises(ValueError):
            parse_quota('glm_plan', {'data': {'limits': [{'type': 'TOKENS_LIMIT'}]}})

    def test_kimi_partial_fields_do_not_fabricate_remaining(self):
        q = parse_quota('kimi_plan', {'limits': [{'detail': {'limit': '100', 'remaining': '25', 'resetTime': '2030-01-01T00:00:00Z'}}],
                                     'usage': {'limit': '500'}})
        self.assertEqual(len(q['windows']), 1)
        self.assertEqual(q['windows'][0]['remaining_percent'], 25)
        with self.assertRaises(ValueError):
            parse_quota('kimi_plan', {'usage': {'limit': 0, 'remaining': 0}})

    def test_host_matching_is_exact_and_explicit_profile_cannot_send_proxy_key(self):
        for host in ['https://api.deepseek.com.evil.test', 'https://evil.test/api.deepseek.com', 'http://api.deepseek.com']:
            self.assertEqual(query_kind(ProviderEntry(name='p', base_url=host, quota_query='deepseek_balance')), 'unsupported')
        self.assertEqual(query_kind(ProviderEntry(name='p', base_url='https://api.deepseek.com')), 'deepseek_balance')
        with self.assertRaises(ValueError):
            kimi().model_copy(update={}).__class__(**{**kimi().model_dump(), 'base_url': 'https://proxy.test'})


class ProviderAccountApiTests(unittest.TestCase):
    def test_public_fields_rename_delete_and_secret_isolation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(vault, '_root', Path(tmp)), patch.object(vault, '_fernet', None):
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp), models_config_path=str(root / 'models.yaml'), agent_models_path=str(root / 'agents.yaml'))
            modelcfg.write_config(cfg.models_config_path, [kimi().model_dump()], [{'model': 'kimi-model', 'provider_ref': 'kimi'}])
            vault.set_oauth('kimi', {'device_id': 'device', 'pending': {'flow_id': 'old'},
                'token': {'access_token': 'fixture-api-private', 'refresh_token': 'fixture-refresh-private', 'expires_at': time.time() + 3600}})
            app = FastAPI()
            mount_model_routes(app, cfg)
            with TestClient(app) as client:
                data = client.get('/api/providers').json()
                self.assertEqual(data['providers'][0]['key_source'], 'oauth')
                self.assertNotIn('fixture-api-private', json.dumps(data))
                payload = kimi('renamed').model_dump()
                result = client.put('/api/providers/kimi', json=payload)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(vault.get_oauth('kimi'), {})
                self.assertNotIn('pending', vault.get_oauth('renamed'))
                self.assertEqual(vault.get_oauth('renamed')['token']['access_token'], 'fixture-api-private')
                with patch('spiritscout.web.provider_auth.access_token', new=AsyncMock()) as access:
                    result = client.post('/api/providers/renamed/models', json={'base_url': 'https://evil.test'})
                self.assertEqual(result.status_code, 422)
                access.assert_not_called()
                self.assertEqual(client.delete('/api/providers/renamed').status_code, 422)
                self.assertTrue(vault.get_oauth('renamed'))
                modelcfg.write_config(cfg.models_config_path, modelcfg.read_providers(cfg.models_config_path), [])
                self.assertEqual(client.delete('/api/providers/renamed').status_code, 200)
                self.assertEqual(vault.get_oauth('renamed'), {})
