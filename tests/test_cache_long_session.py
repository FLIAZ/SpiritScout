"""长会话的增量传输、历史投影、消费事实和模型显式实例保留。"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import TaskStatus
from spiritscout.ctf.platform import PlatformAdapter, parse_platform_profile
from spiritscout.gateway.context import DecisionMemory, decision_messages
from spiritscout.gateway.gateway import ChatResponse, chat_decision
from tests import test_competition_continuous
from tests.test_platform_adapter import PROFILE, _challenge


class LongSessionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_decision_transport_preserves_prefix_and_reasoning(self) -> None:
        response = ChatResponse(content='{"continue_iteration":true}', reasoning='保留推理字段',
                                model='fixture', prompt_tokens=1, completion_tokens=1,
                                cost_usd=0, latency_ms=0)
        gateway = SimpleNamespace(chat=AsyncMock(return_value=response))
        memory = DecisionMemory()
        first = decision_messages('契约', {'nodes': [{'id': 'n1', 'status': 'running'}]})
        await chat_decision(gateway, first, memory=memory, audit=NullAuditLogger())
        sent = copy.deepcopy(gateway.chat.call_args.args[0])
        await chat_decision(gateway, decision_messages('契约', {
            'nodes': [{'id': 'n1', 'status': 'done'}, {'id': 'n2', 'status': 'running'}]}), memory=memory)
        second = gateway.chat.call_args.args[0]
        self.assertEqual(second[:len(sent)], sent)
        self.assertEqual(second[-2]['reasoning_content'], '保留推理字段')
        self.assertTrue(gateway.chat.call_args.kwargs['preserve_context'])
        patch = json.loads(second[-1]['content'])['patch']
        self.assertEqual(patch, [
            {'op': 'replace', 'path': '/nodes/0/status', 'value': 'done'},
            {'op': 'add', 'path': '/nodes/-', 'value': {'id': 'n2', 'status': 'running'}}])

    def test_array_removals_are_replayed_from_end_and_escaped_paths_remain_valid(self) -> None:
        self.assertEqual(DecisionMemory.patch({'a/b': [1, 2, 3]}, {'a/b': [4]}), [
            {'op': 'replace', 'path': '/a~1b/0', 'value': 4},
            {'op': 'remove', 'path': '/a~1b/2'}, {'op': 'remove', 'path': '/a~1b/1'}])

    def test_repeated_history_does_not_expand_model_snapshot_or_erase_completion(self) -> None:
        dag = TaskDAG()
        for i in range(1000):
            node = dag.add_node(f'CTF-WEB@{i}', '重复取证目标', meta={'challenge': {'id': f'c{i % 4}'}})
            dag.mark(node.node_id, TaskStatus.DONE)
        current = dag.add_node('CTF-WEB@new', '新的原始证据', meta={'challenge': {'id': 'c0'}})
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(extra={'platform': SimpleNamespace(
            continuous=True, active_instance_ids=lambda: {'c0'},
            progress=lambda: {'solved_ids': ['solved'], 'unsolved_ids': ['c0', 'c1', 'c2', 'c3']})})
        status = master._competition_status(dag)
        self.assertEqual(status['solved_ids'], ['solved'])
        self.assertEqual(status['challenges']['c0']['attempt_count'], 251)
        self.assertEqual(status['challenges']['c0']['status_counts']['done'], 250)
        self.assertEqual(status['challenges']['c0']['nodes'][-1]['node_id'], current.node_id)
        self.assertEqual(len(master._decision_nodes(dag)), 5)
        self.assertEqual(len(dag.all_nodes()), 1001)
        self.assertLess(len(json.dumps(status)), len(json.dumps(master._challenge_states(dag))) / 10)

    def test_spending_is_observed_data_and_resets_on_partial_flag_score_change(self) -> None:
        calls = []
        flags = {'accepted': 1}
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(gateway=SimpleNamespace(budget=SimpleNamespace(call_log=calls)),
                                    extra={'flag_manager': SimpleNamespace(stats=lambda: flags)})
        progress = {'solved_ids': ['done']}
        master._spending_observation(progress)
        calls.append({'input_tokens': 1000, 'cached_input_tokens': 900, 'estimated_cost_cny_min': .03})
        observed = master._spending_observation(progress)
        self.assertEqual(observed['calls_since_observed_score_change'], 1)
        self.assertEqual(observed['uncached_input_tokens'], 100)
        self.assertEqual(observed['estimated_cny_min'], .03)
        flags['accepted'] = 2
        self.assertEqual(master._spending_observation(progress)['calls_since_observed_score_change'], 0)

    async def test_retained_idle_capacity_is_returned_to_model_instead_of_deadlocking(self) -> None:
        started = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/challenges'):
                return httpx.Response(200, json={'data': {'challenges': [_challenge('c1'), _challenge('c2')]}})
            if request.url.path.endswith('/start'):
                started.append(json.loads(request.content)['unique_code'])
            return httpx.Response(200, json={'container_addr': 'http://fixture.box'})

        adapter = PlatformAdapter(parse_platform_profile({**PROFILE, 'limits': {'max_active': 1}}),
                                  continuous=True)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(adapter.aclose)
        await adapter.list_challenges()
        await adapter.ensure_instance({'id': 'c1'})

        async def run(task):
            task.status = TaskStatus.DONE

        master = test_competition_continuous.ContinuousCompetitionTests()._master(adapter, run, planning=True)
        master._llm_plan = AsyncMock(return_value={
            'dag_nodes': [], 'defer_challenges': [{'id': 'c1', 'reason': '模型选择换题释放名额', 'release_instance': True}]})
        dag = TaskDAG()
        old = dag.add_node('CTF-WEB', 'c1', meta={
            'challenge': {'id': 'c1'}, 'retain_instance': True})
        dag.mark(old.node_id, TaskStatus.DONE)
        dag.add_node('CTF-WEB@2', 'c2', meta={'challenge': {'id': 'c2'}})
        await asyncio.wait_for(master._dispatch({
            'dag': dag.to_dict(), 'round_no': 0, 'target': 'http://fixture.box',
            'tasks_dispatched': []}), 1)
        self.assertEqual(started, ['c1', 'c2'])
        self.assertEqual(master._llm_plan.await_count, 1)

    async def test_explicit_model_retention_keeps_instance_until_model_defers_it(self) -> None:
        operations = []

        def handle(request: httpx.Request) -> httpx.Response:
            operations.append(request.url.path.rsplit('/', 1)[-1])
            if request.url.path.endswith('/challenges'):
                return httpx.Response(200, json={'data': {'challenges': [_challenge('c1')]}})
            return httpx.Response(200, json={'container_addr': 'http://fixture.box'})

        adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(adapter.aclose)
        await adapter.list_challenges()

        async def run(task):
            task.status = TaskStatus.DONE

        master = test_competition_continuous.ContinuousCompetitionTests()._master(adapter, run)
        dag = TaskDAG()
        node = dag.add_node('CTF-WEB', 'c1', meta={'challenge': {'id': 'c1'}})
        await master._apply_competition_decisions(dag, {'retain_instances': ['c1']})
        dispatched = await master._dispatch({'dag': dag.to_dict(), 'round_no': 0,
                                             'target': 'http://fixture.box', 'tasks_dispatched': []})
        self.assertEqual(adapter.active_instance_ids(), {'c1'})
        self.assertNotIn('close', operations)
        finished = TaskDAG.from_dict(dispatched['dag'])
        await master._apply_competition_decisions(finished, {
            'defer_challenges': [{'id': 'c1', 'reason': '需要换题', 'release_instance': True}]})
        self.assertEqual(adapter.active_instance_ids(), set())
        self.assertFalse(finished.get(node.node_id).meta.get('retain_instance'))
        self.assertEqual(operations.count('close'), 1)
