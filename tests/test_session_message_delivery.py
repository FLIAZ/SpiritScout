from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.ring import RingLedger
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry, SystemConfig, WorkspaceConfig
from spiritscout.core.models import AgentType, BlackboardLayer, RunMode
from spiritscout.ctf.flags import FlagManager
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.web.app import create_app
from spiritscout.web.conversation import ConversationResources
from spiritscout.web.sessions import (
    Session,
    SessionManager,
    _master_reply,
    _session_run_facts,
)


class SessionMessageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)),
                                audit_log_path=str(self.root / 'control-audit.jsonl'),
                                models_config_path=str(self.root / 'models.yaml'))
        self.app = create_app(self.cfg, [])
        self.manager = self.app.state.manager
        self.session = Session('sess-messages', 'ctf', 'fixture', self.manager.root / 'sess-messages', status='running')
        self.session.workspace.mkdir(parents=True, exist_ok=True)
        self.manager._sessions[self.session.session_id] = self.session
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    async def upload(self) -> str:
        response = await self.client.post('/api/uploads?name=evidence.txt', content=b'fixture evidence')
        self.assertEqual(response.status_code, 200)
        return response.json()['upload_id']

    async def test_folder_upload_preserves_paths_and_conflicting_roots(self) -> None:
        for suffix in ("", "-2"):
            response = await self.client.post('/api/uploads/folders', params={'name': '题目'})
            self.assertEqual(response.status_code, 200, response.text)
            uid = response.json()['upload_id']
            base = f'/api/uploads/folders/{uid}'
            for path, content, directory in [
                ('子目录', b'', True), ('子目录/payload.weird', b'\x00\xff', False),
                ('payload.weird', b'root', False), ('子目录/empty', b'', False),
                ('empty-dir', b'', True),
            ]:
                response = await self.client.put(base, params={'path': path, 'directory': directory}, content=content)
                self.assertEqual(response.status_code, 200, response.text)
            response = await self.client.post('/api/sessions/sess-messages/messages',
                json={'upload_ids': [uid], 'message_id': f'folder{suffix or "-1"}'})
            self.assertEqual(response.status_code, 200, response.text)
            root = self.session.workspace / 'attachments' / f'题目{suffix}'
            self.assertEqual((root / '子目录/payload.weird').read_bytes(), b'\x00\xff')
            self.assertEqual((root / 'payload.weird').read_bytes(), b'root')
            self.assertTrue((root / 'empty-dir').is_dir())
            self.assertEqual((root / '子目录/empty').stat().st_size, 0)
            self.assertTrue(any('子目录/payload.weird' in r['name']
                                for r in self.session.user_inbox[-1]['attachments']))

    async def test_initial_folder_reaches_session_before_queueing(self) -> None:
        from spiritscout.web.file_routes import resolve_staged_uploads

        response = await self.client.post('/api/uploads/folders', params={'name': 'challenge'})
        uid = response.json()['upload_id']
        response = await self.client.put(f'/api/uploads/folders/{uid}',
                                         params={'path': 'src/main.c'}, content=b'int main(){}')
        self.assertEqual(response.status_code, 200)
        plan = prepare_launch(LaunchRequest(mode=RunMode.CTF, mission='分析本地源码', has_uploads=True))
        with patch('spiritscout.config.load_model_config', return_value=[ModelEntry(model='fixture', provider='mock')]), \
             patch.object(self.manager, 'start'):
            created = await self.manager.create({'mode': 'ctf', 'no_llm': True}, plan,
                staged_files=resolve_staged_uploads(self.cfg, {'upload_ids': [uid]}))
        target = created.workspace / 'attachments/challenge/src/main.c'
        self.assertEqual(target.read_bytes(), b'int main(){}')
        self.assertIn(str(target.resolve()), plan.challenges[0]['attachments'])
        self.assertEqual(created.user_inbox[0]['attachments'][0]['name'], 'challenge/src/main.c')
        self.assertFalse((self.root / 'uploads-staging' / uid).exists())

    async def test_folder_upload_rejects_path_escape_and_oversize(self) -> None:
        response = await self.client.post('/api/uploads/folders', params={'name': 'folder'})
        uid = response.json()['upload_id']
        for path in ('../escape', '/absolute', 'nested/../../escape', 'a\\b', 'a//b'):
            response = await self.client.put(f'/api/uploads/folders/{uid}', params={'path': path}, content=b'x')
            self.assertEqual(response.status_code, 422, response.text)
        with patch('spiritscout.web.file_routes._MAX_UPLOAD_BYTES', 3):
            response = await self.client.put(f'/api/uploads/folders/{uid}', params={'path': 'x'}, content=b'1234')
            self.assertEqual(response.status_code, 413)

    async def test_message_attachment_envelope_is_atomic_and_retry_is_idempotent(self) -> None:
        uid = await self.upload()
        payload = {'content': '检查附件', 'upload_ids': [uid], 'message_id': 'message-fixture'}
        url = '/api/sessions/sess-messages/messages'
        for _ in range(2):
            response = await self.client.post(url, json=payload)
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.session.user_inbox), 1)
        item = self.session.user_inbox[0]
        self.assertEqual(item['text'], '检查附件')
        self.assertTrue(Path(item['attachments'][0]['path']).is_absolute())
        self.assertTrue(Path(item['attachments'][0]['path']).is_file())
        events = [json.loads(line) for line in self.session.audit_path.read_text().splitlines()]
        self.assertEqual([event['type'] for event in events], ['user.message', 'user.attachment'])
        self.assertEqual({event['detail']['message_id'] for event in events}, {'message-fixture'})
        conflict = await self.client.post(url, json={**payload, 'content': '另一条消息'})
        self.assertEqual(conflict.status_code, 409)

    async def test_attachment_only_survives_restart_and_acknowledgement(self) -> None:
        uid = await self.upload()
        response = await self.client.post('/api/sessions/sess-messages/messages',
            json={'upload_ids': [uid], 'message_id': 'message-file'})
        self.assertEqual(response.status_code, 200, response.text)
        restored = SessionManager(self.cfg, []).get(self.session.session_id)
        self.assertEqual(len(restored.user_inbox), 1)
        self.assertEqual(restored.dialogue_history[-1]['content'], '')
        self.assertTrue(restored.dialogue_history[-1]['attachments'])
        restored.acknowledge_user_feed(list(restored.user_inbox))
        self.manager._persist(restored)
        again = SessionManager(self.cfg, []).get(self.session.session_id)
        self.assertEqual(again.user_inbox, [])
        self.assertIn('message-file', again.handled_user_messages)

    async def test_attachment_only_reaches_master_without_text(self) -> None:
        uid = await self.upload()
        response = await self.client.post('/api/sessions/sess-messages/messages',
            json={'content': '', 'upload_ids': [uid], 'message_id': 'file-to-master'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.session.user_inbox), 1)
        item = self.session.user_inbox[0]
        path = item['attachments'][0]['path']
        from spiritscout.web.conversation import dialogue_messages

        self.assertIn(path, dialogue_messages(self.session.dialogue_history)[-1]['content'])
        master = object.__new__(MasterAgent)
        master.agent_id = 'master-fixture'
        master.ctx = SimpleNamespace(extra={'user_inbox': self.session.user_inbox, 'dialogue_history': []},
                                     audit=Mock(), ring=RingLedger(), blackboard=None)
        master._llm_user_chat = AsyncMock(return_value=('附件已接收', []))
        self.assertTrue(await master._respond_user_feed())
        args = master._llm_user_chat.await_args.args
        self.assertEqual(args[0], [])
        self.assertEqual(args[1], [path])
        self.assertEqual(Path(path).read_bytes(), b'fixture evidence')
        self.assertEqual(self.session.user_inbox, [])
        events = [json.loads(line) for line in self.session.audit_path.read_text().splitlines()]
        self.assertEqual([event['type'] for event in events], ['user.attachment'])
        self.assertEqual(events[0]['detail']['message_id'], 'file-to-master')

    async def test_direct_upload_returns_sandbox_readable_absolute_path(self) -> None:
        response = await self.client.post('/api/sessions/sess-messages/attachments?name=extra.txt', content=b'fixture')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Path(response.json()['path']).is_absolute())
        self.assertEqual(self.session.user_inbox[0]['attachments'][0]['path'], response.json()['path'])

    async def test_failed_chat_does_not_consume_or_claim_forwarding(self) -> None:
        master = object.__new__(MasterAgent)
        master.agent_id = 'master-fixture'
        inbox = [{'kind': 'message', 'text': '改方向', 'statement_id': 'message-1'}]
        audit = Mock()
        master.ctx = SimpleNamespace(extra={'user_inbox': inbox, 'dialogue_history': []}, audit=audit,
                                     ring=RingLedger(), blackboard=None)
        master._llm_user_chat = AsyncMock(return_value=None)
        self.assertFalse(await master._respond_user_feed())
        self.assertEqual(len(inbox), 1)
        text = audit.log.call_args.args[2]['text']
        self.assertIn('尚未确认转达', text)
        master._llm_user_chat = AsyncMock(return_value=('现在已排队', [
            {'agent': 'ctf_web', 'task_id': 't', 'hint': '执行新的检查'}]))
        self.assertTrue(await master._respond_user_feed())
        self.assertEqual(inbox, [])
        self.assertEqual(len(master.ctx.ring.steerings_for('ctf_web', task_id='t')), 1)

    async def test_chat_uses_current_task_addresses_after_model_returns(self) -> None:
        master = object.__new__(MasterAgent)
        master.agent_id = 'master-fixture'
        active = []
        master.ctx = SimpleNamespace(agents={AgentType.CTF_MISC: None}, pause_gate=None, audit=Mock(),
                                     gateway=SimpleNamespace(), blackboard=None, ring=RingLedger())
        master._mission = lambda: 'fixture'
        master._world_state = lambda: {}
        master._system_prompt = lambda: 'fixture'
        master._engagement_status = lambda: {'active_tasks': copy.deepcopy(active)}

        async def reply(*args, **kwargs):
            active.append({'agent': 'ctf_misc', 'task_id': 'new-task'})
            return SimpleNamespace(content=json.dumps({'reply': '处理附件',
                'steering': [{'agent': 'ctf_misc', 'hint': '读取新附件'}]}))

        with patch('spiritscout.agents.master.interaction.chat_decision', new=reply):
            result = await master._llm_user_chat([], ['evidence.txt'], [])
        self.assertEqual(result[1][0]['task_id'], 'new-task')

    async def test_terminal_reply_can_read_full_evidence_and_save_full_report(self) -> None:
        self.session.workspace.mkdir(parents=True, exist_ok=True)
        reports = self.session.workspace / 'reports'
        reports.mkdir()
        evidence = '原始证据' * 1000
        (reports / 'old.md').write_text(evidence)
        content = '完整新报告' * 2000
        seen = []
        responses = [
            SimpleNamespace(content='', tool_calls=[{'id': 'read', 'name': 'session_read',
                'arguments': {'source': 'file', 'path': 'reports/old.md', 'limit': 12000}}]),
            SimpleNamespace(content='', tool_calls=[{'id': 'save', 'name': 'save_report',
                'arguments': {'name': 'new.md', 'content': content}}]),
            SimpleNamespace(content='完整报告已保存', tool_calls=[]),
        ]

        async def chat(messages, **kwargs):
            seen.append(copy.deepcopy(messages))
            return responses.pop(0)

        gateway = SimpleNamespace(chat=chat)
        with patch('spiritscout.web.sessions._conversation_gateway', return_value=gateway):
            reply = await _master_reply(self.cfg, [ModelEntry(model='fixture', provider='mock')], '按新要求写报告', Mock(),
                kind='dialogue', session=self.session, history=[{'role': 'user', 'content': '首轮上下文' * 1000}])
        self.assertEqual(reply, '完整报告已保存')
        self.assertEqual((reports / 'new.md').read_text(), content)
        self.assertIn(evidence, seen[1][-1]['content'])
        self.assertIn('首轮上下文' * 1000, json.dumps(seen[0], ensure_ascii=False))

    async def test_followup_execution_keeps_original_challenge_and_new_goal(self) -> None:
        plan = prepare_launch(LaunchRequest(mode=RunMode.CTF, mission='原始题面'))
        self.session.plan = plan
        self.session.launch_request = {'mode': 'ctf', 'mission': '原始题面'}
        self.session.status = 'completed'
        self.session.remember_user_message('答案错误，请重新验证', message_id='message-recheck', enqueue=True)
        bb = Blackboard(self.session.bb_path)
        bb.write(BlackboardLayer.TARGET_AUTH, plan.authorization.auth_id, plan.authorization.model_dump(mode='json'))
        bb.close()

        goal = '重新确认本题答案；核对发现的 https://unapproved.invalid/ 线索'

        async def reply(*args, **kwargs):
            kwargs['execution_request'].update(mission=goal, reopen_challenges=['mission-1'])
            return '重新验证'

        result = SimpleNamespace(run_summary={'answers_complete': True}, verify_stats={}, flag_stats={}, report_path='')
        with patch('spiritscout.web.sessions._master_reply', new=reply), \
             patch('spiritscout.runtime.run_ctf_engagement', new=AsyncMock(return_value=result)) as runtime:
            await self.manager._run(self.session, self.cfg, plan, False, [ModelEntry(model='fixture', provider='mock')], True)
        self.assertEqual(runtime.call_args.kwargs['mission'], goal)
        self.assertNotIn('https://unapproved.invalid/', runtime.call_args.kwargs['authorization'].scope.allowed_urls)
        self.assertNotIn('unapproved.invalid', runtime.call_args.kwargs['authorization'].scope.allowed_domains)
        self.assertTrue(runtime.call_args.kwargs['followup_goal'])
        self.assertEqual(runtime.call_args.kwargs['reopen_challenges'], ['mission-1'])
        self.assertEqual(self.session.launch_request['mission'], '原始题面')
        self.assertEqual(self.session.user_inbox, [])

    async def test_successful_execution_request_dispatches_without_an_extra_model_turn(self) -> None:
        gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content='', tool_calls=[
            {'id': 'continue', 'name': 'request_execution', 'arguments': {'mission': '继续原题验证'}}])))
        request = {}
        with patch('spiritscout.web.sessions._conversation_gateway', return_value=gateway):
            reply = await _master_reply(self.cfg, [ModelEntry(model='fixture', provider='openai_compatible',
                local_runtime='llama_cpp')], '继续', Mock(),
                kind='dialogue', session=self.session, execution_request=request, execution_only=True)
        gateway.chat.assert_awaited_once()
        self.assertEqual(gateway.chat.call_args.kwargs['tool_choice'], 'required')
        self.assertEqual([tool['function']['name'] for tool in gateway.chat.call_args.kwargs['tools']],
                         ['request_execution'])
        self.assertEqual(request['mission'], '继续原题验证')
        self.assertIn('继续处理', reply)

    async def test_execution_handoff_keeps_ollama_and_mixed_backends_compatible(self) -> None:
        ollama = ModelEntry(model='fixture', provider='ollama')
        llama = ModelEntry(model='fixture-cpp', provider='openai_compatible', local_runtime='llama_cpp')
        for models in ([ollama], [llama, ollama]):
            with self.subTest(providers=[entry.provider for entry in models]):
                gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content='', tool_calls=[
                    {'id': 'continue', 'name': 'request_execution', 'arguments': {'mission': '继续原题验证'}}])))
                request = {}
                with patch('spiritscout.web.sessions._conversation_gateway', return_value=gateway):
                    await _master_reply(self.cfg, models, '继续', Mock(), kind='dialogue', session=self.session,
                                        execution_request=request, execution_only=True)
                gateway.chat.assert_awaited_once()
                self.assertEqual(gateway.chat.call_args.kwargs['tool_choice'], 'auto')
                self.assertEqual(request['mission'], '继续原题验证')

    async def test_rejected_execution_request_keeps_dialogue_open_for_correction(self) -> None:
        gateway = SimpleNamespace(chat=AsyncMock(side_effect=[
            SimpleNamespace(content='', tool_calls=[{'id': 'bad', 'name': 'request_execution',
                                                     'arguments': {'mission': ''}}]),
            SimpleNamespace(content='缺少执行目标', tool_calls=[])]))
        request = {}
        with patch('spiritscout.web.sessions._conversation_gateway', return_value=gateway):
            reply = await _master_reply(self.cfg, [ModelEntry(model='fixture', provider='mock')], '继续', Mock(),
                kind='dialogue', session=self.session, execution_request=request)
        self.assertEqual(gateway.chat.await_count, 2)
        self.assertFalse(request)
        self.assertEqual(reply, '缺少执行目标')

    def test_initial_attachment_metadata_enriches_one_original_message(self) -> None:
        initial = self.session.remember_user_message('原始任务', message_id='message-initial')
        attachments = [{'name': 'source.txt', 'path': str(self.root / 'source.txt')}]
        self.session.remember_user_message('原始任务', message_id='message-initial',
                                          attachments=attachments, enqueue=True)
        self.assertEqual(len(self.session.dialogue_history), 1)
        self.assertEqual(self.session.dialogue_history[0]['attachments'], attachments)
        self.assertEqual(len(self.session.user_inbox), 1)
        bb = Blackboard(self.session.bb_path, readonly=True)
        try:
            statements = bb.read(BlackboardLayer.TARGET_AUTH, 'user-context')['statements']
        finally:
            bb.close()
        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0]['statement_id'], initial['statement_id'])
        self.assertTrue(statements[0]['delivery_pending'])

    async def test_message_arriving_at_completion_is_answered_without_another_send(self) -> None:
        plan = prepare_launch(LaunchRequest(mode=RunMode.CTF, mission='原始任务'))
        self.session.plan = plan
        self.session.launch_request = {'mode': 'ctf', 'mission': '原始任务'}
        result = SimpleNamespace(run_summary={'answers_complete': True}, verify_stats={}, flag_stats={}, report_path='')

        async def finish(**kwargs):
            bb = Blackboard(self.session.bb_path)
            bb.write(BlackboardLayer.TARGET_AUTH, plan.authorization.auth_id, plan.authorization.model_dump(mode='json'))
            bb.close()
            self.session.remember_user_message('结束时追加的问题', message_id='message-late', enqueue=True)
            return result

        with patch('spiritscout.runtime.run_ctf_engagement', new=AsyncMock(side_effect=finish)) as runtime, \
             patch('spiritscout.web.sessions._llm_extract_platform', new=AsyncMock(return_value=None)), \
             patch('spiritscout.web.sessions._master_reply', new=AsyncMock(return_value='现场结论')) as reply:
            await self.manager._run(self.session, self.cfg, plan, False, [ModelEntry(model='fixture', provider='mock')])
        self.assertEqual(runtime.await_count, 1)
        self.assertEqual(reply.await_count, 1)
        self.assertIn('结束时追加的问题', reply.call_args.args[2])
        self.assertEqual(self.session.user_inbox, [])
        self.assertEqual(self.session.status, 'completed')

    def test_old_task_callback_cannot_overwrite_new_running_task(self) -> None:
        self.session.task = Mock()
        self.manager._settle_task(self.session, Mock())
        self.assertEqual(self.session.status, 'running')

    def test_session_config_exposes_absolute_sandbox_coordinates(self) -> None:
        session = Session('sess-relative', 'ctf', 'fixture', Path('relative-session'))
        cfg = self.manager._session_cfg(session, None)
        self.assertTrue(Path(cfg.workspace.root).is_absolute())
        self.assertTrue(Path(cfg.report.output_dir).is_absolute())

    def test_old_missing_summary_recovers_existing_execution_facts(self) -> None:
        self.session.workspace.mkdir(parents=True, exist_ok=True)
        bb = Blackboard(self.session.bb_path)
        bb.write(BlackboardLayer.TASK_STATE, 'task-1', {'task': {'status': 'done', 'payload': {'node_title': '已验证'}}})
        bb.close()
        facts = json.loads(_session_run_facts(self.session))
        self.assertEqual(facts['recorded_tasks'][0]['status'], 'done')

    def test_read_tools_reject_other_session_files(self) -> None:
        resources = ConversationResources(self.session, Mock(), {})
        self.assertFalse(resources.call('session_read', {'source': 'file', 'path': '/etc/passwd'})['ok'])
        self.assertFalse(resources.call('save_report', {'name': '../outside.md', 'content': 'bad'})['ok'])


