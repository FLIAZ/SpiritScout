"""督导识别摘要及来源，进度数字和投递状态不消耗新一轮模型调用。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.ring import RingLedger
from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer


class SteeringEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.bb = Blackboard(Path(self.folder.name) / 'bb.db')
        self.addCleanup(self.bb.close)
        self.master = object.__new__(MasterAgent)
        self.master.ctx = SimpleNamespace(blackboard=self.bb, audit=Mock(), ring=RingLedger(), extra={})

    def digest(self, progress, note):
        self.bb.write(BlackboardLayer.TASK_STATE, 't', {
            'task': {'status': 'running', 'agent_type': 'ctf_web'},
            'routes': [{'route_id': 'r', 'name': 'solve', 'status': 'running',
                        'progress': progress, 'notes': [note]}]})
        return self.master._progress_digest(['t'])

    async def watch(self, snapshots):
        checked = asyncio.Event()
        reads = 0
        def digest(ids):
            nonlocal reads
            result = snapshots[min(reads, len(snapshots) - 1)]
            reads += 1
            if reads > len(snapshots) + 3:
                checked.set()
            return result
        self.master._STEER_INTERVAL = .001
        self.master._progress_digest = digest
        self.master._llm_steer = AsyncMock(return_value=[])
        watcher = asyncio.create_task(self.master._steering_watch(['t']))
        try:
            await asyncio.wait_for(checked.wait(), 1)
            return self.master._llm_steer.await_count
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def test_metric_and_version_changes_do_not_retrigger_supervision(self):
        first = self.digest(.1, '正在核对入口响应')
        second = self.digest(.2, '正在核对入口响应')
        self.assertNotEqual(first['tasks'][0]['blackboard_ref'], second['tasks'][0]['blackboard_ref'])
        self.assertEqual(await self.watch([first, second]), 1)

    async def test_changed_unseen_tail_is_detected_but_not_presented_as_full_evidence(self):
        first = self.digest(.1, 'x' * 600 + '未确认')
        second = self.digest(.2, 'x' * 600 + '已出现新响应')
        route = first['tasks'][0]['routes'][0]
        self.assertEqual(len(route['notes'][0]), 500)
        self.assertEqual(route['notes_view']['kind'], 'agent_summary')
        self.assertTrue(route['notes_view']['partial'])
        self.assertEqual(route['notes'], second['tasks'][0]['routes'][0]['notes'])
        self.assertNotEqual(route['notes_view']['digest'], second['tasks'][0]['routes'][0]['notes_view']['digest'])
        ref = second['tasks'][0]['blackboard_ref']
        record, version = self.bb.read_versioned(BlackboardLayer(ref['layer']), ref['record_id'])
        self.assertEqual(version, ref['version'])
        self.assertTrue(record['routes'][0]['notes'][0].endswith('新响应'))
        self.assertEqual(await self.watch([first, second]), 2)


if __name__ == '__main__':
    unittest.main()
