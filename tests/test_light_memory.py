"""轻量归档后可定位原文、持久笔记与旧检查点兼容回归。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import test_single_context

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer
from spiritscout.ctf.agents import _complete_tool_history, _ToolOutcome
from spiritscout.ctf.evidence_review import observe
from spiritscout.ctf.light_memory import (
    MEMORY_KEY,
    catalog_rows,
    memory_execute,
    memory_message,
    read_history,
    tool_records,
    working_snapshot,
)
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.tools.journal import ExecutionJournal


def add_result(state, name, arguments, text, *, ok=True):
    index = len(state['messages'])
    state['messages'].extend([
        {'role': 'assistant', 'content': 'MODEL_HYPOTHESIS_NOT_EVIDENCE', 'tool_calls': [{
            'id': str(index), 'function': {'name': name, 'arguments': json.dumps(arguments)}}]},
        {'role': 'tool', 'tool_call_id': str(index), 'content': text}])
    observe(state, name, arguments, _ToolOutcome(ok=ok, text=text, metadata={'invocation_id': f'inv-{index}'}))
    return index + 1


def history():
    state = {'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_CONTRACT'}]}
    for page in range(3):
        add_result(state, 'binary_inspect', {'path': '/fixture/bin', 'operation': 'disasm',
            'address': '0x1040', 'offset': page * 20}, json.dumps({
                'ok': True, 'function': {'name': 'dispatch_fixture', 'addr': 4160},
                'items': [{'address': hex(4160 + page * 20 + i), 'disasm': 'fixture_instruction'} for i in range(20)],
                'offset': page * 20, 'has_more': page < 2}))
    for index in range(30):
        add_result(state, 'shell', {'command': f'read fixture-{index}', 'action': 'analysis'},
                   f'UNIQUE_RESULT_{index}\n' + 'observed output\n' * 130)
    for _ in range(3):
        add_result(state, 'shell', {'command': 'read repeated', 'action': 'analysis'}, 'REPEATED_OUTPUT\n' * 150)
    return state


class LightMemoryTests(unittest.TestCase):
    def setUp(self):
        fixture = test_single_context.SingleContextTests()
        fixture.setUp()
        self.agent, self.task, self.route = fixture.agent, fixture.task, fixture.route

    def test_observations_exist_without_model_notes_and_survive_blackboard_and_checkpoint(self):
        state = {'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_GOAL'}]}
        args = {'path': '/fixture/bin', 'operation': 'disasm', 'address': '0x1040'}
        for _ in range(6):
            add_result(state, 'binary_inspect', args, json.dumps({
                'ok': True, 'items': ['OBSERVED_INSTRUCTION'], 'offset': 0, 'next_offset': 16, 'has_more': True}))
        self.assertNotIn(MEMORY_KEY, state)
        memory = working_snapshot(state)
        self.assertEqual(memory['notes']['revision'], 0)
        self.assertEqual(memory['notes']['items'], [])
        self.assertEqual(memory['observations'][0]['same_result_count'], 6)
        self.assertEqual(memory['observations'][0]['pagination']['next_offset'], 16)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bb = Blackboard(root / 'bb.db')
            self.agent.ctx.blackboard = bb
            self.agent.ctx.journal = ExecutionJournal(bb, root, 'memory-fixture')
            try:
                self.agent._save_inv_state(self.task, self.route, state)
                restored = self.agent.ctx.journal.latest_checkpoint('ctf_conversation', 'one:r')['state']
                persisted = bb.read(BlackboardLayer.TOPOLOGY, 'working-memory:one:r')
                self.assertEqual(persisted['observations'], memory['observations'])
                self.assertEqual(working_snapshot(restored), memory)
                view = self.agent._light_model_messages(self.task, self.route, restored)
                self.assertIn('当前工作黑板', str(view))
                self.assertIn('OBSERVED_INSTRUCTION', str(view))
                self.assertEqual(len([m for m in view if m['role'] == 'tool']), 1)
                self.assertNotIn(MEMORY_KEY, restored)
                self.assertTrue(_complete_tool_history(view))
            finally:
                bb.close()

    def test_initial_failures_do_not_displace_working_paths_but_recent_failures_remain(self):
        state = {'messages': []}
        failed = add_result(state, 'shell', {'command': 'read missing', 'action': 'analysis'}, 'NO_SUCH_PATH', ok=False)
        add_result(state, 'shell', {'command': 'read also-missing', 'action': 'analysis'}, 'NOT_A_DIRECTORY', ok=False)
        working = add_result(state, 'shell', {'command': 'read actual/path', 'action': 'analysis'}, 'FIRST_REAL_PAGE')
        second = add_result(state, 'shell', {'command': 'read actual/path --offset 1', 'action': 'analysis'}, 'SECOND_PAGE')
        for index in range(20):
            add_result(state, 'shell', {'command': f'next {index}', 'action': 'analysis'}, f'PAGE_{index}')
        latest = add_result(state, 'shell', {'command': 'verify', 'action': 'analysis'}, 'LATEST_FAILURE', ok=False)
        original = copy.deepcopy(state['messages'])
        memory = working_snapshot(state)
        self.assertEqual([row['message_index'] for row in memory['observations'][:2]], [working, second])
        self.assertEqual(memory['observations'][-1]['message_index'], latest)
        self.assertFalse(memory['observations'][-1]['ok'])
        self.assertIn('NO_SUCH_PATH', read_history(state, {'message_index': failed, 'view': 'content'})['text'])
        self.assertEqual(state['messages'], original)

    def test_patch_artifact_facts_survive_unrelated_reads_and_receipts_keep_hash_identity(self):
        state = {'messages': []}
        edit = add_result(state, 'edit_source', {}, json.dumps({
            'ok': True, 'patched': '/work/edited.py', 'sha256': 'edit-hash'}))
        built = add_result(state, 'build_patch', {}, json.dumps({
            'ok': True, 'archive': '/work/patch.tar.gz', 'sha256': 'package-one'}))
        verified = add_result(state, 'verify_patch', {}, json.dumps({
            'ok': False, 'status': 'inconclusive', 'patch_sha256': 'package-one',
            'verification_ref': 'actual-receipt', 'issues': ['normal: LoadError']}), ok=False)
        for index in range(12):
            add_result(state, 'shell', {'command': f'read {index}'}, 'ordinary output')
        artifacts = working_snapshot(state)['patch_artifacts']
        for name, index in (('edit_source', edit), ('build_patch', built), ('verify_patch', verified)):
            self.assertEqual(artifacts[name]['read_call']['arguments']['message_index'], index)
        self.assertEqual(artifacts['build_patch']['archive'], '/work/patch.tar.gz')
        self.assertTrue(artifacts['verify_patch']['matches_latest_build'])
        self.assertFalse(artifacts['verify_patch']['ok'])
        self.assertIn('normal: LoadError', str(memory_message(state)))
        add_result(state, 'build_patch', {}, json.dumps({
            'ok': True, 'archive': '/work/new.tar.gz', 'sha256': 'package-two'}))
        self.assertFalse(working_snapshot(state)['patch_artifacts']['verify_patch']['matches_latest_build'])
        add_result(state, 'build_patch', {}, json.dumps({'ok': False, 'error': 'bad source'}), ok=False)
        self.assertEqual(working_snapshot(state)['patch_artifacts']['build_patch']['sha256'], 'package-two')

    def test_named_evidence_remains_locatable_without_reexecuting_after_long_repetition(self):
        state = history()
        state['messages'].append({'role': 'user', 'content': 'NEW_USER_BOUNDARY', '_context_contract': True})
        original = copy.deepcopy(state['messages'])
        view = self.agent._light_model_messages(self.task, self.route, state)
        catalog = next(m['content'] for m in view if str(m.get('content', '')).startswith('历史证据目录'))
        self.assertIn('dispatch_fixture', catalog)
        self.assertIn('same_request_and_result_count', catalog)
        self.assertIn('NEW_USER_BOUNDARY', str(view))
        self.assertTrue(_complete_tool_history(view))
        self.assertLessEqual(estimate_input_tokens(view, self.agent._model_tool_schemas()), 10650)
        found = read_history(state, {'view': 'index', 'find': 'dispatch_fixture'})
        self.assertEqual(found['total_matches'], 3)
        for row in found['items']:
            page = read_history(state, {'message_index': row['message_index'], 'view': 'content', 'length': 4000})
            self.assertEqual(page['text'], original[row['message_index']]['content'])
        self.assertEqual(state['messages'], original)

    def test_index_searches_hidden_middle_and_pages_only_original_tool_sources(self):
        state = {'messages': []}
        for i in range(25):
            add_result(state, 'shell', {'command': f'read {i}'}, 'prefix ' * 1000 + 'NEEDLE' + ' suffix' * 1000)
        add_result(state, 'history_read', {}, 'NEEDLE')
        add_result(state, 'evidence_memory', {}, 'NEEDLE')
        state['messages'].append({'role': 'assistant', 'content': 'NEEDLE'})
        indices, cursor = [], 0
        while True:
            result = read_history(state, {'view': 'index', 'find': 'NEEDLE', 'offset': cursor, 'limit': 7})
            self.assertEqual(result['total_matches'], 25)
            self.assertLess(len(json.dumps(result, ensure_ascii=False)), 6000)
            indices.extend(item['message_index'] for item in result['items'])
            if not result['has_more']:
                break
            self.assertGreater(result['next_offset'], cursor)
            cursor = result['next_offset']
        self.assertEqual(indices, list(range(1, 50, 2)))
        page = read_history(state, {'message_index': 1, 'view': 'content', 'find': 'NEEDLE', 'length': 100})
        self.assertIn('NEEDLE', page['text'])
        self.assertEqual(page['match_offset'], 7000)
        self.assertEqual(read_history({'messages': []}, {'view': 'index', 'find': 'NEEDLE'})['items'], [])

    def test_memory_latest_revision_survives_real_checkpoint_without_old_belief(self):
        state = history()
        arguments = {'action': 'update', 'base_revision': 0, 'upsert': [{
            'id': 'function', 'kind': 'fact', 'text': 'SOURCE_BASED_CURRENT_FINDING',
            'sources': [{'message_index': 3, 'quote': 'dispatch_fixture'}]},
            {'id': 'guess', 'kind': 'hypothesis', 'text': 'OBSOLETE_GUESS'}]}
        result = memory_execute(state, arguments)
        self.assertTrue(result['ok'], result)
        add_result(state, 'evidence_memory', arguments, json.dumps(result))
        updated = {'action': 'update', 'base_revision': 1, 'remove': ['guess'], 'upsert': [{
            'id': 'gap', 'kind': 'hypothesis', 'text': 'MISSING_VALIDATION'}]}
        result = memory_execute(state, updated)
        self.assertTrue(result['ok'], result)
        add_result(state, 'evidence_memory', updated, json.dumps(result))
        raw = copy.deepcopy(state['messages'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bb = Blackboard(root / 'bb.db')
            try:
                journal = ExecutionJournal(bb, root, 'fixture')
                journal.checkpoint('ctf_conversation', 'one:r', 'turn_saved', state)
                restored = journal.latest_checkpoint('ctf_conversation', 'one:r')['state']
            finally:
                bb.close()
        view = self.agent._light_model_messages(self.task, self.route, restored)
        text = json.dumps(view, ensure_ascii=False)
        self.assertIn('SOURCE_BASED_CURRENT_FINDING', text)
        self.assertIn('MISSING_VALIDATION', text)
        self.assertIn('dispatch_fixture', text)
        self.assertNotIn('OBSOLETE_GUESS', text)
        self.assertEqual(restored[MEMORY_KEY]['revision'], 2)
        self.assertEqual(restored['messages'], raw)
        self.assertLessEqual(estimate_input_tokens(view, self.agent._model_tool_schemas()), 10650)
        self.assertTrue(_complete_tool_history(view))

    def test_next_call_advances_content_and_remembers_repeat_without_changing_requested_page(self):
        state = {'messages': []}
        index = add_result(state, 'shell', {}, 'FIRST' + 'x' * 2100 + 'RECEIPT-TAIL')
        args = {'message_index': index, 'view': 'content'}
        first = read_history(state, args)
        again = read_history(state, args)
        self.assertEqual(again['text'], first['text'])
        self.assertEqual(again['same_page_count'], 2)
        self.assertEqual(again['next_call']['arguments']['offset'], 2000)
        last = read_history(state, first['next_call']['arguments'])
        self.assertIn('RECEIPT-TAIL', last['text'])
        self.assertFalse(last['has_more'])
        self.assertNotIn('next_call', last)
        self.assertEqual(working_snapshot(state)['history_reads'][-1]['next_offset'], 2117)
        state['messages'][index]['content'] = 'CHANGED'
        self.assertFalse(read_history(state, first['next_call']['arguments'])['ok'])

    def test_index_next_call_preserves_search_but_content_continuation_does_not_skip_between_hits(self):
        state = {'messages': []}
        for _ in range(3):
            add_result(state, 'shell', {}, 'NEEDLE ' + 'x' * 200 + 'NEEDLE')
        first = read_history(state, {'view': 'index', 'find': 'NEEDLE', 'limit': 1})
        second = read_history(state, first['next_call']['arguments'])
        self.assertEqual([first['items'][0]['message_index'], second['items'][0]['message_index']], [1, 3])
        self.assertEqual(first['search_scope'], 'tool_arguments_and_message_content')
        page = read_history(state, {**first['items'][0]['read_call']['arguments'], 'length': 30})
        self.assertNotIn('find', page['next_call']['arguments'])
        following = read_history(state, page['next_call']['arguments'])
        self.assertEqual(following['offset'], 30)
        self.assertEqual(following['text'], 'x' * 30)

    def test_invalid_index_offers_real_user_source_and_directory_without_fabricating_a_page(self):
        state = {'messages': [{'role': 'system', 'content': 'rules'},
                              {'role': 'user', 'content': 'ORIGINAL_TASK'}]}
        result = read_history(state, {'message_index': 973, 'view': 'content'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['valid_message_range'], [0, 1])
        original = read_history(state, result['user_message_calls'][0]['arguments'])
        self.assertEqual(original['text'], 'ORIGINAL_TASK')
        self.assertNotIn('text', result)

    def test_memory_rejects_forged_self_and_stale_revision_updates_atomically(self):
        state = history()
        good = {'action': 'update', 'base_revision': 0, 'upsert': [{
            'id': 'one', 'kind': 'fact', 'text': 'supported',
            'sources': [{'message_index': 3, 'quote': 'dispatch_fixture'}]}]}
        for index, quote in ((2, 'MODEL_HYPOTHESIS'), (3, 'FORGED_QUOTE')):
            bad = copy.deepcopy(good)
            bad['upsert'][0]['sources'] = [{'message_index': index, 'quote': quote}]
            self.assertFalse(memory_execute(state, bad)['ok'])
            self.assertNotIn(MEMORY_KEY, state)
        self.assertTrue(memory_execute(state, good)['ok'])
        before = copy.deepcopy(state[MEMORY_KEY])
        self.assertFalse(memory_execute(state, good)['ok'])
        index = add_result(state, 'evidence_memory', {}, 'dispatch_fixture')
        bad = copy.deepcopy(good)
        bad['base_revision'] = 1
        bad['upsert'][0]['sources'][0]['message_index'] = index
        self.assertFalse(memory_execute(state, bad)['ok'])
        self.assertEqual(state[MEMORY_KEY], before)
        state['messages'][3]['content'] = 'changed source'
        self.assertIn('stale', memory_message(state)['content'])

    def test_old_checkpoint_sources_are_validated_without_rewriting_messages(self):
        state = history()
        for message in state['messages']:
            message.pop('_light_observation', None)
        raw = copy.deepcopy(state['messages'])
        # 原始记录已离开最近 12 条，但仍可用 first_observation 的精确引用恢复。
        result = memory_execute(state, {'action': 'update', 'base_revision': 0, 'upsert': [{
            'id': 'legacy', 'kind': 'fact', 'text': 'known function',
            'sources': [{'message_index': 3, 'quote': 'dispatch_fixture'}]}]})
        self.assertTrue(result['ok'], result)
        self.assertEqual(state['messages'], raw)
        self.assertNotIn('normal_evidence_memory', state)

    def test_catalog_does_not_merge_different_sources_with_equal_shortened_paths(self):
        state = {'messages': []}
        for name in ['a', 'b']:
            add_result(state, 'shell', {'path': '/' + 'prefix' * 20 + name + 'suffix' * 20}, name)
        self.assertEqual(len(catalog_rows(tool_records(state['messages']))), 2)
        add_result(state, 'shell', {'path': '/' + 'prefix' * 20 + 'a' + 'suffix' * 20}, 'failure', ok=False)
        self.assertEqual(len(catalog_rows(tool_records(state['messages']))), 3)

    def test_capacity_failure_keeps_existing_notes_and_failed_observation_is_not_a_fact(self):
        state = history()
        failed = add_result(state, 'shell', {'command': 'validate'}, 'ACTUAL_FAILURE', ok=False)
        args = {'action': 'update', 'base_revision': 0, 'upsert': [{
            'id': 'failure', 'kind': 'fact', 'text': 'failed path',
            'sources': [{'message_index': failed, 'quote': 'ACTUAL_FAILURE'}]}]}
        self.assertFalse(memory_execute(state, args)['ok'])
        args['upsert'][0]['kind'] = 'rejected'
        self.assertTrue(memory_execute(state, args)['ok'])
        saved = copy.deepcopy(state[MEMORY_KEY])
        result = memory_execute(state, {'action': 'update', 'base_revision': 1, 'upsert': [
            {'id': str(i), 'kind': 'hypothesis', 'text': 'x' * 900} for i in range(6)]})
        self.assertFalse(result['ok'])
        self.assertEqual(state[MEMORY_KEY], saved)


class LightMemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_memory_and_repeated_failure_prompt_recovery_without_new_evidence(self):
        fixture = test_single_context.SingleContextTests()
        fixture.setUp()
        agent, state = fixture.agent, history()
        rows = copy.deepcopy(state['evidence_rows'])
        args = {'action': 'update', 'base_revision': 0, 'upsert': [{
            'id': 'pending', 'kind': 'hypothesis', 'text': '需要对照原始材料'}]}
        call = {'name': 'evidence_memory', 'arguments': args}
        first = await agent._run_tool_call_inner(fixture.task, fixture.route, state, call)
        self.assertTrue(first.ok)
        args['base_revision'] = 1
        repeated = await agent._run_tool_call_inner(fixture.task, fixture.route, state, call)
        agent._observe_tool_result(state, call, repeated)
        self.assertTrue(repeated.ok)
        self.assertTrue(state['review_due']['unchanged'])
        sent = agent._light_model_messages(fixture.task, fixture.route, state)
        self.assertIn('内容未变化不代表保存失败', str(sent))
        args['base_revision'] = 99
        for _ in range(2):
            failed = await agent._run_tool_call_inner(fixture.task, fixture.route, state, call)
            agent._observe_tool_result(state, call, failed)
        self.assertEqual(state['review_due']['tool'], 'evidence_memory')
        self.assertEqual(state['review_due']['same_request_count'], 2)
        self.assertEqual(state['evidence_rows'], rows)
        args['upsert'][0]['text'] = '只改笔记文案，修订冲突仍然相同'
        changed_request = await agent._run_tool_call_inner(fixture.task, fixture.route, state, call)
        agent._observe_tool_result(state, call, changed_request)
        self.assertEqual(state['review_due']['same_request_count'], 1)
        self.assertEqual(state['review_due']['same_failure_count'], 3)

    async def test_repeated_invalid_history_reads_trigger_recovery_without_becoming_evidence(self):
        fixture = test_single_context.SingleContextTests()
        fixture.setUp()
        agent, state = fixture.agent, history()
        call = {'name': 'history_read', 'arguments': {'message_index': 973, 'view': 'content'}}
        previous_rows = copy.deepcopy(state['evidence_rows'])
        for _ in range(2):
            outcome = await agent._run_tool_call_inner(fixture.task, fixture.route, state, call)
            state['messages'].append({'role': 'tool', 'tool_call_id': 'bad-index', 'content': outcome.text})
            agent._observe_tool_result(state, call, outcome)
        self.assertEqual(state['review_due']['tool'], 'history_read')
        self.assertEqual(state['review_due']['same_request_count'], 2)
        self.assertEqual(state['evidence_rows'], previous_rows)

    async def test_memory_update_cannot_make_user_contract_unfit_or_replace_previous_revision(self):
        fixture = test_single_context.SingleContextTests()
        fixture.setUp()
        agent = fixture.agent
        state = {'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'USER_CONTRACT'}]}
        agent._minimal_tool_view = True
        budget = estimate_input_tokens(state['messages'], agent._model_tool_schemas()) + 250
        agent.ctx.gateway.context_budget = lambda **kw: budget
        agent._light_model_messages(fixture.task, fixture.route, state)
        result = await agent._run_tool_call_inner(fixture.task, fixture.route, state, {
            'name': 'evidence_memory', 'arguments': {'action': 'update', 'base_revision': 0, 'upsert': [{
                'id': 'large', 'kind': 'hypothesis', 'text': '待核实' * 250}]}})
        self.assertFalse(result.ok)
        self.assertIn('原修订未改变', result.text)
        self.assertNotIn(MEMORY_KEY, state)

    async def test_direct_and_deferred_memory_tools_use_same_state_without_model_calls(self):
        fixture = test_single_context.SingleContextTests()
        fixture.setUp()
        state = history()
        args = {'action': 'update', 'base_revision': 0, 'upsert': [{
            'id': 'one', 'kind': 'hypothesis', 'text': 'Need an actual validation'}]}
        result = await fixture.agent._run_tool_call_inner(fixture.task, fixture.route, state, {
            'name': 'tool_invoke', 'arguments': {'name': 'evidence_memory', 'arguments': args}})
        self.assertTrue(result.ok, result.text)
        read = await fixture.agent._run_tool_call_inner(fixture.task, fixture.route, state, {
            'name': 'evidence_memory', 'arguments': {'action': 'read'}})
        self.assertEqual(json.loads(read.text)['revision'], 1)
        index = await fixture.agent._run_tool_call_inner(fixture.task, fixture.route, state, {
            'name': 'history_read', 'arguments': {'view': 'index', 'find': 'dispatch_fixture'}})
        self.assertTrue(index.ok)
        self.assertEqual(json.loads(index.text)['total_matches'], 3)
