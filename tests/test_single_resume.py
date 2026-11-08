"""轻量续聊跨运行 ID 恢复真实检查点，保留证据编号、笔记和回读位置。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from test_single_executor import context

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.evidence_memory import source_digest
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.tools.journal import ExecutionJournal


class SingleResumeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.bb = Blackboard(root / 'bb.db')
        self.addCleanup(self.bb.close)
        self.ctx = context(RunMode.CTF)
        self.ctx.blackboard, self.ctx.audit, self.ctx.ring = self.bb, Mock(), None
        self.ctx.journal = ExecutionJournal(self.bb, root, 'first-run')
        self.ctx.gateway.context_identity = lambda: {'model': 'fixture', 'api_format': 'chat'}
        self.ctx.gateway.context_budget = lambda **kw: 16000
        self.world = {'user_context': {'mission': 'ORIGINAL_GOAL'}, 'authorization': {'testing_rules': 'LOCAL_ONLY'}}
        self.agent = self.make_agent()
        self.task = Task(task_id='old-task', agent_type=self.agent.agent_type, task_type='ctf_single',
            target='file://fixture', payload={'challenge': {'id': 'one', 'attachments': ['fixture.zip']},
                                            'mission': 'ORIGINAL_GOAL'})
        self.route = SimpleNamespace(route_id='old-route', name='solve')
        self.state = self.agent._inv_state(self.task, self.route)
        self.agent._seed_messages(self.task, self.route, None, self.state)
        self.state['messages'].extend([
            {'role': 'assistant', 'tool_calls': [{'id': 'actual', 'type': 'function', 'function': {
                'name': 'shell', 'arguments': '{"command":"read fixture"}'}}]},
            {'role': 'tool', 'tool_call_id': 'actual', 'content': 'ORIGINAL_OBSERVATION',
             '_light_observation': {'tool': 'shell', 'ok': True, 'invocation_id': 'inv-original'}}])
        self.state.update(turn=12, system_initialized=True,
            answer_review={'answer': 'candidate', 'turn': 10},
            light_evidence_memory={'revision': 7, 'items': [{
                'id': 'fact', 'kind': 'fact', 'text': '已核对原文', 'sources': [{
                    'message_index': 3, 'quote': 'ORIGINAL_OBSERVATION',
                    'sha256': source_digest(self.state['messages'][3]), 'invocation_id': 'inv-original'}]}]},
            history_read_progress=[{'source': {'view': 'output', 'message_index': 3},
                'offset': 2000, 'next_offset': 4000, 'has_more': True, 'same_page_count': 1}])
        self.agent._save_inv_state(self.task, self.route, self.state)
        self.original = copy.deepcopy(self.state)
        self.ctx.journal = ExecutionJournal(self.bb, root, 'second-run')

    def make_agent(self):
        agent = SingleCTFAgent(self.ctx, flag_manager=FlagManager(local_only=True))
        agent._world_snapshot = lambda: copy.deepcopy(self.world)
        return agent

    def new_task(self, **payload):
        return self.task.model_copy(update={'task_id': 'new-task', 'payload': {
            **copy.deepcopy(self.task.payload), 'followup_goal': True, 'mission': 'CURRENT_FOLLOWUP', **payload}})

    def test_new_run_restores_notes_cursor_and_message_indices_without_replaying_tools(self):
        self.world['user_context']['mission'] = 'CURRENT_FOLLOWUP'
        fresh = self.make_agent()
        task, route = self.new_task(), SimpleNamespace(route_id='new-route', name='solve')
        restored = fresh._inv_state(task, route)
        self.assertEqual(restored['messages'][1:4], self.original['messages'][1:4])
        self.assertEqual(restored['light_evidence_memory'], self.original['light_evidence_memory'])
        self.assertEqual(restored['history_read_progress'], self.original['history_read_progress'])
        self.assertEqual(restored['answer_review'], self.original['answer_review'])
        self.assertEqual(restored['turn'], 12)
        self.assertIn('CURRENT_FOLLOWUP', restored['messages'][-1]['content'])
        self.assertEqual(restored['world_state'], self.world)
        self.assertIs(fresh._inv_state(task, route), restored)
        self.assertEqual(len(restored['messages']), 5)
        fresh._save_inv_state(task, route, restored)
        self.assertEqual(self.ctx.journal.latest_checkpoint('ctf_conversation', 'old-task:old-route')['state'],
                         self.original)
        self.assertEqual(self.ctx.journal.latest_checkpoint('ctf_conversation', 'new-task:new-route')['state']['turn'], 12)

    def test_new_goal_after_completion_preserves_evidence_but_reopens_execution_state(self):
        self.state.update(concluded=True, conclusion_text='OLD_COMPLETION', stall='OLD_STALL',
                          context_blocked=True, user_feed=[{'text': 'ALREADY_HANDLED'}])
        self.agent._save_inv_state(self.task, self.route, self.state)
        fresh = self.make_agent()
        task, route = self.new_task(), SimpleNamespace(route_id='followup', name='solve')
        restored = fresh._inv_state(task, route)
        self.assertFalse(restored['concluded'])
        self.assertFalse(restored['stall'])
        self.assertFalse(restored['context_blocked'])
        self.assertNotIn('answer_review', restored)
        self.assertNotIn('user_feed', restored)
        self.assertEqual(restored['light_evidence_memory']['revision'], 7)
        self.assertIn('CURRENT_FOLLOWUP', str(fresh._light_model_messages(task, route, restored)))

    def test_other_challenge_model_and_new_task_do_not_inherit_conversation(self):
        cases = [('other-challenge', self.new_task(challenge={'id': 'other'}), False),
                 ('new-task', self.new_task(followup_goal=False), False),
                 ('other-model', self.new_task(), True)]
        for name, task, change_model in cases:
            with self.subTest(name=name):
                self.ctx.gateway.context_identity = lambda: {'model': 'changed' if change_model else 'fixture',
                                                            'api_format': 'chat'}
                fresh = self.make_agent()
                state = fresh._inv_state(task, SimpleNamespace(route_id=name, name='solve'))
                self.assertEqual(state['messages'], [])
                self.assertNotIn('light_evidence_memory', state)
