from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from spiritscout.ctf.evidence_memory import execute, snapshot
from spiritscout.gateway.normal_context import observe, project
from tests.test_normal_context import history


class EvidenceMemoryTests(unittest.TestCase):
    def test_conditioned_rejection_survives_archive_and_can_be_revised_for_new_scope(self):
        item = self.args['upsert'][0]
        item.update(kind='rejected', text='该输入未产生预期输出', scope='实例 A / 身份 1',
                    conditions='输入 X；响应版本 7', revalidate_when='实例、身份、版本变化或出现新的区分性输入')
        self.assertTrue(self.remember()['ok'])
        self.state['messages'].extend(history(30)[4:])
        view = project(self.state, [], 9000)['messages']
        self.assertIn('实例 A / 身份 1', str(view))
        self.assertIn('新的区分性输入', str(view))
        self.args['base_revision'] = 1
        item.update(kind='hypothesis', scope='实例 B / 身份 2', text='条件变化，需重新检验')
        result = execute(self.state, self.args)
        self.assertTrue(result['ok'])
        self.assertEqual(result['revision'], 2)
        self.assertEqual(len(result['items']), 1)

    def test_execution_success_never_certifies_target_origin_and_mixed_output_is_supported(self):
        source = self.args['upsert'][0]['sources'][0]
        # 字符串确实出现在执行结果，但默认不推断它来自目标。
        result = execute(self.state, self.args)
        saved = result['items'][0]['sources'][0]
        self.assertEqual(saved['runtime_source'], 'tool_output')
        self.assertEqual(saved['origin_assessment'], 'unknown')
        self.args['base_revision'] = 1
        source['origin_assessment'] = 'generated'
        self.assertTrue(execute(self.state, self.args)['ok'])
        self.args['base_revision'] = 2
        source['origin_assessment'] = 'target_response'
        self.assertFalse(execute(self.state, self.args)['ok'])
        source.update(origin_assessment='mixed', origin_basis='脚本输出同时包含候选输入和实际响应')
        result = execute(self.state, self.args)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['sources'][0]['origin_assessment'], 'mixed')
        self.args['base_revision'] = 3
        source.update(origin_assessment='target_response', origin_basis='引用来自已保存响应文件的正文片段')
        result = execute(self.state, self.args)
        self.assertTrue(result['ok'])
        self.assertEqual(result['items'][0]['sources'][0]['runtime_source'], 'tool_output')

    def test_source_status_refresh_appends_once_and_survives_checkpoint(self):
        self.remember()
        first = project(self.state, [], 1000000)['messages']
        # 原始来源不可变指纹用于检测变化；刷新不能重写旧笔记。
        self.state['messages'][3]['content'] += '\nnew source version'
        second = project(self.state, [], 1000000)['messages']
        self.assertEqual(first[4:], second[4:-1])
        self.assertIn('stale', second[-1]['content'])
        restored = json.loads(json.dumps(self.state))
        self.assertEqual(second, project(restored, [], 1000000)['messages'])
        self.state['messages'][3]['content'] = self.state['messages'][3]['content'].removesuffix('\nnew source version')
        third = project(self.state, [], 1000000)['messages']
        self.assertEqual(second[4:], third[4:-1])
        self.assertIn('matched', third[-1]['content'])

    def test_shared_notes_cannot_launder_generated_data_into_independent_evidence(self):
        self.state['messages'][3]['_normal_observation']['tool'] = 'blackboard_read'
        result = execute(self.state, self.args)
        self.assertFalse(result['ok'])
        self.assertEqual(snapshot(self.state)['revision'], 0)

    def test_lightweight_schema_and_storage_do_not_gain_cloud_fields(self):
        from spiritscout.ctf.evidence_memory import NORMAL_SCHEMA, SCHEMA, normal_schema
        from spiritscout.ctf.light_memory import MEMORY_SCHEMA

        for schema in (SCHEMA, MEMORY_SCHEMA):
            fields = schema['function']['parameters']['properties']['upsert']['items']['properties']
            self.assertNotIn('scope', fields)
            self.assertNotIn('origin_assessment', fields['sources']['items']['properties'])
        self.assertIn('scope', NORMAL_SCHEMA['function']['parameters']['properties']['upsert']['items']['properties'])
        original = SCHEMA['function']['description']
        try:
            SCHEMA['function']['description'] = 'snapshot-specific'
            self.assertEqual(normal_schema()['function']['description'], 'snapshot-specific')
        finally:
            SCHEMA['function']['description'] = original
        self.state['messages'][3]['_light_observation'] = {'tool': 'python', 'ok': True}
        result = execute(self.state, self.args, memory_key='light_evidence_memory',
                         observation_key='_light_observation')
        self.assertTrue(result['ok'])
        self.assertNotIn('scope', result['items'][0])
        self.assertNotIn('origin_assessment', result['items'][0]['sources'][0])

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
            {'role': 'tool', 'tool_call_id': 'memory', 'content': json.dumps(result, ensure_ascii=False)}])
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
        self.assertEqual(json.loads(memory['content'])['items'][0]['source_status'], 'matched')
        self.assertIn('stale', result['messages'][-1]['content'])
        self.assertEqual(result['messages'], project(self.state, [], 9000)['messages'])
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
