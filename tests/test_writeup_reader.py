"""单题报告正文材料去除编排字段，技术原文与追溯映射保持完整。"""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from spiritscout.ctf.writeup import _writer_material, writeup_body
from spiritscout.ctf.writeup_style import fallback_writeup


class WriteupReaderTests(unittest.IsolatedAsyncioTestCase):
    def digest(self):
        return {'challenges': [{'id': 'mission-1', 'title': '流量分析'}],
                'flags': [{'value': 'fixture-answer', 'flag_id': 'flag-private', 'found_by': 'ctf_misc',
                           'challenge_id': 'mission-1', 'submitted': False, 'accepted': None}],
                'local_answer_completion': {'mission-1': {'completed': True, 'answer_ids': ['flag-private']}},
                'rounds': 4, 'dag': [{'title': 'internal-node'}],
                'routes_notes': [{'task_id': 'task-private', 'agent': 'ctf_misc',
                                  'routes': [{'name': 'route-private', 'notes': ['观察到查询参数分段']}]}],
                'chronology': [{'text': 'record_answer 由执行器返回 completed=true'}],
                'technical_evidence': {'executions': [{'invocation_id': 'inv-private', 'task_id': 'task-private',
                    'route_id': 'route-private', 'workdir': '/private/session', 'returncode': 0,
                    'parameters': {'command': "python -c 'completed = 1; print(completed)'"},
                    'stdout': '1', 'output_file': '/private/session/output.log'}],
                    'source_files': [{'path': 'attachments/solve.py', 'start_line': 1, 'excerpt': False,
                                      'content': 'def accepted(value):\n    return value\n'}]}}

    async def test_reader_projection_and_source_index_preserve_evidence_not_orchestration(self):
        digest = self.digest()
        original = copy.deepcopy(digest)
        material = json.loads(_writer_material(digest))
        for key in ('local_answer_completion', 'dag', 'rounds', 'routes_notes', 'flags'):
            self.assertNotIn(key, material)
        self.assertNotIn('mission-1', json.dumps(material))
        self.assertNotIn('flag-private', json.dumps(material))
        row = material['technical_evidence']['executions'][0]
        self.assertEqual(row['source_ref'], 'E1')
        self.assertNotIn('invocation_id', row)
        self.assertEqual(row['parameters'], original['technical_evidence']['executions'][0]['parameters'])
        self.assertEqual(material['technical_evidence']['source_files'][0]['content'],
                         original['technical_evidence']['source_files'][0]['content'])
        gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(
            content='# 流量分析\n\n实际输出 [E1]：\n```text\n1\n```', finish_reason='stop')))
        body = await writeup_body(gateway, digest=digest)
        prose, index = body.split('## 证据索引')
        self.assertNotIn('inv-private', prose)
        self.assertIn('**E1**', index)
        self.assertIn('inv-private', index)
        self.assertIn('**S1**', index)
        self.assertEqual(digest, original)
        self.assertEqual(gateway.chat.await_count, 1, '不为润色额外调用模型')

    async def test_fallback_does_not_present_module_notes_as_solution(self):
        body = fallback_writeup(self.digest())
        prose = body.split('## 技术证据与源码附录')[0]
        self.assertIn('待整理', prose)
        self.assertNotIn('record_answer', prose)
        self.assertNotIn('completed=true', prose)
        self.assertIn('实际标准输出', body)
        self.assertIn('def accepted', body, '保留题目中的同名变量与函数')

    async def test_empty_completion_is_still_single_question_and_not_success(self):
        digest = self.digest()
        digest['local_answer_completion'] = {}
        material = json.loads(_writer_material(digest))
        self.assertIn('尚未', material['challenges'][0]['答案记录状态'])
        self.assertNotIn('rounds', material)

    async def test_short_evidence_ids_survive_whole_item_budget_trimming(self):
        digest = self.digest()
        item = digest['technical_evidence']['executions'][0]
        digest['technical_evidence']['executions'] = [
            {**item, 'invocation_id': f'inv-{i}', 'stdout': 'x' * 12000} for i in range(8)]
        rows = json.loads(_writer_material(digest))['technical_evidence']['executions']
        self.assertLess(len(rows), 8)
        self.assertEqual(rows[-1]['source_ref'], 'E8')
        self.assertEqual(len(rows[-1]['stdout']), 12000)
