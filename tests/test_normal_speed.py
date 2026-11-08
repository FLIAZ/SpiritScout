from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from spiritscout.gateway.normal_context import observe, project
from spiritscout.gateway.observation_text import lossless_runs


class NormalSpeedTests(unittest.TestCase):
    def test_repeated_lines_are_exactly_reversible_including_counts_and_endings(self):
        for newline in ('\n', '\r\n', '\r'):
            with self.subTest(newline=newline):
                raw = ('日志 β ' + newline) * 900 + '中间唯一证据 "seq":17' + newline + ('tail' + newline) * 900 + 'EOF'
                encoded = lossless_runs(raw)
                self.assertIsNotNone(encoded)
                data = json.loads(encoded.split('\n', 1)[1])
                restored = ''.join(part if isinstance(part, str) else part['text'] * part['repeat'] for part in data['parts'])
                self.assertEqual(restored, raw)
                self.assertEqual(data['original_chars'], len(raw))
                self.assertLess(len(encoded), len(raw) / 4)

    def test_unique_or_small_outputs_are_not_changed(self):
        for raw in ('same\n' * 5, '\n'.join(f'unique {i}' for i in range(3000))):
            self.assertIsNone(lossless_runs(raw))

    def state(self):
        state = {'messages': [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': '原始要求'}]}
        for i in range(10):
            raw = f'unique-{i}\n' + 'same observation data\n' * 3000 + f'evidence-{i}'
            state['messages'].extend([
                {'role': 'assistant', 'content': '', 'tool_calls': [{'id': str(i), 'function': {'name': 'shell', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': str(i), 'content': raw}])
            observe(state, {'name': 'shell', 'arguments': {'command': str(i)}},
                SimpleNamespace(ok=True, stop=False, text=raw, metadata={'invocation_id': f'proof-{i}'}))
        return state

    def test_lossless_projection_preserves_middle_evidence_and_prefix_without_extra_call(self):
        state = self.state()
        original = copy.deepcopy(state['messages'])
        first = project(state, [], 80000)
        self.assertEqual(first['lossless_messages'], 10)
        self.assertEqual(first['archived_turns'], 0)
        self.assertEqual(state['messages'], original)
        for i in range(10):
            self.assertIn(f'evidence-{i}', str(first['messages']))
            self.assertIn(f'proof-{i}', str(first['messages']))
        state['messages'].append({'role': 'user', 'content': '继续'})
        second = project(state, [], 80000)
        self.assertEqual(first['messages'], second['messages'][:-1])

    def test_unique_history_below_actual_window_does_not_hit_arbitrary_48k_cutoff(self):
        state = self.state()
        for m in state['messages']:
            if m['role'] == 'tool':
                m['content'] = '\n'.join(f'unique record {i}' for i in range(1800))
        before = copy.deepcopy(state['messages'])
        result = project(state, [], 800000)
        self.assertGreater(result['estimated_tokens'], 48000)
        self.assertEqual(result['archived_turns'], 0)
        self.assertEqual(result['lossless_messages'], 0)
        self.assertEqual(state['messages'], before)

    def test_contract_json_is_not_recoded(self):
        state = self.state()
        state['messages'][3]['_context_contract'] = True
        first = project(state, [], 800000)
        self.assertEqual(first['messages'][3]['content'], state['messages'][3]['content'])

    def test_old_lossy_views_are_rebuilt_from_intact_history_once(self):
        state = self.state()
        raw = copy.deepcopy(state['messages'])
        state['normal_context_policy'] = 1
        state['normal_context_archives'] = {'2': {'end': 4, 'message': {'role': 'user', 'content': 'old partial preview'}}}
        result = project(state, [], 800000)
        self.assertEqual(state['normal_context_policy'], 2)
        self.assertEqual(state['messages'], raw)
        self.assertNotIn('old partial preview', str(result['messages']))
        self.assertEqual(result['messages'], project(state, [], 800000)['messages'])
