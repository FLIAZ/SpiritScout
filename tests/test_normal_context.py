from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from spiritscout.gateway.normal_context import observe, project


def history(count=10):
    messages = [{'role': 'system', 'content': 'system contract'}, {'role': 'user', 'content': '用户原话与授权范围'}]
    for i in range(count):
        messages.extend([
            {'role': 'assistant', 'content': '', 'reasoning_content': '推导' * 4000,
             'tool_calls': [{'id': f'c{i}', 'type': 'function', 'function': {'name': 'sandbox_exec', 'arguments': '{"command":"read"}'}}]},
            {'role': 'tool', 'tool_call_id': f'c{i}', 'content': f'执行 {i}\n' + 'raw code/output\n' * 800},
        ])
    return messages


class NormalContextTests(unittest.TestCase):
    def test_archive_preserves_raw_user_contract_and_recent_pairs(self):
        original = history()
        original.insert(8, {'role': 'user', 'content': '新要求，保留所有字节'})
        state = {'messages': copy.deepcopy(original)}
        result = project(state, [], 16000)
        self.assertGreater(result['archived_turns'], 0)
        self.assertLess(result['estimated_tokens'], result['original_tokens'])
        self.assertEqual(state['messages'], original)
        self.assertEqual(result['messages'][-8:], original[-8:])
        self.assertTrue(all(m in result['messages'] for m in original if m['role'] in {'user', 'system'}))
        ids = {c['id'] for m in result['messages'] for c in m.get('tool_calls', [])}
        self.assertEqual(ids, {m['tool_call_id'] for m in result['messages'] if m['role'] == 'tool'})
        self.assertTrue(any('history_read' in str(m['content']) for m in result['messages']))

    def test_incomplete_and_contract_groups_never_archive(self):
        messages = history()
        messages[2]['_context_contract'] = True
        messages[4]['tool_calls'].append({'id': 'missing', 'function': {'name': 'tool'}})
        result = project({'messages': messages}, [], 12000)
        calls = [c['id'] for m in result['messages'] for c in m.get('tool_calls', [])]
        self.assertIn('c0', calls)
        self.assertIn('missing', calls)

    def test_repetition_hint_stays_stable_when_appending_and_raw_is_readable(self):
        state = {'messages': [{'role': 'tool', 'content': 'raw output', 'tool_call_id': 'a'}]}
        call = {'name': 'sandbox_exec', 'arguments': {'command': 'read'}}
        outcome = SimpleNamespace(ok=True, stop=False, text='same', metadata={'observation': {'comparison_scope': 'raw_streams', 'digest': 'same'}})
        observe(state, call, outcome)
        state['messages'].append({'role': 'tool', 'content': 'raw output', 'tool_call_id': 'b'})
        observe(state, call, outcome)
        first = project(state, [])['messages']
        self.assertIn('相同内容 2 次', first[-1]['content'])
        state['messages'].append({'role': 'user', 'content': 'next'})
        second = project(state, [])['messages']
        self.assertEqual(first, second[:-1])
        self.assertEqual(state['messages'][1]['content'], 'raw output')

    def test_identical_truncated_preview_is_not_full_result_equivalence(self):
        state = {'messages': [{'role': 'tool', 'content': 'same preview'}]}
        outcome = SimpleNamespace(ok=True, stop=False, text='same', metadata={'observation': {'comparison_scope': 'display_preview', 'digest': 'same'}})
        for _ in range(3):
            observe(state, {'name': 'sandbox_exec', 'arguments': {'command': 'read'}}, outcome)
        self.assertNotIn('repeated', state['messages'][-1]['_normal_observation'])

    def test_repeated_projection_is_deterministic_and_does_not_alias_history(self):
        state = {'messages': history()}
        first = project(state, [], 16000)
        second = project(state, [], 16000)
        self.assertEqual(first['messages'], second['messages'])
        self.assertEqual(second['new_archived_turns'], 0)
        second['messages'][-2]['tool_calls'][0]['function']['arguments'] = 'modified'
        self.assertNotEqual(state['messages'][-2]['tool_calls'][0]['function']['arguments'], 'modified')
