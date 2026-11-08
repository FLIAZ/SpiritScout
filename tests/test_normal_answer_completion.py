"""正常模式：发布证据和完成确认均成功才结束，失败不能留下可误确认的答案。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import BlackboardLayer, RunMode, Task
from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.ctf.flags import FlagManager
from spiritscout.tools.invocation import ExecutionScope
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.tools.spec import ToolResult


class NormalAnswerCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bb = Blackboard(self.root / 'bb.db')
        self.addAsyncCleanup(self.bb.aclose)
        self.flags = FlagManager(local_only=True, store_path=self.root / 'flags.json')
        self.journal = ExecutionJournal(self.bb, self.root, 'run')
        self.journal.begin('observed', 'sandbox_exec', 'digest', 'never', scope=ExecutionScope(task_id='solve'))
        self.journal.transition('observed', 'succeeded', result=ToolResult(tool='sandbox_exec', ok=True, stdout='answer-from-evidence'))
        ctx = SimpleNamespace(mode=RunMode.CTF, shared_cache=None, search_enabled=False,
            gateway=SimpleNamespace(config=SimpleNamespace(lightweight_local=False)),
            audit=NullAuditLogger(), builtin_ctx={}, blackboard=self.bb, policy=None,
            tools=SimpleNamespace(journal=self.journal))
        ctx.for_agent = lambda role: ctx
        self.agent = CTFBaseAgent(ctx, flag_manager=self.flags)
        self.task = Task(task_id='solve', agent_type=self.agent.agent_type, task_type='ctf_solve',
                         target='', payload={'challenge': {'id': 'one'}})
        self.route = SimpleNamespace(name='analysis')
        self.args = {'answer': 'answer-from-evidence', 'evidence_refs': ['observed'], 'review': {
            'requirements': '恢复附件内的原始文本', 'derivation': '从解码输出定位文本',
            'validation': '重编码与原始字节一致', 'validation_evidence_refs': ['observed']}}

    async def record(self):
        return await self.agent._tool_record_answer(self.task, self.route, self.args)

    async def test_publication_failure_does_not_register_share_or_complete(self):
        for failure in ({'ok': False, 'error': 'bad reference'}, {}):
            with self.subTest(failure=failure), patch('spiritscout.blackboard.exchange.execute', return_value=failure):
                result = await self.record()
            self.assertFalse(result.ok)
            self.assertFalse(result.stop)
            self.assertEqual(self.flags.all(), [])
            self.assertFalse(self.flags.completed_ids())
            self.assertEqual(self.agent._shared_flags(self.task), [])
            self.assertFalse((self.root / 'flags.json').exists())
            finish = self.agent._tool_finish_challenge(self.task, {'answer_ids': [], 'reason': 'done'})
            self.assertFalse(finish.ok)

    async def test_real_blackboard_rejection_and_exception_leave_no_candidate(self):
        with patch.object(self.bb, 'write', side_effect=ValueError('cannot publish')):
            self.assertFalse((await self.record()).ok)
        with patch('spiritscout.blackboard.exchange.execute', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                await self.record()
        self.assertFalse(self.flags.all())
        self.assertFalse(self.flags.completed_ids())

    async def test_success_persists_and_repeated_completion_is_idempotent(self):
        result = await self.record()
        self.assertTrue(result.ok and result.stop)
        self.assertEqual(self.flags.completed_ids(), {'one'})
        restored = FlagManager(local_only=True, store_path=self.root / 'flags.json')
        self.assertEqual(restored.completed_ids(), {'one'})
        with patch('spiritscout.blackboard.exchange.execute') as publish:
            self.assertTrue((await self.record()).stop)
        publish.assert_not_called()
        self.assertEqual(len(self.flags.all()), 1)

    async def test_failed_confirmation_does_not_stop_and_can_retry_existing_answer(self):
        with patch.object(self.flags, 'confirm_complete', return_value=False):
            result = await self.record()
        self.assertFalse(result.ok or result.stop)
        self.assertEqual(len(self.flags.all()), 1)
        self.assertFalse(self.flags.completed_ids())
        self.assertTrue((await self.record()).stop)
        self.assertEqual(len(self.flags.all()), 1)

    async def test_multiple_answers_require_explicit_completion_after_publication(self):
        self.flags.multiple_answers = True
        result = await self.record()
        self.assertTrue(result.ok)
        self.assertFalse(result.stop)
        self.args['answer'] = 'second answer'
        with patch('spiritscout.blackboard.exchange.execute', return_value={'ok': False}):
            self.assertFalse((await self.record()).ok)
        self.assertEqual(len(self.flags.all()), 1)
        self.assertFalse(self.agent._tool_finish_challenge(self.task, {
            'answer_ids': list(self.flags.answer_ids('one')), 'reason': 'done'}).ok)
        self.assertTrue((await self.record()).ok)
        self.assertTrue(self.agent._tool_finish_challenge(self.task, {
            'answer_ids': list(self.flags.answer_ids('one')), 'reason': 'both confirmed'}).stop)

    async def test_missing_blackboard_or_invalid_refs_cannot_complete(self):
        with patch.object(self.agent.ctx, 'blackboard', None):
            self.assertFalse((await self.record()).ok)
        for refs in ('tool:x', [''], [12], []):
            self.args['evidence_refs'] = refs
            self.assertFalse((await self.record()).ok)
        self.assertFalse(self.flags.all())

    async def test_answer_registered_to_another_challenge_is_not_shared(self):
        self.flags.register(self.args['answer'], challenge_id='other', found_by='fixture')
        self.agent._record_flag = Mock()
        self.assertFalse((await self.record()).ok)
        self.agent._record_flag.assert_not_called()
        self.assertFalse(self.flags.completed_ids())

    async def test_failed_forged_and_cross_task_execution_cannot_complete(self):
        self.journal.begin('other', 'sandbox_exec', 'digest', 'never', scope=ExecutionScope(task_id='unrelated'))
        self.journal.transition('other', 'succeeded', result=ToolResult(tool='sandbox_exec', ok=True))
        self.journal.begin('failed', 'sandbox_exec', 'digest', 'never', scope=ExecutionScope(task_id='solve'))
        self.journal.transition('failed', 'failed', result=ToolResult(tool='sandbox_exec', ok=False, returncode=1))
        for ref in ('invented', 'other', 'failed'):
            self.args['evidence_refs'] = [ref]
            self.assertFalse((await self.record()).ok)
            self.assertFalse(self.flags.all())

    async def test_model_review_required_and_restored_for_finish(self):
        review = self.args.pop('review')
        self.assertFalse((await self.record()).ok)
        self.args['review'] = review
        self.flags.multiple_answers = True
        self.assertTrue((await self.record()).ok)
        self.args['answer'] = 'second'
        self.assertTrue((await self.record()).ok)
        restored = FlagManager(local_only=True, multiple_answers=True, store_path=self.root / 'flags.json')
        self.agent.flag_manager = restored
        self.assertEqual(restored.all()[0].answer_review, review)
        self.journal.transition('observed', 'failed', result=ToolResult(tool='sandbox_exec', ok=False))
        result = self.agent._tool_finish_challenge(self.task, {
            'answer_ids': sorted(restored.answer_ids('one')), 'reason': 'done'})
        self.assertFalse(result.ok)
        self.assertFalse(restored.completed_ids())

    async def test_blackboard_dependency_version_and_same_challenge_peer(self):
        self.bb.write(BlackboardLayer.TOPOLOGY, 'source', {'kind': 'finding', 'text': 'observed', 'evidence_refs': ['observed']})
        self.bb.write(BlackboardLayer.TOPOLOGY, 'derived', {'kind': 'finding', 'text': 'derived', 'evidence_refs': ['topology:source']})
        self.args['evidence_refs'] = ['topology:derived']
        self.bb.write(BlackboardLayer.TOPOLOGY, 'source', {'kind': 'finding', 'text': 'changed', 'evidence_refs': ['observed']})
        self.assertFalse((await self.record()).ok)
        self.journal.begin('peer-observed', 'sandbox_exec', 'digest', 'never', scope=ExecutionScope(task_id='peer'))
        self.journal.transition('peer-observed', 'succeeded', result=ToolResult(tool='sandbox_exec', ok=True))
        self.bb.write(BlackboardLayer.TASK_STATE, 'peer', {'task': self.task.model_copy(update={'task_id': 'peer'}).model_dump(mode='json')})
        self.args['evidence_refs'] = ['peer-observed']
        self.assertTrue((await self.record()).ok)
