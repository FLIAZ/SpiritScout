"""轻量探索复用普通模式的公开层级，并贯穿黑板、检查点与下一次请求。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_single_executor import context

from spiritscout.agents.sub.base import RoutePlan
from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import AuditLogger
from spiritscout.core.models import BlackboardLayer, RunMode, Task
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.light_exploration import FORMAT_GUIDANCE, record_progress
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.gateway.progress import EXPLORATION_PROGRESS_GUIDANCE
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.web.exploration_stages import read_public_stages
from spiritscout.web.process_graph import project_blackboard


class LightExplorationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bb = Blackboard(self.root / 'bb.db')
        self.addCleanup(self.bb.close)
        ctx = context(RunMode.CTF)
        ctx.blackboard, ctx.ring = self.bb, None
        ctx.audit = AuditLogger(self.root / 'audit.jsonl')
        ctx.journal = ExecutionJournal(self.bb, self.root, 'exploration-fixture')
        ctx.gateway.context_budget = lambda **kw: 10650
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))
        self.task = Task(task_id='one', agent_type=self.agent.agent_type, task_type='ctf_single', target='',
                         payload={'challenge': {'id': 'one'}, 'mission': '核对本地程序'})
        self.route = SimpleNamespace(route_id='r', name='solve')
        self.plan = RoutePlan(name='solve', strategy='', steps=[{'action': 'investigate'}])
        self.state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, self.plan, self.state)
        self.state['system_initialized'] = True

    async def test_prose_recovery_does_not_append_display_format_after_action_instruction(self):
        self.agent._ensure_progress_guidance(self.state)
        self.state['messages'].append({'role': 'assistant', 'content':
            '{"progress":"仍在检查","exploration":{"intent":"读取源码","attempt":"读取","state":"running"}}'})
        before = copy.deepcopy(self.state['messages'])
        self.agent.ctx.gateway.chat = AsyncMock(return_value=SimpleNamespace(content='', tool_calls=[{
            'id': 'actual', 'name': 'tool_lookup', 'arguments': {'name': 'source_context'}}]))
        calls = await self.agent._forced_tool_recovery(self.task, self.route, self.state, 1)
        sent = self.agent.ctx.gateway.chat.call_args.args[0]
        self.assertNotIn(FORMAT_GUIDANCE, [m.get('content') for m in sent])
        self.assertTrue(any('本次通过原生 tool_calls' in str(m.get('content')) for m in sent))
        self.assertEqual(self.state['messages'][:len(before)], before)
        self.assertEqual(calls[0]['name'], 'tool_lookup')
        self.assertEqual(self.state['messages'][-1]['tool_calls'][0]['id'], 'actual')
        # 恢复后的常规工具回合仍能展示新进展；不会永久关闭探索图。
        self.state['messages'].append({'role': 'tool', 'tool_call_id': 'actual', 'content': 'found'})
        next_view = self.agent._light_model_messages(self.task, self.route, self.state)
        self.assertEqual(next_view[-1]['content'], FORMAT_GUIDANCE)

    async def test_streamed_semantics_persist_grouped_attempts_and_feed_next_turn(self):
        statements = [
            {'progress': '入口条件不满足，记录这条失败路径。', 'analysis': 'PRIVATE_ANALYSIS', 'exploration': {
                'intent': '确认输入条件', 'attempt': '空输入对照', 'state': 'refuted', 'reason': '原始程序拒绝空输入'}},
            '继续核对已有输出，不产生新发现。',
            {'progress': '带参数的对照发现了新的输出分支。', 'exploration': {
                'intent': '确认输入条件', 'attempt': '带参数对照', 'from': '空输入对照',
                'reason': '空输入路径已排除', 'state': 'running', 'milestone': {
                    'kind': 'discovery', 'title': '参数触发不同输出', 'basis': '公开观察显示返回分支改变，尚待验证',
                    'next': '核对边界输入', 'evidence_refs': ['fixture-output.log']}}}]
        captured = []
        async def chat(messages, **kwargs):
            captured.append(copy.deepcopy(messages))
            self.assertIn(EXPLORATION_PROGRESS_GUIDANCE, [m.get('content') for m in messages])
            self.assertEqual(messages[-1]['content'], FORMAT_GUIDANCE)
            self.assertNotIn(FORMAT_GUIDANCE, [m.get('content') for m in self.state['messages']])
            self.assertLessEqual(estimate_input_tokens(messages, kwargs['tools']), 10650)
            body = statements[len(captured) - 1]
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            kwargs['on_delta']('text', text[:20])
            kwargs['on_delta']('text', text[20:])
            return SimpleNamespace(content=text, tool_calls=[{
                'id': f'lookup-{len(captured)}', 'name': 'tool_lookup', 'arguments': {}}])
        self.agent.ctx.gateway.chat = AsyncMock(side_effect=chat)
        for _ in statements:
            result = await self.agent.execute_step(self.task, self.route, self.plan, {'action': 'investigate'})
            self.assertTrue(result.ok)
        self.assertEqual(self.agent.ctx.gateway.chat.await_count, 3)
        self.assertIn('原始程序拒绝空输入', str(captured[1]))
        rows = self.bb.query(BlackboardLayer.TOPOLOGY, data_filter=('kind', 'exploration'))
        self.assertEqual(len(rows), 3)
        attempts = {row['data']['text']: row for row in rows if row['data']['node_type'] == 'attempt'}
        self.assertEqual(attempts['空输入对照']['data']['state'], 'refuted')
        self.assertIn('topology:' + attempts['空输入对照']['record_id'], attempts['带参数对照']['data']['parent_refs'])
        stages = read_public_stages(self.root / 'audit.jsonl')['items']
        self.assertEqual(len(stages), 3)
        self.assertNotIn('PRIVATE_ANALYSIS', str(stages))
        self.assertEqual(stages[2]['exploration']['milestone']['kind'], 'discovery')
        graph = project_blackboard(self.bb)
        self.assertEqual(len([n for n in graph['nodes'] if n['kind'] == 'exploration']), 3)
        self.assertFalse(any(n['kind'] == 'finding' for n in graph['nodes']))
        self.assertTrue(any(e['kind'] == 'derives' for e in graph['edges']))
        restored = self.agent.ctx.journal.latest_checkpoint('ctf_conversation', 'one:r')['state']
        next_view = self.agent._light_model_messages(self.task, self.route, restored)
        self.assertIn('参数触发不同输出', str(next_view))
        self.assertIn('原始程序拒绝空输入', str(next_view))
        # 检查点丢失也复用同一黑板节点，不按每条公开事件增加节点。
        result = record_progress(self.bb, {}, {'text': '同一方法仍在验证', 'exploration': statements[2]['exploration']},
            actor=self.agent.agent_id, agent_type=self.agent.agent_type,
            source={'task_id': 'one', 'route_id': 'r', 'route': 'solve', 'challenge': 'one'})
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.bb.query(BlackboardLayer.TOPOLOGY, data_filter=('kind', 'exploration'))), 3)

    async def test_deferred_graph_tools_update_latest_blackboard_and_link_facts(self):
        names = {s['function']['name'] for s in self.agent.tool_schemas}
        self.assertTrue({'exploration_write', 'graph_read', 'blackboard_write', 'asset_link'} <= names)
        async def invoke(name, args):
            outcome = await self.agent._run_tool_call_inner(self.task, self.route, self.state, {
                'name': 'tool_invoke', 'arguments': {'name': name, 'arguments': args}})
            self.assertTrue(outcome.ok, outcome.text)
            return json.loads(outcome.text)
        created = await invoke('exploration_write', {'node_type': 'attempt', 'text': '检查输入', 'state': 'running'})
        ref = created['blackboard_ref']
        await invoke('exploration_write', {'record_id': ref['record_id'], 'version': ref['version'],
            'node_type': 'attempt', 'text': '检查输入', 'state': 'exhausted', 'reason': '已有对照没有支持该假设'})
        view = self.agent._light_model_messages(self.task, self.route, self.state)
        self.assertIn('已有对照没有支持该假设', str(view))
        directory = await invoke('graph_read', {'graph': 'exploration', 'state': 'exhausted'})
        self.assertEqual(directory['items'][0]['state'], 'exhausted')
        await invoke('blackboard_write', {'text': '原始程序返回了错误码', 'evidence_refs': ['fixture-output.log'],
                                          'exploration_refs': ['topology:' + ref['record_id']]})
        graph = project_blackboard(self.bb)
        self.assertTrue(any(e['kind'] == 'produces' for e in graph['edges']))
        fact = next(n for n in graph['nodes'] if n['kind'] == 'finding')
        self.assertEqual(fact['evidence_state'], 'unverified')

    def test_natural_language_does_not_invent_exploration_or_reopen_refuted_attempt(self):
        source = {'task_id': 'one', 'route_id': 'r'}
        args = dict(actor=self.agent.agent_id, agent_type=self.agent.agent_type, source=source)
        self.assertFalse(record_progress(self.bb, self.state, {'text': '看起来已经突破'}, **args)['ok'])
        first = {'text': '该方法被现场否定', 'exploration': {'intent': '检查条件', 'attempt': '对照',
                 'state': 'refuted', 'reason': '现场否定'}}
        self.assertTrue(record_progress(self.bb, self.state, first, **args)['ok'])
        second = {'text': '回看同一方法', 'exploration': {'intent': '检查条件', 'attempt': '对照'}}
        self.assertTrue(record_progress(self.bb, self.state, second, **args)['ok'])
        row = self.bb.read(BlackboardLayer.TOPOLOGY, self.state['light_active_attempt'])
        self.assertEqual(row['state'], 'refuted')
        self.assertEqual(row['reason'], '现场否定')
