"""公开阶段目录独立于聊天页，且不会把推理／工具回执当成探索正文。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritscout.web.exploration_stages import (
    _LIMIT,
    _cache,
    public_digest,
    read_public_stages,
)


class PublicStagesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'audit.jsonl'
        self.seq = 0

    def add(self, event_type, **detail):
        self.seq += 1
        with self.path.open('a') as stream:
            stream.write(json.dumps({'seq': self.seq, 'type': event_type, 'actor': 'web',
                                     'detail': {'task_id': 't', 'route_id': 'r', **detail}}) + '\n')

    def test_public_stages_keep_scope_and_output_provenance(self):
        self.add('agent.delta', turn=1, kind='reasoning', text='不可放入图的内部推理')
        self.add('agent.progress', turn=1, text='先验证入口')
        self.add('agent.tool_call', turn=1, calls=[{'id': 'c1', 'name': 'http_request', 'arguments': {'secret': '参数原文'}}])
        self.add('agent.progress', turn=2, text='入口要求登录，核对访问边界')
        self.add('agent.progress', turn=1, route_id='parallel', text='另一分支')
        self.add('task.done')
        page = read_public_stages(self.path)
        self.assertEqual(len(page['items']), 3)
        self.assertEqual(page['items'][0]['tool_call_ids'], ['c1'])
        self.assertEqual({r['status'] for r in page['items']}, {'done'})
        self.assertNotIn('内部推理', str(page))
        self.assertNotIn('参数原文', str(page))
        self.assertFalse(page['has_more'])

    def test_incremental_reads_partial_lines_and_replacement(self):
        self.add('agent.progress', turn=1, text='第一步')
        first = read_public_stages(self.path)
        cache = _cache(str(self.path.resolve()))
        with patch.object(cache, 'consume', wraps=cache.consume) as consume:
            self.assertEqual(read_public_stages(self.path), first)
            consume.assert_not_called()
            self.add('agent.progress', turn=2, text='第二步')
            self.assertEqual(len(read_public_stages(self.path)['items']), 2)
            self.assertEqual(consume.call_count, 1)
        with self.path.open('ab') as stream:
            stream.write(b'{"type":"agent.progress"')
        self.assertEqual(len(read_public_stages(self.path)['items']), 2)
        replacement = self.path.with_suffix('.new')
        replacement.write_text('')
        replacement.replace(self.path)
        self.assertEqual(read_public_stages(self.path)['items'], [])
        self.add('agent.progress', turn=3, text='新文件')
        result = read_public_stages(self.path)
        result['items'][0]['body'] = '不能污染缓存'
        self.assertEqual(read_public_stages(self.path)['items'][0]['body'], '新文件')

    def test_late_result_updates_its_own_stage_and_preserves_failure(self):
        self.add('agent.progress', turn=1, text='第一项验证')
        self.add('agent.tool_call', turn=1, calls=[{'id': 'slow'}])
        self.add('agent.progress', turn=2, text='另一项验证')
        self.assertEqual(read_public_stages(self.path)['items'][0]['status'], 'waiting')
        self.add('agent.tool_result', id='slow', ok=False)
        rows = read_public_stages(self.path)['items']
        self.assertEqual(rows[0]['status'], 'failed')
        self.assertEqual(rows[1]['status'], 'running')
        self.add('task.done')
        self.assertEqual(read_public_stages(self.path)['items'][0]['status'], 'failed')

    def test_bounded_directory_reports_missing_older_stages(self):
        for turn in range(1, _LIMIT + 3):
            self.add('agent.progress', turn=turn, text=f'阶段 {turn}')
        page = read_public_stages(self.path)
        self.assertEqual(len(page['items']), _LIMIT)
        self.assertTrue(page['has_more'])

    def test_tool_only_turns_keep_details_without_inventing_intentions(self):
        self.add('agent.progress', turn=1, text='验证匿名访问',
                 exploration={'intent': '核对访问边界', 'attempt': '匿名请求对照'})
        self.add('agent.tool_call', turn=2, calls=[{'id': 'second', 'name': 'http_request',
                                                'arguments': {'secret': '不能进入投影'}}])
        self.add('agent.tool_result', turn=2, id='second', name='http_request', ok=True, note='已返回')
        rows = read_public_stages(self.path)['items']
        self.assertEqual(rows[0]['exploration']['attempt'], '匿名请求对照')
        self.assertNotIn('exploration', rows[1])
        self.assertEqual(rows[1]['kind'], 'execution')
        self.assertEqual(rows[1]['tools'][0]['status'], 'done')
        self.assertNotIn('不能进入投影', str(rows))

    def test_milestone_survives_replay_and_later_plain_progress_in_same_turn(self):
        self.add('agent.progress', turn=3, text='发现账号记录重叠', exploration={
            'intent': '核对隔离', 'attempt': '账号对照', 'milestone': {
                'kind': 'discovery', 'title': '账号记录重叠', 'basis': '实际响应标识相同',
                'next': '核对归属', 'evidence_refs': ['topology:response'], 'analysis': '隐藏字段'}})
        self.add('agent.tool_call', turn=3, calls=[{'id': 'check', 'name': 'http_probe'}])
        self.add('agent.progress', turn=3, text='继续核对归属')
        self.add('agent.tool_result', turn=3, id='check', ok=True, note='响应已保存')
        first = read_public_stages(self.path)
        row = first['items'][0]
        self.assertEqual(row['exploration']['milestone']['title'], '账号记录重叠')
        self.assertEqual(row['tools'][0]['status'], 'done')
        self.assertNotIn('隐藏字段', str(first))
        self.assertEqual(read_public_stages(self.path), first)

    def test_historical_annotations_are_read_only_and_bound_to_public_source(self):
        self.add('agent.progress', turn=1, text='正在核对入口')
        page = read_public_stages(self.path)
        annotation = {'version': 1, 'source_digest': public_digest(page['items']), 'groups': [
            {'task_id': 't', 'route_id': 'r', 'turns': [1], 'intent': '定位入口', 'attempt': '核对公开线索'}]}
        sidecar = self.path.with_name('topology-intents.json')
        sidecar.write_text(json.dumps(annotation))
        before = self.path.read_bytes()
        row = read_public_stages(self.path)['items'][0]
        self.assertEqual(row['exploration']['attempt'], '核对公开线索')
        self.assertEqual(row['semantic_origin'], 'reviewed_public_progress')
        self.assertEqual(self.path.read_bytes(), before)
        self.add('agent.progress', turn=2, text='原文新增后需要重新核对归类')
        self.assertNotIn('exploration', read_public_stages(self.path)['items'][0])

    def test_web_state_exposes_stages_without_writing_blackboard_or_audit(self):
        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import BlackboardLayer
        from spiritscout.web.app import _read_state
        from spiritscout.web.sessions import Session

        session = Session(session_id='fixture', mode='ctf', target='fixture',
                          workspace=self.path.parent, status='completed')
        bb = Blackboard(session.bb_path)
        bb.write(BlackboardLayer.TOPOLOGY, 'fact', {'kind': 'finding', 'text': '已观察'})
        bb.close()
        self.add('agent.progress', turn=1, text='公开阶段')
        before = self.path.read_bytes(), session.bb_path.read_bytes()
        state = _read_state(session)
        self.assertEqual(state['topology']['public_stages']['items'][0]['body'], '公开阶段')
        self.assertEqual(before, (self.path.read_bytes(), session.bb_path.read_bytes()))


if __name__ == '__main__':
    unittest.main()
