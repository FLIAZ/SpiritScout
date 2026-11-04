"""实时督导关键决策只接受同题、当前且实际展示过的依据。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.evidence import check_basis
from spiritscout.agents.ring import RingLedger
from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import BlackboardLayer
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope
from spiritscout.tools.journal import ExecutionJournal


class SteeringBasisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.bb = Blackboard(root/'bb.db')
        self.addCleanup(self.bb.close)
        self.bb.write(BlackboardLayer.TASK_STATE, 't1', {
            'task': {'task_id': 't1', 'status': 'running', 'agent_type': 'ctf_web',
                     'payload': {'challenge': {'id': 'c1'}}},
            'routes': [{'route_id': 'r1', 'name': 'solve', 'status': 'running', 'notes': ['阶段说明']}]})
        journal = ExecutionJournal(self.bb, root, 'run')
        journal.begin('inv-proof', 'read', 'digest', 'safe')
        journal.transition('inv-proof', 'succeeded')
        token = current_execution_scope.set(ExecutionScope(task_id='t1', agent_id='worker'))
        try:
            self.bb.write(BlackboardLayer.TOPOLOGY, 'proof', {'kind': 'finding', 'text': '可核对的工具观察',
                          'evidence_refs': ['inv-proof']}, updated_by='worker')
        finally:
            current_execution_scope.reset(token)
        self.master = object.__new__(MasterAgent)
        self.master.agent_id = 'master'
        self.master.ctx = SimpleNamespace(blackboard=self.bb, audit=NullAuditLogger(), ring=RingLedger(),
                                          extra={}, gateway=None)
        self.master._mission = lambda: '按用户规则推进'
        self.master._system_prompt = lambda: '稳定系统提示'
        self.master._world_state = lambda: {'shared_facts': [
            {'blackboard_ref': {'layer': 'topology', 'record_id': 'proof', 'version': 1}}]}

    async def decide(self, item, side_effect=None):
        digest = self.master._progress_digest(['t1'])
        queue = asyncio.Queue()
        response = SimpleNamespace(content=json.dumps({'steering': [], 'defer_challenges': [item]}))
        async def reply(*args, **kwargs):
            if side_effect:
                side_effect()
            return response
        with patch('spiritscout.agents.master.interaction.chat_decision', AsyncMock(side_effect=reply)) as call:
            await self.master._llm_steer(digest, queue)
        call.assert_awaited_once()
        self.assertTrue(call.call_args.args[1][0]['content'].startswith('稳定系统提示\nROLE: collab_judge\n'))
        self.assertNotIn('basis_refs', call.call_args.args[1][0]['content'], 'basis contract must not rewrite the stable system prefix')
        return queue

    async def test_summary_can_defer_new_dispatch_but_cannot_cancel_or_release(self):
        item = {'id': 'c1', 'reason': '模型建议暂缓', 'basis_refs': ['task_state:t1@1']}
        self.assertFalse((await self.decide(item)).empty())
        for action in ['cancel_running', 'release_instance']:
            self.assertTrue((await self.decide({**item, action: True})).empty())

    async def test_same_task_fresh_observation_allows_explicit_critical_decision(self):
        item = {'id': 'c1', 'reason': '现场可核对', 'release_instance': True,
                'basis_refs': ['task_state:t1@1', 'topology:proof@1']}
        queue = await self.decide(item)
        decision = queue.get_nowait()['defer_challenges'][0]
        self.assertEqual(decision['_steering_task_ids'], ['t1'])
        self.assertTrue(self.master._steering_decision_current(decision))
        self.bb.write(BlackboardLayer.TOPOLOGY, 'proof', {'kind': 'finding', 'text': '新的现场'})
        self.assertFalse(self.master._steering_decision_current(decision), 'queued decisions must be rechecked before cancelling work')

    async def test_missing_unseen_stale_and_cross_task_refs_are_rejected(self):
        base = {'id': 'c1', 'reason': '候选决定', 'cancel_running': True}
        for refs in [[], ['task_state:t1@1', 'topology:invented@1'], ['task_state:t1@999', 'topology:proof@1']]:
            self.assertTrue((await self.decide({**base, 'basis_refs': refs})).empty())
        def changed():
            self.bb.write(BlackboardLayer.TOPOLOGY, 'proof', {'kind': 'finding', 'text': 'changed'})
        self.assertTrue((await self.decide({**base, 'basis_refs': ['task_state:t1@1', 'topology:proof@1']}, changed)).empty())
        self.assertFalse(check_basis(self.bb, {'id': 'c2', 'basis_refs': ['task_state:t1@1']}, ['t1']))

    async def test_foreign_task_fact_cannot_cancel_current_task(self):
        token = current_execution_scope.set(ExecutionScope(task_id='another-task', agent_id='worker'))
        try:
            self.bb.write(BlackboardLayer.TOPOLOGY, 'foreign', {
                'kind': 'finding', 'text': '别题工具观察', 'evidence_refs': ['inv-proof']})
        finally:
            current_execution_scope.reset(token)
        self.assertFalse(check_basis(self.bb, {'id': 'c1', 'cancel_running': True,
                         'basis_refs': ['task_state:t1@1', 'topology:foreign@1']}, ['t1']))

    async def test_numeric_progress_update_conservatively_invalidates_queued_cancellation(self):
        item = {'id': 'c1', 'reason': '检查排队期间现场', 'cancel_running': True,
                'basis_refs': ['task_state:t1@1', 'topology:proof@1']}
        decision = (await self.decide(item)).get_nowait()['defer_challenges'][0]
        data = self.bb.read(BlackboardLayer.TASK_STATE, 't1')
        data['routes'][0]['progress'] = .9
        self.bb.write(BlackboardLayer.TASK_STATE, 't1', data)
        self.assertFalse(self.master._steering_decision_current(decision))

    async def test_unseen_existing_record_is_not_a_valid_model_basis(self):
        self.bb.write(BlackboardLayer.TOPOLOGY, 'hidden', {'kind': 'finding', 'text': '未展示的记录'})
        queue = await self.decide({'id': 'c1', 'reason': '引用不能靠猜',
                                  'basis_refs': ['task_state:t1@1', 'topology:hidden@1']})
        self.assertTrue(queue.empty())


if __name__ == '__main__':
    unittest.main()
