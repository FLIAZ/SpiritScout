"""新对话共享前缀、旧检查点兼容和无损事实排序。"""
from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.agents.profiles import agent_prompt_parts, agent_system_prompt
from spiritscout.config.settings import ModelEntry
from spiritscout.core.models import AgentType, RunMode
from spiritscout.ctf.agents import CTFBaseAgent, CTFCryptoAgent, CTFMiscAgent
from spiritscout.gateway.context import DecisionMemory, decision_messages
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry


class StableLayoutTests(unittest.TestCase):
    def test_shared_profile_parts_preserve_original_role_contract(self) -> None:
        for kind in AgentType:
            common, role = agent_prompt_parts(kind, allow_search=False)
            self.assertEqual(role + common, agent_system_prompt(kind, allow_search=False))
        crypto = agent_prompt_parts(AgentType.CTF_CRYPTO, allow_search=False)
        misc = agent_prompt_parts(AgentType.CTF_MISC, allow_search=False)
        self.assertEqual(crypto[0], misc[0])
        self.assertNotEqual(crypto[1], misc[1])

    def test_new_ctf_prompt_has_long_shared_contract_and_correct_artifact_location(self) -> None:
        prompts = []
        for cls in (CTFCryptoAgent, CTFMiscAgent):
            agent = object.__new__(cls)
            agent.ctx = SimpleNamespace(mode=RunMode.CTF, search_enabled=False)
            prompts.append(agent._solver_system_prompt(shared_prefix=True))
            self.assertNotIn('../artifacts/', prompts[-1])
            self.assertIn('environment.artifacts', prompts[-1])
            legacy = agent._solver_system_prompt(shared_prefix=False)
            self.assertTrue(legacy.startswith('你是灵枢'))
            self.assertIn('../artifacts/', legacy)
            fixed = agent._solver_system_prompt(correct_artifact_path=True)
            self.assertTrue(fixed.startswith('你是灵枢'))
            self.assertNotIn('../artifacts/', fixed)
            self.assertIn('environment.artifacts',fixed)
        prefix = 0
        for a, b in zip(*prompts):
            if a != b:
                break
            prefix += 1
        self.assertGreater(len(prompts[0][:prefix].encode()), 6000)

    def test_snapshot_stable_facts_precede_dynamic_values_without_data_loss(self) -> None:
        payload = {'active_tasks':[{'id':'new-1'}], 'round':5,
                   'world_state':{'authorization':{'rules':'规则原文'}},
                   'mission':'全部证据均须保留', 'reference_skills':[{'original':'完整协议'}]}
        memory = DecisionMemory()
        first = memory.prepare(decision_messages('契约', payload), stable_snapshot_prefix=True)
        snap = json.loads(first[-1]['content'])['snapshot']
        self.assertEqual(snap, payload)
        self.assertEqual(list(snap)[:3], ['world_state','mission','reference_skills'])
        memory.commit('{"ok":true}', reasoning='已验证的推理')
        prefix = copy.deepcopy(memory.messages)
        payload['round'] = 6
        later = memory.prepare(decision_messages('契约',payload), stable_snapshot_prefix=True)
        self.assertEqual(later[:len(prefix)], prefix)
        self.assertEqual(later[-2]['reasoning_content'], '已验证的推理')

    def test_old_history_is_not_rewritten_when_new_layout_is_enabled(self) -> None:
        memory = DecisionMemory()
        memory.prepare(decision_messages('契约', {'z':1,'world_state':{'rules':'旧原文'}}))
        memory.commit('{"ok":true}')
        old = copy.deepcopy(memory.messages)
        after = memory.prepare(decision_messages('契约', {'z':2,'world_state':{'rules':'旧原文'}}),
                               stable_snapshot_prefix=True)
        self.assertEqual(after[:len(old)], old)


class ToolPrefixTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_conversation_pins_tool_layout_across_config_changes(self) -> None:
        from tests.test_steering_delivery import SteeringExecutionTests

        agent,state,task,route,plan=SteeringExecutionTests()._solver()
        agent.ctx.gateway.config=SimpleNamespace(shared_ctf_prefix=True)
        agent.ctx.gateway.chat.return_value=SimpleNamespace(
            content='继续核对现场',reasoning='',tool_calls=[],finish_reason='stop',model='fixture')
        agent._save_inv_state=lambda *args:None
        await agent._solve_turn(task,route,plan)
        self.assertFalse(agent.ctx.gateway.chat.call_args.kwargs['preserve_tool_order'])
        state['concluded']=False
        state['shared_prefix']=True
        agent.ctx.gateway.config.shared_ctf_prefix=False
        await agent._solve_turn(task,route,plan)
        self.assertTrue(agent.ctx.gateway.chat.call_args.kwargs['preserve_tool_order'])

    def test_base_tools_remain_identical_prefix_across_specialties(self) -> None:
        common=CTFBaseAgent.tool_schemas
        for cls in (CTFCryptoAgent,CTFMiscAgent):
            self.assertEqual(cls.tool_schemas[:len(common)],common)

    async def test_explicit_tool_order_preserves_shared_prefix_without_mutation(self) -> None:
        gateway = ModelGateway(ModelRegistry([ModelEntry(model='fixture',provider='mock')]))
        tools = [{'type':'function','function':{'name':name,'parameters':{'type':'object'}}}
                 for name in ['shared_z','shared_a','special_b']]
        original = copy.deepcopy(tools)
        backend = SimpleNamespace(complete=AsyncMock(return_value={
            'content':'ok','prompt_tokens':100,'completion_tokens':1}))
        with patch('spiritscout.gateway.gateway.backend_for', return_value=backend):
            await gateway.chat([{'role':'user','content':'完整题面'}],tools=tools,preserve_tool_order=True)
            sent = backend.complete.call_args.kwargs['tools']
            self.assertEqual([t['function']['name'] for t in sent], ['shared_z','shared_a','special_b'])
            await gateway.chat([{'role':'user','content':'完整题面'}],tools=tools)
            self.assertEqual([t['function']['name'] for t in backend.complete.call_args.kwargs['tools']],
                             ['shared_a','shared_z','special_b'])
        self.assertEqual(tools, original)
