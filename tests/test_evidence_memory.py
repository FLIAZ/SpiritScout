from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from spiritscout.ctf.evidence_memory import execute, snapshot
from spiritscout.gateway.normal_context import observe, project
from tests.test_normal_context import history


class EvidenceMemoryTests(unittest.TestCase):
    def setUp(self):
        self.state = {'messages': history()[:4]}
        self.state['messages'][-1]['content'] = 'prefix\n' + 'x' * 9000 + '\n关键中间证据: generation=7\n' + 'z' * 9000
        observe(self.state, {'name': 'sandbox_exec', 'arguments': {'command': 'read'}},
                SimpleNamespace(ok=True, stop=False, text=self.state['messages'][-1]['content'], metadata={}))
        self.args = {'action': 'update', 'base_revision': 0, 'upsert': [{
            'id': 'generation', 'kind': 'fact', 'text': '当前快照记录 generation 为 7',
            'sources': [{'message_index': 3, 'quote': '关键中间证据: generation=7'}]}]}

    def remember(self):
        result = execute(self.state, self.args)
        self.state['messages'].extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'memory', 'function': {
                'name': 'evidence_memory', 'arguments': json.dumps(self.args)}}]},
            {'role': 'tool', 'tool_call_id': 'memory', 'content': json.dumps(result)}])
        observe(self.state, {'name': 'evidence_memory'}, SimpleNamespace(ok=True, stop=False,
            metadata={'memory_revision': result['revision']}))
        return result

    def test_middle_evidence_survives_archive_and_checkpoint_with_stable_prefix(self):
        self.assertTrue(self.remember()['ok'])
        self.state['messages'].extend(history()[4:])
        raw = copy.deepcopy(self.state['messages'])
        first = project(self.state, [], 9000)
        self.assertGreater(first['archived_turns'], 0)
        self.assertIn('关键中间证据: generation=7', str(first['messages']))
        self.assertTrue(any(m.get('tool_call_id') == 'memory' for m in first['messages']))
        self.assertEqual(self.state['messages'], raw)
        restored = json.loads(json.dumps(self.state))
        restored['messages'].append({'role': 'user', 'content': '继续核实'})
        self.assertEqual(first['messages'], project(restored, [], 9000)['messages'][:-1])

    def test_reject_forged_and_self_references_atomically(self):
        bad = copy.deepcopy(self.args)
        bad['upsert'][0]['sources'][0]['quote'] = 'invented'
        self.assertFalse(execute(self.state, bad)['ok'])
        bad['upsert'][0]['sources'][0] = {'message_index': 2, 'quote': 'assistant'}
        self.assertFalse(execute(self.state, bad)['ok'])
        self.assertEqual(snapshot(self.state)['revision'], 0)
        self.remember()
        bad = copy.deepcopy(self.args)
        bad['base_revision'] = 1
        bad['upsert'][0]['sources'][0] = {'message_index': 5, 'quote': 'generation'}
        self.assertFalse(execute(self.state, bad)['ok'])

    def test_revision_conflict_delete_and_failure_vs_hypothesis(self):
        execute(self.state, self.args)
        self.assertFalse(execute(self.state, self.args)['ok'])
        self.assertTrue(execute(self.state, {'action': 'update', 'base_revision': 1,
                                            'remove': ['generation']})['ok'])
        self.assertEqual(snapshot(self.state)['items'], [])
        self.state['messages'][3]['_normal_observation']['ok'] = False
        self.args['base_revision'] = 2
        self.assertFalse(execute(self.state, self.args)['ok'])
        self.args['upsert'][0]['kind'] = 'rejected'
        self.assertTrue(execute(self.state, self.args)['ok'])

    def test_changed_source_cannot_be_hidden_by_old_archive(self):
        self.remember()
        self.state['messages'].extend(history()[4:])
        project(self.state, [], 9000)
        self.state['messages'][3]['content'] = 'replacement observation'
        result = project(self.state, [], 9000)
        self.assertEqual(snapshot(self.state)['items'][0]['source_status'], 'stale')
        memory = next(m for m in result['messages'] if m.get('tool_call_id') == 'memory')
        self.assertEqual(json.loads(memory['content'])['items'][0]['source_status'], 'stale')
        self.assertIn('replacement observation', str(result['messages']))

    def test_empty_optional_digest_and_unchanged_update_do_not_create_false_conflicts(self):
        self.args['upsert'][0]['sources'][0]['expected_sha256'] = ''
        first = execute(self.state, self.args)
        self.assertTrue(first['ok'])
        self.assertTrue(first['changed'])
        self.args['base_revision'] = first['revision']
        repeated = execute(self.state, self.args)
        self.assertTrue(repeated['ok'])
        self.assertFalse(repeated['changed'])
        self.assertEqual(repeated['revision'], first['revision'])
        self.assertIn('无需重复保存', repeated['notice'])

    def test_invalid_and_mismatched_explicit_digest_still_reject_atomically(self):
        for digest in ('guessed', '0' * 64, 42):
            with self.subTest(digest=digest):
                self.args['upsert'][0]['sources'][0]['expected_sha256'] = digest
                result = execute(self.state, self.args)
                self.assertFalse(result['ok'])
                self.assertEqual(snapshot(self.state)['revision'], 0)
                self.assertIn('来源版本已经变化' if digest == '0' * 64 else 'expected_sha256', result['error'])

    def test_retire_previous_snapshot_and_keep_routes_separate(self):
        self.remember()
        other = {'messages': history()}
        self.assertEqual(snapshot(other)['items'], [])
        self.args = {'action': 'update', 'base_revision': 1, 'remove': ['generation'],
                     'upsert': [{'id': 'pending', 'kind': 'hypothesis', 'text': '重新核对版本'}]}
        self.remember()
        self.state['messages'].extend(history()[4:])
        result = project(self.state, [], 9000)
        newest = [m for m in result['messages'] if m.get('tool_call_id') == 'memory'][-1]
        self.assertEqual(json.loads(newest['content'])['revision'], 2)
        self.assertEqual(json.loads(newest['content'])['items'][0]['source_status'], 'unreferenced')
