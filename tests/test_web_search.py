"""检索缓存的计费边界、并发取消、持久化和模型续裁链路。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from spiritscout.agents.recon.agent import ReconAgent
from spiritscout.config.settings import SearchConfig
from spiritscout.core.models import AgentType
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.web_search import (
    SEARCH_SCHEMA,
    SEARCH_SPEC,
    SearchService,
    web_search,
)


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'search.sqlite3'
        self.calls: list[dict] = []
        self.status = 200

        async def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(json.loads(request.content))
            return httpx.Response(self.status, json={'results': [
                {'title': '官方文档', 'url': 'https://docs.example.test/page',
                 'publishedDate': '2026-10-20T00:00:00Z', 'highlights': ['version 3.14 新特性']}]})
        self.handler = handler
        self.service = self.make_service()

    def make_service(self) -> SearchService:
        return SearchService(SearchConfig(enabled=True), self.path, api_key='test-secret',
                             transport=httpx.MockTransport(self.handler))

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_same_query_reuses_identical_body_across_services_and_reasons(self) -> None:
        first = await self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query='Python   3.14', reason='查询新版本',
                                          include_domains=['DOCS.example.test', 'example.test'])
        second = await self.make_service().search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query=' Python 3.14 ', reason='其他 Agent 的信息缺口',
                                                  include_domains=['example.test', 'docs.example.test'])
        self.assertTrue(first.ok)
        self.assertTrue(second.parsed['search_cache_hit'])
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('test-secret', first.stdout)
        self.assertNotIn('reason', self.calls[0])
        self.assertEqual(self.calls[0]['contents'], {'highlights': True})

    async def test_expired_and_explicit_refresh_fetch_again(self) -> None:
        await self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query='q', reason='查证')
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE search_cache SET created=0')
        expired = await self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query='q', reason='查证')
        self.assertFalse(expired.parsed['search_cache_hit'])
        fresh = await self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query='q', reason='公告已更新', refresh=True)
        self.assertFalse(fresh.parsed['search_cache_hit'])
        self.assertEqual(len(self.calls), 3)

    async def test_filters_are_not_mixed_and_are_sent_to_provider(self) -> None:
        for after in ('2026-10-01', '2026-10-20'):
            result = await self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query='q', reason='查最新公告', published_after=after)
            self.assertTrue(result.ok)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1]['startPublishedDate'], '2026-10-20T00:00:00.000Z')

    async def test_concurrent_calls_merge_and_cancelled_waiter_does_not_abort_others(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(request: httpx.Request) -> httpx.Response:
            entered.set()
            await release.wait()
            return await self.handler(request)
        self.service.transport = httpx.MockTransport(slow)
        first = asyncio.create_task(self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query='q', reason='第一个'))
        await entered.wait()
        others = [asyncio.create_task(self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query='q', reason='第二个')) for _ in range(5)]
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        results = await asyncio.gather(*others)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(r.ok and (r.parsed['search_coalesced'] or r.parsed['search_cache_hit'])
                            for r in results))
        cached = await self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query='q', reason='后续查询')
        self.assertTrue(cached.parsed['search_cache_hit'])

    async def test_errors_are_not_cached_or_automatically_retried_by_registry(self) -> None:
        registry = ToolRegistry(SimpleNamespace(), max_retries=5)
        registry.register_builtin('web_search', web_search, SEARCH_SPEC)
        self.status = 429
        result = await registry.call('web_search', {'query': 'q', 'reason': '查证', 'paid': True, 'blocker': '模拟明确阻塞，免费资料不足'}, search=self.service)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(self.calls), 1)
        self.status = 200
        result = await registry.call('web_search', {'query': 'q', 'reason': '查证', 'paid': True, 'blocker': '模拟明确阻塞，免费资料不足'}, search=self.service)
        self.assertTrue(result.ok)
        self.assertFalse(result.parsed['search_cache_hit'])
        self.assertEqual(len(self.calls), 2)

    async def test_parameter_errors_do_not_send_network_requests(self) -> None:
        for change in ({'query': ''}, {'num_results': True}, {'refresh': 'false'},
                       {'include_domains': ['https://docs.example.test']}, {'published_after': 'yesterday'},
                       {'reason': ''}):
            result = await self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",**{'query': 'q', 'reason': '查证', **change})
            self.assertFalse(result.ok)
        self.assertEqual(self.calls, [])

    async def test_unconfigured_tool_does_not_claim_a_search(self) -> None:
        result = await web_search(query='q', reason='查证')
        self.assertFalse(result.ok)
        self.assertIn('未启用', result.error)
        self.service.api_key = ''
        result = await web_search(query='q', reason='查证', paid=True, blocker='模拟明确阻塞', search=self.service)
        self.assertFalse(result.ok)
        self.assertIn('密钥', result.error)
        self.assertEqual(self.calls, [])

    async def test_bounded_results_remain_valid_json_and_have_no_fabricated_dates(self) -> None:
        async def large(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={'results': [
                {'title': '文档' * 200, 'url': 'https://docs.example.test/' + str(i),
                 'text': '很长的内容' * 2000} for i in range(20)]})
        self.service.transport = httpx.MockTransport(large)
        result = await self.service.search(paid=True, blocker="模拟明确阻塞，现有与免费资料不足",query='q', reason='查证', num_results=8)
        data = json.loads(result.stdout)
        self.assertTrue(result.ok)
        self.assertLessEqual(len(result.stdout), 5700)
        self.assertTrue(data['results'])
        self.assertTrue(all(row['published_at'] == '' for row in data['results']))

    async def test_json_agent_requests_search_then_resumes_with_same_prefix(self) -> None:
        agent = object.__new__(ReconAgent)
        agent.agent_id, agent.agent_type = 'recon-test', AgentType.RECON
        agent.enable_llm_planning = True
        agent._world_state = Mock(return_value={})
        agent._system_prompt = Mock(return_value='职责与规则')
        gateway = SimpleNamespace(chat=AsyncMock(side_effect=[
            SimpleNamespace(content=json.dumps({'web_search': {'query': 'Python 3.14', 'reason': '版本差异', 'paid': True, 'blocker': '模拟明确阻塞，免费资料不足'}}), finish_reason='stop'),
            SimpleNamespace(content=json.dumps({'paths': [], 'rationale': '已核对版本资料'}), finish_reason='stop')]))
        registry = ToolRegistry(SimpleNamespace())
        registry.register_builtin('web_search', web_search, SEARCH_SPEC)
        agent.ctx = SimpleNamespace(audit=Mock(), ring=None, blackboard=None, gateway=gateway,
                                    tools=registry, builtin_ctx={'search': self.service})
        result = await agent._llm_chat_json('path_planner', {'issue': '版本差异'}, _allow_ask=False)
        self.assertEqual(result['rationale'], '已核对版本资料')
        self.assertEqual(len(self.calls), 1)
        requests = gateway.chat.await_args_list
        original, followup = (r.args[0] for r in requests)
        self.assertEqual(followup[:len(original)], original)
        self.assertIn('search_observations', followup[-1]['content'])
        self.assertIn('官方文档', followup[-1]['content'])
        self.assertEqual(SEARCH_SCHEMA['function']['name'], 'web_search')


class FreeFirstSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = SearchService(SearchConfig(enabled=True), Path(self.tmp.name) / 'cache.db', api_key='fixture')
        self.body = json.dumps({'provider': 'free-fixture', 'results': [{'title': '资料', 'url': 'https://docs.test'}]})
        self.service._fetch = AsyncMock(return_value=self.body.replace('free-fixture', 'paid-fixture'))
        self.service._fetch_free = AsyncMock(return_value=self.body)
        self.service._fetch_public = AsyncMock(return_value=self.body.replace('free-fixture', 'public-fixture'))

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_default_is_free_even_with_paid_key(self) -> None:
        first = await self.service.search(query='q', reason='普通技术问题')
        second = await self.service.search(query='q', reason='复用缓存')
        self.assertTrue(first.ok)
        self.assertFalse(first.parsed['search_paid_request'])
        self.assertTrue(second.parsed['search_cache_hit'])
        self.service._fetch_free.assert_awaited_once()
        self.service._fetch.assert_not_awaited()

    async def test_free_quota_exhausted_uses_public_curl_provider_not_paid(self) -> None:
        self.service._fetch_free.side_effect = ValueError('free quota exhausted 429')
        result = await self.service.search(query='q', reason='普通问题')
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.stdout)['provider'], 'public-fixture')
        self.service._fetch_public.assert_awaited_once()
        self.service._fetch.assert_not_awaited()

    async def test_all_free_sources_fail_without_paid_fallback(self) -> None:
        self.service._fetch_free.side_effect = ValueError('quota')
        self.service._fetch_public.side_effect = ValueError('公开搜索失败')
        result = await self.service.search(query='q', reason='普通问题')
        self.assertFalse(result.ok)
        self.service._fetch.assert_not_awaited()

    async def test_paid_requires_explicit_valid_flag_and_blocker(self) -> None:
        missing = await self.service.search(query='q', reason='普通问题', paid=True)
        invalid = await self.service.search(query='q', reason='普通问题', paid='true', blocker='阻塞')
        self.assertFalse(missing.ok)
        self.assertFalse(invalid.ok)
        self.service._fetch.assert_not_awaited()
        allowed = await self.service.search(query='q', reason='无法推进验证', paid=True,
                                            blocker='已核对本地文档与免费来源，仍缺少影响后续验证的特定版本信息')
        self.assertTrue(allowed.ok)
        self.assertTrue(allowed.parsed['search_paid_request'])
        self.service._fetch.assert_awaited_once()

    async def test_valid_paid_cache_can_be_reused_without_paid_declaration(self) -> None:
        first = await self.service.search(query='q', reason='阻塞', paid=True, blocker='免费来源不足，关键版本信息阻塞验证')
        second = await self.service.search(query='q', reason='普通查询')
        self.assertTrue(second.ok)
        self.assertEqual(first.stdout, second.stdout)
        self.assertTrue(second.parsed['search_cache_hit'])
        self.assertFalse(second.parsed['search_paid_request'])
        self.service._fetch.assert_awaited_once()
        self.service._fetch_free.assert_not_awaited()

    async def test_explicit_public_source_skips_exa_and_never_sends_key(self) -> None:
        result = await self.service.search(query='"exact error" site:docs.test', reason='免费额度耗尽', source='public')
        self.assertTrue(result.ok)
        self.service._fetch_public.assert_awaited_once()
        self.service._fetch_free.assert_not_awaited()
        self.service._fetch.assert_not_awaited()
        self.assertNotIn('api_key', self.service._fetch_public.call_args.args[0])

    async def test_public_curl_arguments_are_not_executed_as_shell_code(self) -> None:
        rss = b'<rss><channel><item><title>doc</title><link>https://docs.test/p</link><description>body</description><pubDate>now</pubDate></item></channel></rss>'
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(rss, b'')))
        from unittest.mock import patch

        with patch('spiritscout.tools.web_search.shutil.which', return_value='/usr/bin/curl'), \
             patch('spiritscout.tools.web_search.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as launch:
            body = await SearchService._fetch_public(self.service, {'query': '"error"; touch /tmp/never',
                'num_results': 3, 'include_domains': ['docs.test'], 'published_after': ''})
        command = launch.call_args.args
        self.assertIn('q="error"; touch /tmp/never (site:docs.test)', command)
        self.assertEqual(command[0], '/usr/bin/curl')
        self.assertNotIn('fixture', ' '.join(command))
        self.assertEqual(json.loads(body)['results'][0]['published_at'], '')
        self.assertEqual(json.loads(body)['provider'], 'public-search-curl')


class SearchCleaningTests(unittest.TestCase):
    def test_html_scripts_navigation_repetition_and_tracking_are_removed(self) -> None:
        from spiritscout.tools.web_search import _format

        response = {'results': [
            {'title': '<b>Python</b>\x1b[31m 官方\x1b[0m',
             'url': 'https://DOCS.test/p?version=3.14&utm_source=search&gclid=tracker#section',
             'highlights': ['<nav>目录</nav><script>secretScript()</script><style>.ad{}</style>'
                            '<div class="cookie-banner"><div>接受 Cookie</div></div>'
                            '<p>新增 &lt;T&gt; 类型；x &lt; 2。</p><p>新增 &lt;T&gt; 类型；x &lt; 2。</p>'
                            '<footer>版权导航</footer>', '新增 &lt;T&gt; 类型；x &lt; 2。']},
            {'title': '重复页面', 'url': 'https://docs.test/p?version=3.14&utm_medium=other#section', 'text': '重复'},
            {'title': '坏地址', 'url': 'https://[invalid', 'text': '不要阻断其他结果'},
        ]}
        body = _format({'query': '版本变化', 'num_results': 5}, response, '2026-10-23T00:00:00Z')
        data = json.loads(body)
        self.assertEqual(len(data['results']), 1)
        row = data['results'][0]
        self.assertEqual(row['title'], 'Python 官方')
        self.assertEqual(row['url'], 'https://docs.test/p?version=3.14#section')
        self.assertEqual(row['snippet'], '新增 <T> 类型；x < 2。')
        for noise in ('secretScript', 'Cookie', '版权导航', 'utm_source', '<style>', '\x1b'):
            self.assertNotIn(noise, body)

    def test_real_parameters_and_technical_operators_are_not_damaged(self) -> None:
        from spiritscout.tools.web_search import _clean_search_text, _clean_search_url

        self.assertEqual(_clean_search_url('https://docs.test/p?q=a%20b&sig=A%2fb%2B&ref=chapter&utm_source=x'),
                         'https://docs.test/p?q=a%20b&sig=A%2fb%2B&ref=chapter')
        self.assertEqual(_clean_search_text('List<T> and x < 0, API v3.14'), 'List<T> and x < 0, API v3.14')

    def test_total_budget_remains_valid_json_including_public_notice(self) -> None:
        from spiritscout.tools.web_search import _format

        data = {'results': [{'title': 'title' * 100, 'url': 'https://docs.test/' + str(i) + '?' + 'x' * 700,
                             'text': '<p>' + '内容' * 3000 + '</p>'} for i in range(8)]}
        body = _format({'query': 'q', 'num_results': 8}, data, '2026-10-23', provider='public-search-curl')
        self.assertLessEqual(len(body), 4800)
        parsed = json.loads(body)
        self.assertIn('日期语法', parsed['notice'])
        self.assertTrue(parsed['results'])
        self.assertTrue(all(len(row['snippet']) <= 480 for row in parsed['results']))

    def test_old_paid_cache_is_cleaned_locally_without_new_request_or_changed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = SearchService(SearchConfig(enabled=True), Path(tmp) / 'cache.db', api_key='fixture')
            original = {'schema': 'web-search-v1', 'provider': 'exa', 'query': 'q',
                        'fetched_at': '2026-10-22T00:00:00Z', 'results': [{
                            'title': '<b>doc</b>', 'url': 'https://docs.test/p?utm_source=search',
                            'published_at': '2026-10-20', 'snippet': '<p>版本 3.14 的事实</p><style>noise</style>'}]}
            service._write('old-paid-key', json.dumps(original))
            first = service._read('old-paid-key')
            second = service._read('old-paid-key')
            self.assertEqual(first, second)
            data = json.loads(first)
            self.assertEqual(data['schema'], 'web-search-v2')
            self.assertEqual(data['fetched_at'], original['fetched_at'])
            self.assertEqual(data['results'][0]['published_at'], '2026-10-20')
            self.assertEqual(data['results'][0]['snippet'], '版本 3.14 的事实')
            self.assertEqual(data['results'][0]['url'], 'https://docs.test/p')
