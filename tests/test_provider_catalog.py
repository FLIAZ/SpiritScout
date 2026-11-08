"""端点目录、额度语义和查询凭据生命周期；不访问真实账户。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.config.settings import ProviderEntry, SystemConfig, WorkspaceConfig
from spiritscout.web import modelcfg, vault
from spiritscout.web.model_routes import mount_model_routes
from spiritscout.web.provider_api import PROVIDER_PRESETS
from spiritscout.web.provider_quota import endpoint_for, parse_quota, query_kind


class ProviderCatalogTests(unittest.TestCase):
    def test_presets_validate_and_group_variants_without_fake_oauth(self):
        keys = [p['key'] for p in PROVIDER_PRESETS]
        self.assertEqual(len(keys), len(set(keys)))
        for preset in PROVIDER_PRESETS:
            entry = ProviderEntry.model_validate({**{k: v for k, v in preset.items() if k in ProviderEntry.model_fields}, 'name': preset['key']})
            self.assertEqual(entry.auth_method, 'api_key')
            self.assertTrue(preset['family_name'])
        grouped = {}
        for p in PROVIDER_PRESETS:
            grouped.setdefault(p['family'], []).append(p)
        for family in ('ark', 'zhipu', 'moonshot', 'minimax', 'qwen', 'qwencloud', 'qianfan', 'mimo'):
            self.assertGreaterEqual(len(grouped[family]), 2)
        self.assertFalse(any(p.get('billing_scheme', '').endswith('_plan') for p in grouped['deepseek']))

    def test_exact_host_region_and_plan_selection(self):
        def p(url, **kw):
            return ProviderEntry(name='fixture', base_url=url, **kw)
        for kind, cn, other in [
            ('glm_plan', 'https://open.bigmodel.cn/api/coding/paas/v4', 'https://api.z.ai/api/coding/paas/v4'),
            ('kimi_plan', 'https://api.kimi.com/coding/v1', 'https://api.kimi.ai/coding/v1'),
            ('minimax_plan', 'https://api.minimax.cn/v1', 'https://api.minimax.io/v1'),
            ('siliconflow_balance', 'https://api.siliconflow.cn/v1', 'https://api.siliconflow.com/v1'),
        ]:
            first, second = p(cn, quota_query=kind), p(other, quota_query=kind)
            self.assertEqual(query_kind(first), kind)
            self.assertEqual(query_kind(second), kind)
            self.assertNotEqual(endpoint_for(first, kind), endpoint_for(second, kind))
            self.assertEqual(query_kind(p(cn.replace('https://', 'https://evil.test/'), quota_query=kind)), 'unsupported')
        for url in ('https://api.minimax.cn/v1', 'https://ark.cn-beijing.volces.com/api/v3', 'https://opencode.ai/zen/v1'):
            self.assertEqual(query_kind(p(url)), 'unsupported')
        self.assertEqual(query_kind(p('https://api.deepseek.com:broken')), 'unsupported')

    def test_balances_preserve_zero_currency_and_reject_missing(self):
        for kind, body, expected in [
            ('siliconflow_balance', {'data': {'totalBalance': '0', 'balance': '99'}}, 0),
            ('stepfun_balance', {'balance': '-1.2'}, -1.2),
            ('moonshot_balance', {'data': {'available_balance': '12.5'}}, 12.5),
            ('glm_balance', {'success': True, 'data': {'availableBalance': 0, 'balance': 99}}, 0),
            ('openrouter_balance', {'data': {'total_credits': 100, 'total_usage': 42}}, 58),
        ]:
            result = parse_quota(kind, body, currency='USD')
            self.assertEqual(result['balances'][0], {'remaining': expected, 'currency': 'USD'})
            with self.assertRaises(ValueError):
                parse_quota(kind, {'data': {}})

    def test_plan_units_reset_and_no_invented_week_limit(self):
        row = {'model_name': 'general', 'current_interval_remaining_percent': 80,
               'current_weekly_status': 3, 'current_weekly_remaining_percent': 100, 'end_time': 1800000000000}
        result = parse_quota('minimax_plan', {'model_remains': [{'model_name': 'video'}, row]})
        self.assertEqual(len(result['windows']), 1)
        self.assertEqual(result['windows'][0]['remaining_percent'], 80)
        self.assertIn('2027-', result['windows'][0]['resets_at'])
        row.update(current_weekly_status=2, current_weekly_remaining_percent=0)
        self.assertEqual(parse_quota('minimax_plan', {'model_remains': [row]})['windows'][1]['remaining_percent'], 0)
        self.assertEqual(parse_quota('opencode_plan', {'usage': {'rolling': {'percent': 0, 'resetsAt': '2030-01-01T00:00:00Z'}}})['windows'][0]['resets_at'], None)
        with self.assertRaises(ValueError):
            parse_quota('ark_plan', {'Result': {'AFPFiveHour': {'Quota': 100}}})
        self.assertEqual(parse_quota('ark_plan', {'Result': {'QuotaUsage': [{'Level': 'weekly', 'Percent': 80, 'ResetTime': -1}]}})['windows'][0],
                         {'label': '周额度', 'remaining_percent': 20, 'resets_at': None})

    def test_query_secrets_validate_encrypt_rename_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(vault, '_root', Path(tmp)), patch.object(vault, '_fernet', None):
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp), models_config_path=str(Path(tmp) / 'models.yaml'))
            modelcfg.write_config(cfg.models_config_path, [], [])
            app = FastAPI()
            mount_model_routes(app, cfg)
            with TestClient(app) as client:
                p = {'name': 'ark', 'base_url': 'https://ark.cn-beijing.volces.com/api/plan/v3', 'billing_scheme': 'other_token_plan'}
                secret = {'access_key_id': 'fixture-ak', 'secret_access_key': 'fixture-private-sk'}
                self.assertEqual(client.post('/api/providers', json={**p, 'quota_credentials': {'access_key_id': 'partial'}}).status_code, 422)
                self.assertEqual(modelcfg.read_providers(cfg.models_config_path), [])
                result = client.post('/api/providers', json={**p, 'quota_credentials': secret})
                self.assertEqual(result.status_code, 201, result.text)
                self.assertTrue(result.json()['providers'][0]['quota_credentials_configured'])
                self.assertNotIn('fixture-private-sk', result.text)
                self.assertNotIn(b'fixture-private-sk', (Path(tmp) / 'vault.db').read_bytes())
                self.assertNotIn('quota_credentials', Path(cfg.models_config_path).read_text())
                result = client.put('/api/providers/ark', json={**p, 'name': 'renamed'})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(vault.get_quota_credentials('renamed'), secret)
                self.assertEqual(vault.get_quota_credentials('ark'), {})
                self.assertNotIn('fixture-private-sk', json.dumps(client.get('/api/models').json()))
                self.assertEqual(client.delete('/api/providers/renamed').status_code, 200)
                self.assertEqual(vault.get_quota_credentials('renamed'), {})
