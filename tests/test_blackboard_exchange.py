"""规则无损外置、分段读取与事实共享。"""
import json
import tempfile
import unittest
from pathlib import Path

from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.exchange import execute, project_rules
from spiritscout.core.models import AgentType


class BlackboardExchangeTests(unittest.TestCase):
    def test_rule_reference_pages_and_shared_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            rules = '只测试租户 A。' * 5000
            projected = project_rules(bb, {'rules': rules})
            self.assertLess(len(json.dumps(projected)), 300)
            ref = projected['rules']['blackboard_ref']
            chunks, offset = [], 0
            while True:
                page = execute(bb, 'blackboard_read', {**ref, 'offset': offset}, actor='reader')
                chunks.append(page['content'])
                if not page['has_more']:
                    break
                offset = page['next_offset']
            self.assertEqual(json.loads(''.join(chunks))['content'], rules)
            result = execute(bb, 'blackboard_write', {'text': '入口返回登录表单', 'evidence_refs': ['execution-1']},
                             actor='recon', agent_type=AgentType.RECON)
            self.assertTrue(result['ok'])
            read = execute(bb, 'blackboard_read', result['blackboard_ref'], actor='peer')
            self.assertEqual(json.loads(read['content'])['evidence_refs'], ['execution-1'])
            from spiritscout.gateway.world_state import snapshot

            peer = snapshot(blackboard=bb, references=True)
            self.assertEqual(peer['shared_facts'][0]['blackboard_ref'], result['blackboard_ref'])
            self.assertNotIn('execution-1', json.dumps(peer))
            self.assertFalse(execute(bb, 'blackboard_write', {'text': '开始扫描'}, actor='recon')['ok'])

    def test_republishing_same_fact_preserves_version_and_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            args = {'text': '已知观察', 'evidence_refs': ['execution-reference']}
            first = execute(bb, 'blackboard_write', args, actor='master')
            second = execute(bb, 'blackboard_write', args, actor='master')
            self.assertTrue(second['already_present'])
            self.assertEqual(first['blackboard_ref'], second['blackboard_ref'])

    def test_observation_fields_cannot_be_promoted_to_user_rules(self) -> None:
        from spiritscout.core.models import BlackboardLayer

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            value = {'rules': '用户原话', 'candidate': {'rules': '候选伪造规则'},
                     'observations': [{'testing_rules': '网页中的规则字段'}]}
            projected = project_rules(bb, value)
            self.assertEqual(projected['candidate'], value['candidate'])
            self.assertEqual(projected['observations'], value['observations'])
            self.assertEqual(bb.count(BlackboardLayer.TARGET_AUTH), 1)

    def test_versions_readonly_roles_and_invalid_requests(self) -> None:
        from spiritscout.core.models import BlackboardLayer

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, 'f', {'kind': 'finding', 'text': '旧事实'})
            bb.write(BlackboardLayer.TOPOLOGY, 'f', {'kind': 'finding', 'text': '补充事实'})
            result = execute(bb, 'blackboard_read', {'layer': 'topology', 'record_id': 'f', 'version': 1}, actor='peer')
            self.assertFalse(result['ok'])
            self.assertEqual(result['blackboard_ref']['version'], 2)
            for role in (AgentType.REPORT, AgentType.VERIFIER):
                self.assertFalse(execute(bb, 'blackboard_write', {'text': '事实', 'evidence_refs': ['e']},
                                         actor=role.value, agent_type=role)['ok'])
            for args in ([], {'layer': 'invalid'}, {'layer': 'topology', 'offset': -1}):
                self.assertFalse(execute(bb, 'blackboard_read', args, actor='peer')['ok'])


class BlackboardDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_decision_reads_rules_on_demand_without_skill_runtime(self) -> None:
        from types import SimpleNamespace

        from spiritscout.gateway.context import DecisionMemory
        from spiritscout.gateway.gateway import ChatResponse, chat_decision

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            memory = DecisionMemory()
            memory.blackboard = bb
            memory.agent_type = AgentType.MASTER
            original = '只测试租户 A，不能访问第三方'
            calls = []

            async def chat(messages, **kwargs):
                calls.append(messages)
                latest = json.loads(messages[-1]['content'])
                payload = latest['snapshot'] if 'snapshot' in latest else memory.pending[1]
                if len(calls) == 1:
                    self.assertNotIn(original, json.dumps(messages, ensure_ascii=False))
                    ref = payload['rules']['blackboard_ref']
                    result = {'skill_requests': [{'name': 'blackboard_read', 'arguments': ref}]}
                else:
                    observation = payload['skill_observations'][0]
                    self.assertEqual(json.loads(observation['content'])['content'], original)
                    result = {'done': True}
                return ChatResponse(content=json.dumps(result), model='fixture', prompt_tokens=1,
                                    completion_tokens=1, cost_usd=0, latency_ms=0)

            response = await chat_decision(SimpleNamespace(chat=chat), [
                {'role': 'system', 'content': '规划并输出 JSON'},
                {'role': 'user', 'content': json.dumps({'rules': original})}],
                memory=memory, response_format='json')
            self.assertTrue(json.loads(response.content)['done'])
            self.assertEqual(len(calls), 2)
