"""开放赛制：自定义名称与规则不触发固定赛制分支或执行模板。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.competition.rules import CompetitionRules
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, RunMode
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.modes.assembler import ModeAssembler


class OpenCompetitionTests(unittest.IsolatedAsyncioTestCase):
    def test_mission_does_not_force_a_known_contest(self) -> None:
        text = '创新混合赛制：含 AWD 服务保活、靶场题与协作取证，最终提交证据包。'
        plan = prepare_launch(LaunchRequest(mode=RunMode.COMPETITION, mission=text))
        self.assertEqual(plan.rules.contest_type.value, 'adaptive')
        self.assertEqual(plan.rules.raw['rules_text'], text)
        self.assertFalse(plan.needs_mode_routing)

    def test_custom_yaml_preserves_unrecognized_rule_fields(self) -> None:
        plan = prepare_launch(LaunchRequest(mode=RunMode.COMPETITION, rules_yaml='''
contest_type: 协作取证-vNext
scoring:
  evidence_chain: 50
  service_uptime: 50
stages: [协作取证, 维持服务, 提交证据]
'''))
        self.assertEqual(plan.rules.contest_type.value, '协作取证-vNext')
        self.assertEqual(plan.rules.raw['scoring']['service_uptime'], 50)
        self.assertEqual(plan.rules.raw['stages'][-1], '提交证据')

    def test_all_contest_labels_get_same_capabilities_and_model_planner(self) -> None:
        assembler = ModeAssembler(SimpleNamespace(), enable_llm_planning=True)
        # 复用真实装配分支，角色实例用无副作用对象替代。
        def src() -> SimpleNamespace:
            return SimpleNamespace(mode=RunMode.SRC,
                agents={AgentType.RECON: object(), AgentType.BUSINESS_CONTEXT: object()},
                plan_provider=None)

        def ctf(*args, **kwargs) -> SimpleNamespace:
            return SimpleNamespace(agents={AgentType.CTF_WEB: object()}, plan_provider=Mock())

        with patch.object(assembler, '_assemble_src', side_effect=src), \
             patch.object(assembler, '_assemble_ctf', side_effect=ctf):
            for label in ('adaptive', 'jeopardy', 'awd', 'range', '自创赛制-v3'):
                rules = CompetitionRules.from_dict({'contest_type': label, 'custom_goal': 'evidence'})
                assembly = assembler.assemble(RunMode.COMPETITION,
                    challenges=[{'id': 'stage-1'}], rules=rules)
                self.assertIsNone(assembly.plan_provider)
                self.assertIn(AgentType.RECON, assembly.agents)
                self.assertIn(AgentType.CTF_WEB, assembly.agents)
                self.assertEqual(assembly.competition_context['rules']['custom_goal'], 'evidence')
                self.assertEqual(assembly.competition_context['challenges'][0]['id'], 'stage-1')

    def test_competition_does_not_insert_fixed_business_phase(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(extra={'mode_prompt': '开放赛制'})
        nodes = [{'tactic': 'CTF-WEB', 'title': '赛方目标', 'depends_on': []}]
        self.assertEqual(master._ensure_business_gate(nodes), nodes)

    async def test_unavailable_planning_does_not_fall_back_to_src_template(self) -> None:
        master = object.__new__(MasterAgent)
        master.agent_id = 'master-test'
        master.ctx = SimpleNamespace(audit=NullAuditLogger(), pause_gate=None,
            enable_llm_planning=True, plan_override=None,
            extra={'mode_prompt': '开放赛制'}, tactic_map={}, agents={})
        master._llm_plan = AsyncMock(return_value=None)
        master._rule_plan = Mock(side_effect=AssertionError('不应套用固定 SRC 流程'))
        master._record_plan_result = Mock()
        master._write_dag_snapshot = Mock()
        result = await master._meta_plan({'dag': TaskDAG().to_dict(),
                                         'round_no': 0, 'target': 'contest'})
        self.assertEqual(result['tasks_dispatched'], [])
        self.assertEqual(result['plan_source'], 'unavailable')
