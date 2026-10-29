"""双图的持久记录、显式关系与并发更新边界。"""
import json
import tempfile
import unittest
from pathlib import Path

from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.exchange import execute
from spiritscout.core.models import AgentType, BlackboardLayer
from spiritscout.gateway.world_state import snapshot
from spiritscout.web.process_graph import project_blackboard


class ExplorationGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bb = Blackboard(Path(self.tmp.name) / 'blackboard.db')
        self.addCleanup(self.bb.close)
        self.bb.write(BlackboardLayer.ASSET, 'login', {'asset_type': 'url', 'value': 'https://fixture.test/login'})

    def call(self, name: str, args: dict, role: AgentType = AgentType.CTF_WEB) -> dict:
        return execute(self.bb, name, args, actor='fixture', agent_type=role, source={'task_id': 'task-a'})

    def create(self, **kwargs) -> dict:
        result = self.call('exploration_write', {'node_type': 'hypothesis', 'text': '核对权限边界',
                          'state': 'open', 'asset_refs': ['asset:login'], **kwargs})
        self.assertTrue(result['ok'], result)
        return result['blackboard_ref']

    def test_linked_graphs_keep_hypotheses_out_of_facts_and_survive_reopen(self) -> None:
        parent = self.create()
        attempt = self.create(node_type='attempt', text='普通账号访问管理端点',
                              parent_refs=[f'topology:{parent["record_id"]}@1'])
        related_assets = self.call('graph_read', {'graph': 'assets', 'related_ref': f'topology:{attempt["record_id"]}'})
        self.assertEqual(related_assets['items'][0]['blackboard_ref']['record_id'], 'login')
        self.assertTrue(self.call('blackboard_write', {'text': '返回 403', 'evidence_refs': ['response-1'],
            'exploration_refs': [f'topology:{attempt["record_id"]}'], 'asset_refs': ['asset:login']})['ok'])
        reopened = Blackboard(Path(self.tmp.name) / 'blackboard.db', readonly=True)
        self.addCleanup(reopened.close)
        graph = project_blackboard(reopened)
        self.assertEqual(sum(n['kind'] == 'exploration' for n in graph['nodes']), 2)
        self.assertEqual(sum(n['kind'] == 'finding' for n in graph['nodes']), 2)
        self.assertEqual({edge['kind'] for edge in graph['edges']}, {'anchor', 'derives', 'produces'})
        self.assertEqual(len(graph['edges']), 5)

    def test_versions_failed_branches_and_readonly_roles(self) -> None:
        ref = self.create()
        update = {'record_id': ref['record_id'], 'version': 1, 'node_type': 'hypothesis',
                  'text': '核对权限边界', 'state': 'refuted', 'reason': '普通成员被拒绝'}
        self.assertTrue(self.call('exploration_write', update)['ok'])
        self.assertFalse(self.call('exploration_write', update)['ok'])
        read = self.call('blackboard_read', {**ref, 'version': 2})
        self.assertEqual(json.loads(read['content'])['asset_refs'], ['asset:login'])
        failed = self.call('graph_read', {'graph': 'exploration', 'state': 'refuted', 'related_ref': 'asset:login'})
        self.assertEqual(failed['total'], 1)
        self.assertFalse(self.call('exploration_write', {**update, 'version': 2, 'reason': ''})['ok'])
        for role in (AgentType.REPORT, AgentType.VERIFIER):
            self.assertFalse(self.call('exploration_write', {**update, 'version': 2}, role)['ok'])
            self.assertTrue(self.call('graph_read', {'graph': 'exploration'}, role)['ok'])
        self.assertEqual(self.bb.read_versioned(BlackboardLayer.TOPOLOGY, ref['record_id'])[1], 2)

    def test_bad_references_do_not_write_nodes_and_asset_directory_supports_legacy_ctf(self) -> None:
        for field, value in [('asset_refs', ['asset:missing']), ('parent_refs', ['asset:login']),
                             ('asset_refs', ['asset:login@99']), ('evidence_refs', 'not-an-array')]:
            result = self.call('exploration_write', {'node_type': 'goal', 'text': '目标', 'state': 'open', field: value})
            self.assertFalse(result['ok'], result)
        self.assertEqual(self.call('graph_read', {'graph': 'exploration'})['total'], 0)
        self.bb.write(BlackboardLayer.TOPOLOGY, 'ctf-url', {'kind': 'finding', 'type': 'asset',
                      'text': 'https://fixture.test/api'})
        assets = self.call('graph_read', {'graph': 'assets', 'limit': 1})
        self.assertEqual(assets['total'], 2)
        self.assertTrue(assets['has_more'])
        self.create(asset_refs=['topology:ctf-url'])
        self.assertFalse(self.call('graph_read', {'graph': 'exploration', 'offset': -1})['ok'])

    def test_world_state_has_bounded_directory_without_hypothesis_as_fact(self) -> None:
        for i in range(10):
            self.create(text=f'假设 {i}')
        state = snapshot(blackboard=self.bb, references=True)
        self.assertEqual(len(state['exploration_graph']['items']), 8)
        self.assertTrue(state['exploration_graph']['truncated'])
        self.assertNotIn('shared_facts', state)

    def test_shared_fact_keeps_links_to_multiple_attempts(self) -> None:
        first = self.create(node_type='attempt')
        second = self.create(node_type='attempt')
        args = {'text': '可复用观察', 'evidence_refs': ['response-a']}
        one = self.call('blackboard_write', {**args, 'exploration_refs': [f'topology:{first["record_id"]}']})
        two = self.call('blackboard_write', {**args, 'exploration_refs': [f'topology:{second["record_id"]}']})
        self.assertEqual(one['blackboard_ref']['record_id'], two['blackboard_ref']['record_id'])
        record = self.bb.read(BlackboardLayer.TOPOLOGY, two['blackboard_ref']['record_id'])
        self.assertEqual(record['exploration_refs'], [f'topology:{first["record_id"]}', f'topology:{second["record_id"]}'])

    def test_asset_relationships_are_traceable_and_readable_in_both_directions(self) -> None:
        self.bb.write(BlackboardLayer.ASSET, 'api', {'asset_type': 'endpoint', 'value': 'https://fixture.test/api'})
        args = {'source_ref': 'asset:login', 'target_ref': 'asset:api', 'relation': 'exposes',
                'evidence_refs': ['response-fixture']}
        first = self.call('asset_link', args)
        self.assertTrue(first['ok'], first)
        self.assertEqual(first['evidence_state'], 'unverified')
        self.assertEqual(first, self.call('asset_link', args))
        for reference, neighbor in [('asset:login', 'api'), ('asset:api', 'login')]:
            page = self.call('graph_read', {'graph': 'assets', 'related_ref': reference, 'limit': 1})
            self.assertEqual(page['items'][0]['blackboard_ref']['record_id'], neighbor)
            self.assertEqual(page['relations'][0]['relation'], 'exposes')
        graph = project_blackboard(self.bb)
        edge = next(edge for edge in graph['edges'] if edge['kind'] == 'exposes')
        self.assertEqual(edge['record_id'], first['blackboard_ref']['record_id'])
        self.assertEqual(edge['source'], '__process_bb:asset:login')
        self.assertEqual(edge['target'], '__process_bb:asset:api')
        self.assertEqual(edge['evidence_state'], 'unverified')
        for role in (AgentType.REPORT, AgentType.VERIFIER):
            self.assertFalse(self.call('asset_link', args, role)['ok'])
        for invalid in ({'target_ref': 'asset:missing'}, {'target_ref': 'asset:login'}, {'evidence_refs': []}):
            self.assertFalse(self.call('asset_link', {**args, **invalid})['ok'])

    def test_asset_relationship_results_have_separate_pagination(self) -> None:
        self.bb.write(BlackboardLayer.ASSET, 'api', {'value': 'https://fixture.test/api'})
        for relation in ('contains', 'exposes'):
            self.assertTrue(self.call('asset_link', {'source_ref': 'asset:login', 'target_ref': 'asset:api',
                'relation': relation, 'evidence_refs': ['response-fixture']})['ok'])
        page = self.call('graph_read', {'graph': 'assets', 'limit': 1})
        self.assertEqual(page['relations_total'], 2)
        self.assertEqual(len(page['relations']), 1)
        self.assertTrue(page['relations_has_more'])
        next_page = self.call('graph_read', {'graph': 'assets', 'limit': 1, 'relation_offset': page['next_relation_offset']})
        self.assertNotEqual(page['relations'][0]['relation'], next_page['relations'][0]['relation'])
        self.assertFalse(next_page['relations_has_more'])


class ExplorationDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_planner_can_write_then_query_exploration(self) -> None:
        from types import SimpleNamespace

        from spiritscout.gateway.context import DecisionMemory
        from spiritscout.gateway.gateway import ChatResponse, chat_decision

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'blackboard.db')
            self.addCleanup(bb.close)
            memory = DecisionMemory()
            memory.blackboard, memory.agent_type = bb, AgentType.MASTER
            calls = []

            async def chat(messages, **kwargs):
                calls.append(messages)
                self.assertIn('exploration_write', messages[0]['content'])
                if len(calls) == 1:
                    result = {'skill_requests': [{'name': 'exploration_write', 'arguments': {
                        'node_type': 'goal', 'text': '验证访问边界', 'state': 'open'}}]}
                elif len(calls) == 2:
                    result = {'skill_requests': [{'name': 'graph_read', 'arguments': {'graph': 'exploration'}}]}
                else:
                    result = {'done': True}
                return ChatResponse(content=json.dumps(result), model='fixture', prompt_tokens=1,
                                    completion_tokens=1, cost_usd=0, latency_ms=0)

            response = await chat_decision(SimpleNamespace(chat=chat), [
                {'role': 'system', 'content': '根据探索图规划'}, {'role': 'user', 'content': '{}'}],
                memory=memory, response_format='json')
            self.assertTrue(json.loads(response.content)['done'])
            self.assertEqual(len(calls), 3)
            self.assertEqual(bb.count(BlackboardLayer.TOPOLOGY), 1)
