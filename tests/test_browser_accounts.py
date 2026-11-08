"""账号的加密、隔离、并发撤销与敏感答复边界。真实 MCP 验证另用本地 fixture。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import (
    McpServerConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import Authorization, RunMode, Scope
from spiritscout.gateway import world_state
from spiritscout.policy.questions import QuestionBroker
from spiritscout.resources.browser_accounts import BrowserAccounts, filter_state
from spiritscout.resources.store import ResourceStore
from spiritscout.runtime import _build_infra
from spiritscout.tools.browser_account_access import register_account_access
from spiritscout.tools.browser_account_auth import fill
from spiritscout.tools.browser_accounts import account_spec, session_specs
from spiritscout.tools.mcp_client import ChromeMcpClient, build_client
from spiritscout.web.browser_accounts import mount_browser_accounts


def cookie(value: str, domain: str = 'example.test', expires: float = -1) -> dict:
    return {'name': 'sid', 'value': value, 'domain': domain, 'path': '/', 'expires': expires}


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.accounts = BrowserAccounts(ResourceStore(self.root / 'resources'))
        self.base = McpServerConfig(name='chrome', command='python', args=['-m', 'spiritscout.tools.chrome_mcp'])
        self.data = {'name': '测试账号', 'origins': ['https://example.test/login'],
                     'username': 'fixture-user', 'password': 'fixture-secret', 'mfa_method': 'email',
                     'mfa_destination': 'fixture@example.test'}
        self.accounts.save('a', self.data, 0)

    def test_credentials_encrypted_and_public_metadata_only(self) -> None:
        view = self.accounts.capture('a', {'cookies': [cookie('private-cookie')], 'origins': []}, 1)
        payload = json.dumps(view)
        for secret in ('fixture-secret', 'fixture-user', 'fixture@example.test', 'private-cookie'):
            self.assertNotIn(secret, payload)
            for path in (self.root / 'resources').glob('*.db*'):
                self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertTrue(view['credentials_configured'])
        self.assertFalse(view['login_verified'])
        self.assertEqual(self.accounts.snapshot('a')[1]['cookies'][0]['value'], 'private-cookie')

    def test_domain_expiry_and_storage_filter(self) -> None:
        raw = {'cookies': [cookie('ok'), cookie('foreign', 'other.test'),
                           cookie('expired', expires=time.time() - 1), cookie('public', '.co.uk')],
               'origins': [{'origin': 'https://other.test', 'localStorage': [{'name': 'token', 'value': 'foreign'}]}]}
        state = filter_state(raw, ['https://example.test', 'https://example.co.uk'])
        self.assertEqual([c['value'] for c in state['cookies']], ['ok'])
        self.assertEqual(state['origins'], [])
        partitioned = {**cookie('partitioned'), 'secure': True, 'partitionKey': 'https://example.test'}
        kept = filter_state({'cookies': [partitioned]}, ['https://example.test'])
        self.assertEqual(kept['cookies'][0]['partitionKey'], 'https://example.test')
        foreign = {**partitioned, 'partitionKey': 'https://other.test'}
        self.assertEqual(filter_state({'cookies': [foreign]}, ['https://example.test'])['cookies'], [])
        with patch('spiritscout.resources.browser_accounts.time.time', return_value=time.time() + 40 * 86400):
            self.accounts.capture('a', raw, 1)
        # 显式过期而非把缓存存在等同于已登录。
        with patch('spiritscout.resources.browser_accounts.time.time', return_value=time.time() + 90 * 86400):
            self.assertEqual(self.accounts.snapshot('a')[0]['cache_status'], 'expired')
            self.assertFalse(self.accounts.snapshot('a')[1]['cookies'])

    def test_revoke_and_concurrent_capture_cannot_restore_old_identity(self) -> None:
        self.accounts.capture('a', {'cookies': [cookie('a')], 'origins': []}, 1)
        self.accounts.clear('a', 2)
        with self.assertRaises(SpiritScoutError):
            self.accounts.capture('a', {'cookies': [cookie('old')], 'origins': []}, 2)
        self.assertEqual(self.accounts.snapshot('a')[0]['cache_status'], 'empty')
        self.accounts.save('a', {'password': 'new'}, 3)
        self.accounts.clear('a', 4, delete=True)
        with self.assertRaises(KeyError):
            self.accounts.snapshot('a')
        with self.assertRaises(SpiritScoutError):
            self.accounts.save('a', self.data, 0)

    def test_isolated_spec_and_binding(self) -> None:
        self.accounts.save('b', {**self.data, 'name': '另一账号'}, 0)
        self.accounts.bind('session-a', ['a', 'b', 'a'])
        specs = session_specs([self.base], self.accounts.store, 'session-a')
        self.assertEqual(len(specs), 2)
        self.assertNotEqual(specs[0].name, specs[1].name)
        self.assertEqual([s.env['SPIRITSCOUT_BROWSER_ACCOUNT'] for s in specs], ['a', 'b'])
        self.assertTrue(all(s.env['SPIRITSCOUT_CHROME_ISOLATED'] == '1' for s in specs))
        self.assertEqual(self.accounts.selected('another-session'), [])

    def access(self, *, interactive: bool = True):
        functions, catalog = {}, {}
        registry = SimpleNamespace(register_builtin=lambda name, fn, spec: functions.update({name: fn}))
        questions = QuestionBroker(interactive=interactive)
        activate = AsyncMock(return_value=['approved-browser-tool'])
        with patch.object(self.accounts, 'list', wraps=self.accounts.list) as listing:
            register_account_access(registry, catalog, self.base, self.accounts, questions, activate)
            listing.assert_not_called()
        return functions.get('mcp__chrome__account_request'), questions, activate

    async def test_account_access_requires_explicit_choice_and_never_loads_on_denial(self) -> None:
        for answer in ({'choice': 0}, {'text': '仅本次使用此账号'},
                       {'choice': 1, 'text': '先不要使用'}, None, {'choice': 1}):
            with self.subTest(answer=answer):
                request, questions, activate = self.access()
                task = asyncio.create_task(request('https://EXAMPLE.test:443/account',
                    '当前任务要求核对本人账号的个人设置，公开页面无法查看', '个人设置页明确要求本人登录'))
                await asyncio.sleep(0)
                pending = questions.view()
                self.assertEqual(len(pending), 1)
                activate.assert_not_awaited()
                for value in ('fixture-secret', 'fixture-user', 'fixture@example.test'):
                    self.assertNotIn(value, json.dumps(pending))
                if answer is None:
                    questions.cancel_all()
                else:
                    questions.answer(pending[0]['question_id'], **answer)
                result = await task
                granted = answer == {'choice': 1}
                self.assertEqual(result.parsed['status'], 'approved' if granted else 'declined')
                if granted:
                    activate.assert_awaited_once_with('a', 'https://example.test', 1)
                else:
                    activate.assert_not_awaited()
                self.assertEqual(questions.view(), [])

    async def test_account_access_matches_exact_platform_and_requires_justification(self) -> None:
        request, questions, activate = self.access()
        for url in ('https://example.test.evil.invalid', 'https://sub.example.test',
                    'http://example.test', 'https://example.test:8443'):
            self.assertEqual((await request(url, '需要本人账号', '任务明确要求')).parsed['status'], 'unavailable')
        for reason, evidence in (('', '依据'), ('需要身份', ''), ('x' * 601, '依据')):
            self.assertFalse((await request('https://example.test', reason, evidence)).ok)
        self.assertEqual(questions.view(), [])
        activate.assert_not_awaited()
        self.assertIsNone(self.access(interactive=False)[0])
        self.accounts.save('b', {**self.data, 'name': '第二个身份'}, 0)
        result = await request('https://example.test', '需要本人账号', '任务明确要求')
        self.assertEqual(result.parsed['status'], 'choose_account')
        self.assertEqual([a['id'] for a in result.parsed['accounts']], ['a', 'b'])
        self.assertNotIn('username_hint', result.stdout)
        self.assertEqual(questions.view(), [])

    async def test_account_changed_or_wait_cancelled_does_not_activate(self) -> None:
        request, questions, activate = self.access()
        task = asyncio.create_task(request('https://example.test', '需要本人身份', '个人设置需要登录'))
        await asyncio.sleep(0)
        self.accounts.save('a', {'password': 'changed-secret'}, 1)
        questions.answer(questions.view()[0]['question_id'], choice=1)
        self.assertEqual((await task).parsed['status'], 'changed')
        activate.assert_not_awaited()
        task = asyncio.create_task(request('https://example.test', '需要本人身份', '个人设置需要登录'))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(questions.view(), [])
        activate.assert_not_awaited()

    async def test_approved_browser_limits_state_and_preserves_other_sites_on_save(self) -> None:
        self.accounts.save('a', {'origins': ['https://example.test', 'https://other.test']}, 1)
        self.accounts.capture('a', {'cookies': [cookie('approved'), cookie('unrelated', 'other.test')]}, 2)
        spec = account_spec(self.base, self.accounts, 'a', approved_origin='https://example.test', expected_version=3)
        client = build_client(spec)
        self.addAsyncCleanup(client.close, save=False)
        self.assertEqual(client.identity['origins'], ['https://example.test'])
        state_path = Path(client._params.env['SPIRITSCOUT_CHROME_STORAGE_STATE'])
        self.assertEqual([c['value'] for c in json.loads(state_path.read_text())['cookies']], ['approved'])
        client._used = True
        state = {'cookies': [cookie('new-approved')], 'origins': []}
        with patch.object(ChromeMcpClient, 'call', new=AsyncMock(return_value={
                'is_error': False, 'text': '### Result\n' + json.dumps(state)})):
            await client.capture()
        self.assertEqual(client.identity['origins'], ['https://example.test'])
        self.assertEqual({c['value'] for c in self.accounts.snapshot('a')[1]['cookies']}, {'new-approved', 'unrelated'})
        self.assertEqual([c['value'] for c in json.loads(state_path.read_text())['cookies']], ['new-approved'])
        with self.assertRaises(ValueError):
            build_client(spec)

    async def test_runtime_keeps_anonymous_browser_until_explicit_account_approval(self) -> None:
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root / 'session')),
                           audit_log_path=str(self.root / 'audit.jsonl'), mcp_servers=[self.base])
        cfg.resources.root = str(self.accounts.store.root)
        cfg.blackboard.sqlite_path = str(self.root / 'bb.db')
        cfg.knowledge.sqlite_path = str(self.root / 'kb.db')
        auth = Authorization(auth_id='fixture-account', mode=RunMode.SRC, human_confirmed=True,
                             scope=Scope(allowed_domains=['example.test']))
        questions, built = QuestionBroker(), []
        tools = [{'name': name, 'inputSchema': {'type': 'object', 'properties': {}}}
                 for name in ('browser_navigate', 'browser_run_code')]

        def build(spec):
            built.append(spec)
            client = SimpleNamespace(start=AsyncMock(return_value=tools), close=AsyncMock())
            if account_id := spec.env.get('SPIRITSCOUT_BROWSER_ACCOUNT'):
                client.account_id = account_id
                client.identity = self.accounts.public(self.accounts.row(account_id))
            return client

        with patch('spiritscout.tools.mcp_client.build_client', side_effect=build):
            infra = await _build_infra(cfg, [ModelEntry(model='fixture', provider='mock')], auth,
                                       question_broker=questions)
            try:
                self.assertEqual(len(built), 1)
                self.assertEqual(built[0].env['SPIRITSCOUT_CHROME_ISOLATED'], '1')
                self.assertNotIn('SPIRITSCOUT_BROWSER_ACCOUNT', built[0].env)
                name = 'mcp__chrome__account_request'
                self.assertIn(name, infra.mcp_tools)
                task = asyncio.create_task(infra.tools.call(name, {'url': 'https://example.test',
                    'reason': '核对本人个人设置，匿名页面不包含此信息', 'evidence': '任务明确要求检查个人设置'},
                    **infra.builtin_ctx))
                for _ in range(50):
                    if questions.view():
                        break
                    await asyncio.sleep(0)
                self.assertEqual(len(built), 1, '未同意前不创建账号浏览器')
                questions.answer(questions.view()[0]['question_id'], choice=1)
                result = await task
                self.assertTrue(result.ok, result.error)
                self.assertEqual(len(built), 2)
                self.assertEqual(built[1].env['SPIRITSCOUT_BROWSER_ACCOUNT_ORIGIN'], 'https://example.test')
                self.assertTrue(any(name.endswith('__account_login') for name in result.parsed['tools']))
                self.assertTrue(all(name in infra.tools.names() for name in result.parsed['tools']))
                self.assertEqual(self.accounts.selected('session'), [], '本次确认不保存成跨运行绑定')
            finally:
                await infra.close()

    async def test_secret_fill_and_following_snapshots_do_not_echo_credentials(self) -> None:
        client = build_client(account_spec(self.base, self.accounts, 'a'))
        client.protect(['a'])
        self.assertEqual(client.redact('page value: "a"'), 'page value: "[账号私密内容]"')
        with patch.object(ChromeMcpClient, 'call', new=AsyncMock(return_value={
                'text': 'fixture-secret fixture-user OTP-123456', 'is_error': False})) as call:
            await fill(client, [('#code', 'OTP-123456')])
            self.assertIn('OTP-123456', call.call_args.args[1]['code'])
            output = await client.call('browser_snapshot')
            for secret in ('fixture-secret', 'fixture-user', 'OTP-123456'):
                self.assertNotIn(secret, output['text'])
            self.accounts.clear('a', 1)
            with self.assertRaises(ValueError):
                await client.call('browser_snapshot')
        await client.close(save=False)

    async def test_secret_answer_never_enters_world_state_or_audit(self) -> None:
        audit = AuditLogger(self.root / 'audit.jsonl')
        bb = Blackboard(self.root / 'bb.db')
        self.addCleanup(bb.close)
        broker = QuestionBroker(audit)
        broker.bind_context(bb)
        pending = asyncio.create_task(broker.ask('browser', '输入验证码', ['取消验证'], secret=True))
        await asyncio.sleep(0)
        question = broker.view()[0]
        self.assertTrue(question['secret'])
        broker.answer(question['question_id'], text='OTP-789012')
        self.assertEqual((await pending)['answer'], 'OTP-789012')
        self.assertNotIn('OTP-789012', json.dumps(world_state.snapshot(blackboard=bb)))
        self.assertNotIn('OTP-789012', (self.root / 'audit.jsonl').read_text())

    async def test_api_invalid_types_and_version_conflicts_never_echo_secrets(self) -> None:
        app = FastAPI()
        browsers = mount_browser_accounts(app, SystemConfig(), self.accounts.store)
        self.addAsyncCleanup(browsers.aclose)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for change in ({'mfa_method': []}, {'storage_state': {'cookies': [cookie('private') | {'sameSite': []}]}},
                           {'clear_credentials': True, 'password': 'fixture-secret'}):
                response = await client.put('/api/browser-accounts/a', json={'data': change, 'expected_version': 1})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn('fixture-secret', response.text)
            response = await client.put('/api/browser-accounts/a', json={'data': self.data, 'expected_version': 0})
            self.assertEqual(response.status_code, 409)
            response = await client.get('/api/browser-accounts')
            self.assertNotIn('fixture-secret', response.text)

    async def test_same_name_secret_challenges_never_share_answers(self) -> None:
        broker = QuestionBroker()
        tasks = [asyncio.create_task(broker.ask('browser', '同名账号的验证码', ['取消验证'], secret=True)) for _ in range(2)]
        await asyncio.sleep(0)
        questions = broker.view()
        self.assertEqual(len(questions), 2)
        broker.answer(questions[0]['question_id'], text='first-only')
        await asyncio.sleep(0)
        self.assertEqual(len(broker.view()), 1)
        broker.cancel_all()
        results = await asyncio.gather(*tasks)
        self.assertEqual(sum(r.get('answer') == 'first-only' for r in results), 1)

    async def test_browser_close_saves_and_refreshes_context_restore_state(self) -> None:
        client = build_client(account_spec(self.base, self.accounts, 'a'))
        state = {'cookies': [cookie('fresh-session')], 'origins': []}
        calls = []

        async def reply(tool, arguments=None, **kwargs):
            calls.append(tool)
            return {'is_error': False, 'text': '### Result\n' + json.dumps(state) if tool == client.code_tool else 'closed'}

        client._used = True
        path = Path(client._params.env['SPIRITSCOUT_CHROME_STORAGE_STATE'])
        self.assertEqual(json.loads(path.read_text())['cookies'], [])
        with patch.object(ChromeMcpClient, 'call', side_effect=reply):
            await client.call('browser_close')
        self.assertEqual(calls, [client.code_tool, 'browser_close'])
        self.assertEqual(json.loads(path.read_text())['cookies'][0]['value'], 'fresh-session')
        self.assertEqual(self.accounts.snapshot('a')[1]['cookies'][0]['value'], 'fresh-session')
        await client.close(save=False)
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
