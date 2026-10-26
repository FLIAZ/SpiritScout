"""督导任务隔离、指令持续可见与执行回执闭环。"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.ring import RingLedger
from spiritscout.agents.sub.base import RoutePlan
from spiritscout.core.models import AgentType, Task
from spiritscout.ctf.agents import CTFBaseAgent, _ToolOutcome
from spiritscout.gateway.context import compact_json


class SteeringRoutingTests(unittest.TestCase):
    def test_same_agent_tasks_keep_distinct_instructions(self) -> None:
        ring = RingLedger()
        for tid in ('a-05', 'a-13', 'a-17'):
            ring.steer('ctf_web', f'仅处理 {tid}', task_id=tid)
        for tid in ('a-05', 'a-13', 'a-17'):
            records = ring.steerings_for('ctf_web', task_id=tid)
            self.assertEqual([r['hint'] for r in records], [f'仅处理 {tid}'])
        self.assertIsNone(ring.steering_for('ctf_web', task_id='next'))
        self.assertEqual(ring.steerings_for('ctf_misc', task_id='a-13'), [])
        self.assertEqual(len(ring.snapshot()['task_steering']), 3)

    def test_feedback_is_scoped_and_old_instruction_is_rejected(self) -> None:
        audit = Mock()
        ring = RingLedger(audit)
        ring.steer('ctf_web', '定向读取证据', task_id='t1')
        rec = ring.steering_for('ctf_web', task_id='t1')
        args = dict(task_id='t1', route_id='r1', instruction_id=rec['id'])
        self.assertFalse(ring.steering_feedback('ctf_web', **{**args, 'task_id': 't2'},
                                               status='completed'))
        self.assertTrue(ring.steering_feedback('ctf_web', **args, status='accepted', detail='先读取证据'))
        ring.steering_feedback('ctf_web', **args, status='delivered')
        current = ring.steering_for('ctf_web', task_id='t1')
        self.assertEqual(current['feedback']['t1|r1']['status'], 'accepted')
        self.assertEqual(rec['feedback'], {})
        ring.steer('ctf_web', '更新后的指令', task_id='t1')
        self.assertFalse(ring.steering_feedback('ctf_web', **args, status='completed'))

    def test_master_rejects_ambiguous_or_wrong_recipient(self) -> None:
        master = object.__new__(MasterAgent)
        master.agent_id = 'master'
        master.ctx = SimpleNamespace(audit=Mock())
        tasks = [{'agent': 'ctf_web', 'task_id': 't1'},
                 {'agent': 'ctf_web', 'task_id': 't2'},
                 {'agent': 'ctf_misc', 'task_id': 't3'}]
        result = master._target_steering([
            {'agent': 'ctf_web', 'hint': '歧义指令'},
            {'agent': 'ctf_web', 'task_id': 't3', 'hint': '错误收件人'},
            {'agent': 'ctf_web', 'task_id': 't1', 'hint': '只读 t1 的证据'},
            {'agent': 'ctf_web', 'task_id': 't2', 'hint': '只读 t2 的证据'},
        ], tasks)
        self.assertEqual([r['task_id'] for r in result], ['t1', 't2'])

    def test_context_projection_keeps_all_recipients_and_feedback(self) -> None:
        protected = {
            'active_tasks': [{'task_id': f'task-{i}', 'agent': 'ctf_web'} for i in range(3)],
            'steering_status': {'task-1': {'status': 'blocked', 'detail': '缺少实际源码'}},
            '灵枢当前指令': [{'id': 'steer-1', 'hint': '先读取已保存的完整响应'}],
        }
        result = json.loads(compact_json({**protected, 'old_output': '旧数据' * 5000}, 300))
        for key, value in protected.items():
            self.assertEqual(result[key], value)


class SteeringExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stateless_decisions_do_not_consume_another_tasks_instruction(self) -> None:
        agent, state, task, route, plan = self._solver()
        agent.enable_llm_planning = True
        agent._system_prompt = Mock(return_value='系统约束')
        agent._world_state = Mock(return_value={})
        agent.ctx.blackboard = None
        agent.ctx.gateway.chat.return_value = SimpleNamespace(
            content='{}', tool_calls=[], finish_reason='stop')
        agent.ctx.ring.steer('ctf_web', '任务一当前指令', task_id='t1')
        agent.ctx.ring.steer('ctf_web', '任务二当前指令', task_id='t2')
        other = task.model_copy(update={'task_id': 't2'})
        for current in (task, other, task):
            result = await agent._llm_chat_json(
                role='verify', user_obj={}, task=current, _allow_ask=False)
            self.assertEqual(result, {})
            sent = agent.ctx.gateway.chat.call_args.args[0]
            payload = json.loads([m for m in sent if m['role'] == 'user'][0]['content'])['snapshot']
            self.assertEqual(payload['灵枢当前指令'][0]['task_id'], current.task_id)

    def _solver(self, *, first_turn: bool = False) -> tuple:
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = 'web-test'
        agent.agent_type = AgentType.CTF_WEB
        agent.direction = 'web'
        agent.flag_manager = None
        state = {'messages': [] if first_turn else [
            {'role': 'system', 'content': '系统约束'}, {'role': 'user', 'content': '题面'}],
            'seed': [], 'observations': [], 'flags': [], 'concluded': False, 'stall': '',
            'turn': 0, 'llm_failures': 0, 'empty_streak': 0, 'system_initialized': not first_turn,
            'fetches': {}, 'empty_replies': 0,
            'peer_seen': {'site': [], 'notes': 0, 'concl': 0, 'flags': []}}
        agent.cache = SimpleNamespace(get=lambda ns, key: state if key.startswith('inv|') else None,
                                      put=Mock())
        agent.ctx = SimpleNamespace(
            audit=Mock(), ring=RingLedger(), journal=None, builtin_ctx={},
            blackboard=SimpleNamespace(write=Mock(), read=Mock(return_value=None)),
            gateway=SimpleNamespace(chat=AsyncMock()))
        agent._inv_bootstrap = AsyncMock(return_value=(0, ''))
        agent._seed_messages = lambda task, route, plan, st: st['messages'].extend([
            {'role': 'system', 'content': '系统约束'}, {'role': 'user', 'content': '题面'}])
        task = Task(task_id='t1', task_type='ctf_solve', target='http://fixture.test',
                    agent_type=AgentType.CTF_WEB, payload={'challenge': {'id': 'c1'}})
        route = SimpleNamespace(name='solve', route_id='r1')
        plan = RoutePlan(name='solve', strategy='', steps=[{'action': 'investigate'}])
        return agent, state, task, route, plan

    @staticmethod
    def _reply(calls: list) -> SimpleNamespace:
        return SimpleNamespace(content='按任务指令处理', tool_calls=calls, finish_reason='tool_calls')

    async def test_first_turn_delivers_only_own_instruction_and_runs_changed_action(self) -> None:
        agent, state, task, route, plan = self._solver(first_turn=True)
        ring = agent.ctx.ring
        ring.steer('ctf_web', '读取本任务保存的完整响应，再报告证据', task_id='t1')
        own = ring.steering_for('ctf_web', task_id='t1')
        ring.steer('ctf_web', '别题才需要重新登录', task_id='t2')
        agent.ctx.gateway.chat.return_value = self._reply([
            {'id': 'ack', 'name': 'steering_feedback', 'arguments': {
                'instruction_id': own['id'], 'status': 'accepted', 'detail': '先读取保存文件'}},
            {'id': 'execute', 'name': 'shell', 'arguments': {
                'command': 'cat saved_response.txt', 'action': 'analysis'}}])
        agent._tool_sandbox = AsyncMock(return_value=_ToolOutcome(
            ok=True, note='读取完整响应', text='实际证据'))
        await agent._solve_turn(task, route, plan)
        sent = agent.ctx.gateway.chat.call_args.args[0]
        self.assertIn(own['hint'], sent[-1]['content'])
        self.assertNotIn('别题才需要重新登录', json.dumps(sent, ensure_ascii=False))
        agent._tool_sandbox.assert_awaited_once()
        self.assertEqual(agent._tool_sandbox.call_args.args[-1]['command'], 'cat saved_response.txt')
        status = ring.steering_for('ctf_web', task_id='t1')['feedback']['t1|r1']
        self.assertEqual(status['status'], 'accepted')
        self.assertFalse(any('灵枢当前任务指令' in m.get('content', '') for m in state['messages']))

    async def test_unfinished_instruction_persists_and_feedback_reaches_master(self) -> None:
        agent, state, task, route, plan = self._solver()
        ring = agent.ctx.ring
        ring.steer('ctf_web', '确认入口失效的证据后反馈', task_id='t1')
        own = ring.steering_for('ctf_web', task_id='t1')
        agent.ctx.gateway.chat.return_value = self._reply([{
            'id': 'ack', 'name': 'steering_feedback', 'arguments': {
                'instruction_id': own['id'], 'status': 'blocked', 'detail': '实例入口返回失效'}}])
        await agent._solve_turn(task, route, plan)
        await agent._solve_turn(task, route, plan)
        for call in agent.ctx.gateway.chat.call_args_list:
            self.assertIn(own['hint'], call.args[0][-1]['content'])
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(
            ring=ring, extra={}, blackboard=SimpleNamespace(read=lambda layer, tid: {
                'task': {**task.model_dump(mode='json'), 'status': 'running'}, 'routes': []}))
        progress = master._progress_digest(['t1'])
        self.assertEqual(progress['tasks'][0]['steering'][0]['feedback']['t1|r1']['status'], 'blocked')
        ring.steering_feedback('ctf_web', task_id='t1', route_id='r1',
                               instruction_id=own['id'], status='completed', detail='已报告证据')
        self.assertEqual(agent._active_steering(task, route), [])
        self.assertEqual(len(agent._active_steering(task, SimpleNamespace(route_id='r2'))), 1)

    async def test_issuing_instructions_does_not_retrigger_supervision_without_progress(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(audit=Mock(), ring=RingLedger())
        master._STEER_INTERVAL = 0.001
        checked = asyncio.Event()
        reads = 0

        def digest(ids: list) -> dict:
            nonlocal reads
            reads += 1
            if reads > 5:
                checked.set()
            return {'tasks': [{'agent': 'ctf_web', 'task_id': 't1', 'routes': [],
                               'steering': master.ctx.ring.steerings_for('ctf_web', task_id='t1')}]}

        master._progress_digest = digest
        master._llm_steer = AsyncMock(return_value=[{
            'agent': 'ctf_web', 'task_id': 't1', 'hint': '先读取证据'}])
        watcher = asyncio.create_task(master._steering_watch(['t1']))
        try:
            await asyncio.wait_for(checked.wait(), 1)
            master._llm_steer.assert_awaited_once()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
