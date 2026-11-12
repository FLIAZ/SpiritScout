"""轻量题型提示、公开正文与重复观察复盘回归。"""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_ctf_runtime import EmptyReplyRepairTests

from spiritscout.agents.sub.base import RoutePlan
from spiritscout.core.models import AgentType, RunMode, Task
from spiritscout.ctf.agents import _ToolOutcome
from spiritscout.ctf.evidence_review import observe, result_reference, review_row
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.gateway.context import estimate_input_tokens


def single():
    ctx = SimpleNamespace(mode=RunMode.CTF, shared_cache=None, search_enabled=False,
        gateway=SimpleNamespace(config=SimpleNamespace(lightweight_local=True),
                                context_budget=lambda **kw: 10650, chat=AsyncMock()),
        audit=Mock(), builtin_ctx={}, blackboard=None, policy=None, ring=None)
    ctx.for_agent = lambda role: ctx
    return SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))


class ObservationTests(unittest.TestCase):
    def test_legacy_references_resolve_results_without_changing_checkpoint(self):
        messages = [{'role': 'assistant', 'tool_calls': [{'id': 'one'}, {'id': 'two'}]},
                    {'role': 'tool', 'tool_call_id': 'one', 'content': '执行记录 inv-one\nFIRST'},
                    {'role': 'tool', 'tool_call_id': 'two', 'content': '执行记录 inv-two\nSECOND'}]
        original = {'message_index': 0, 'invocation_id': 'inv-two'}
        resolved = result_reference(original, messages)
        self.assertEqual(resolved['message_index'], 2)
        self.assertEqual(original['message_index'], 0)
        self.assertIsNone(result_reference({'message_index': 0}, messages)['message_index'])
        messages[0]['tool_calls'] = [{'id': 'one'}]
        self.assertEqual(result_reference({'message_index': 0}, messages)['message_index'], 1)

    def test_display_only_matches_cannot_evict_potentially_different_originals(self):
        state = {'messages': [{'role': 'tool', 'tool_call_id': 'one', 'content': 'lossy preview'}]}
        observe(state, 'shell', {'command': 'fixture'}, _ToolOutcome(ok=True, text='lossy preview', metadata={
            'observation': {'digest': 'same-preview', 'preview': 'lossy preview', 'comparison_scope': 'display_preview'}}))
        self.assertNotIn('_context_repeat_key', state['messages'][0])

    def test_text_review_retains_tail_of_existing_head_tail_preview(self):
        row = {'preview': 'FIRST_EVIDENCE' + 'x' * 700 + 'LAST_EVIDENCE', 'structured': False}
        preview = review_row(row)['result_preview']
        self.assertIn('FIRST_EVIDENCE', preview)
        self.assertIn('LAST_EVIDENCE', preview)
        self.assertLessEqual(len(preview), 400)

    def test_partial_success_keeps_receipt_and_turn_but_cannot_claim_complete_validation(self):
        state = {'messages': [], 'turn': 4}
        observe(state, 'shell', {'command': 'fixture', 'action': 'analysis'},
            _ToolOutcome(ok=True, text='partial output', metadata={'invocation_id': 'inv-partial',
                'observation': {'digest': 'fixture', 'preview': 'partial output', 'partial': True}}))
        row = state['evidence_rows'][0]
        self.assertEqual(row['invocation_id'], 'inv-partial')
        self.assertEqual(row['turn'], 4)
        self.assertTrue(row['partial'])
        self.assertTrue(row['ok'])

    def test_repeated_empty_queries_are_observed_across_all_tools(self):
        state = {"messages": []}
        outcome = _ToolOutcome(ok=True, text=json.dumps({"ok": True, "items": [], "total": 0}))
        self.assertIsNone(observe(state, "blackboard_read", {"layer": "asset"}, outcome))
        review = observe(state, "blackboard_read", {"layer": "asset"}, outcome)
        self.assertTrue(review["empty"])
        self.assertEqual(review["same_result_count"], 2)
        self.assertEqual(review["reason"], "重复空查询")

    def test_results_not_paths_or_call_ids_determine_repetition(self):
        state = {"messages": []}
        first = _ToolOutcome(ok=True, text="wrapper with invocation-one", metadata={
            "invocation_id": "one", "observation": {"digest": "actual-output", "preview": "real bytes"}})
        second = _ToolOutcome(ok=True, text="different wrapper with invocation-two", metadata={
            "invocation_id": "two", "observation": {"digest": "actual-output", "preview": "real bytes"}})
        self.assertIsNone(observe(state, "shell", {"command": "read-a"}, first))
        self.assertIsNotNone(observe(state, "shell", {"command": "read-b"}, second))
        changed = _ToolOutcome(ok=True, text="", metadata={"observation": {"digest": "new-output", "preview": "new bytes"}})
        self.assertIsNone(observe(state, "shell", {"command": "read-a"}, changed))

    def test_repeated_request_with_changing_failed_output_triggers_review_without_folding_results(self):
        state = {'messages': []}
        args = {'command': 'analyze logs', 'action': 'analysis'}
        keys = []
        for count in (41, 42):
            text = f'Found {count} log files\nAttributeError: fixture'
            state['messages'].append({'role': 'tool', 'tool_call_id': str(count), 'content': text})
            review = observe(state, 'shell', args, _ToolOutcome(ok=False, text=text))
            keys.append(state['messages'][-1]['_context_repeat_key'])
            if count == 41:
                self.assertIsNone(review)
        self.assertEqual(review['same_request_count'], 2)
        self.assertEqual(review['same_result_count'], 1)
        self.assertTrue(review['failed'])
        self.assertIn('本次返回不同', review['reason'])
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(len(state['evidence_seen']), 2)
        self.assertIsNone(observe(state, 'shell', {'command': 'inspect input structure'},
            _ToolOutcome(ok=True, text='new observation')))

    def test_structured_binary_summary_keeps_addresses_and_pagination(self):
        summary = json.dumps({"items": [{"name": "function-" + str(i), "address": hex(i)} for i in range(12)],
                              "next_offset": 12, "has_more": True})
        original = {"name": "binary_inspect", "preview": summary, "structured": True, "message_index": 3}
        projected = review_row(original)
        self.assertEqual(json.loads(projected["result_preview"])["next_offset"], 12)
        self.assertEqual(len(json.loads(projected["result_preview"])["items"]), 12)
        self.assertTrue(json.loads(projected["result_preview"])["has_more"])

    def test_review_projection_does_not_duplicate_full_parameters_and_results(self):
        original = {"name": "binary_inspect", "arguments_preview": "argument" * 1000,
                    "first_request": "duplicate" * 1000, "preview": "result" * 1000,
                    "message_index": 7, "invocation_id": "inv-one"}
        projected = review_row(original)
        self.assertNotIn("first_request", projected)
        self.assertLessEqual(len(projected["arguments_preview"]), 300)
        self.assertLessEqual(len(projected["result_preview"]), 400)
        self.assertEqual(projected["message_index"], 7)
        self.assertTrue(projected["partial"])
        self.assertEqual(len(original["preview"]), 6000)

    def test_compact_binary_review_preserves_function_and_continuation_without_whole_page(self):
        data = {"items": [{"address": hex(i), "disasm": "fixture"} for i in range(20)],
                "function": {"name": "fcn.fixture", "addr": 0}, "next_offset": 20,
                "has_more": True, "requested_address": "0x0", "next_address": "0x14"}
        row = {"structured": True, "preview": json.dumps(data)}
        result = json.loads(review_row(row, compact=True)["result_preview"])
        self.assertEqual(result['function'], data['function'])
        self.assertEqual(result['next_address'], '0x14')
        self.assertEqual(result['next_offset'], 20)
        self.assertEqual(len(result['items_preview']), 3)
        self.assertEqual(result['displayed_items'], 20)
        self.assertTrue(result['partial'])

    def test_earlier_observations_survive_a_long_run_of_repeated_failures(self):
        state = {"messages": []}
        observe(state, "binary_inspect", {"operation": "info"},
                _ToolOutcome(ok=True, text='{"ok":true,"info":{"class":"PE32"}}'))
        for _ in range(15):
            observe(state, "shell", {"command": "wrong tool"}, _ToolOutcome(ok=False, text="unsupported format"))
        remembered = [entry["first_observation"] for entry in state["evidence_seen"].values()]
        self.assertTrue(any("PE32" in entry["preview"] for entry in remembered))
        self.assertLessEqual(len(state["evidence_rows"]), 12)

    def test_successful_writes_without_output_are_not_mistaken_for_stagnation(self):
        state = {"messages": []}
        outcome = _ToolOutcome(ok=True, text="", metadata={"observation": {"digest": "no-output", "empty": True}})
        for _ in range(3):
            self.assertIsNone(observe(state, "shell", {"action": "intrusive_write", "command": "write"}, outcome))

    def test_repeated_failures_trigger_review_but_terminal_results_do_not(self):
        state = {"messages": []}
        failed = _ToolOutcome(ok=False, text="format not supported")
        observe(state, "shell", {"command": "bad"}, failed)
        self.assertTrue(observe(state, "shell", {"command": "bad"}, failed)["failed"])
        self.assertIsNone(observe(state, "submit_fix", {}, _ToolOutcome(ok=True, text="registered", stop=True)))


