"""轻量途中消息贯穿 HTTP 入队、真实黑板、请求投影和消费确认。"""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from test_single_executor import context

from spiritscout.agents.sub.base import RoutePlan
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.models import BlackboardLayer, RunMode, Task
from spiritscout.ctf.agents import CTFBaseAgent, _complete_tool_history
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session


class SingleUserFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)),
                           audit_log_path=str(self.root / 'audit.jsonl'),
                           models_config_path=str(self.root / 'models.yaml'))
        self.app = create_app(cfg, [])
        self.manager = self.app.state.manager
        self.session = Session('sess-feed', 'ctf', '', self.manager.root / 'sess-feed', status='running')
        self.session.workspace.mkdir(parents=True)
        self.manager._sessions[self.session.session_id] = self.session
        self.session.remember_user_message('INITIAL_GOAL', message_id='initial')
        bb = Blackboard(self.session.bb_path)
        self.addCleanup(bb.close)
        ctx = context(RunMode.CTF)
        ctx.blackboard, ctx.ring, ctx.journal, ctx.audit = bb, None, None, Mock()
        ctx.gateway.context_budget = lambda **kw: 10650
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))
        self.agent.user_inbox = self.session.user_inbox
        self.agent.on_user_feed = self.session.acknowledge_user_feed
        self.task = Task(task_id='one', agent_type=self.agent.agent_type, task_type='ctf_single', target='',
                         payload={'challenge': {'id': 'one'}, 'mission': 'INITIAL_GOAL'})
        self.route = SimpleNamespace(route_id='r', name='solve')
        self.plan = RoutePlan(name='solve', strategy='', steps=[{'action': 'investigate'}])
        self.state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, self.plan, self.state)
        self.state['messages'].append({'role': 'assistant', 'content': 'PREVIOUS_ANALYSIS'})
        self.state['system_initialized'] = True
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1')
        self.addAsyncCleanup(self.client.aclose)

    async def send(self, text, message_id):
        response = await self.client.post('/api/sessions/sess-feed/messages',
                                          json={'content': text, 'message_id': message_id})
        self.assertEqual(response.status_code, 200, response.text)

    def response(self):
        return SimpleNamespace(content='收到，重新核对。', tool_calls=[{
            'id': f'lookup-{self.state["turn"]}', 'name': 'tool_lookup', 'arguments': {}}])

    async def step(self):
        return await self.agent.execute_step(self.task, self.route, self.plan, {'action': 'investigate'})

    async def test_http_message_reaches_model_as_raw_text_and_is_acknowledged_after_response(self):
        await self.send('请停止原来的方向，检查新附件说明。', 'midway')
        # 复现旧轻量会话继承普通模式引用：下一回合必须重新读取原话。
        old = CTFBaseAgent._world_snapshot(self.agent)
        self.assertIn('blackboard_ref', old['user_context'])
        self.state['world_state'] = old
        async def chat(messages, **kwargs):
            self.assertNotIn('midway', self.session.handled_user_messages)
            world = next(json.loads(m['content'].split('\n', 1)[1].rsplit('\n', 1)[0])
                         for m in messages if m.get('content', '').startswith('<world_state>'))
            self.assertEqual(world['user_context']['statements'][-1]['text'], '请停止原来的方向，检查新附件说明。')
            self.assertIn('请停止原来的方向，检查新附件说明。', messages[-1]['content'])
            self.assertTrue(_complete_tool_history(messages))
            self.assertLessEqual(estimate_input_tokens(messages, kwargs['tools']), 10650)
            return self.response()
        self.agent.ctx.gateway.chat = AsyncMock(side_effect=chat)
        result = await self.step()
        self.assertTrue(result.ok)
        self.agent.ctx.gateway.chat.assert_awaited_once()
        self.assertIn('midway', self.session.handled_user_messages)
        self.assertEqual(self.session.user_inbox, [])
        delivery = [call.args[2] for call in self.agent.ctx.audit.log.call_args_list
                    if call.args[0] == 'user.feed_delivered']
        self.assertEqual(delivery[0]['statement_ids'], ['midway'])
        self.assertEqual(delivery[0]['delivery_stage'], 'model_response_received')

    async def test_message_arriving_during_request_remains_pending_until_next_request(self):
        await self.send('FIRST_MESSAGE', 'first')
        sent = []
        async def chat(messages, **kwargs):
            sent.append(copy.deepcopy(messages))
            if len(sent) == 1:
                await self.send('SECOND_MESSAGE', 'second')
            return self.response()
        self.agent.ctx.gateway.chat = AsyncMock(side_effect=chat)
        await self.step()
        self.assertEqual([x['statement_id'] for x in self.session.user_inbox], ['second'])
        self.assertNotIn('SECOND_MESSAGE', str(sent[0]))
        await self.step()
        self.assertIn('SECOND_MESSAGE', sent[1][-1]['content'])
        self.assertEqual(self.session.user_inbox, [])
        self.assertEqual(self.session.handled_user_messages, ['first', 'second'])

    async def test_delivered_message_stays_in_tool_recovery_and_later_turn_without_reacknowledging(self):
        await self.send('CORRECTION_KEEP_THIS_CONSTRAINT', 'correction')
        sent = []
        async def chat(messages, **kwargs):
            sent.append(copy.deepcopy(messages))
            self.assertIn('CORRECTION_KEEP_THIS_CONSTRAINT', messages[-1]['content'])
            if len(sent) == 1:
                return SimpleNamespace(content='{"progress":"收到，准备更正"}', tool_calls=[])
            self.assertIn('不是重复投递的新消息', messages[-1]['content'])
            return self.response()
        self.agent.ctx.gateway.chat = AsyncMock(side_effect=chat)
        await self.step()
        self.assertEqual(len(sent), 2)
        self.assertEqual(self.session.user_inbox, [])
        await self.step()
        self.assertEqual(len(sent), 3)
        delivered = [call for call in self.agent.ctx.audit.log.call_args_list if call.args[0] == 'user.feed_delivered']
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.session.handled_user_messages, ['correction'])

    async def test_failed_cancelled_and_overflowed_requests_do_not_consume_user_message(self):
        await self.send('PENDING_MESSAGE', 'pending')
        self.agent.ctx.gateway.chat = AsyncMock(side_effect=RuntimeError('fixture request failed'))
        await self.step()
        self.assertEqual(len(self.session.user_inbox), 1)
        self.assertNotIn('pending', self.session.handled_user_messages)
        self.agent.ctx.gateway.chat = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.step()
        self.assertEqual(len(self.session.user_inbox), 1)
        self.agent.ctx.gateway.context_budget = lambda **kw: 10
        self.agent.ctx.gateway.chat = AsyncMock(side_effect=AssertionError('超窗不能调用模型'))
        await self.step()
        self.agent.ctx.gateway.chat.assert_not_awaited()
        self.assertNotIn('pending', self.session.handled_user_messages)
        self.assertEqual(self.session.user_statement('pending')['text'], 'PENDING_MESSAGE')
        self.assertIn('PENDING_MESSAGE', str(self.agent.ctx.blackboard.read(BlackboardLayer.TARGET_AUTH, 'user-context')))
