"""审计投影展示真实步骤、产出及其进入后续上下文的关系。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritscout.web.process_graph import project_process, read_process_graph


class ProcessGraphTests(unittest.TestCase):
    def test_projection_is_bounded_cached_and_does_not_duplicate_dispatch_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            events = [{'seq': i, 'actor': 'a', 'type': 'agent.turn', 'detail': {
                'task_id': 't', 'route_id': 'r', 'turn': i}} for i in range(1, 501)]
            path.write_text(''.join(json.dumps(e) + '\n' for e in events))
            graph = read_process_graph(path)
            self.assertTrue(graph['has_more'])
            self.assertEqual(len(graph['nodes']), 320)
            with patch('spiritscout.web.process_graph.read_activity_page', side_effect=AssertionError):
                graph['nodes'].clear()
                self.assertEqual(len(read_process_graph(path)['nodes']), 320)
        graph = project_process([
            {'seq': 1, 'actor': 'a', 'type': 'agent.tool_call', 'detail': {
                'task_id': 't', 'route_id': 'r', 'calls': [{'id': 'internal', 'name': 'investigate'}]}},
            {'seq': 2, 'actor': 'a', 'type': 'agent.turn', 'detail': {
                'task_id': 't', 'route_id': 'r', 'turn': 1}},
            {'seq': 3, 'actor': 'a', 'type': 'agent.tool_result', 'detail': {
                'task_id': 't', 'route_id': 'r', 'id': 'internal', 'ok': True, 'note': '调度摘要'}},
        ])
        self.assertEqual(len(graph['nodes']), 1)

    def test_results_feed_next_turn_without_inventing_parallel_tasks(self) -> None:
        events = []

        def add(kind: str, **detail) -> None:
            events.append({'seq': len(events) + 1, 'actor': 'web-a', 'type': kind,
                           'detail': {'task_id': 't', 'route_id': 'r', **detail}})

        add('agent.turn', turn=1)
        add('agent.tool_call', turn=1, calls=[{'id': 'c1', 'name': 'http_request'}])
        add('agent.tool_result', id='c1', name='http_request', ok=True, result='入口响应')
        add('agent.turn', turn=2)
        add('agent.tool_call', turn=2, calls=[{'id': 'c2', 'name': 'shell'}])
        add('agent.tool_result', id='c2', name='shell', ok=True, result='证据', output_file='out.log')
        add('agent.turn', turn=3)
        graph = project_process(events)
        nodes = graph['nodes']
        steps = [n for n in nodes if n['kind'] == 'step']
        outputs = [n for n in nodes if n['kind'] == 'observation']
        artifact = next(n for n in nodes if n['kind'] == 'artifact')
        self.assertEqual(len(steps), 3)
        self.assertEqual({n['task_id'] for n in nodes}, {'t'})
        self.assertIn({'source': outputs[0]['id'], 'target': steps[1]['id'],
                       'kind': 'context'}, graph['edges'])
        self.assertIn({'source': artifact['id'], 'target': steps[2]['id'],
                       'kind': 'context'}, graph['edges'])

    def test_ambiguous_old_events_are_not_attached_to_another_task(self) -> None:
        events = [{'actor': 'same', 'seq': i, 'type': 'agent.turn',
                   'detail': {'task_id': t, 'route_id': t, 'route': 'solve', 'turn': 1}}
                  for i, t in enumerate(['a', 'b'])]
        events.append({'actor': 'same', 'seq': 3, 'type': 'agent.note',
                       'detail': {'route': 'solve', 'text': '未知归属'}})
        graph = project_process(events)
        self.assertEqual(len(graph['nodes']), 2)
        self.assertFalse(graph['edges'])

    def test_failed_results_are_observations_not_verified_findings(self) -> None:
        graph = project_process([
            {'seq': 1, 'actor': 'a', 'type': 'agent.turn',
             'detail': {'task_id': 't', 'route_id': 'r', 'turn': 1}},
            {'seq': 2, 'actor': 'a', 'type': 'agent.tool_result',
             'detail': {'task_id': 't', 'route_id': 'r', 'ok': False, 'result': '<script>fail</script>'}},
            {'seq': 3, 'actor': 'a', 'type': 'agent.delta',
             'detail': {'task_id': 't', 'route_id': 'r', 'turn': 1,
                        'kind': 'reasoning', 'text': '不展示内部思考'}},
        ])
        self.assertEqual(graph['nodes'][-1]['status'], 'failed')
        self.assertEqual(graph['nodes'][-1]['kind'], 'observation')
        self.assertNotIn('不展示内部思考', str(graph))

    def test_late_results_enter_next_context_without_rewinding_active_turn(self) -> None:
        base = {'task_id': 't', 'route_id': 'r'}
        details = [
            ('agent.turn', {'turn': 1}),
            ('agent.tool_call', {'turn': 1, 'calls': [{'id': 'late', 'name': 'shell'}]}),
            ('agent.turn', {'turn': 2}),
            ('agent.tool_result', {'turn': 1, 'id': 'late', 'name': 'shell', 'result': '迟到结果'}),
            ('agent.turn', {'turn': 3}),
            ('task.cancelled', {}),
        ]
        graph = project_process([{'seq': i, 'actor': 'a', 'type': kind,
                                  'detail': {**base, **detail}}
                                 for i, (kind, detail) in enumerate(details)])
        result = next(n for n in graph['nodes'] if n['kind'] == 'observation')
        last = next(n for n in graph['nodes'] if n['kind'] == 'step' and n['turn'] == 3)
        self.assertIn({'source': result['id'], 'target': last['id'], 'kind': 'context'}, graph['edges'])
        self.assertEqual(last['status'], 'cancelled')
