"""图直接投影持久化事实，执行日志不制造节点或因果。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer
from spiritscout.web.process_graph import project_blackboard, rules_text


class ProcessGraphTests(unittest.TestCase):
    def test_only_blackboard_facts_rules_and_explicit_evidence_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TASK_STATE, 'step', {'title': '开始扫描'})
            bb.write(BlackboardLayer.TOPOLOGY, 'reply', {'kind': 'reply', 'text': '已写输出产物'})
            bb.write(BlackboardLayer.TARGET_AUTH, 'user-context', {'standing_rules': '仅测试 A' * 10000})
            bb.write(BlackboardLayer.ASSET, 'asset', {'value': 'https://fixture.test/login'})
            bb.write(BlackboardLayer.TOPOLOGY, 'fact', {'kind': 'finding', 'text': '表单需要登录',
                     'evidence_refs': ['asset', 'response.log'], 'output_file': 'response.log'})
            graph = project_blackboard(bb)
            self.assertEqual(len(graph['nodes']), 3)
            self.assertEqual({n['kind'] for n in graph['nodes']}, {'finding', 'rule'})
            self.assertEqual(len(graph['edges']), 1)
            asset = next(n for n in graph['nodes'] if n['record_id'] == 'asset')
            self.assertEqual(asset['reference_type'], 'assets')
            fact = next(n for n in graph['nodes'] if n['record_id'] == 'fact')
            self.assertIn('response.log', fact['body'])
            self.assertEqual(fact['reference_type'], '')
            self.assertIn('provenance', fact)
            self.assertNotIn('仅测试 A', str(graph))
            self.assertNotIn('step', {node['record_id'] for node in graph['nodes']})

    def test_business_facts_and_evidence_exclude_workflow_steps_and_hypotheses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, 'business-context', {
                'facts': [{'statement': '登录表单存在', 'evidence_refs': ['E1']}],
                'evidence': [{'id': 'E1', 'url': 'https://fixture.test/login'}],
                'workflows': [{'name': '登录', 'steps': ['打开', '提交']}],
                'hypotheses': [{'statement': '可能存在越权'}]})
            graph = project_blackboard(bb)
            self.assertEqual({n['kind'] for n in graph['nodes']}, {'finding', 'evidence'})
            self.assertEqual(len(graph['edges']), 1)
            self.assertNotIn('可能存在越权', str(graph))
            self.assertNotIn('提交', str(graph))

    def test_ambiguous_ids_do_not_invent_evidence_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, 'same', {'value': '入口'})
            bb.write(BlackboardLayer.RECON_CLUE, 'same', {'value': '线索'})
            bb.write(BlackboardLayer.TOPOLOGY, 'fact', {'kind': 'finding', 'text': '结论',
                     'evidence_refs': ['same']})
            self.assertFalse(project_blackboard(bb)['edges'])

    def test_structured_response_is_reference_and_versioned_fact_link_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.RECON_CLUE, 'response', {
                'value': 'https://fixture.test/api', 'detail': {'status_code': 403}})
            bb.write(BlackboardLayer.TOPOLOGY, 'observed', {'kind': 'finding', 'text': '普通成员被拒绝'})
            row = bb.read_versioned(BlackboardLayer.TOPOLOGY, 'observed')
            bb.write(BlackboardLayer.TOPOLOGY, 'derived', {
                'kind': 'finding', 'text': '管理接口存在权限边界',
                'evidence_refs': [f'topology:observed@{row[1]}']})
            graph = project_blackboard(bb)
            response = next(n for n in graph['nodes'] if n['record_id'] == 'response')
            self.assertEqual(response['reference_type'], 'evidence')
            self.assertEqual(response['title'], 'https://fixture.test/api')
            self.assertEqual(len(graph['edges']), 1)

    def test_rule_content_is_readable_and_empty_templates_are_not_nodes(self) -> None:
        self.assertEqual(rules_text({'kind': 'rule', 'content': '保持会话，遇到登录等我操作'}),
                         '保持会话，遇到登录等我操作')
        text = rules_text({'auth_id': 'auth', 'target_description': 'https://fixture.test',
                           'scope': {'allowed_urls': [], 'allowed_domains': [], 'testing_rules': ''}})
        self.assertEqual(text, '授权目标：https://fixture.test')
        self.assertEqual(rules_text({'mission': '', 'standing_rules': '', 'statements': []}), '')
        self.assertIn('用户输入：继续调查', rules_text({'statements': [{'kind': 'message', 'text': '继续调查'}]}))
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TARGET_AUTH, 'empty', {'mission': '', 'standing_rules': '', 'statements': []})
            self.assertEqual(project_blackboard(bb)['nodes'], [])
