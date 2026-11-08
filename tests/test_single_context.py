"""单执行者按需工具、连续回合与小窗口发送视图回归。"""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_single_executor import context

from spiritscout.agents.sub.base import RoutePlan
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.agents import _ToolOutcome
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.gateway.light_context import project_context
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.resources.runtime import SkillRuntime
from spiritscout.resources.store import ResourceStore
from spiritscout.tools.builtin import file_analyze
from spiritscout.tools.invocation import current_execution_scope
from spiritscout.tools.journal import ExecutionJournal


class SingleContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ctx = context(RunMode.CTF)
        ctx.ring = None
        ctx.journal = None
        ctx.audit = Mock()
        ctx.gateway.context_budget = lambda **kw: 10650
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))
        self.task = Task(task_id="one", agent_type=self.agent.agent_type, task_type="ctf_solve", target="",
            payload={"challenge": {"id": "one"}, "mission": "USER_GOAL", "rules": "ORIGINAL_RULE"})
        self.route = SimpleNamespace(route_id="r", name="solve")
        self.world = {"user_context": {"mission": "USER_GOAL"},
                      "authorization": {"testing_rules": "ORIGINAL_RULE"}}
        self.agent._world_snapshot = lambda: self.world

    async def test_image_tool_remains_directly_available_in_compact_requests(self):
        for minimal in (False, True):
            self.agent._minimal_tool_view = minimal
            names = {s['function']['name'] for s in self.agent._model_tool_schemas()}
            self.assertIn('view_image', names)
        self.agent.ctx.tools = SimpleNamespace(call=AsyncMock(return_value=SimpleNamespace(
            ok=True, stdout='左侧蓝色圆形', error='', invocation_id='image-observation',
            parsed={'images': [{'path': 'artifacts/view-images/proof.png'}]})))
        result = await self.agent._run_tool_call_inner(self.task, self.route, {},
            {'name': 'view_image', 'arguments': {'path': 'attachments/question.png'}})
        self.assertTrue(result.ok)
        self.assertIn('蓝色圆形', result.text)
        self.assertEqual(result.metadata['invocation_id'], 'image-observation')

    async def test_llama_recovery_requires_native_action_and_mixed_candidates_keep_auto(self):
        local = ModelEntry(model='llama', local_runtime='llama_cpp')
        self.agent.ctx.gateway.registry = ModelRegistry([local])
        self.agent.ctx.gateway.resolve_model = lambda **kwargs: local
        call = {'id': 'finish', 'name': 'finish_challenge',
                'arguments': {'complete': False, 'reason': '缺少附件'}}
        self.agent.ctx.gateway.chat = AsyncMock(return_value=SimpleNamespace(
            content='', tool_calls=[call], finish_reason='tool_calls'))
        self.agent.task_effort = lambda task: ''
        state = {'messages': [{'role': 'user', 'content': '原始用户要求'}]}
        result = await self.agent._forced_tool_recovery(self.task, self.route, state, 1)
        self.assertEqual(result, [call])
        options = self.agent.ctx.gateway.chat.call_args.kwargs
        self.assertEqual(options['tool_choice'], 'required')
        self.assertIn('finish_challenge', {s['function']['name'] for s in options['tools']})
        self.agent.ctx.gateway.registry = ModelRegistry([
            local, ModelEntry(model='ollama', provider='ollama')])
        self.assertEqual(self.agent._recovery_tool_choice(), 'required')
        self.agent.ctx.gateway.registry = ModelRegistry([
            local, ModelEntry(model='ollama', provider='ollama', is_fallback_for=[local.key])])
        self.assertEqual(self.agent._recovery_tool_choice(), 'auto')

    async def test_concurrent_and_deferred_results_have_readable_distinct_history_references(self):
        second_finished = asyncio.Event()

        async def run(task, route, state, name, args):
            if name == 'shell':
                await second_finished.wait()
            else:
                second_finished.set()
            return _ToolOutcome(ok=True, text=name + '_ACTUAL_RESULT', metadata={'invocation_id': 'inv-' + name})

        self.agent._tool_sandbox = run
        self.agent.ctx.gateway.chat = AsyncMock(return_value=SimpleNamespace(content='', tool_calls=[
            {'id': 'first', 'name': 'shell', 'arguments': {'command': 'fixture', 'action': 'analysis'}},
            {'id': 'second', 'name': 'tool_invoke', 'arguments': {
                'name': 'python', 'arguments': {'code': 'fixture', 'action': 'analysis'}}}]))
        state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, None, state)
        state['system_initialized'] = True
        result = await self.agent._solve_turn(self.task, self.route,
            RoutePlan(name='solve', strategy='', steps=[{'action': 'investigate'}]))
        self.assertTrue(result.ok)
        self.assertEqual(len(state['evidence_rows']), 2)
        for row, name, call_id in zip(state['evidence_rows'], ('shell', 'python'), ('first', 'second')):
            self.assertEqual(row['name'], name)
            read = await self.agent._run_tool_call_inner(self.task, self.route, state, {
                'name': 'history_read', 'arguments': {'message_index': row['message_index']}})
            original = json.loads(json.loads(read.text)['text'])
            self.assertEqual(original['role'], 'tool')
            self.assertEqual(original['tool_call_id'], call_id)
            self.assertEqual(original['content'], name + '_ACTUAL_RESULT')
            self.assertEqual(row['same_result_count'], 1)

    async def test_history_read_with_many_escapes_remains_valid_json_after_a_turn(self):
        state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, None, state)
        state['system_initialized'] = True
        state['messages'].extend([
            {'role': 'assistant', 'tool_calls': [{'id': 'source', 'function': {
                'name': 'python', 'arguments': '{"code":"fixture","action":"analysis"}'}}]},
            {'role': 'tool', 'tool_call_id': 'source', 'content': '\\"' * 3000}])
        source_index = len(state['messages']) - 1
        source = copy.deepcopy(state['messages'][source_index])
        self.agent.ctx.gateway.chat = AsyncMock(return_value=SimpleNamespace(content='', tool_calls=[{
            'id': 'read', 'name': 'history_read', 'arguments': {'message_index': source_index, 'length': 4000}}]))
        result = await self.agent._solve_turn(self.task, self.route,
            RoutePlan(name='solve', strategy='', steps=[{'action': 'investigate'}]))
        self.assertTrue(result.ok)
        returned = state['messages'][-1]['content']
        self.assertGreater(len(returned), 6000)
        page = json.loads(returned)
        self.assertEqual(page['text'], json.dumps(source, ensure_ascii=False, separators=(',', ':'))[:4000])
        self.assertTrue(page['has_more'])

    async def test_followup_prose_only_turn_recovers_a_real_tool_action(self):
        self.task.payload['followup_goal'] = True
        state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, None, state)
        state['system_initialized'] = True
        self.agent.ctx.gateway.chat = AsyncMock(side_effect=[
            SimpleNamespace(content='{"progress":"准备继续读取日志"}', tool_calls=[]),
            SimpleNamespace(content='', tool_calls=[{'id': 'actual-read', 'name': 'tool_lookup', 'arguments': {}}])])
        result = await self.agent._solve_turn(self.task, self.route,
            RoutePlan(name='solve', strategy='', steps=[{'action': 'investigate'}]))
        self.assertTrue(result.ok)
        self.assertEqual(self.agent.ctx.gateway.chat.await_count, 2)
        self.assertEqual(state['messages'][-1]['role'], 'tool')
        self.assertEqual(state['messages'][-1]['tool_call_id'], 'actual-read')
        self.assertIn('tools', json.loads(state['messages'][-1]['content']))
        self.assertFalse(state['concluded'])


    async def test_deferred_catalog_returns_complete_schemas_without_growing_request(self):
        before = copy.deepcopy(self.agent._model_tool_schemas())
        directory = await self.agent._run_tool_call_inner(self.task, self.route, {},
            {"name": "tool_lookup", "arguments": {}})
        names = json.loads(directory.text)["tools"]
        self.assertTrue({"http_request", "mcp_search", "skill_open", "cve_lookup", "shell_start"} <= set(names))
        for name in names:
            result = await self.agent._run_tool_call_inner(self.task, self.route, {},
                {"name": "tool_lookup", "arguments": {"name": name}})
            self.assertTrue(result.ok)
            original = next(s for s in self.agent.tool_schemas if s["function"]["name"] == name)
            self.assertEqual(json.loads(result.text), original)
        self.assertEqual(self.agent._model_tool_schemas(), before)

    async def test_evicted_skill_is_reread_from_real_store_instead_of_claimed_still_in_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResourceStore(Path(directory))
            store.import_skills([{"skill_id": f"fixture-{index}", "description": "reference", "enabled": True,
                "body": f"# Skill {index}\n" + "reference material " * 700} for index in range(5)])
            runtime = SkillRuntime(store)
            self.agent._skill_runtime = lambda: runtime
            state = {"messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "USER_GOAL"}],
                     "skills": {}, "world_state": self.world}
            refs = []
            for index in range(5):
                result = runtime.execute('skill_open', {'skill_id': f'fixture-{index}'}, state['skills'])
                self.assertTrue(result['ok'], result)
                refs.append(result['ref'])
                state['messages'].extend([
                    {'role': 'assistant', 'tool_calls': [{'id': str(index), 'function': {
                        'name': 'skill_open', 'arguments': json.dumps({'skill_id': f'fixture-{index}'})}}]},
                    {'role': 'tool', 'tool_call_id': str(index), 'content': json.dumps(result),
                     '_context_contract': True, '_skill_ref': result['ref']}])
            original = copy.deepcopy(state['messages'])
            sent = self.agent._light_model_messages(self.task, self.route, state)
            self.assertLessEqual(estimate_input_tokens(sent, self.agent._model_tool_schemas()), 10650)
            self.assertNotIn(refs[0], state['model_skill_refs'])
            result = await self.agent._run_tool_call_inner(self.task, self.route, state,
                {'name': 'skill_open', 'arguments': {'skill_id': 'fixture-0'}})
            body = json.loads(result.text)
            self.assertIn('content', body)
            self.assertNotIn('already_in_context', body)
            self.assertTrue(body['content'].startswith('# Skill 0'))
            self.assertEqual(state['messages'], original)

    async def test_deferred_call_uses_existing_dispatch_and_actual_execution_scope(self):
        arguments = {"path": "/fixture", "method": "POST", "action": "intrusive_write", "data": "a=1"}

        async def http(task, route, state, args):
            self.assertEqual(args, arguments)
            self.assertEqual(current_execution_scope.get().tool_name, "http_request")
            return _ToolOutcome(ok=True, text="fixture result", metadata={"invocation_id": "evidence-one"})

        self.agent._tool_http = AsyncMock(side_effect=http)
        result = await self.agent._run_tool_call_inner(self.task, self.route, {"messages": []},
            {"id": "call-one", "name": "tool_invoke", "arguments": {
                "name": "http_request", "arguments": arguments}})
        self.assertTrue(result.ok)
        self.assertEqual(result.metadata["invoked_tool"], "http_request")
        self.assertEqual(result.metadata["invocation_id"], "evidence-one")
        self.assertIsNone(current_execution_scope.get())

    async def test_invalid_deferred_calls_are_errors_without_execution(self):
        self.agent._tool_http = AsyncMock()
        for arguments in (None, [], {"name": []}, {"name": "unknown", "arguments": {}},
                          {"name": "tool_invoke", "arguments": {}}, {"name": "http_request", "arguments": []}):
            result = await self.agent._run_tool_call_inner(self.task, self.route, {},
                {"name": "tool_invoke", "arguments": arguments})
            self.assertFalse(result.ok)
        self.agent._tool_http.assert_not_awaited()

    async def test_checkpoint_restore_keeps_full_history_and_recomputes_same_view(self):
        state = {'seed': [], 'world_state': self.world}
        self.agent._seed_messages(self.task, self.route, None, state)
        for index in range(12):
            state['messages'].extend([
                {'role': 'assistant', 'tool_calls': [{'id': str(index), 'function': {
                    'name': 'shell', 'arguments': '{"command":"fixture"}'}}]},
                {'role': 'tool', 'tool_call_id': str(index), 'content': 'observed 中文 output ' * 300}])
        first = self.agent._light_model_messages(self.task, self.route, state)
        raw = copy.deepcopy(state['messages'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bb = Blackboard(root / 'bb.db')
            try:
                journal = ExecutionJournal(bb, root, 'qa-context')
                journal.checkpoint('ctf_conversation', 'one:r', 'turn_saved', state)
                restored = journal.latest_checkpoint('ctf_conversation', 'one:r')['state']
                fresh = SingleCTFAgent(self.agent.ctx, flag_manager=FlagManager(local_only=True))
                fresh._world_snapshot = lambda: self.world
                second = fresh._light_model_messages(self.task, self.route, restored)
                self.assertEqual(first, second)
                self.assertEqual(restored['messages'], raw)
                self.assertEqual(restored['model_skill_refs'], state['model_skill_refs'])
            finally:
                await bb.aclose()

    async def test_candidate_does_not_complete_until_a_later_turn_with_model_review(self):
        self.agent._record_flag = Mock()
        state = {"turn": 3, "messages": [{"role": "system", "content": "rules"},
            {"role": "user", "content": "USER_GOAL"}], "world_state": self.world}
        candidate = {"answer": "intermediate-value", "evidence_refs": ["inv-transform"]}
        call = {"name": "record_answer", "arguments": candidate}
        result = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertFalse(result.stop)
        self.assertEqual(self.agent.flag_manager.all(), [])
        self.assertFalse(self.agent.flag_manager.completed_ids())
        sent = self.agent._light_model_messages(self.task, self.route, state)
        self.assertIn("当前待复核候选", str(sent))
        self.assertIn("附件说明", str(sent))
        review = {"requirements": "fixture instructions define expected output", "derivation": "complete fixture chain",
                  "validation": "observed fixture output, inv-confirmed"}
        call["arguments"] = {**candidate, "review": review}
        same_turn = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertFalse(same_turn.stop)
        self.assertEqual(self.agent.flag_manager.all(), [])
        state['turn'] += 1
        call['arguments'] = {**candidate, "review": {"requirements": "only one field"}}
        incomplete = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertFalse(incomplete.stop)
        call['arguments'] = {**candidate, "review": review}
        no_proof = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertFalse(no_proof.stop)
        self.assertFalse(no_proof.ok)
        self.assertFalse(self.agent.flag_manager.completed_ids())
        state['evidence_rows'] = [{'name': 'python', 'ok': True, 'invocation_id': 'inv-confirmed', 'turn': state['turn']}]
        state['turn'] += 1
        review['validation_evidence_refs'] = ['inv-confirmed']
        complete = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertTrue(complete.stop)
        self.assertTrue(self.agent.flag_manager.completed_ids())
        self.assertNotIn('answer_review', state)
        self.assertFalse(self.agent.flag_manager.all()[0].submitted)

    async def test_small_archive_texts_are_visible_in_first_model_request_without_extracting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.zip"
            with zipfile.ZipFile(path, 'w') as bundle:
                bundle.writestr('data.txt', 'encoded fixture')
                bundle.writestr('README.txt', '题目要求：还原全部变换，以最终文本为准。')
                bundle.writestr('huge.txt', 'x' * 9000)
                bundle.writestr('binary.bin', b'\x00\xff\x01')
            parsed = (await file_analyze(str(path), preview_archive_text=True)).parsed
            self.assertNotIn('archive', (await file_analyze(str(path))).parsed)
            archive = parsed['archive']
            rows = {row['name']: row for row in archive['entries']}
            self.assertIn('全部变换', rows['README.txt']['text'])
            self.assertNotIn('text', rows['huge.txt'])
            self.assertNotIn('text', rows['binary.bin'])
            state = {'seed': [], 'attachment_observations': [{'path': str(path), 'archive': archive}]}
            self.agent._seed_messages(self.task, self.route, None, state)
            sent = self.agent._light_model_messages(self.task, self.route, state)
            self.assertIn('全部变换', str(sent))
            self.assertFalse((Path(directory) / 'README.txt').exists())

    async def test_pre_registration_validation_is_reused_after_a_separate_model_review(self):
        self.agent._record_flag = Mock()
        # 实测复现：第 89 回合验证成功，第 90 回合才首次调用 record_answer。
        state = {'turn': 90, 'messages': [], 'evidence_rows': [
            {'name': 'python', 'invocation_id': 'inv-verified', 'ok': True, 'turn': 89,
             'output_file': '/work/output-verified.log'}]}
        args = {'answer': 'fixture', 'evidence_refs': ['original-observation'], 'review': {
            'requirements': '核对原文件中的两个字段', 'derivation': '逐页读取原始输出并组合两个字段',
            'validation': 'Python 从原文件取值，两个断言实际通过', 'validation_evidence_refs': ['inv-verified']}}
        call = {'name': 'record_answer', 'arguments': args}
        candidate = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertTrue(candidate.ok)
        self.assertFalse(candidate.stop)
        self.assertIn('登记前已完成的检查也可引用', candidate.text)
        same_turn = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertFalse(same_turn.stop)
        self.assertFalse(self.agent.flag_manager.all())
        state['turn'] = 91
        result = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertTrue(result.ok)
        self.assertTrue(result.stop)
        self.assertTrue(self.agent.flag_manager.completed_ids())
        self.assertEqual(state['evidence_rows'][0]['turn'], 89)
        audit = next(call.args[2] for call in self.agent.ctx.audit.log.call_args_list
                     if call.args[0] == 'solver.answer_review')
        self.assertEqual(audit['review']['validation_evidence_refs'], ['inv-verified'])

    async def test_candidate_validation_rejects_failed_unread_same_turn_and_invented_receipts(self):
        state = {'turn': 3, 'messages': []}
        candidate = {'answer': 'fixture', 'evidence_refs': ['derivation']}
        await self.agent._run_tool_call_inner(self.task, self.route, state,
            {'name': 'record_answer', 'arguments': candidate})
        state['turn'] = 5
        state['evidence_rows'] = [
            {'invocation_id': 'unread', 'ok': True, 'turn': 0},
            {'invocation_id': 'failed', 'ok': False, 'turn': 4},
            {'invocation_id': 'partial', 'ok': True, 'turn': 4, 'partial': True},
            {'name': 'tool_help', 'invocation_id': 'help-only', 'ok': True, 'turn': 4},
            {'invocation_id': 'same-turn', 'ok': True, 'turn': 5}]
        for row in state['evidence_rows']:
            row['output_file'] = '/work/' + row['invocation_id'] + '.log'
        for reference in ('unread', 'failed', 'partial', 'help-only', 'same-turn', 'invented',
                          'unread.log', 'failed.log', 'partial.log', 'help-only.log', 'same-turn.log'):
            result = await self.agent._run_tool_call_inner(self.task, self.route, state,
                {'name': 'record_answer', 'arguments': {**candidate, 'review': {
                    'requirements': 'fixture', 'derivation': 'fixture', 'validation': 'fixture',
                    'validation_evidence_refs': [reference]}}})
            self.assertFalse(result.ok, reference)
            self.assertFalse(result.stop, reference)
            detail = json.loads(result.text[result.text.index('{'):])
            self.assertEqual(detail['invalid_validation_refs'][0]['ref'], reference)
            self.assertTrue(detail['invalid_validation_refs'][0]['reason'])
        finish = await self.agent._run_tool_call_inner(self.task, self.route, state,
            {'name': 'finish_challenge', 'arguments': {'complete': True, 'answer_ids': ['fixture']}})
        self.assertFalse(finish.ok)
        self.assertIn('尚未', finish.text)
        self.assertFalse(self.agent.flag_manager.completed_ids())

    async def test_validation_log_aliases_resolve_to_unique_real_receipts(self):
        self.agent._record_flag = Mock()
        row = {'name': 'python', 'invocation_id': 'inv-check', 'ok': True, 'turn': 4,
               'output_file': '/work/output-check.log'}
        state = {'turn': 5, 'messages': [], 'answer_review': {'answer': 'fixture', 'turn': 3},
                 'evidence_rows': [row], 'evidence_seen': {'same': {'first_observation': dict(row)}}}
        aliases = ['output-check.log', '/work/output-check.log', 'inv-check']
        result = await self.agent._run_tool_call_inner(self.task, self.route, state,
            {'name': 'record_answer', 'arguments': {'answer': 'fixture', 'evidence_refs': ['source'],
                'review': {'requirements': 'fixture', 'derivation': 'fixture', 'validation': 'assert passed',
                           'validation_evidence_refs': aliases}}})
        self.assertTrue(result.ok)
        self.assertTrue(result.stop)
        audit = next(call.args[2] for call in self.agent.ctx.audit.log.call_args_list
                     if call.args[0] == 'solver.answer_review')
        self.assertEqual(audit['review']['validation_evidence_refs'], ['inv-check'])
        self.assertEqual(audit['review']['original_validation_evidence_refs'], aliases)

    async def test_ambiguous_log_basename_returns_real_receipts_without_rerunning(self):
        self.agent._record_flag = Mock()
        state = {'turn': 10, 'messages': [], 'answer_review': {'answer': 'fixture', 'turn': 3},
                 'evidence_rows': [{'name': 'python', 'invocation_id': 'inv-' + str(turn),
                     'ok': True, 'turn': turn, 'output_file': f'/work/{turn}/output.log'}
                     for turn in range(4, 10)]}
        review = {'requirements': 'fixture', 'derivation': 'fixture', 'validation': 'fixture',
                  'validation_evidence_refs': ['output.log']}
        call = {'name': 'record_answer', 'arguments': {'answer': 'fixture', 'evidence_refs': ['source'],
                                                      'review': review}}
        rejected = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertFalse(rejected.ok)
        self.assertIn('勿仅为换取 ID 重跑', rejected.text)
        available = json.loads(rejected.text[rejected.text.index('{'):])['available_validation_receipts']
        self.assertEqual([row['invocation_id'] for row in available], ['inv-6', 'inv-7', 'inv-8', 'inv-9'])
        self.assertFalse(self.agent.flag_manager.completed_ids())
        review['validation_evidence_refs'] = ['/work/9/output.log']
        accepted = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertTrue(accepted.ok)
        self.assertTrue(accepted.stop)

    async def test_message_references_resolve_only_to_the_actual_validation_execution(self):
        self.agent._record_flag = Mock()
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'goal'},
            {'role': 'assistant', 'tool_calls': [{'id': 'check', 'function': {'name': 'python', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'check', 'content': '执行记录 inv-check\nverified'},
            {'role': 'assistant', 'tool_calls': [{'id': 'read', 'function': {'name': 'history_read', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'read', 'content': 'old output page'}]
        state = {'turn': 10, 'messages': messages, 'answer_review': {'answer': 'fixture', 'turn': 9},
                 'evidence_rows': [{'name': 'python', 'invocation_id': 'inv-check', 'ok': True,
                                    'turn': 8, 'message_index': 2}]}
        args = {'answer': 'fixture', 'evidence_refs': ['3'], 'review': {
            'requirements': 'fixture', 'derivation': 'fixture', 'validation': 'verified',
            'validation_evidence_refs': ['5']}}
        call = {'name': 'record_answer', 'arguments': args}
        rejected = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertFalse(rejected.ok)  # 原输出回读页不是另一条验证执行。
        args['review']['validation_evidence_refs'] = ['3', 'msg-3', 'message:3']
        accepted = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
        self.assertTrue(accepted.stop)
        audit = next(call.args[2] for call in self.agent.ctx.audit.log.call_args_list
                     if call.args[0] == 'solver.answer_review')
        self.assertEqual(audit['review']['validation_evidence_refs'], ['inv-check'])
        self.assertEqual(audit['review']['original_validation_evidence_refs'], ['3', 'msg-3', 'message:3'])

    def test_repeated_candidate_rejection_triggers_reference_review_without_new_evidence(self):
        state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, None, state)
        call = {'name': 'record_answer', 'arguments': {'answer': 'fixture', 'evidence_refs': ['missing']}}
        outcome = _ToolOutcome(ok=False, text='引用未匹配，available_validation_receipts=[]')
        for _ in range(2):
            state['messages'].append({'role': 'tool', 'tool_call_id': 'rejected', 'content': outcome.text})
            self.agent._observe_tool_result(state, call, outcome)
        self.assertEqual(state['review_due']['tool'], 'record_answer')
        self.assertEqual(state['review_due']['same_request_count'], 2)
        self.assertFalse(state.get('evidence_rows'))
        sent = self.agent._light_model_messages(self.task, self.route, state)
        self.assertIn('有对应验证时用真实 invocation_id 修正引用', str(sent))
        self.agent._observe_tool_result(state, call, _ToolOutcome(ok=True, text='候选已暂存'))
        self.assertNotIn('record_answer_failure', state)

    async def test_identical_rejections_fold_without_erasing_history_or_hiding_latest_error(self):
        state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, None, state)
        state.update(turn=6, answer_review={'answer': 'fixture', 'turn': 3})
        args = {'answer': 'fixture', 'evidence_refs': ['original'], 'review': {
            'requirements': 'fixture', 'derivation': 'fixture', 'validation': 'fixture',
            'validation_evidence_refs': ['missing-receipt']}}
        for index in range(3):
            identifier = f'rejected-{index}'
            call = {'id': identifier, 'name': 'record_answer', 'arguments': args}
            state['messages'].append({'role': 'assistant', 'tool_calls': [{'id': identifier,
                'type': 'function', 'function': {'name': 'record_answer', 'arguments': json.dumps(args)}}]})
            result = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
            state['messages'].append({'role': 'tool', 'tool_call_id': identifier, 'content': result.text})
            self.agent._observe_tool_result(state, call, result)
        original = copy.deepcopy(state['messages'])
        sent = self.agent._light_model_messages(self.task, self.route, state)
        shown = [m['tool_call_id'] for m in sent if m.get('role') == 'tool']
        self.assertEqual(shown, ['rejected-2'])
        self.assertIn('未找到真实执行', next(m['content'] for m in sent if m.get('role') == 'tool'))
        self.assertEqual(state['messages'], original)
        self.assertFalse(state.get('evidence_rows'))
        self.assertFalse(state.get('concluded'))

    def test_pending_candidate_keeps_repeat_review_and_new_user_message_after_history_archiving(self):
        state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, None, state)
        self.agent._ensure_progress_guidance(state)
        state.update(system_initialized=True, turn=12,
            answer_review={'answer': 'fixture-candidate', 'evidence_refs': ['inv-original'], 'turn': 4},
            review_due={'reason': '重复失败结果', 'same_result_count': 9, 'failed': True},
            user_feed=[{'statement_id': 'new-direction', 'text': 'STOP_REPEATING_THIS_FAILURE'}])
        for index in range(8):
            state['messages'].extend([
                {'role': 'assistant', 'tool_calls': [{'id': str(index), 'function': {
                    'name': 'shell', 'arguments': '{"command":"fixture","action":"analysis"}'}}]},
                {'role': 'tool', 'tool_call_id': str(index), 'content': 'OLD_TOOL_HISTORY ' * 1800}])
        original = copy.deepcopy(state['messages'])
        sent = self.agent._light_model_messages(self.task, self.route, state)
        self.assertEqual(state['last_evidence_review']['trigger']['same_result_count'], 9)
        self.assertIsNone(state['review_due'])
        self.assertIn('重复失败结果', str(sent))
        self.assertIn('fixture-candidate', str(sent))
        self.assertIn('review.validation_evidence_refs', str(sent))
        self.assertIn('record_answer', str(sent))
        self.assertIn('STOP_REPEATING_THIS_FAILURE', sent[-1]['content'])
        self.assertIn('有新进展需要公开说明时，正文可使用一个 JSON 对象', sent[-2]['content'])
        self.assertLessEqual(estimate_input_tokens(sent, self.agent._model_tool_schemas()), 10650)
        self.assertEqual(state['messages'], original)

    def test_long_history_and_framework_observations_keep_user_contract_and_raw_history(self):
        state = {"seed": ["bootstrap observation" * 1000]}
        self.agent._seed_messages(self.task, self.route, None, state)
        for index in range(40):
            state["messages"].extend([
                {"role": "assistant", "tool_calls": [{"id": str(index), "function": {
                    "name": "binary_inspect", "arguments": json.dumps({"path": "/attachments/challenge.zip",
                        "operation": "strings", "offset": index})}}]},
                {"role": "tool", "tool_call_id": str(index), "content": "strings result " * 2000},
                {"role": "user", "_light_kind": "observation", "content": "后台任务通知：" + "output" * 1000}])
        state["messages"].append({"role": "user", "content": "用户追加要求：只读，不修改附件。"})
        original = copy.deepcopy(state["messages"])
        sent = self.agent._light_model_messages(self.task, self.route, state)
        self.assertLessEqual(estimate_input_tokens(sent, self.agent._model_tool_schemas()), 10650)
        self.assertIn("USER_GOAL", str(sent))
        self.assertIn("ORIGINAL_RULE", str(sent))
        self.assertIn("用户追加要求：只读，不修改附件。", str(sent))
        self.assertIn("history_read", str(sent))
        self.assertEqual(state["messages"], original)

    def test_old_checkpoint_gets_current_workdir_without_rewriting_history_even_in_review(self):
        self.agent.ctx.builtin_ctx = {'workspace_root': '/workspace/fixture'}
        state = {'messages': [{'role': 'system', 'content': 'rules'},
                              {'role': 'user', 'content': 'USER_GOAL'}], 'world_state': self.world}
        original = copy.deepcopy(state['messages'])
        for review in (None, {'reason': 'repeated', 'same_result_count': 2}):
            state['review_due'] = review
            sent = self.agent._light_model_messages(self.task, self.route, state)
            self.assertIn('/workspace/fixture/sandbox/one', str(sent))
            self.assertIn('workspace_root_access', str(sent))
            self.assertIn('read-only', str(sent))
            self.assertEqual(state['messages'], original)
            self.assertLessEqual(estimate_input_tokens(sent, self.agent._model_tool_schemas()), 10650)
            if review:
                self.assertIsNone(state['review_due'])
                self.assertIn('last_evidence_review', state)

    def test_review_can_drop_all_summary_rows_to_keep_actual_evidence(self):
        self.agent._model_tool_schemas = lambda: []
        self.agent._solver_system_prompt = lambda: 'rules'
        self.agent.ctx.gateway.context_budget = lambda **kw: 900
        output = 'ACTUAL_EVIDENCE ' * 80
        state = {'messages': [
            {'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_GOAL'},
            {'role': 'assistant', 'tool_calls': [{'id': 'latest', 'function': {
                'name': 'shell', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'latest', 'content': output}],
            'review_due': {'reason': 'repeated'}, 'world_state': self.world,
            'evidence_rows': [{'name': 'shell', 'ok': True, 'empty': False,
                              'message_index': 3, 'preview': 'HISTORY_SUMMARY ' * 40}]}
        original = copy.deepcopy(state['messages'])
        sent = self.agent._light_model_messages_inner(self.task, self.route, state)
        self.assertEqual([m['content'] for m in sent if m['role'] == 'tool'], [output])
        self.assertEqual(state['last_evidence_review']['observations'], [])
        self.assertLessEqual(estimate_input_tokens(sent), 900)
        self.assertEqual(state['messages'], original)

    def test_tool_prefix_reduces_further_when_user_contract_needs_more_space(self):
        state = {"messages": [{"role": "system", "content": "rules"},
            {"role": "user", "content": "完整用户契约" + "x" * 26000}]}
        original = copy.deepcopy(state)
        sent = self.agent._light_model_messages(self.task, self.route, state)
        self.assertTrue(self.agent._minimal_tool_view)
        self.assertLessEqual(estimate_input_tokens(sent, self.agent._model_tool_schemas()), 10650)
        self.assertEqual(sent, original["messages"])
        self.assertEqual(state['messages'], original['messages'])
        self.assertEqual(state['model_skill_refs'], [])

    async def test_recovery_sends_tools_matching_the_adapted_projection(self):
        state = {"messages": [{"role": "system", "content": "rules"},
            {"role": "user", "content": "完整用户契约" + "x" * 25000}]}
        call = {"id": "next", "name": "tool_invoke", "arguments": {
            "name": "shell", "arguments": {"command": "pwd", "action": "analysis"}}}

        async def chat(messages, **kwargs):
            self.assertTrue(self.agent._minimal_tool_view)
            self.assertEqual(kwargs["tools"], self.agent._model_tool_schemas())
            self.assertLessEqual(estimate_input_tokens(messages, kwargs["tools"]), 10650)
            return SimpleNamespace(content="继续调查。", tool_calls=[call])

        self.agent.ctx.gateway.chat = AsyncMock(side_effect=chat)
        result = await self.agent._forced_tool_recovery(self.task, self.route, state, 1)
        self.assertEqual(result, [call])
        self.agent.ctx.gateway.chat.assert_awaited_once()

    async def test_real_solve_loop_uses_same_bounded_tools_and_keeps_lookup_json_complete(self):
        for schema in self.agent.tool_schemas:
            if schema["function"]["name"] == "http_request":
                schema["function"]["description"] += "fixture " * 1000
        state = {"seed": [], "concluded": False, "stall": "", "turn": 0, "llm_failures": 0,
                 "empty_streak": 0, "empty_replies": 0}
        self.agent._seed_messages(self.task, self.route, None, state)
        self.agent._inv_state = lambda *args: state
        self.agent._save_inv_state = Mock()
        self.agent._peer_win_notice = lambda *args: ""
        self.agent._peer_updates = lambda *args: ""
        self.agent._tool_http = AsyncMock(return_value=_ToolOutcome(ok=True, text="HTTP output " * 1000))
        turn = 0

        async def chat(messages, **kwargs):
            nonlocal turn
            self.assertLessEqual(estimate_input_tokens(messages, kwargs["tools"]), 10650)
            names = {s["function"]["name"] for s in kwargs["tools"]}
            self.assertIn("tool_invoke", names)
            self.assertNotIn("http_request", names)
            turn += 1
            return SimpleNamespace(content="继续核对证据。", reasoning="", tool_calls=[{
                "id": f"call-{turn}", "name": "tool_lookup" if turn == 1 else "tool_invoke",
                "arguments": {"name": "http_request", **({} if turn == 1 else {
                    "arguments": {"path": f"/fixture-{turn}"}})}}])

        self.agent.ctx.gateway.chat = AsyncMock(side_effect=chat)
        plan = RoutePlan(name="solve", strategy="", steps=[{"action": "investigate"}])
        for index in range(3):
            result = await self.agent._solve_turn(self.task, self.route, plan)
            self.assertTrue(result.ok)
            self.assertFalse(state.get("context_blocked"))
            if index == 0:
                self.assertGreater(len(state["messages"][-1]["content"]), 6000)
                self.assertEqual(json.loads(state["messages"][-1]["content"])["function"]["name"], "http_request")
        self.assertEqual(self.agent._tool_http.await_count, 2)


class ArchiveIndexTests(unittest.TestCase):
    def test_archive_index_can_shrink_below_two_rows(self):
        history = [{"role": "system", "content": "rules"}, {"role": "user", "content": "USER_GOAL"}]
        for index in range(2):
            history.extend([{"role": "assistant", "tool_calls": [{"id": str(index), "function": {
                "name": "shell", "arguments": "x" * 1000}}]},
                {"role": "tool", "tool_call_id": str(index), "content": "结果" * 1000}])
        view = project_context(history, [], 450)
        self.assertEqual(view.archived_turns, 2)
        self.assertLessEqual(view.estimated_tokens, 450)
        self.assertIn("first_message_index", str(view.messages))