class InstructionAndAnswerRecoveryTests(unittest.TestCase):
    def test_instructions_survive_overlapping_updates_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bb = Blackboard(Path(temp) / 'bb.db')
            ring = RingLedger()
            ring.bind_blackboard(bb)
            first = ring.steer('ctf_web', '先读取附件', task_id='t')
            second = ring.steer('ctf_web', '再核对端口', task_id='t')
            restored = RingLedger()
            restored.bind_blackboard(bb)
            self.assertEqual([item['id'] for item in restored.steerings_for('ctf_web', task_id='t')], [first, second])
            self.assertTrue(restored.steering_feedback('ctf_web', task_id='t', route_id='r', instruction_id=first, status='completed'))
            bb.close()

    def test_reopened_answer_keeps_old_candidate_but_requires_new_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'flags.json'
            manager = FlagManager(local_only=True, store_path=path)
            old = manager.register('flag{old}', challenge_id='c', found_by='fixture')
            manager.confirm_complete('c', [old.flag_id], '先前结论')
            manager.reopen('c', '用户要求复核')
            restored = FlagManager(local_only=True, store_path=path)
            self.assertEqual(restored.completed_ids(), set())
            self.assertIsNotNone(restored.get('flag{old}'))
            new = restored.register('flag{correct}', challenge_id='c', found_by='fixture')
            self.assertTrue(restored.confirm_complete('c', [new.flag_id], '新证据确认'))
            self.assertEqual(restored.completed_ids(), {'c'})


if __name__ == '__main__':
    unittest.main()
