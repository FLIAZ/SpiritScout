"""主控 JSON、SRC 子 Agent 与 CTF 原生工具共用真实技能读取。"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import test_steering_delivery

from spiritscout.agents.recon.agent import ReconAgent
from spiritscout.core.models import AgentType
from spiritscout.gateway.context import (
    DecisionMemory,
    compact_history_tokens,
    decision_messages,
)
from spiritscout.gateway.gateway import chat_decision
from spiritscout.resources.assistant import validate_skill
from spiritscout.resources.runtime import SkillRuntime
from spiritscout.resources.store import ResourceStore, builtin_skills


def reply(data: dict) -> SimpleNamespace:
    return SimpleNamespace(content=json.dumps(data, ensure_ascii=False), finish_reason='stop', reasoning='')


def request(name: str, **arguments) -> dict:
    return {'skill_requests': [{'name': name, 'arguments': arguments}]}


class SkillAgentFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ResourceStore(self.tmp.name)
        data = {'skill_id': 'flow-skill', 'description': '仅适用于核对测试系统版本与验证范围',
                'files': {'SKILL.md': '# 指引\n' + '完整原文保留。' * 1200,
                          'references/scope.md': '# 测试边界\n授权仍由用户上下文定义。'}}
        validate_skill(data)
        self.store.save('skill', 'flow-skill', data, 0)
        self.runtime = SkillRuntime(self.store)

    async def test_master_json_progressive_loading_no_duplicate_content_and_resume(self) -> None:
        gateway = SimpleNamespace(skill_runtime=self.runtime, chat=AsyncMock(side_effect=[
            reply(request('skill_open', skill_id='flow-skill')),
            reply(request('skill_read', skill_id='flow-skill', path='references/scope.md')),
            reply(request('skill_open', skill_id='flow-skill')),
            reply({'plan': '基于原文与授权形成动态计划'}),
        ]))
        memory, audit = DecisionMemory(), Mock()
        result = await chat_decision(gateway, decision_messages('主控规划', {'mission': '核对版本'}),
                                     actor='master-fixture', audit=audit, memory=memory, response_format='json')
        self.assertIn('动态计划', result.content)
        sent = gateway.chat.await_args_list
        self.assertNotIn('完整原文保留', str(sent[0].args[0]))
        final = json.dumps(sent[-1].args[0], ensure_ascii=False)
        self.assertEqual(final.count('完整原文保留'), 1200)
        self.assertIn('授权仍由用户上下文定义', final)
        self.assertIn('already_in_context', final)
        loaded = [call for call in audit.log.call_args_list if call.args[0] == 'skill.loaded']
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].args[1], 'master-fixture')
        # 换决策段仍从已读内容恢复，不依赖进程级的“已加载”布尔标记。
        memory.soft_chars = 1
        gateway.chat.side_effect = [reply({'plan': '继续核对'})]
        await chat_decision(gateway, decision_messages('新的主控契约', {'mission': '核对版本'}),
                            memory=memory, response_format='json')
        self.assertIn('完整原文保留', gateway.chat.call_args.args[0][1]['content'])

    async def test_no_implicit_forced_tool_call(self) -> None:
        gateway = SimpleNamespace(skill_runtime=self.runtime, chat=AsyncMock(return_value=reply({'reply': '直接回答普通问题'})))
        audit = Mock()
        await chat_decision(gateway, decision_messages('普通对话', {'message': '你好'}),
                            audit=audit, response_format='json')
        self.assertFalse(any(call.args[0] == 'skill.loaded' for call in audit.log.call_args_list))
        self.assertEqual(gateway.chat.await_count, 1)

    async def test_src_json_subagent_has_same_skill_capability(self) -> None:
        agent = object.__new__(ReconAgent)
        agent.agent_id, agent.agent_type = 'recon-fixture', AgentType.RECON
        agent.enable_llm_planning = True
        agent._world_state = Mock(return_value={})
        agent._system_prompt = Mock(return_value='侦察职责')
        gateway = SimpleNamespace(skill_runtime=self.runtime, chat=AsyncMock(side_effect=[
            reply(request('skill_open', skill_id='flow-skill')), reply({'paths': [], 'rationale': '已核对参考'})]))
        agent.ctx = SimpleNamespace(audit=Mock(), ring=None, blackboard=None, gateway=gateway)
        result = await agent._llm_chat_json('path_planner', {'mission': '核对版本'}, _allow_ask=False)
        self.assertEqual(result['rationale'], '已核对参考')
        self.assertIn('完整原文保留', str(gateway.chat.call_args.args[0]))

    async def test_native_ctf_result_is_complete_protected_and_route_local(self) -> None:
        agent, state, task, route, plan = test_steering_delivery.SteeringExecutionTests()._solver()
        agent.ctx.gateway.skill_runtime = self.runtime
        agent.ctx.gateway.chat.side_effect = [
            SimpleNamespace(content='采用相关参考', tool_calls=[{'id': 'open-1', 'name': 'skill_open',
                'arguments': {'skill_id': 'flow-skill'}}], finish_reason='tool_calls'),
            SimpleNamespace(content='读取引用', tool_calls=[{'id': 'read-1', 'name': 'skill_read',
                'arguments': {'skill_id': 'flow-skill', 'path': 'references/scope.md'}}], finish_reason='tool_calls'),
        ]
        await agent._solve_turn(task, route, plan)
        tool = state['messages'][-1]
        self.assertGreater(len(tool['content']), 6000)
        self.assertTrue(tool['_context_contract'])
        compact_history_tokens(state['messages'], 100)
        self.assertEqual(state['messages'][-1]['content'], tool['content'])
        await agent._solve_turn(task, route, plan)
        self.assertIn('授权仍由用户上下文定义', state['messages'][-1]['content'])
        other = {'messages': []}
        result = await agent._run_tool_call(task, route, other, {'name': 'skill_open', 'arguments': {'skill_id': 'flow-skill'}})
        self.assertIn('完整原文保留', result.text)
        self.assertNotIn('already_in_context', result.text)

    def test_all_builtin_descriptions_have_real_scope(self) -> None:
        rows = builtin_skills().values()
        self.assertEqual(len(rows), 206)
        self.assertTrue(all(30 <= len(row['description']) <= 2000 for row in rows))
        self.assertTrue(all(row['description'].strip() not in ('>', '|') for row in rows))

    async def test_business_explorer_reads_complete_skill_through_native_loop(self) -> None:
        from spiritscout.agents.business.agent import BusinessContextAgent
        from spiritscout.agents.sub.base import RoutePlan
        from spiritscout.core.models import Task

        agent = object.__new__(BusinessContextAgent)
        agent.agent_id, agent._explore = 'business-fixture', {}
        agent._world_state = Mock(return_value={})
        agent._system_prompt = Mock(return_value='业务职责')
        agent._explore_brief = Mock(return_value='业务现场')
        agent.ctx = SimpleNamespace(audit=Mock(), ring=None, builtin_ctx={}, mcp_tools={},
            gateway=SimpleNamespace(skill_runtime=self.runtime, chat=AsyncMock(return_value=SimpleNamespace(
                content='读取业务参考', reasoning='', tool_calls=[{'id': 'open', 'name': 'skill_open',
                'arguments': {'skill_id': 'flow-skill'}}]))))
        task = Task(task_id='business-task', task_type='business_context', target='http://fixture.test', agent_type=AgentType.BUSINESS_CONTEXT)
        await agent._explore_turn(task, SimpleNamespace(route_id='r1'), RoutePlan('browse', '', []))
        turn = next(call.args[2] for call in agent.ctx.audit.log.call_args_list if call.args[0] == 'agent.turn')
        self.assertEqual((turn['task_id'], turn['route_id'], turn['route'], turn['turn']),
                         ('business-task', 'r1', 'browse', 1))
        message = agent._explore[task.task_id]['messages'][-1]
        self.assertIn('完整原文保留', message['content'])
        self.assertGreater(len(message['content']), 6000)
        self.assertTrue(message['_context_contract'])
        self.assertIn('skill_read', [s['function']['name'] for s in agent._tool_schemas()])

    async def test_verifier_can_read_skill_but_cannot_use_it_as_independent_evidence(self) -> None:
        from spiritscout.agents.verifier import VerifierAgent

        verifier = object.__new__(VerifierAgent)
        verifier.agent_id = 'verifier'
        verifier.rule_context = Mock(return_value={'world_state': {}})
        verifier.ctx = SimpleNamespace(audit=Mock(), pause_gate=None, mcp_tools={},
            gateway=SimpleNamespace(skill_runtime=self.runtime, chat=AsyncMock(side_effect=[
                SimpleNamespace(content='先读参考', tool_calls=[{'id': 'skill-only', 'name': 'skill_open',
                    'arguments': {'skill_id': 'flow-skill'}}]),
                SimpleNamespace(content=json.dumps({'verdict': 'confirmed', 'harm': '试图将参考当证据',
                    'evidence': [{'execution_id': 'skill-only', 'quote': '完整原文保留'}]}), tool_calls=[])])))
        candidate = SimpleNamespace(vuln_id='v1', model_dump=lambda **kwargs: {'id': 'v1'})
        result = await verifier.review(candidate, {})
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertIn('完整原文保留', str(verifier.ctx.gateway.chat.call_args.args[0]))
        self.assertIn('skill_read', [s['function']['name'] for s in verifier.schemas()])

    async def test_restart_restores_pinned_skill_version_and_read_context(self) -> None:
        import copy

        from spiritscout.gateway.context import decision_memory

        checkpoints = {}
        journal = SimpleNamespace(
            latest_checkpoint=lambda kind,key: checkpoints.get((kind,key)),
            checkpoint=lambda kind,key,status,state: checkpoints.__setitem__((kind,key), {'state': copy.deepcopy(state)}))
        owner = SimpleNamespace(ctx=SimpleNamespace(journal=journal))
        memory = decision_memory(owner, 'task-1:route-1:plan')
        gateway = SimpleNamespace(skill_runtime=self.runtime, chat=AsyncMock(side_effect=[
            reply(request('skill_open', skill_id='flow-skill')), reply({'reply': '已读根文件'})]))
        await chat_decision(gateway, decision_messages('计划', {'mission': '核对版本'}),
                            memory=memory, response_format='json')
        data = self.store.skills('flow-skill', include_body=True)[0]
        data['files']['references/scope.md'] = '修改后的新版本'
        validate_skill(data)
        self.store.save('skill', 'flow-skill', data, 1)
        restored = decision_memory(SimpleNamespace(ctx=SimpleNamespace(journal=journal)), 'task-1:route-1:plan')
        self.assertEqual(restored.skills, memory.skills)
        gateway.chat.side_effect = [reply(request('skill_read', skill_id='flow-skill', path='references/scope.md')),
                                    reply({'reply': '继续旧版本任务'})]
        await chat_decision(gateway, decision_messages('计划', {'mission': '核对版本'}),
                            memory=restored, response_format='json')
        self.assertIn('授权仍由用户上下文定义', str(gateway.chat.call_args.args[0]))
        self.assertNotIn('修改后的新版本', str(gateway.chat.call_args.args[0]))
