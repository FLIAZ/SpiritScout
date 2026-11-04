from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.sub.base import RoutePlan
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.models import AgentType, RunMode, Task
from spiritscout.ctf.agents import CTFBaseAgent, _challenge_view
from spiritscout.gateway.context import compact_json
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.modes.assembler import CTF_TACTIC_MAP, _planning_challenges
from spiritscout.web.app import SessionCreatePayload, create_app
from spiritscout.web.sessions import Session, SessionManager


class TypeHintTests(unittest.TestCase):
    def test_open_types_and_extra_are_hints_without_scope_or_mode_changes(self) -> None:
        request = LaunchRequest(mode=RunMode.CTF, mission='解答附件题目',
            ctf_type_hints=['Web', ' IoT ', 'AI 安全', 'Web', '脑机接口'],
            ctf_type_note='固件协议与提示词注入；参考 https://not-authorized.invalid/')
        plan = prepare_launch(request)
        hints = plan.challenges[0]['user_type_hints']
        self.assertEqual(hints['types'], ['Web', 'IoT', 'AI 安全', '脑机接口'])
        self.assertEqual(hints['extra'], request.ctf_type_note)
        self.assertEqual(hints['binding'], 'direction_hint_only')
        self.assertEqual(plan.authorization.mode, RunMode.CTF)
        self.assertEqual(plan.authorization.scope.allowed_urls, [])
        self.assertEqual(plan.authorization.scope.allowed_domains, [])
        self.assertNotIn('direction', plan.challenges[0])
        self.assertEqual(_planning_challenges(plan.challenges)[0]['user_type_hints'], hints)

    def test_empty_types_preserve_automatic_behavior_and_other_modes_ignore_hint(self) -> None:
        plan = prepare_launch(LaunchRequest(mode=RunMode.CTF, mission='附件题'))
        self.assertNotIn('user_type_hints', plan.challenges[0])
        src = prepare_launch(LaunchRequest(mode=RunMode.SRC, mission='审计本地代码',
                                          ctf_type_hints=['IoT'], ctf_type_note='自定义'))
        self.assertEqual(src.authorization.mode, RunMode.SRC)
        self.assertEqual(src.challenges, [])

    def test_custom_only_and_long_extra_survive_projection(self) -> None:
        note = '自定义协议与设备固件混合分析。' * 100
        plan = prepare_launch(LaunchRequest(mode=RunMode.CTF, mission='附件题', ctf_type_note=note))
        hints = plan.challenges[0]['user_type_hints']
        self.assertEqual(hints['types'], [])
        self.assertEqual(_challenge_view(plan.challenges[0], total_cap=10)['user_type_hints'], hints)
        projected = json.loads(compact_json({'challenge': {'user_type_hints': hints}, 'old': 'x'*10000}, 100))
        self.assertEqual(projected['challenge']['user_type_hints'], hints)

    def test_hint_is_restored_from_saved_launch_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=temp), audit_log_path=str(Path(temp)/'audit.jsonl'))
            manager = SessionManager(cfg, [])
            session = Session('sess-hints', 'ctf', 'fixture', manager.root/'sess-hints', status='completed')
            session.workspace.mkdir()
            session.launch_request = {'mode': 'ctf', 'mission': '附件题', 'ctf_type_hints': ['IoT', 'AI 安全'],
                                      'ctf_type_note': '固件与代理协议'}
            manager._sessions[session.session_id] = session
            manager._persist(session)
            restored_manager = SessionManager(cfg, [])
            restored = restored_manager.get(session.session_id)
            plan = restored_manager._rebuild_plan(restored)
            self.assertEqual(plan.challenges[0]['user_type_hints']['types'], ['IoT', 'AI 安全'])
            self.assertEqual(plan.challenges[0]['user_type_hints']['extra'], '固件与代理协议')

    def test_worker_receives_full_hint_as_nonrestrictive_context(self) -> None:
        hints = {'types': ['IoT', 'AI 安全'], 'extra': '自定义类型说明。'*100, 'source': 'user', 'binding': 'direction_hint_only'}
        agent = object.__new__(CTFBaseAgent)
        agent.agent_type = AgentType.CTF_WEB
        agent.direction = 'web'
        agent.flag_manager = None
        agent.ctx = SimpleNamespace(blackboard=SimpleNamespace(read=Mock(return_value=None)),
                                    gateway=SimpleNamespace(config=None), journal=None)
        agent._world_snapshot = Mock(return_value={})
        agent._shared_conclusions = Mock(return_value=[])
        agent._shared_notes = Mock(return_value=[])
        agent._shared_site = Mock(return_value={})
        agent._peer_flags = Mock(return_value=[])
        agent._solver_system_prompt = Mock(return_value='fixture')
        agent._inherit_conversation = Mock()
        task = Task(task_id='hint-task', task_type='ctf_solve', agent_type=AgentType.CTF_WEB,
                    target='ctf://challenges', payload={'challenge': {'id': 'c', 'description': '题面', 'user_type_hints': hints}})
        state = {'seed': []}
        agent._seed_messages(task, SimpleNamespace(route_id='r', name='solve'), RoutePlan('solve', '', []), state)
        body = state['messages'][1]['content']
        self.assertIn(hints['extra'], body)
        self.assertIn('不限制角色、工具或方法', body)
        self.assertEqual(body.count(hints['extra']), 1)


class TypeHintIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_platform_challenge_keeps_user_hint(self) -> None:
        from spiritscout.runtime import run_ctf_engagement
        from tests.test_platform_adapter import PROFILE

        hints = {'types': ['IoT', 'AI 安全'], 'extra': '混合题', 'binding': 'direction_hint_only'}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=temp), audit_log_path=str(root/'audit.jsonl'))
            plan = prepare_launch(LaunchRequest(mode=RunMode.CTF, mission='附件题'))
            infra = SimpleNamespace(workspace=root, cfg=cfg, audit=Mock(), bus=None, blackboard=Mock(),
                                    agent_models={}, gateway=Mock(), experience=None, close=AsyncMock())
            adapter = SimpleNamespace(facts=[], precheck=AsyncMock(), list_challenges=AsyncMock(return_value=[{'id': 'server-c'}]),
                                      submit=AsyncMock(), summary=Mock(return_value={}),
                                      close_all=AsyncMock(), aclose=AsyncMock())
            assembler = SimpleNamespace(assemble=Mock(return_value=SimpleNamespace(notes=[])))
            with patch('spiritscout.runtime._build_infra', new=AsyncMock(return_value=infra)), \
                 patch('spiritscout.ctf.platform.PlatformAdapter', return_value=adapter), \
                 patch('spiritscout.runtime._make_assembler', return_value=assembler), \
                 patch('spiritscout.runtime._run_master', new=AsyncMock(return_value={'rounds_executed': 1, 'dag_nodes': []})), \
                 patch('spiritscout.runtime._render_writeup', new=AsyncMock(return_value=root/'out.md')), \
                 patch('spiritscout.runtime._schedule_background_distill', return_value=None):
                await run_ctf_engagement(cfg=cfg, models=[], authorization=plan.authorization,
                    challenges=[{'id': 'mission-1', 'user_type_hints': hints}], platform_profile=PROFILE,
                    enable_llm_planning=False)
            challenge = assembler.assemble.call_args.kwargs['challenges'][0]
            self.assertEqual(challenge['id'], 'server-c')
            self.assertEqual(challenge['user_type_hints'], hints)

    async def test_api_transports_types_and_rejects_invalid_field_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=temp), audit_log_path=str(Path(temp)/'audit.jsonl'))
            app = create_app(cfg, [])
            session = Session('sess-api-hints', 'ctf', 'fixture', Path(temp)/'sessions/sess-api-hints')
            with patch.object(app.state.manager, 'create', new=AsyncMock(return_value=session)) as create:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
                    result = await client.post('/api/sessions', json={'mode': 'ctf', 'mission': '附件题',
                        'ctf_type_hints': ['IoT', 'AI 安全'], 'ctf_type_note': '其他设备协议'})
                    self.assertEqual(result.status_code, 201, result.text)
                    self.assertEqual(create.call_args.args[1].challenges[0]['user_type_hints']['extra'], '其他设备协议')
                    invalid = await client.post('/api/sessions', json={'mode': 'ctf', 'mission': '附件题', 'ctf_type_hints': 'IoT'})
                    self.assertEqual(invalid.status_code, 422)
                    self.assertEqual(create.await_count, 1)
        self.assertEqual(SessionCreatePayload(ctf_type_hints=['任意新类型']).ctf_type_hints, ['任意新类型'])

    async def test_master_keeps_all_roles_and_receives_user_hint(self) -> None:
        hints = {'types': ['IoT', 'AI 安全'], 'extra': '混合设备题', 'binding': 'direction_hint_only'}
        master = object.__new__(MasterAgent)
        master.agent_id = 'master-hints'
        master.ctx = SimpleNamespace(tactic_map=CTF_TACTIC_MAP, gateway=SimpleNamespace(), audit=Mock(),
            extra={'competition_context': {'challenges': [{'id': 'c', 'user_type_hints': hints}]}})
        master._world_state = Mock(return_value={})
        master._project_rules = Mock(return_value='')
        master._system_prompt = Mock(return_value='fixture')
        master._mission = Mock(return_value='附件题')
        master._situation_digest = Mock(return_value={})
        master._consult_secretary = AsyncMock(side_effect=lambda data, payload: data)
        with patch('spiritscout.agents.master.planning.chat_decision', new=AsyncMock(
                return_value=SimpleNamespace(content='{"dag_nodes":[]}'))) as call:
            result = await master._llm_plan({'dag': TaskDAG().to_dict(), 'round_no': 0,
                                            'target': 'ctf://challenges', 'round_views': []})
        self.assertIsNotNone(result)
        sent = call.call_args.args[1]
        payload = {}
        for message in sent:
            if message['role']=='user':
                payload.update(json.loads(message['content']))
        self.assertEqual(payload['competition_context']['challenges'][0]['user_type_hints'], hints)
        self.assertEqual(set(payload['available_tactics']), set(CTF_TACTIC_MAP))
        self.assertIn('未选择的方向仍然可用', sent[0]['content'])
