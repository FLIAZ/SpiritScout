"""重复动作进入一次模型行动复核；动作、重试与退出仍由模型选择。"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_single_executor import context

from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.agents import _ToolOutcome
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.light_memory import tool_records, working_snapshot
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.tools.invocation import current_execution_scope


class LoopRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ctx = context(RunMode.CTF)
        ctx.audit, ctx.ring, ctx.journal = Mock(), None, None
        ctx.gateway.context_budget = lambda **kw: 16000
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))
        self.agent._world_snapshot = lambda: {'user_context': {'mission': 'ORIGINAL_GOAL'}}
        self.task = Task(task_id='one', agent_type=self.agent.agent_type, target='', task_type='ctf_single',
                         payload={'challenge': {'id': 'one'}, 'mission': 'ORIGINAL_GOAL'})
        self.route = SimpleNamespace(route_id='route', name='solve')
        self.state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, None, self.state)
        self.state.update(turn=6, system_initialized=True,
                          user_feed=[{'text': 'LATEST_USER_REQUIREMENT', 'statement_id': 'latest'}])

    def enter_recovery(self):
        self.state.update(last_evidence_request={'count': 2},
                          review_due={'tool': 'python', 'same_result_count': 2, 'reason': 'same result'})
        return self.agent._light_model_messages(self.task, self.route, self.state)

    def test_same_results_change_request_contract_and_keep_latest_user_requirement(self):
        original = copy.deepcopy(self.state['messages'])
        sent = self.enter_recovery()
        tools = self.agent._model_tool_schemas()
        available = [s['function']['name'] for s in tools]
        self.assertTrue({'python', 'shell', 'history_read', 'tool_lookup', 'finish_challenge'} <= set(available))
        self.assertNotIn('revise_step', available)
        self.assertIn('ORIGINAL_GOAL', str(sent))
        self.assertIn('原始用户目标和附件说明', str(sent))
        self.assertIn('LATEST_USER_REQUIREMENT', sent[-1]['content'])
        self.assertEqual(self.state['messages'], original)
        # 复盘提示已消费，工具契约仍保留到模型真正选择一个动作。
        self.assertIsNone(self.state['review_due'])
        self.agent._light_model_messages(self.task, self.route, self.state)
        self.assertIn('python', {s['function']['name'] for s in self.agent._model_tool_schemas()})

    def test_changed_results_keep_normal_tool_calls(self):
        self.state.update(last_evidence_request={'count': 3},
                          review_due={'tool': 'python', 'same_request_count': 3, 'same_result_count': 1})
        self.agent._light_model_messages(self.task, self.route, self.state)
        self.assertFalse(self.state.get('loop_recovery'))
        self.assertIn('python', {s['function']['name'] for s in self.agent._model_tool_schemas()})

    def test_changed_verification_inputs_with_same_issue_archive_old_plans_only_in_view(self):
        self.agent.ctx.mode = RunMode.AWDP
        self.agent.flag_manager.fix_mode = True
        for index in range(2):
            args = {'archive': 'same.tar.gz', 'tests': [{'name': f'changed-{index}'}]}
            call = {'name': 'verify_patch', 'arguments': args}
            self.state['messages'].extend([
                {'role': 'assistant', 'content': f'STALE_PLAN_{index}', 'tool_calls': [{
                    'id': str(index), 'function': {'name': 'verify_patch', 'arguments': json.dumps(args)}}]},
                {'role': 'tool', 'tool_call_id': str(index), 'content': json.dumps({
                    'ok': False, 'status': 'inconclusive', 'issues': ['output mismatch']})}])
            outcome = _ToolOutcome(ok=False, text=str(index), metadata={
                'verification_failure': str(index), 'verification_issues': ['before:functional:stdout_mismatch']})
            self.agent._observe_tool_result(self.state, call, outcome)
        self.assertEqual(self.state['review_due']['same_issue_count'], 2)
        original = copy.deepcopy(self.state['messages'])
        sent = self.agent._light_model_messages(self.task, self.route, self.state)
        self.assertNotIn('STALE_PLAN_0', str(sent))
        self.assertIn('STALE_PLAN_1', str(sent))
        self.assertIn('LATEST_USER_REQUIREMENT', str(sent))
        self.assertEqual(self.state['messages'], original)
        self.assertIn('loop_recovery', self.state)

    async def test_awdp_changed_outputs_review_repeated_request_without_blocking_poll(self):
        self.agent.ctx.mode = RunMode.AWDP
        self.agent.flag_manager.fix_mode = True
        self.state.update(last_evidence_request={'count': 3}, review_due={
            'tool': 'shell', 'same_request_count': 3, 'same_result_count': 1})
        messages = self.agent._light_model_messages(self.task, self.route, self.state)
        self.assertIn('loop_recovery', self.state)
        self.assertIn('参数重复而输出不同也可能是正常轮询', str(messages))
        self.agent._tool_sandbox = AsyncMock(return_value=_ToolOutcome(ok=True, text='new status'))
        result = await self.agent._run_tool_call(self.task, self.route, self.state, {
            'name': 'shell', 'arguments': {'action': 'analysis', 'command': 'read fixture status'}})
        self.assertTrue(result.ok)
        self.assertFalse(self.state['concluded'])
        self.agent._tool_sandbox.assert_awaited_once()

    async def test_chosen_action_uses_real_dispatch_scope_and_has_normal_history_source(self):
        self.enter_recovery()
        scopes = []

        async def sandbox(task, route, state, name, args):
            scopes.append(current_execution_scope.get())
            return _ToolOutcome(ok=True, text='REAL_RESULT', metadata={'invocation_id': 'inv-actual'})

        self.agent._tool_sandbox = AsyncMock(side_effect=sandbox)
        args = {'requirement': '原题要求核对完整输入', 'adjustment': '修正遗漏的输入部分',
                'name': 'python', 'arguments': {'action': 'analysis', 'code': 'print("new observation")'}}
        call = {'id': 'revised', 'name': 'revise_step', 'arguments': args}
        self.state['messages'].append({'role': 'assistant', 'tool_calls': [{'id': 'revised', 'type': 'function',
            'function': {'name': 'revise_step', 'arguments': json.dumps(args)}}]})
        result = await self.agent._run_tool_call(self.task, self.route, self.state, call)
        self.assertTrue(result.ok)
        self.assertEqual(scopes[0].tool_name, 'python')
        self.assertEqual(scopes[0].task_id, 'one')
        self.assertEqual(self.agent._tool_sandbox.call_args.args[-1], args['arguments'])
        self.state['messages'].append({'role': 'tool', 'tool_call_id': 'revised', 'content': result.text})
        self.agent._observe_tool_result(self.state, call, result)
        row = tool_records(self.state['messages'])[-1]
        self.assertEqual(row['tool'], 'python')
        self.assertEqual(row['arguments'], args['arguments'])
        self.assertEqual(row['message']['_light_observation']['invocation_id'], 'inv-actual')
        self.assertEqual(working_snapshot(self.state)['last_recovery']['invocation_id'], 'inv-actual')
        self.assertNotIn('loop_recovery', self.state)
        self.assertIn('python', {s['function']['name'] for s in self.agent._model_tool_schemas()})
        self.assertFalse(self.state['concluded'])

    async def test_invalid_recovery_does_not_execute_or_finish_and_legitimate_retry_is_allowed(self):
        self.enter_recovery()
        self.agent._tool_sandbox = AsyncMock(return_value=_ToolOutcome(ok=True, text='observed again'))
        direct = {'name': 'python', 'arguments': {'action': 'analysis', 'code': 'print(1)'}}
        args = {'requirement': '等待服务状态变化', 'adjustment': '用户要求轮询，读取现在的状态作时序对照',
                'name': 'python', 'arguments': direct['arguments']}
        for invalid in ({**args, 'requirement': ''}, {**args, 'name': 'revise_step'}, {**args, 'arguments': 'bad'}):
            outcome = await self.agent._run_tool_call_inner(self.task, self.route, self.state,
                {'name': 'revise_step', 'arguments': invalid})
            self.assertFalse(outcome.ok)
            self.agent._tool_sandbox.assert_not_awaited()
            self.assertIn('loop_recovery', self.state)
        retry = await self.agent._run_tool_call(self.task, self.route, self.state,
            {'id': 'retry', 'name': 'revise_step', 'arguments': args})
        self.assertTrue(retry.ok)
        self.agent._tool_sandbox.assert_awaited_once()
        self.assertFalse(self.state['concluded'])

    async def test_interrupted_recovery_keeps_decision_pending(self):
        self.enter_recovery()
        self.agent._tool_sandbox = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.agent._run_tool_call(self.task, self.route, self.state, {'name': 'revise_step', 'arguments': {
                'requirement': '原始目标', 'adjustment': '重新读取现场', 'name': 'python',
                'arguments': {'action': 'analysis', 'code': 'print(1)'}}})
        self.assertIn('loop_recovery', self.state)
        self.assertIn('python', {s['function']['name'] for s in self.agent._model_tool_schemas()})

    async def test_direct_action_can_escape_old_recovery_and_missing_tool_has_real_catalog(self):
        self.enter_recovery()
        self.agent._tool_sandbox = AsyncMock(return_value=_ToolOutcome(ok=True, text='real result'))
        invalid = await self.agent._run_tool_call(self.task, self.route, self.state, {
            'name': 'revise_step', 'arguments': {'requirement': '读取附件', 'adjustment': '获取完整原文',
                'name': 'source_read', 'arguments': {}}})
        feedback = json.loads(invalid.text)
        self.assertFalse(invalid.ok)
        self.assertEqual(feedback['recovery_call'], {'name': 'tool_lookup', 'arguments': {}})
        self.assertIn('python', feedback['available_tools'])
        outcome = await self.agent._run_tool_call(self.task, self.route, self.state, {
            'name': 'python', 'arguments': {'action': 'analysis', 'code': 'print(1)'}})
        self.assertTrue(outcome.ok)
        self.assertTrue(self.state['last_loop_recovery']['direct'])
        self.assertNotIn('loop_recovery', self.state)
        self.agent._tool_sandbox.assert_awaited_once()

    async def test_cancelled_direct_recovery_preserves_pending_state_and_can_retry(self):
        self.enter_recovery()
        pending = copy.deepcopy(self.state['loop_recovery'])
        self.agent._tool_sandbox = AsyncMock(side_effect=asyncio.CancelledError())
        call = {'name': 'python', 'arguments': {'action': 'analysis', 'code': 'print(1)'}}
        with self.assertRaises(asyncio.CancelledError):
            await self.agent._run_tool_call(self.task, self.route, self.state, call)
        self.assertEqual(self.state['loop_recovery'], pending)
        self.agent._tool_sandbox = AsyncMock(return_value=_ToolOutcome(ok=True, text='observed'))
        result = await self.agent._run_tool_call(self.task, self.route, self.state, call)
        self.assertTrue(result.ok)
        self.assertNotIn('loop_recovery', self.state)

    async def test_message_without_execution_log_returns_actual_content_and_source_read_calls(self):
        read = {'name': 'source_manifest', 'arguments': {'action': 'read', 'source_ref': 'actual-source'}}
        self.state['awdp_originals'] = {'files': [{'read_call': read}]}
        outcome = await self.agent._run_tool_call(self.task, self.route, self.state, {
            'name': 'history_read', 'arguments': {'view': 'output', 'message_index': 1}})
        data = json.loads(outcome.text)
        self.assertFalse(outcome.ok)
        self.assertEqual(data['recovery_call'], {'name': 'history_read', 'arguments': {
            'view': 'content', 'message_index': 1}})
        self.assertEqual(data['source_calls'], [read])


if __name__ == '__main__':
    unittest.main()
