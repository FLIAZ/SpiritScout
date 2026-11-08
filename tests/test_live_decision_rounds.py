"""主控轮次来自实时调度快照，不能等并行任务全部完成才显示。"""
from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer
from spiritscout.web.session_state import _read_state
from spiritscout.web.sessions import Session


class LiveDecisionRoundsTests(unittest.TestCase):
    def test_running_parallel_round_is_visible_and_matches_terminal_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session(session_id='round-test', mode='ctf', target='fixture', workspace=root, status='running')
            with closing(Blackboard(session.bb_path)) as bb:
                bb.write(BlackboardLayer.TASK_STATE, 'dag-snapshot', {'dag_snapshot': [], 'round': 2})
                for index in range(3):
                    bb.write(BlackboardLayer.TASK_STATE, f'task-{index}', {'task': {
                        'task_id': f'task-{index}', 'round_no': 2, 'status': 'running'}})
                bb.write(BlackboardLayer.TASK_STATE, 'scanner-control', {'kind': 'scanner_control', 'status': 'idle'})
            live = _read_state(session)
            self.assertEqual(live['decision_rounds'], 3)
            self.assertEqual(live['round_views'], [])
            self.assertEqual(len(live['tasks']), 3, '扫描控制记录不伪装成空任务')
            session.status = 'completed'
            session.summary = {'run': {'rounds_executed': 3, 'round_views': [{}, {}, {}]}}
            self.assertEqual(_read_state(session)['decision_rounds'], 3)

    def test_no_blackboard_and_legacy_task_round_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(session_id='legacy', mode='ctf', target='fixture', workspace=Path(directory), status='queued')
            self.assertEqual(_read_state(session)['decision_rounds'], 0)
            with closing(Blackboard(session.bb_path)) as bb:
                bb.write(BlackboardLayer.TASK_STATE, 'task', {'task': {'task_id': 'task', 'round_no': 0}})
            self.assertEqual(_read_state(session)['decision_rounds'], 1)
