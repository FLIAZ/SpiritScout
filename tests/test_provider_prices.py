from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from spiritscout.config import load_model_config
from spiritscout.config.settings import ModelEntry, ProviderEntry
from spiritscout.gateway.billing import estimate, quote_for
from spiritscout.gateway.costs import cost_summary
from spiritscout.gateway.official_prices import (
    PriceCatalog,
    glm_plan_fees,
    parse_prices,
)
from spiritscout.gateway.usage import UsageLedger
from spiritscout.web import modelcfg

GLM_TABLE = '''输入单价（元/百万 Tokens） 缓存命中（元/百万 Tokens）
| GLM-example | 1M | 8 | 28 | 限时免费 | 2 |
| GLM-tiered | 输入短 | 4 | 18 | 限时免费 | 1 |
| GLM-tiered | 输入长 | 6 | 22 | 限时免费 | 1.5 |'''


class PriceParserTests(unittest.TestCase):
    def test_tiers_currency_units_and_unknown_changes(self):
        quote = parse_prices('glm_api', GLM_TABLE)
        self.assertEqual(quote['currency'], 'CNY')
        self.assertEqual(len(quote['models']['glm-tiered']), 2)
        self.assertEqual(quote['models']['glm-example'][0]['cached_input'], 2)
        with self.assertRaises(ValueError):
            parse_prices('glm_api', GLM_TABLE.replace('元/百万', '美元/千'))
        with self.assertRaises(ValueError):
            parse_prices('kimi_api', '<table><tr><td>unknown layout</td></tr></table>')

    def test_kimi_literal_table_does_not_execute_remote_code(self):
        text = '缓存写入（5min） [_jsx(_components.a,{children:`kimi-test`}),`¥6.50 / MTok`,`¥27.00 / MTok`,`¥1.30 / MTok`]'
        quote = parse_prices('kimi_api', text)
        self.assertEqual(quote['models']['kimi-test'][0]['output'], 27)
        self.assertEqual(quote['models']['kimi-test'][0]['cached_input'], 1.3)

    def test_plan_versions_and_period_prices(self):
        old = 'const old="V2",items=[{type:"lite",unitKey:"month",renewAmount:1}]'
        new = 'const current="V3",items=[' + ','.join(
            f'{{type:"{tier}",unitKey:"{period}",renewAmount:{amount}}}'
            for tier in ['lite', 'pro', 'max'] for period, amount in [('month', 118), ('quarter', 283.2), ('year', 991.2)]) + ']'
        quote = glm_plan_fees(old + new)
        self.assertEqual(quote['version'], 'V3')
        self.assertEqual(quote['fees']['Lite']['year'], 991.2)
        with self.assertRaises(ValueError):
            glm_plan_fees(old)

    def test_provider_scheme_round_trip_and_no_deepseek_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'models.yaml'
            modelcfg.write_config(path, [{'name': 'glm', 'provider': 'openai_compatible', 'base_url': 'https://open.bigmodel.cn/api/paas/v4',
                'billing_scheme': 'glm_token_plan', 'billing_plan': 'Pro'}], [{'model': 'glm-example', 'provider_ref': 'glm'}])
            entry = load_model_config(path)[0]
            self.assertEqual((entry.billing_scheme, entry.billing_plan), ('glm_token_plan', 'Pro'))
        self.assertEqual(ProviderEntry(name='legacy').billing_scheme, 'manual')
        with self.assertRaises(ValueError):
            ProviderEntry(name='invalid', billing_scheme='deepseek_token_plan')


class PriceCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_fetch_persist_stale_backoff_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'prices.json'
            service = PriceCatalog(path)
            with patch('spiritscout.gateway.official_prices._fetch', new=AsyncMock(return_value=GLM_TABLE)) as fetch:
                results = await asyncio.gather(*(service.get('glm_api') for _ in range(5)))
            self.assertEqual(fetch.await_count, 1)
            self.assertTrue(all(q['status'] == 'fresh' for q in results))
            self.assertEqual(PriceCatalog(path).data['glm_api']['version'], results[0]['version'])
            service.data['glm_api']['fetched_at'] = 1
            service.data['glm_api']['attempted_at'] = 1
            with patch('spiritscout.gateway.official_prices._fetch', new=AsyncMock(side_effect=ValueError('layout changed'))) as fetch:
                result = await service.get('glm_api')
                again = await service.get('glm_api')
            self.assertEqual(result['status'], 'stale')
            self.assertEqual(result['models'], results[0]['models'])
            self.assertEqual(again, result)
            self.assertEqual(fetch.await_count, 1)
            service.data['glm_api']['attempted_at'] = 1
            with patch('spiritscout.gateway.official_prices._fetch', new=AsyncMock(return_value=GLM_TABLE.replace('| 8 |', '| 9 |'))):
                result = await service.get('glm_api')
            self.assertEqual(result['status'], 'fresh')
            self.assertNotEqual(result['version'], results[0]['version'])

    async def test_snapshot_isolated_and_proxy_does_not_receive_official_rates(self):
        service = PriceCatalog()
        service.data['glm_api'] = {**parse_prices('glm_api', GLM_TABLE), 'fetched_at': time.time(), 'version': 'one'}
        entry = ModelEntry(model='glm-example', billing_scheme='glm_api', base_url='https://open.bigmodel.cn/api/paas/v4')
        quote = await quote_for(entry, service)
        service.data['glm_api']['models']['glm-example'][0]['input'] = 999
        self.assertEqual(quote['rates'][0]['input'], 8)
        proxy = entry.model_copy(update={'base_url': 'https://proxy.example/api'})
        self.assertEqual((await quote_for(proxy, service))['status'], 'endpoint_mismatch')
        for scheme, endpoint in [
            ('glm_api', 'https://open.bigmodel.cn/api/coding/paas/v4'),
            ('glm_token_plan', 'https://open.bigmodel.cn/api/paas/v4'),
            ('kimi_api', 'https://api.kimi.com/coding/v1'),
            ('kimi_api', 'https://api.moonshot.ai/v1'),  # 不把国内人民币报价用于国际账户。
        ]:
            invalid = entry.model_copy(update={'base_url': endpoint, 'billing_scheme': scheme})
            self.assertEqual((await quote_for(invalid, service))['status'], 'endpoint_mismatch')
        other = entry.model_copy(update={'billing_scheme': 'other_token_plan'})
        quote = await quote_for(other, service)
        self.assertEqual(quote['kind'], 'subscription')
        self.assertEqual(quote['status'], 'subscription_price_unknown')

    async def test_ledger_preserves_quote_and_plan_is_not_free_api_cost(self):
        service = PriceCatalog()
        service.data['glm_api'] = {**parse_prices('glm_api', GLM_TABLE), 'fetched_at': time.time(), 'version': 'one'}
        entry = ModelEntry(model='glm-example', billing_scheme='glm_api', base_url='https://open.bigmodel.cn/api/paas/v4')
        quote = await quote_for(entry, service)
        raw = {'prompt_tokens': 1_000_000, 'completion_tokens': 100_000, 'cached_input_tokens': 900_000}
        result = estimate(entry, quote, time.time(), raw)
        self.assertAlmostEqual(result['estimated_cost_cny_min'], 5.4)
        self.assertIsNone(result['estimated_cost_usd'])
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(Path(tmp) / 'usage.db')
            ledger.append({'ts': time.time(), 'tokens': 1_100_000, 'usage_status': 'reported', **result})
            row = ledger.read()['call_series'][0]
            self.assertEqual(row['pricing_snapshot'], quote)
            self.assertTrue(cost_summary([row])['cny']['complete'])
        entry = entry.model_copy(update={'billing_scheme': 'kimi_token_plan'})
        plan = estimate(entry, {'kind': 'subscription', 'status': 'fresh', 'rates': []}, time.time(), raw)
        self.assertIsNone(plan['estimated_cost_usd'])
        self.assertNotIn('estimated_cost_cny_min', plan)
        self.assertFalse(cost_summary([plan])['cny']['complete'])
        self.assertIn('kimi_token_plan', cost_summary([plan])['subscriptions'])

    async def test_unknown_cache_or_stale_quote_not_priced(self):
        entry = ModelEntry(model='glm-example', billing_scheme='glm_api')
        quote = {'kind': 'api', 'status': 'fresh', 'rates': [{'input': 8, 'output': 28, 'cached_input': 2}]}
        result = estimate(entry, quote, time.time(), {'prompt_tokens': 100, 'completion_tokens': 10})
        self.assertEqual(result['pricing_status'], 'unknown_cache_usage')
        result = estimate(entry, {**quote, 'status': 'stale'}, time.time(), {'prompt_tokens': 100, 'completion_tokens': 10, 'cached_input_tokens': 0})
        self.assertNotIn('estimated_cost_cny_min', result)
