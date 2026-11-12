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
from spiritscout.ctf.agents import CTFBaseAgent, _ToolOutcome
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.light_exploration import record_progress
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.gateway.progress import EXPLORATION_PROGRESS_GUIDANCE, FORMAT_GUIDANCE
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.web.exploration_stages import read_public_stages
from spiritscout.web.process_graph import project_blackboard


class LightExplorationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_inline_discovery_shares_action_round_without_reporting_tool_or_retry(self):
        self.agent.ctx.gateway.config.lightweight_local = False
        agent = CTFBaseAgent(self.agent.ctx, flag_manager=FlagManager(local_only=True))
        state = agent._inv_state(self.task, self.route)
        agent._seed_messages(self.task, self.route, self.plan, state)
        state['system_initialized'] = True
        original_messages = copy.deepcopy(state['messages'])
        stable_tools = copy.deepcopy(agent._model_tool_schemas())
        discovery = {'key': 'response', 'intent': '核对响应差异', 'title': '响应包含新的输入条件',
                     'basis': '原始响应显示限制，下一步据此选择对照输入', 'importance': 3,
                     'confidence': 'observed', 'kind': 'discovery', 'evidence_refs': ['previous-execution']}
        bodies = ['已确认输入条件，继续执行对照。\n' + json.dumps({'discovery': discovery}, ensure_ascii=False),
                  '继续核实。\n' + json.dumps({'discovery': {**discovery, 'importance': 'invalid'}}, ensure_ascii=False)]
        agent._run_tool_call_inner = AsyncMock(return_value=_ToolOutcome(ok=True, text='实际执行输出'))

        async def chat(messages, **kwargs):
            self.assertEqual(kwargs['tools'], stable_tools)
            content = bodies[state['turn'] - 1]
            for piece in (content[:20], content[20:65], content[65:]):
                kwargs['on_delta']('text', piece)
                kwargs['on_delta'].flush()
            return SimpleNamespace(content=content, tool_calls=[{
                'id': f"action-{state['turn']}", 'name': 'shell', 'arguments': {'command': 'fixture'}}])

        agent.ctx.gateway.chat = AsyncMock(side_effect=chat)
        for _ in bodies:
            await agent.execute_step(self.task, self.route, self.plan, {'action': 'investigate'})
        self.assertEqual(agent.ctx.gateway.chat.await_count, 2, '展示元数据无效也不增加修复调用')
        self.assertEqual(agent._run_tool_call_inner.await_count, 2)
        self.assertTrue(all(c.args[-1]['name'] == 'shell' for c in agent._run_tool_call_inner.await_args_list))
        self.assertEqual(state['messages'][:len(original_messages)], original_messages)
        self.assertEqual([m['content'] for m in state['messages'] if m['role'] == 'assistant'][-2:], bodies)
        self.assertIn('response', state['discovery_reported_keys'])
        discoveries = [r for r in read_public_stages(self.root / 'audit.jsonl')['items'] if r['kind'] == 'discovery']
        self.assertEqual(len(discoveries), 1)
        self.assertEqual(discoveries[0]['exploration']['milestone']['title'], discovery['title'])
        from spiritscout.core.audit import read_record

        events = [read_record(line) for line in (self.root / 'audit.jsonl').read_text().splitlines()]
        streamed = ''.join(e['detail']['text'] for e in events if e['type'] == 'agent.delta'
                           and e['detail'].get('kind') == 'text')
        self.assertNotIn('discovery', streamed)
        self.assertIn('继续执行对照', streamed)

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
        self.assertNotIn('report_discovery', {s['function']['name'] for s in self.agent.tool_schemas})
        self.assertNotIn('discovery_reporting_v1', self.state)
        self.assertNotIn('discovery_reporting_v3', self.state)
        self.assertNotIn('execution_data_guidance_v1', self.state)
        self.assertNotIn('failure_attribution_guidance_v1', self.state)

    async def test_normal_local_answer_streams_scene_and_persists_milestones_without_extra_calls(self):
        await self._assert_normal_public_progress()

    async def test_awdp_native_progress_uses_same_scene_and_milestone_contract(self):
        self.agent.ctx.mode = RunMode.AWDP
        await self._assert_normal_public_progress()

    async def test_competition_native_progress_uses_same_scene_and_milestone_contract(self):
        self.agent.ctx.mode = RunMode.COMPETITION
        await self._assert_normal_public_progress()

    async def _assert_normal_public_progress(self):
        self.agent.ctx.gateway.config.lightweight_local = False
        agent = CTFBaseAgent(self.agent.ctx, flag_manager=FlagManager(local_only=True))
        state = agent._inv_state(self.task, self.route)
        agent._seed_messages(self.task, self.route, self.plan, state)
        state['system_initialized'] = True
        agent._run_tool_call_inner = AsyncMock(return_value=_ToolOutcome(ok=True, text='fixture observation'))
        statements = [
            {'progress': '这是给定站点的 Web 题，先读取题面并确认入口。', 'exploration': {
                'intent': '理解题目入口', 'attempt': '读取题面', 'state': 'running'}},
            {'progress': '响应暴露了输入条件，下一步用对照核实。', 'exploration': {
                'intent': '理解题目入口', 'attempt': '读取题面', 'state': 'supported', 'milestone': {
                    'kind': 'discovery', 'title': '响应显示输入条件', 'basis': '题面响应包含明确输入限制',
                    'next': '用边界输入对照核实'}}}]

        from spiritscout.agents.profiles import EVIDENCE_FIRST_GUIDANCE

        stable_tools = copy.deepcopy(agent._model_tool_schemas())
        async def chat(messages, **kwargs):
            from spiritscout.gateway.progress import native_discovery_reminder

            self.assertEqual(messages[-1]['content'], EVIDENCE_FIRST_GUIDANCE + '\n' + native_discovery_reminder(state))
            self.assertEqual(kwargs['tools'], stable_tools)
            from spiritscout.gateway.context import (
                EXECUTION_DATA_GUIDANCE,
                FAILURE_ATTRIBUTION_GUIDANCE,
            )

            self.assertEqual(sum(m.get('content') == EXECUTION_DATA_GUIDANCE for m in messages), 1)
            self.assertEqual(sum(m.get('content') == FAILURE_ATTRIBUTION_GUIDANCE for m in messages), 1)
            self.assertEqual(sum(m.get('_progress_reminder', False) for m in state['messages']), state['turn'] - 1)
            value = json.dumps(statements[state['turn'] - 1], ensure_ascii=False)
            kwargs['on_delta']('text', value)
            return SimpleNamespace(content=value, tool_calls=[{
                'id': f"check-{state['turn']}", 'name': 'shell', 'arguments': {'command': 'fixture'}}])

        agent.ctx.gateway.chat = AsyncMock(side_effect=chat)
        for _ in statements:
            result = await agent.execute_step(self.task, self.route, self.plan, {'action': 'investigate'})
            self.assertTrue(result.ok)
        self.assertEqual(agent.ctx.gateway.chat.await_count, 2)
        stages = read_public_stages(self.root / 'audit.jsonl')['items']
        self.assertIn('Web 题', stages[0]['body'])
        self.assertEqual(stages[1]['exploration']['milestone']['title'], '响应显示输入条件')
        rows = self.bb.query(BlackboardLayer.TOPOLOGY, data_filter=('kind', 'exploration'))
        self.assertEqual(len(rows), 2)
        attempt = next(row['data'] for row in rows if row['data']['node_type'] == 'attempt')
        self.assertEqual(attempt['state'], 'supported')
        self.assertEqual(attempt['public_milestone']['next'], '用边界输入对照核实')
        from spiritscout.core.audit import read_record

        events = [read_record(line) for line in (self.root / 'audit.jsonl').read_text().splitlines()]
        self.assertTrue(any(e['type'] == 'agent.delta' and e['detail'].get('kind') == 'text' for e in events))
        progress = [e['detail'] for e in events if e['type'] == 'agent.progress']
        self.assertTrue(all(p['streamed'] for p in progress))

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