class SingleReviewTests(unittest.IsolatedAsyncioTestCase):
    def review_fixture(self, output):
        agent = single()
        task = self.task()
        state = {'seed': [], 'world_state': {}}
        agent._seed_messages(task, None, None, state)
        state['messages'].extend([
            {'role': 'assistant', 'tool_calls': [{'id': 'read-one', 'function': {
                'name': 'shell', 'arguments': '{"command":"cat fixture.rb","action":"analysis"}'}}]},
            {'role': 'tool', 'tool_call_id': 'read-one', 'content': output}])
        state['review_due'] = {'reason': '重复观察', 'same_result_count': 3}
        return agent, task, state

    async def test_review_keeps_latest_complete_source_instead_of_forcing_another_read(self):
        from spiritscout.ctf.agents import _complete_tool_history

        code = 'SOURCE_START\n' + 'fixture code\n' * 100 + 'UNIQUE_MIDDLE_EVIDENCE\n' + 'fixture code\n' * 100 + 'SOURCE_END'
        agent, task, state = self.review_fixture(code)
        original = copy.deepcopy(state['messages'])
        sent = agent._light_model_messages(task, SimpleNamespace(route_id='r'), state)
        self.assertTrue(_complete_tool_history(sent))
        self.assertIn(code, [message.get('content') for message in sent])
        self.assertTrue(state['last_evidence_review']['latest_complete_turn_retained'])
        self.assertEqual(state['messages'], original)
        self.assertLessEqual(estimate_input_tokens(sent, agent._model_tool_schemas()), 10650)

    async def test_review_preserves_cross_file_evidence_user_contract_and_skill(self):
        from spiritscout.ctf.agents import _complete_tool_history

        source_a = 'FILE_A_FULL_BEGIN\n' + 'source A\n' * 80 + 'FILE_A_FULL_END'
        source_b = 'FILE_B_FULL_BEGIN\n' + 'source B\n' * 80 + 'FILE_B_FULL_END'
        agent, task, state = self.review_fixture(source_a)
        state['messages'].extend([
            {'role': 'user', '_context_contract': True, 'content': 'USER_FOLLOWUP_KEEP_NORMAL_FUNCTION'},
            {'role': 'assistant', 'tool_calls': [{'id': 'read-b', 'function': {
                'name': 'shell', 'arguments': '{"command":"cat fixture_b.rb","action":"analysis"}'}}]},
            {'role': 'tool', 'tool_call_id': 'read-b', 'content': source_b},
            {'role': 'assistant', 'tool_calls': [{'id': 'read-skill', 'function': {
                'name': 'skill_open', 'arguments': '{"skill_id":"fixture"}'}}]},
            {'role': 'tool', 'tool_call_id': 'read-skill', '_context_contract': True,
             '_skill_ref': 'fixture-ref', 'content': 'SKILL_BODY_MUST_REMAIN_AVAILABLE'},
            {'role': 'assistant', 'tool_calls': [{'id': 'read-a-again', 'function': {
                'name': 'shell', 'arguments': '{"command":"cat fixture.rb","action":"analysis"}'}}]},
            {'role': 'tool', 'tool_call_id': 'read-a-again', 'content': source_a}])
        original = copy.deepcopy(state['messages'])
        instruction = {'id': 'steering-one', 'hint': 'CURRENT_STEERING_MUST_REMAIN_AVAILABLE'}
        agent._active_steering = lambda *_: [instruction]
        sent = agent._light_model_messages(task, SimpleNamespace(route_id='r'), state)
        self.assertTrue(_complete_tool_history(sent))
        for content in (source_a, source_b, 'USER_FOLLOWUP_KEEP_NORMAL_FUNCTION',
                        'SKILL_BODY_MUST_REMAIN_AVAILABLE', 'CURRENT_STEERING_MUST_REMAIN_AVAILABLE'):
            self.assertIn(content, '\n'.join(message.get('content') or '' for message in sent))
        self.assertIn('fixture-ref', state['model_skill_refs'])
        self.assertEqual(state['messages'], original)
        self.assertLessEqual(estimate_input_tokens(sent, agent._model_tool_schemas()), 10650)

    async def test_review_shrinks_summary_before_archiving_latest_fitting_source(self):
        source = 'LATEST_FULL_SOURCE\n' + 'fixture code\n' * 150
        agent, task, state = self.review_fixture(source)
        agent._context_projection_options = lambda budget: {'target_budget': 1000}
        sent = agent._light_model_messages(task, SimpleNamespace(route_id='r'), state)
        self.assertIn(source, [message.get('content') for message in sent])
        self.assertTrue(state['last_evidence_review']['latest_complete_turn_retained'])
        self.assertLessEqual(estimate_input_tokens(sent, agent._model_tool_schemas()), 10650)

    async def test_review_falls_back_to_references_when_latest_full_pair_cannot_fit(self):
        agent, task, state = self.review_fixture('too large ' * 10000)
        sent = agent._light_model_messages(task, SimpleNamespace(route_id='r'), state)
        self.assertFalse(state['last_evidence_review']['latest_complete_turn_retained'])
        self.assertIn('history_read', str(sent))
        self.assertLessEqual(estimate_input_tokens(sent, agent._model_tool_schemas()), 10650)

    def task(self):
        return Task(task_id="one", agent_type=AgentType.CTF_MISC, task_type="ctf_single", target="file.exe",
            payload={"challenge": {"id": "one", "description": "original challenge",
                "user_type_hints": {"types": ["Reverse"], "source": "user"}}, "mission": "USER_GOAL"})

    async def test_identity_is_separate_from_direction_and_binding(self):
        agent = single()
        task = self.task()
        with patch("spiritscout.ctf.agents.CTFBaseAgent.run", new=AsyncMock(return_value={})):
            await agent.run(task)
        self.assertEqual(agent.agent_type, AgentType.CTF_MISC)
        self.assertEqual(task.payload["executor_kind"], "single")
        self.assertEqual(task.payload["user_direction_hints"], ["Reverse"])
        identity = next(call for call in agent.ctx.audit.log.call_args_list if call.args[0] == "executor.identity")
        self.assertEqual(identity.args[2]["user_direction_hints"], ["Reverse"])
        state = {"seed": []}
        agent._seed_messages(task, None, None, state)
        self.assertIn("用户选择的调查方向：Reverse", state["messages"][1]["content"])
        self.assertIn("不是后端分类", state["messages"][1]["content"])

    async def test_review_replaces_next_view_without_calling_another_model_or_blocking_tools(self):
        agent = single()
        state = {"messages": [{"role": "system", "content": "s"},
                               {"role": "user", "content": "USER_GOAL", "_light_base": "USER_GOAL"}],
                 "world_state": {"authorization": {"testing_rules": "ORIGINAL_RULE"}}}
        empty = _ToolOutcome(ok=True, text='{"ok":true,"items":[],"total":0}')
        with patch("spiritscout.ctf.agents.CTFBaseAgent._run_tool_call_inner", new=AsyncMock(return_value=empty)) as perform:
            for _ in range(2):
                outcome = await agent._run_tool_call_inner(self.task(), None, state,
                    {"name": "blackboard_read", "arguments": {"layer": "asset"}})
                self.assertTrue(outcome.ok)
                state['messages'].append({'role': 'tool', 'tool_call_id': f'call-{_}', 'content': outcome.text})
                agent._observe_tool_result(state, {'name': 'blackboard_read', 'arguments': {'layer': 'asset'}}, outcome)
            self.assertEqual(perform.await_count, 2)
        self.assertTrue(state["review_due"]["empty"])
        sent = agent._light_model_messages(self.task(), SimpleNamespace(route_id="r"), state)
        self.assertIn("证据复盘", sent[-1]["content"])
        self.assertIn("ORIGINAL_RULE", str(sent))
        self.assertIn("USER_GOAL", str(sent))
        self.assertLessEqual(estimate_input_tokens(sent, agent._model_tool_schemas()), 10650)
        agent.ctx.gateway.chat.assert_not_called()
        self.assertIsNone(state["review_due"])


class MissingToolCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_prose_or_empty_replies_never_become_implicit_completion(self):
        for mode in (RunMode.CTF, RunMode.AWDP):
            for content in ('我将解压附件到沙箱工作目录，然后读取源码。', ''):
                with self.subTest(mode=mode, content=content):
                    fixture = EmptyReplyRepairTests()
                    agent = fixture._agent([])
                    agent.flag_manager = FlagManager(local_only=True, fix_mode=mode == RunMode.AWDP)
                    agent.ctx.mode = mode
                    agent.ctx.gateway.config = SimpleNamespace(lightweight_local=True)
                    agent.tool_schemas = [{'function': {'name': 'finish_challenge'}}]
                    agent.ctx.gateway.chat = AsyncMock(return_value=fixture._reply(content))
                    task, route = fixture._task(), fixture._route()
                    plan = RoutePlan(name='solve', strategy='', steps=[{'action': 'investigate'}])
                    for _ in range(4):
                        result = await agent._solve_turn(task, route, plan)
                        self.assertFalse(result.final_note)
                        self.assertFalse(fixture.state['concluded'])
                    # 每次普通回复和恢复回复都留在原始历史，不能在下一次请求里丢掉恢复结果。
                    assistant_messages = [m for m in fixture.state['messages'] if m.get('role') == 'assistant']
                    self.assertEqual(len(assistant_messages), 8)
                    self.assertFalse(any(c.args[0] == 'agent.conclude' for c in agent.ctx.audit.log.call_args_list))
                    # 模型仍能显式报告无法完成；后端不靠正文或回复次数代替这一决定。
                    agent.ctx.gateway.chat = AsyncMock(return_value=SimpleNamespace(content='', tool_calls=[{
                        'id': 'finish-explicit', 'name': 'finish_challenge',
                        'arguments': {'complete': False, 'reason': 'fixture lacks runtime dependency'}}]))
                    result = await agent._solve_turn(task, route, plan)
                    self.assertTrue(result.final_note)
                    self.assertTrue(fixture.state['concluded'])
                    self.assertIn('fixture lacks runtime dependency', result.note)


class PublicBodyTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, public, light):
        fixture = EmptyReplyRepairTests()
        agent = fixture._agent([])
        agent.public_body = public
        agent.flag_manager = FlagManager(local_only=True)
        agent.ctx.mode = RunMode.CTF
        agent.ctx.gateway.config = SimpleNamespace(lightweight_local=light)
        agent.tool_schemas = [{"function": {"name": "shell"}}]
        agent._run_tool_call = AsyncMock(return_value=_ToolOutcome(ok=True, text="actual output"))
        async def reply(*args, **kwargs):
            kwargs["on_delta"]("text", "已确认文件格式。")
            kwargs["on_delta"]("text", "下一步需要核对输入逻辑。")
            return SimpleNamespace(content="已确认文件格式。下一步需要核对输入逻辑。", reasoning="",
                tool_calls=[{"id": "one", "name": "shell", "arguments": {"command": "fixture", "action": "analysis"}}])
        agent.ctx.gateway.chat = AsyncMock(side_effect=reply)
        await agent._solve_turn(fixture._task(), fixture._route(), RoutePlan(name="solve", strategy="", steps=[{"action": "investigate"}]))
        return [call.args for call in agent.ctx.audit.log.call_args_list]

    async def test_light_single_public_body_streams_and_persists_one_progress_event(self):
        events = await self.exercise(public=True, light=True)
        text = "".join(event[2].get("text", "") for event in events if event[0] == "agent.delta" and event[2].get("kind") == "text")
        self.assertEqual(text, "已确认文件格式。下一步需要核对输入逻辑。")
        progress = [event[2] for event in events if event[0] == "agent.progress"]
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["text"], text)
        self.assertTrue(progress[0]["streamed"])

    async def test_normal_local_answer_streams_and_persists_public_body(self):
        events = await self.exercise(public=True, light=False)
        text = "".join(event[2].get("text", "") for event in events
                       if event[0] == "agent.delta" and event[2].get("kind") == "text")
        self.assertEqual(text, "已确认文件格式。下一步需要核对输入逻辑。")
        progress = [event[2] for event in events if event[0] == "agent.progress"]
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["text"], text)
        self.assertTrue(progress[0]["streamed"])
