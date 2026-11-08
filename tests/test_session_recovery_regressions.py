from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry, SystemConfig, WorkspaceConfig
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import BlackboardLayer, RunMode
from spiritscout.gateway import ModelGateway, ModelRegistry
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session, SessionManager


class SessionRecoveryRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)),
                                audit_log_path=str(self.root / 'control.jsonl'),
                                models_config_path=str(self.root / 'models.yaml'),
                                agent_models_path=str(self.root / 'agents.yaml'))
        self.cfg.gateway.automatic_platform_extraction = False
        self.app = create_app(self.cfg, [])
        self.manager = self.app.state.manager
        self.plan = prepare_launch(LaunchRequest(mode=RunMode.CTF, mission='检查 https://a.audit.invalid/'))
        self.session = Session('sess-recovery', 'ctf', self.plan.target,
                               self.manager.root / 'sess-recovery', status='completed',
                               launch_request={'mode': 'ctf', 'mission': self.plan.mission})
        self.session.workspace.mkdir()
        self.session.plan = self.plan
        self.manager._sessions[self.session.session_id] = self.session
        self.models = [ModelEntry(model='fixture', provider='mock')]
        bb = Blackboard(self.session.bb_path)
        bb.write(BlackboardLayer.TARGET_AUTH, self.plan.authorization.auth_id,
                 self.plan.authorization.model_dump(mode='json'))
        bb.close()
        self.manager._persist(self.session)

    async def request_execution(self, *args, **kwargs):
        kwargs['execution_request'].update(mission='继续核对本次目标')
        return '执行目标已接收，等待执行'

    async def test_light_followup_uses_model_intent_then_execution_only_handoff(self) -> None:
        self.cfg.gateway.lightweight_local = True
        self.session.remember_user_message('继续剩余工具工作', message_id='light-resume', enqueue=True)
        reply = AsyncMock(side_effect=self.request_execution)
        result = SimpleNamespace(run_summary={'answers_complete': True}, verify_stats={}, flag_stats={}, report_path='')
        with patch('spiritscout.web.sessions._llm_route_intent', new=AsyncMock(return_value='task')) as classify, \
             patch('spiritscout.web.sessions._master_reply', new=reply), \
             patch('spiritscout.runtime.run_ctf_engagement', new=AsyncMock(return_value=result)) as runtime:
            await self.manager._run(self.session, self.cfg, self.plan, False, self.models, True)
        classify.assert_awaited_once()
        self.assertTrue(reply.call_args.kwargs['execution_only'])
        self.assertTrue(runtime.call_args.kwargs['followup_goal'])
        self.assertEqual(runtime.call_args.kwargs['mission'], '继续核对本次目标')
        self.assertIn('light-resume', self.session.handled_user_messages)

    async def test_light_dialogue_keeps_record_tools_and_does_not_dispatch(self) -> None:
        self.cfg.gateway.lightweight_local = True
        self.session.remember_user_message('解释已有结果', message_id='light-question', enqueue=True)
        with patch('spiritscout.web.sessions._llm_route_intent', new=AsyncMock(return_value='dialogue')), \
             patch('spiritscout.web.sessions._master_reply', new=AsyncMock(return_value='已有结果的解释')) as reply, \
             patch('spiritscout.runtime.run_ctf_engagement', new=AsyncMock()) as runtime:
            await self.manager._run(self.session, self.cfg, self.plan, False, self.models, True)
        self.assertFalse(reply.call_args.kwargs['execution_only'])
        runtime.assert_not_awaited()
        self.assertEqual(self.session.summary['intent'], 'dialogue')

    async def test_resume_refreshes_endpoint_credentials_and_removed_models(self) -> None:
        old = [ModelEntry(model='fixture', provider='mock',
                          base_url='http://old.invalid', api_key='fixture-old-key'),
               ModelEntry(model='removed', provider='mock')]
        fresh = [old[0].model_copy(update={'base_url': 'http://new.invalid',
                                          'api_key': 'fixture-new-key'})]

        async def inspect_run(*args, **kwargs):
            gateway = ModelGateway(ModelRegistry(fresh))
            entry = gateway.resolve_model()
            self.assertEqual(entry.base_url, 'http://new.invalid')
            self.assertEqual(entry.api_key, 'fixture-new-key')
            self.assertFalse(gateway.registry.has('removed'))

        for selected in ('', fresh[0].key):
            with self.subTest(selected=selected):
                self.session.status = 'completed'
                self.session.model = selected
                self.session._model_entries = old
                with patch('spiritscout.config.load_model_config', return_value=fresh), \
                     patch('spiritscout.web.sessions.vault.apply_keys', side_effect=lambda entries: entries), \
                     patch.object(self.manager, 'start'), \
                     patch.object(self.manager, '_run_turns', new=AsyncMock(side_effect=inspect_run)) as run:
                    await self.manager.resume(self.session.session_id)
                    queued = self.manager._queue.get_nowait()
                    await self.manager._run(*queued)
                    self.manager._queue.task_done()
                    run.assert_awaited_once()

    async def test_model_change_while_resume_queued_survives_run_start(self) -> None:
        resumed = [ModelEntry(model='fixture', provider='mock', base_url='http://resumed.invalid')]
        latest = [ModelEntry(model='selected', provider='mock', base_url='http://selected.invalid')]
        self.session._model_entries = self.models

        async def inspect_run(*args, **kwargs):
            gateway = ModelGateway(ModelRegistry(resumed))
            self.assertEqual(gateway.resolve_model().key, latest[0].key)
            self.assertEqual(gateway.resolve_model().base_url, 'http://selected.invalid')

        with patch('spiritscout.config.load_model_config', side_effect=[resumed, latest]), \
             patch('spiritscout.web.sessions.vault.apply_keys', side_effect=lambda entries: entries), \
             patch.object(self.manager, 'start'), \
             patch.object(self.manager, '_run_turns', new=AsyncMock(side_effect=inspect_run)) as run:
            await self.manager.resume(self.session.session_id)
            queued = self.manager._queue.get_nowait()
            self.manager.set_model(self.session.session_id, latest[0].key)
            await self.manager._run(*queued)
            self.manager._queue.task_done()
            run.assert_awaited_once()

    async def test_corrupt_database_does_not_hide_healthy_sessions_or_modify_files(self) -> None:
        broken = self.manager.root / 'sess-broken'
        broken.mkdir()
        meta = broken / 'meta.json'
        metadata = {'session_id': 'sess-broken', 'mode': 'ctf', 'status': 'completed'}
        meta.write_text(json.dumps(metadata))
        database = broken / 'bb.db'
        database.write_bytes(b'not a SQLite database')
        warnings = io.StringIO()
        with redirect_stderr(warnings):
            restored = SessionManager(self.cfg, [])
        self.assertIsNotNone(restored.get(self.session.session_id))
        self.assertIsNone(restored.get('sess-broken'))
        self.assertEqual(json.loads(meta.read_text()), metadata)
        self.assertEqual(database.read_bytes(), b'not a SQLite database')
        self.assertIn('sess-broken', warnings.getvalue())
        self.assertIn('已跳过', warnings.getvalue())

    async def test_failed_dispatch_remains_pending_and_same_id_can_retry(self) -> None:
        self.session.remember_user_message('继续检查', message_id='message-retry', enqueue=True)
        self.manager._persist(self.session)
        with patch('spiritscout.web.sessions._master_reply', new=self.request_execution), \
             patch.object(self.manager, '_rebuild_plan', side_effect=RuntimeError('dispatch failed')):
            await self.manager._run(self.session, self.cfg, self.plan, False, self.models, True)
        self.assertEqual(len(self.session.user_inbox), 1)
        self.assertNotIn('message-retry', self.session.handled_user_messages)
        restored = SessionManager(self.cfg, []).get(self.session.session_id)
        self.assertEqual(restored.user_inbox[0]['statement_id'], 'message-retry')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1') as client:
            with patch.object(self.manager, 'resume', new=AsyncMock(return_value=self.session)) as resume:
                response = await client.post('/api/sessions/sess-recovery/messages',
                                             json={'content': '继续检查', 'message_id': 'message-retry'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['resumed'])
        resume.assert_awaited_once()
        result = SimpleNamespace(run_summary={'answers_complete': True}, verify_stats={}, flag_stats={}, report_path='')
        with patch('spiritscout.web.sessions._master_reply', new=self.request_execution), \
             patch('spiritscout.runtime.run_ctf_engagement', new=AsyncMock(return_value=result)) as runtime:
            await self.manager._run(self.session, self.cfg, self.plan, False, self.models, True)
        runtime.assert_awaited_once()
        self.assertEqual(self.session.user_inbox, [])
        self.assertIn('message-retry', self.session.handled_user_messages)

    async def test_cancel_before_dispatch_does_not_acknowledge_request(self) -> None:
        self.session.remember_user_message('继续检查', message_id='message-cancel', enqueue=True)
        with patch('spiritscout.web.sessions._master_reply', new=self.request_execution), \
             patch.object(self.manager, '_rebuild_plan', side_effect=asyncio.CancelledError):
            await self.manager._run(self.session, self.cfg, self.plan, False, self.models, True)
        self.assertEqual(self.session.status, 'stopped')
        self.assertEqual(len(self.session.user_inbox), 1)
        self.assertNotIn('message-cancel', self.session.handled_user_messages)

    async def test_followup_scope_and_mission_survive_restart(self) -> None:
        original_mission = self.session.launch_request['mission']
        self.session.remember_user_message('追加 https://b.audit.invalid/ 并核对', message_id='message-b', enqueue=True)
        result = SimpleNamespace(run_summary={'answers_complete': True}, verify_stats={}, flag_stats={}, report_path='')
        with patch('spiritscout.web.sessions._master_reply', new=self.request_execution), \
             patch('spiritscout.runtime.run_ctf_engagement', new=AsyncMock(return_value=result)):
            await self.manager._run(self.session, self.cfg, self.plan, False, self.models, True)
        expected = self.session.plan.authorization.model_dump(mode='json')
        restored_manager = SessionManager(self.cfg, [])
        restored = restored_manager.get(self.session.session_id)
        plan = restored_manager._rebuild_plan(restored)
        self.assertEqual(plan.authorization.model_dump(mode='json'), expected)
        self.assertIn('https://b.audit.invalid', plan.authorization.scope.allowed_urls)
        self.assertEqual(plan.mission, '继续核对本次目标')
        self.assertEqual(restored.launch_request['mission'], original_mission)
        self.assertEqual(plan.challenges, self.session.plan.challenges)

    async def test_current_plan_roundtrip_covers_all_execution_modes(self) -> None:
        for mode in (RunMode.SRC, RunMode.CTF, RunMode.AWDP, RunMode.COMPETITION):
            with self.subTest(mode=mode):
                plan = prepare_launch(LaunchRequest(mode=mode, mission='检查 https://a.audit.invalid/'))
                plan.authorization.scope.allowed_urls.append('https://b.audit.invalid/')
                plan.authorization.scope.denied = ['excluded.audit.invalid']
                plan.platform_profile = {'kind': 'fixture', 'base_url': 'https://platform.audit.invalid/'}
                plan.mission = '当前执行目标'
                self.session.mode = mode.value
                self.session.plan = plan
                self.session.execution_plan = self.manager._snapshot_plan(plan)
                self.manager._persist(self.session)
                restored_manager = SessionManager(self.cfg, [])
                restored = restored_manager.get(self.session.session_id)
                rebuilt = restored_manager._rebuild_plan(restored)
                self.assertEqual(rebuilt.authorization.model_dump(mode='json'), plan.authorization.model_dump(mode='json'))
                self.assertEqual(rebuilt.challenges, plan.challenges)
                self.assertEqual(rebuilt.platform_profile, plan.platform_profile)
                self.assertEqual(rebuilt.mission, plan.mission)
                if plan.rules:
                    self.assertEqual(rebuilt.rules.raw, plan.rules.raw)

    async def test_malformed_snapshot_does_not_silently_revert_to_original_scope(self) -> None:
        self.session.execution_plan = {'authorization': {'mode': 'src'}}
        with self.assertRaises(SpiritScoutError):
            self.manager._rebuild_plan(self.session)

    async def test_legacy_session_without_snapshot_remains_rebuildable(self) -> None:
        restored_manager = SessionManager(self.cfg, [])
        restored = restored_manager.get(self.session.session_id)
        self.assertEqual(restored.execution_plan, {})
        plan = restored_manager._rebuild_plan(restored)
        self.assertEqual(plan.mission, self.session.launch_request['mission'])
        self.assertEqual(plan.authorization.mode, RunMode.CTF)
