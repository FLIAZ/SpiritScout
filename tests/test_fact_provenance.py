"""真实执行台账关联、来源不可伪造和依赖版本失效。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import AgentType, BlackboardLayer
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec


class FactProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fact_links_original_execution_even_when_result_was_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / 'bb.db')
            self.addCleanup(bb.close)
            journal = ExecutionJournal(bb, root, 'run')
            registry = ToolRegistry(SimpleNamespace(), journal=journal)

            async def observe(**kwargs):
                bb.write(BlackboardLayer.ASSET, 'page', {'value': 'http://fixture.test/login'},
                         agent_type=AgentType.RECON, updated_by='recon-a')
                return ToolResult(tool='read', ok=True, stdout='login form')

            registry.register_builtin('read', observe, ToolSpec('read', ToolKind.BUILTIN, reuse_ttl_seconds=30))
            token = current_execution_scope.set(ExecutionScope(agent_id='recon-a', task_id='task',
                route_id='route', step_id='step-1', turn=1, tool_call_id='native-call', tool_name='http_request'))
            try:
                first = await registry.call('read', {})
            finally:
                current_execution_scope.reset(token)
            page = bb.read(BlackboardLayer.ASSET, 'page')
            self.assertEqual(page['evidence_state'], 'observed')
            self.assertEqual(page['provenance']['producer']['step_id'], 'step-1')
            self.assertEqual(page['provenance']['producer']['tool_name'], 'http_request')
            second = await registry.call('read', {})
            self.assertEqual(second.source_invocation_id, first.invocation_id)
            bb.write(BlackboardLayer.TOPOLOGY, 'fact', {'kind': 'finding', 'text': '登录入口存在',
                'evidence_refs': [second.invocation_id], 'provenance': {'producer': {'agent_id': 'forged'}},
                'evidence_state': 'independently_verified'}, updated_by='recon-b')
            fact = bb.read(BlackboardLayer.TOPOLOGY, 'fact')
            self.assertEqual(fact['provenance']['producer']['agent_id'], 'recon-b')
            self.assertEqual(fact['provenance']['sources'][0]['invocation_id'], first.invocation_id)
            self.assertEqual(fact['provenance']['sources'][0]['step_id'], 'step-1')
            self.assertNotEqual(fact['evidence_state'], 'independently_verified')
            self.assertEqual(len(fact['provenance']['sources'][0]['params_digest']), 64)

    async def test_dependencies_become_expired_and_rules_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, 'parent', {'kind': 'finding', 'text': '旧观察'})
            bb.write(BlackboardLayer.TOPOLOGY, 'child', {'kind': 'finding', 'text': '衍生结论',
                                                       'evidence_refs': ['topology:parent@1']})
            bb.write(BlackboardLayer.TOPOLOGY, 'parent', {'kind': 'finding', 'text': '新观察'})
            self.assertEqual(bb.read(BlackboardLayer.TOPOLOGY, 'child')['evidence_state'], 'expired')
            rows = bb.query(BlackboardLayer.TOPOLOGY)
            self.assertEqual(next(r['data'] for r in rows if r['record_id'] == 'child')['evidence_state'], 'expired')
            rules = {'standing_rules': '只读测试'}
            bb.write(BlackboardLayer.TARGET_AUTH, 'rules', rules)
            self.assertEqual(bb.read(BlackboardLayer.TARGET_AUTH, 'rules'), rules)

    async def test_nested_business_facts_keep_sources_and_conflicts_need_reason(self) -> None:
        from spiritscout.blackboard.exchange import execute

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, 'page', {'value': 'http://fixture.test/login'})
            bb.write(BlackboardLayer.TOPOLOGY, 'business-context', {
                'facts': [{'statement': '登录表单存在', 'evidence_refs': ['E1']}],
                'evidence': [{'id': 'E1', 'record_id': 'page'}]})
            context = bb.read(BlackboardLayer.TOPOLOGY, 'business-context')
            self.assertEqual(context['facts'][0]['provenance']['sources'][0]['record_id'], 'page')
            bb.write(BlackboardLayer.ASSET, 'page', {'value': '更新的入口'})
            self.assertEqual(bb.read(BlackboardLayer.TOPOLOGY, 'business-context')['facts'][0]['evidence_state'], 'expired')
            args = {'text': '两次观测矛盾', 'evidence_refs': ['page'], 'assessment': 'conflicting'}
            self.assertFalse(execute(bb, 'blackboard_write', args, actor='a')['ok'])
            result = execute(bb, 'blackboard_write', {**args, 'assessment_reason': '角色相同但访问结果不同'}, actor='a')
            self.assertEqual(result['evidence_state'], 'conflicting')

    async def test_paged_reads_detect_evidence_projection_changes(self) -> None:
        from spiritscout.blackboard.exchange import execute

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / 'bb.db')
            self.addCleanup(bb.close)
            journal = ExecutionJournal(bb, root, 'run')
            journal.begin('inv-source', 'read', 'digest', 'safe')
            journal.transition('inv-source', 'running')
            bb.write(BlackboardLayer.TOPOLOGY, 'fact', {'kind': 'finding', 'text': '观察',
                                                       'evidence_refs': ['inv-source']})
            args = {'layer': 'topology', 'record_id': 'fact', 'limit': 10}
            page = execute(bb, 'blackboard_read', args, actor='reader')
            journal.transition('inv-source', 'succeeded')
            changed = execute(bb, 'blackboard_read', {**args, 'offset': page['next_offset'],
                             'projection_digest': page['projection_digest']}, actor='reader')
            self.assertFalse(changed['ok'])
            self.assertIn('重新读取', changed['error'])
