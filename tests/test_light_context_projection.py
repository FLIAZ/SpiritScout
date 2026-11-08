"""轻量发送视图：软阈值、可回读参考资料及逐字重复契约的无损引用。"""
from __future__ import annotations

import copy
import json
import unittest

from spiritscout.ctf.agents import _complete_tool_history, _ToolOutcome
from spiritscout.ctf.evidence_review import observe
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.gateway.light_context import (
    ContextWindowError,
    _ProjectionTokens,
    project_context,
)


def reference_history(name="skill_open", count=5):
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "USER_GOAL"}]
    for index in range(count):
        function = {"name": name, "arguments": "{}"}
        if name == "tool_invoke":
            function["arguments"] = json.dumps({"name": "mcp_search", "arguments": {}})
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": str(index), "function": function}]},
            {"role": "tool", "tool_call_id": str(index), "content": "reference " * 1800,
             "_context_contract": True, **({"_skill_ref": f"skill-{index}@1"} if name == "skill_open" else {})}])
    return messages


class ContextProjectionTests(unittest.TestCase):
    def test_repeated_requests_archive_distinct_results_and_keep_two_full_observations(self):
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_GOAL'}]
        for index in range(5):
            messages.extend([
                {'role': 'assistant', 'content': f'OLD_PLAN_{index}', 'tool_calls': [{
                    'id': str(index), 'function': {'name': 'shell', 'arguments': '{"command":"read status"}'}}]},
                {'role': 'tool', 'tool_call_id': str(index), 'content': f'DISTINCT_STATUS_{index}'}])
        raw = copy.deepcopy(messages)
        for evidence in (False, True):
            view = project_context(messages, [], 10000, fold_repeated_requests=True, evidence_first=evidence)
            self.assertEqual([m['tool_call_id'] for m in view.messages if m['role'] == 'tool'], ['3', '4'])
            self.assertIn('results_may_differ', str(view.messages))
            self.assertNotIn('same_request_and_result_at', str(view.messages))
            self.assertNotIn('OLD_PLAN_0', str(view.messages))
            self.assertTrue(_complete_tool_history(view.messages))
            self.assertEqual(messages, raw)
        # 后续用户原话即使被置于发送视图末尾，也必须隔开前后两组执行。
        messages.insert(6, {'role': 'user', '_light_kind': 'user_feed', 'content': 'NEW_CONSTRAINT'})
        view = project_context(messages, [], 10000, fold_repeated_requests=True)
        self.assertEqual([m['tool_call_id'] for m in view.messages if m['role'] == 'tool'], ['0', '1', '3', '4'])
        self.assertEqual(view.messages[-1]['content'], 'NEW_CONSTRAINT')

    def test_current_memory_snapshot_does_not_hide_latest_update_receipt(self):
        for receipt in ({'ok': True, 'changed': False, 'revision': 2, 'notice': '已经保存，无需重复'},
                        {'ok': False, 'revision': 2, 'error': '引用不匹配'}):
            with self.subTest(receipt=receipt):
                messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_GOAL'}]
                for call_id, result in [('old', {'ok': True, 'items': ['OBSOLETE_BELIEF']}), ('latest', receipt)]:
                    messages.extend([
                        {'role': 'assistant', 'tool_calls': [{'id': call_id, 'function': {
                            'name': 'evidence_memory', 'arguments': '{"action":"update"}'}}]},
                        {'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(result, ensure_ascii=False)}])
                messages.append({'role': 'user', '_light_kind': 'memory', '_context_contract': True,
                                 'content': 'CURRENT_MEMORY_REVISION_2'})
                original = copy.deepcopy(messages)
                view = project_context(messages, [], 10000, evidence_first=True)
                tools = [message for message in view.messages if message['role'] == 'tool']
                self.assertEqual([message['tool_call_id'] for message in tools], ['latest'])
                self.assertEqual(json.loads(tools[0]['content']), receipt)
                self.assertNotIn('OBSOLETE_BELIEF', str(view.messages))
                self.assertTrue(_complete_tool_history(view.messages))
                self.assertEqual(messages, original)

    def test_short_repeated_turns_fold_without_losing_changed_results_or_raw_history(self):
        state = {'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_GOAL'}]}
        messages = state['messages']
        for index, output in enumerate(('FIRST_EVIDENCE', 'SAME', 'SAME', 'CHANGED', 'SAME')):
            args = {'command': 'read fixture', 'action': 'analysis'}
            messages.extend([
                {'role': 'assistant', 'content': f'OLD_ANALYSIS_{index}', 'tool_calls': [{
                    'id': str(index), 'function': {'name': 'shell', 'arguments': json.dumps(args)}}]},
                {'role': 'tool', 'tool_call_id': str(index), 'content': output}])
            observe(state, 'shell', args, _ToolOutcome(ok=True, text=output))
        messages.extend([
            {'role': 'user', '_light_kind': 'user_feed', '_context_contract': True, 'content': 'NEW_USER_MESSAGE'},
            {'role': 'user', '_context_contract': True, 'content': 'FRAMEWORK_REVIEW'}])
        raw = copy.deepcopy(messages)
        budget = 10000
        self.assertLess(estimate_input_tokens(messages), budget // 2)
        view = project_context(messages, [], budget, fold_repeated_turns=True, evidence_first=True)
        self.assertEqual([m['tool_call_id'] for m in view.messages if m['role'] == 'tool'], ['0', '3', '4'])
        self.assertEqual(view.messages[-1]['content'], 'NEW_USER_MESSAGE')
        self.assertNotIn('OLD_ANALYSIS_1', str(view.messages))
        self.assertNotIn('OLD_ANALYSIS_2', str(view.messages))
        self.assertIn('same_request_and_result_count', str(view.messages))
        self.assertTrue(_complete_tool_history(view.messages))
        self.assertEqual(messages, raw)
        wire = [{k: v for k, v in m.items() if not k.startswith('_')} for m in view.messages]
        self.assertEqual(view.estimated_tokens, estimate_input_tokens(wire))

    def test_evidence_first_keeps_source_and_failed_validation_above_soft_target(self):
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_GOAL'}]
        outputs = ['old output ' * 200, 'older output ' * 200,
                   'SOURCE_LINE\n' * 200, 'VALIDATION_FAILURE_DETAIL\n' * 100]
        for index, output in enumerate(outputs):
            messages.extend([
                {'role': 'assistant', 'content': 'MODEL_HYPOTHESIS_' + str(index),
                 'tool_calls': [{'id': str(index), 'function': {'name': 'shell', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': str(index), 'content': output}])
        messages.append({'role': 'user', '_context_contract': True, 'content': 'LATEST_USER_CONSTRAINT'})
        original = copy.deepcopy(messages)
        view = project_context(messages, [], 2200, target_budget=1000, evidence_first=True)
        self.assertEqual([m['tool_call_id'] for m in view.messages if m['role'] == 'tool'], ['2', '3'])
        for text in outputs[-2:] + ['MODEL_HYPOTHESIS_2', 'MODEL_HYPOTHESIS_3', 'LATEST_USER_CONSTRAINT']:
            self.assertIn(text, [m.get('content') for m in view.messages])
        self.assertLessEqual(view.estimated_tokens, 2200)
        self.assertTrue(_complete_tool_history(view.messages))
        self.assertEqual(messages, original)

    def test_recovery_notice_expires_only_after_a_tool_result(self):
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_GOAL'},
                    {'role': 'assistant', 'content': 'still investigating'},
                    {'role': 'user', '_light_kind': 'recovery', 'content': 'NO_TOOL_RECOVERY_NOTICE'}]
        world = {'user_context': {'mission': 'USER_GOAL'}}
        pending = project_context(messages, [], 3000, world=world)
        self.assertIn('NO_TOOL_RECOVERY_NOTICE', str(pending.messages))
        messages.extend([
            {'role': 'assistant', 'tool_calls': [{'id': 'next', 'function': {'name': 'shell', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'next', 'content': 'ACTUAL_FAILURE_REMAINS_VISIBLE'}])
        original = copy.deepcopy(messages)
        completed = project_context(messages, [], 3000, world=world)
        self.assertNotIn('NO_TOOL_RECOVERY_NOTICE', str(completed.messages))
        self.assertIn('ACTUAL_FAILURE_REMAINS_VISIBLE', str(completed.messages))
        self.assertEqual(messages, original)

    def test_repeated_read_results_are_archived_before_unique_cross_file_evidence(self):
        for protect_latest in (False, True):
            with self.subTest(protect_latest=protect_latest):
                state = {'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_GOAL'}]}
                messages = state['messages']
                for index, source in enumerate(('A', 'B', 'B', 'B')):
                    args = {'command': 'read ' + source, 'action': 'analysis'}
                    output = source + '_FULL_SOURCE\n' + (source + ' code\n') * 400
                    messages.extend([
                        {'role': 'assistant', 'tool_calls': [{'id': str(index), 'function': {
                            'name': 'shell', 'arguments': json.dumps(args)}}]},
                        {'role': 'tool', 'tool_call_id': str(index), 'content': output}])
                    observe(state, 'shell', args, _ToolOutcome(ok=True, text=output))
                if protect_latest:
                    messages[-1]['_context_contract'] = True
                messages.append({'role': 'user', '_context_contract': True, 'content': 'LATEST_USER_BOUNDARY'})
                original = copy.deepcopy(messages)
                view = project_context(messages, [], 3500, target_budget=3000, deduplicate_observations=True)
                retained = [m['tool_call_id'] for m in view.messages if m.get('role') == 'tool']
                self.assertEqual(retained, ['0', '3'])
                self.assertIn('LATEST_USER_BOUNDARY', str(view.messages))
                self.assertIn('same_request_and_result_at', str(view.messages))
                self.assertIn('result_message_indices', str(view.messages))
                self.assertTrue(_complete_tool_history(view.messages))
                self.assertEqual(messages, original)
                self.assertLessEqual(view.estimated_tokens, 3500)

    def test_plain_progress_removes_only_framework_contract_without_changing_raw_indices(self):
        from spiritscout.gateway.progress import EXPLORATION_PROGRESS_GUIDANCE

        messages = [{'role': 'system', 'content': 'rules'},
                    {'role': 'user', 'content': 'USER_GOAL'},
                    {'role': 'user', '_context_contract': True, 'content': EXPLORATION_PROGRESS_GUIDANCE},
                    {'role': 'user', 'content': EXPLORATION_PROGRESS_GUIDANCE}]
        original = copy.deepcopy(messages)
        view = project_context(messages, [], 10000, plain_progress=True)
        self.assertEqual(len(view.messages), 3)
        self.assertEqual(view.messages[-1]['content'], EXPLORATION_PROGRESS_GUIDANCE)
        self.assertEqual(messages, original)
        self.assertEqual(view.estimated_tokens, estimate_input_tokens(view.messages))

    def test_small_request_still_includes_current_world_and_active_instructions(self):
        messages = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_GOAL'}]
        original = copy.deepcopy(messages)
        world = {'user_context': {'mission': 'LATEST_MISSION'}}
        view = project_context(messages, [], 10000, world=world,
                               instructions=[{'id': 'one', 'hint': 'LATEST_INSTRUCTION'}])
        sent = '\n'.join(message.get('content') or '' for message in view.messages)
        self.assertIn('LATEST_MISSION', sent)
        self.assertIn('LATEST_INSTRUCTION', sent)
        self.assertEqual(messages, original)
        self.assertEqual(view.estimated_tokens, estimate_input_tokens(view.messages))

    def test_soft_threshold_compacts_before_hard_limit_and_preserves_latest_pair(self):
        messages = reference_history(count=4)
        for message in messages:
            message.pop('_context_contract', None)
        budget = estimate_input_tokens(messages) + 100
        original = copy.deepcopy(messages)
        result = project_context(messages, [], budget, target_budget=8000)
        self.assertGreater(result.archived_turns, 0)
        self.assertEqual(result.messages[-1]['tool_call_id'], '3')
        self.assertTrue(_complete_tool_history(result.messages))
        self.assertLessEqual(result.estimated_tokens, budget)
        self.assertEqual(messages, original)

    def test_soft_threshold_never_rejects_a_valid_irreducible_request(self):
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "USER_BOUNDARY" * 30}]
        result = project_context(messages, [], 3000, target_budget=10)
        self.assertLessEqual(result.estimated_tokens, 3000)
        self.assertEqual(result.messages, messages)

    def test_old_skill_contracts_can_be_archived_but_latest_remains_complete(self):
        messages = reference_history()
        original = copy.deepcopy(messages)
        with self.assertRaises(ContextWindowError):
            project_context(messages, [], 6500)
        result = project_context(messages, [], 8000, evict_references=True)
        self.assertNotIn('skill-0@1', result.skill_refs)
        self.assertIn('skill-4@1', result.skill_refs)
        self.assertEqual(result.messages[-1]['content'], messages[-1]['content'])
        self.assertIn('skill-0@1', str(result.messages))
        self.assertTrue(_complete_tool_history(result.messages))
        self.assertEqual(messages, original)

    def test_deferred_mcp_contracts_are_reclaimable_as_whole_pairs(self):
        messages = reference_history('tool_invoke')
        result = project_context(messages, [], 8000, evict_references=True)
        self.assertGreater(result.archived_turns, 0)
        self.assertTrue(_complete_tool_history(result.messages))
        self.assertEqual(result.messages[-1]['content'], messages[-1]['content'])

    def test_other_protected_contracts_are_not_reclassified_as_reference_material(self):
        with self.assertRaises(ContextWindowError):
            project_context(reference_history('shell'), [], 6500, evict_references=True)

    def test_exact_contract_duplicates_keep_all_statement_ids_and_unique_boundaries(self):
        mission = 'USER_ORIGINAL_CONTRACT ' * 700
        world = {"authorization": {"testing_rules": "UNIQUE_AUTHORIZATION"},
                 "user_context": {"mission": mission, "statements": [
                     {"statement_id": "one", "text": mission}, {"statement_id": "two", "text": mission},
                     {"statement_id": "three", "text": "UNIQUE_LATEST_BOUNDARY"}]}}
        frame = {"mission": mission, "rules": "UNIQUE_RULE", "challenge": {"description": mission}}
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": json.dumps(frame),
            "_light_base": json.dumps(frame), "_light_task_frame": frame, "_light_task_heading": "任务：\n"}]
        original, original_world = copy.deepcopy(messages), copy.deepcopy(world)
        result = project_context(messages, [], 6500, world=world, deduplicate_contracts=True)
        sent = json.dumps([{k: v for k, v in m.items() if not k.startswith('_')} for m in result.messages], ensure_ascii=False)
        self.assertEqual(sent.count(mission), 1)
        self.assertIn('exact_text_ref', sent)
        for expected in ('one', 'two', 'three', 'UNIQUE_LATEST_BOUNDARY', 'UNIQUE_AUTHORIZATION', 'UNIQUE_RULE'):
            self.assertIn(expected, sent)
        self.assertEqual(messages, original)
        self.assertEqual(world, original_world)
        self.assertLessEqual(result.estimated_tokens, 6500)

    def test_incremental_estimator_matches_wire_estimate_including_unicode_and_tool_schemas(self):
        tools = [{'type': 'function', 'function': {'name': 'fixture', 'description': '中文参数 🧪'}}]
        messages = [{'role': 'system', 'content': '中文 English 🧪'},
                    {'role': 'assistant', 'tool_calls': [{'id': 'one', 'function': {
                        'name': 'fixture', 'arguments': '{"text":"中文"}'}}], '_internal': 'not sent'},
                    {'role': 'tool', 'tool_call_id': 'one', 'content': '\noutput "quoted" 中文'}]
        counter = _ProjectionTokens(tools)
        for subset in ([], messages, messages[:2], messages[1:], list(reversed(messages))):
            wire = [{k: v for k, v in m.items() if not k.startswith('_')} for m in subset]
            self.assertEqual(counter(subset), estimate_input_tokens(wire, tools))
        messages[0]['content'] = 'updated 中文'
        counter.invalidate(messages[0])
        wire = [{k: v for k, v in m.items() if not k.startswith('_')} for m in messages]
        self.assertEqual(counter(messages), estimate_input_tokens(wire, tools))
