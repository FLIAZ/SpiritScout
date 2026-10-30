"""图上下文无损增量、旧前缀兼容与请求大小对照。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from spiritscout.agents.sub.base import RoutePlan
from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.exchange import GUIDANCE, SCHEMAS
from spiritscout.core.models import AgentType, BlackboardLayer, RunMode, Task
from spiritscout.ctf.agents import CTFBaseAgent, CTFCryptoAgent
from spiritscout.gateway.context import DecisionMemory, estimate_input_tokens
from spiritscout.gateway.gateway import ChatResponse, chat_decision
from spiritscout.gateway.world_state import render, snapshot, update


def node(key: str, version: int = 1) -> dict:
    return {'blackboard_ref': {'layer': 'topology', 'record_id': key, 'version': version},
            'summary': '核对授权测试账号的身份边界、数据归属与实际响应。' * 3,
            'node_type': 'hypothesis', 'state': 'open' if version == 1 else 'running', 'updated_by': 'fixture'}


def graph(rows: list[dict], **meta) -> dict:
    return {'ok': True, 'items': rows, 'total': len(rows), 'truncated': False,
            'has_more': False, 'next_offset': 8, **meta}


def body(fragment: str) -> dict:
    return json.loads(fragment.split('\n', 1)[1].rsplit('\n', 1)[0])


def restore(previous: dict, message: dict) -> dict:
    result = copy.deepcopy(previous)
    if 'exploration_graph' in message:
        return message['exploration_graph']
    delta = message['exploration_graph_delta']
    if 'meta' in delta:
        result = {**delta['meta'], 'items': result['items']}
    def identity(row: dict) -> tuple[str, str]:
        return row['blackboard_ref']['layer'], row['blackboard_ref']['record_id']
    items = {identity(row): row for row in result['items']}
    for ref in delta['retired']:
        items.pop((ref['layer'], ref['record_id']))
    for row in delta['upsert']:
        items[identity(row)] = row
    result['items'] = sorted(items.values(), key=identity)
    return result


class GraphDeltaTests(unittest.TestCase):
    def test_single_update_is_smaller_and_preserves_complete_authorization(self) -> None:
        before = {'exploration_graph': graph([node(str(i)) for i in range(8)])}
        after = {'exploration_graph': graph([node(str(i), 2 if i == 4 else 1) for i in range(8)]),
                 'authorization': {'testing_rules': '用户原话，不可省略。' * 1000}}
        full = update(before, after)
        compact = update(before, after, incremental_exploration=True)
        message = body(compact)
        self.assertEqual(message['authorization'], after['authorization'])
        self.assertEqual(message['exploration_graph_delta']['upsert'], [node('4', 2)])
        self.assertEqual(restore(before['exploration_graph'], message), after['exploration_graph'])
        self.assertLess(estimate_input_tokens([{'role': 'user', 'content': compact}]),
                        estimate_input_tokens([{'role': 'user', 'content': full}]))
        self.assertLess(len(compact.encode()), len(full.encode()))

    def test_retirement_metadata_and_full_replacement_restore_same_graph(self) -> None:
        before = graph([node(str(i)) for i in range(8)])
        after = graph([node(str(i)) for i in range(1, 9)], total=9, truncated=True, has_more=True)
        message = body(update({'exploration_graph': before}, {'exploration_graph': after}, incremental_exploration=True))
        self.assertEqual(message['exploration_graph_delta']['retired'], [{'layer': 'topology', 'record_id': '0'}])
        self.assertEqual(restore(before, message), after)
        changed = graph([node(str(i), 2) for i in range(1, 9)])
        replacement = update({'exploration_graph': after}, {'exploration_graph': changed}, incremental_exploration=True)
        self.assertIn('exploration_graph', body(replacement), 'large patches must fall back to a complete field')
        self.assertEqual(restore(after, body(replacement)), changed)

    def test_initial_legacy_invalid_rows_and_clear_events_keep_full_contract(self) -> None:
        before = {'exploration_graph': graph([node('a')]), 'environment': {'date': 'fixture'}}
        self.assertEqual(update(None, before, incremental_exploration=True), render(before))
        changed = {**before, 'exploration_graph': graph([node('a', 2)])}
        self.assertIn('exploration_graph', body(update(before, changed)))
        malformed = {**before, 'exploration_graph': graph([{'summary': '无身份旧记录'}])}
        self.assertIn('exploration_graph', body(update(before, malformed, incremental_exploration=True)))
        cleared = update(before, {'environment': before['environment']}, incremental_exploration=True)
        self.assertIsNone(body(cleared)['exploration_graph'])
        for enabled in (False, True):
            ordered = {'exploration_graph': graph([node(str(i)) for i in range(8)])}
            reordered = {'exploration_graph': graph(list(reversed(ordered['exploration_graph']['items'])))}
            self.assertIsNone(update(ordered, reordered, incremental_exploration=enabled))

    def test_recent_membership_is_preserved_without_positional_patch_churn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            for i in range(9):
                bb.write(BlackboardLayer.TOPOLOGY, str(i), {'kind': 'exploration', 'text': f'假设 {i}',
                         'node_type': 'hypothesis', 'state': 'open'})
            before = snapshot(blackboard=bb, references=True)
            self.assertEqual([r['blackboard_ref']['record_id'] for r in before['exploration_graph']['items']], list('12345678'))
            bb.write(BlackboardLayer.TOPOLOGY, '4', {'kind': 'exploration', 'text': '假设 4',
                     'node_type': 'hypothesis', 'state': 'running'})
            after = snapshot(blackboard=bb, references=True)
            self.assertEqual([r['blackboard_ref']['record_id'] for r in after['exploration_graph']['items']], list('12345678'))
            changes = DecisionMemory.patch(before, after)
            self.assertEqual({item['path'] for item in changes}, {
                '/exploration_graph/items/3/blackboard_ref/version', '/exploration_graph/items/3/state'})
            self.assertEqual(before['exploration_graph']['items'][0], after['exploration_graph']['items'][0])

    def test_new_ctf_contract_is_shorter_and_legacy_contract_still_available(self) -> None:
        agent = object.__new__(CTFCryptoAgent)
        agent.ctx = SimpleNamespace(mode=RunMode.CTF, search_enabled=False)
        legacy = agent._solver_system_prompt(correct_artifact_path=True)
        compact = agent._solver_system_prompt(correct_artifact_path=True, compact_graph_context=True)
        self.assertEqual(legacy.count(GUIDANCE), 2)
        self.assertEqual(compact.count(GUIDANCE), 1)
        self.assertIn('exploration_graph_delta', compact)
        self.assertNotIn('exploration_graph_delta', legacy)
        self.assertLess(estimate_input_tokens([{'role': 'system', 'content': compact}]),
                        estimate_input_tokens([{'role': 'system', 'content': legacy}]))

    def test_ctf_updates_require_negotiated_delta_and_keep_snapshot(self) -> None:
        before = {'exploration_graph': graph([node(str(i)) for i in range(8)])}
        after = {'exploration_graph': graph([node(str(i), 2 if i == 4 else 1) for i in range(8)])}
        agent = object.__new__(CTFBaseAgent)
        agent._world_snapshot = lambda: copy.deepcopy(after)
        for enabled in (False, True):
            state = {'world_state': copy.deepcopy(before), 'compact_graph_context_v1': enabled}
            fragment, changed = agent._sync_world_state(state)
            self.assertIn('exploration_graph_delta' if enabled else 'exploration_graph', body(fragment))
            self.assertEqual(changed, ['exploration_graph'])
            self.assertEqual(state['world_state'], after)
            self.assertEqual(agent._sync_world_state(state), ('', []))

    def test_seed_pins_compact_protocol_and_keeps_old_handoff_contract(self) -> None:
        from tests.test_project_injection import CTFProjectMemorySeedTests

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            agent = CTFProjectMemorySeedTests()._agent(bb)
            agent.ctx.gateway = SimpleNamespace(config=SimpleNamespace(shared_ctf_prefix=False))
            agent._solver_system_prompt = lambda **options: json.dumps(options, sort_keys=True)
            agent._inherit_conversation = lambda *args: None
            task = Task(task_id='fixture', task_type='ctf_solve', target='http://fixture.test',
                        agent_type=AgentType.CTF_WEB)
            plan = RoutePlan(name='fixture', strategy='核对现场')
            fresh = {'seed': []}
            agent._seed_messages(task, plan, plan, fresh)
            self.assertTrue(fresh['compact_graph_context_v1'])
            self.assertTrue(json.loads(fresh['messages'][0]['content'])['compact_graph_context'])
            saved = {'state': {'shared_prefix': False, 'artifact_path_v2': True}}
            agent.ctx.journal = SimpleNamespace(latest_checkpoint=lambda *args: saved)
            task.payload['continue_from'] = {'task_id': 'old', 'route_id': 'old-route'}
            inherited = {'seed': []}
            agent._seed_messages(task, plan, plan, inherited)
            self.assertFalse(inherited['compact_graph_context_v1'])
            self.assertNotIn('compact_graph_context', json.loads(inherited['messages'][0]['content']))

    def test_snapshot_keeps_compatibility_with_read_only_blackboard_adapters(self) -> None:
        self.assertEqual(snapshot(blackboard=SimpleNamespace(read=lambda *args: None), references=True), {})


class GraphDecisionPrefixTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_decisions_deduplicate_guidance_without_extra_model_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            response = ChatResponse(content='{"done":true}', model='fixture', prompt_tokens=1,
                                    completion_tokens=1, cost_usd=0, latency_ms=0)
            gateway = SimpleNamespace(chat=AsyncMock(return_value=response))
            memory = DecisionMemory()
            memory.blackboard, memory.agent_type = bb, AgentType.MASTER
            messages = [{'role': 'system', 'content': '原始契约\n' + GUIDANCE}, {'role': 'user', 'content': '{}'}]
            await chat_decision(gateway, messages, memory=memory, response_format='json')
            first = copy.deepcopy(gateway.chat.call_args.args[0])
            self.assertEqual(first[0]['content'].count(GUIDANCE), 1)
            await chat_decision(gateway, messages, memory=memory, response_format='json')
            second = gateway.chat.call_args.args[0]
            self.assertEqual(second[:len(first)], first)
            self.assertEqual(gateway.chat.await_count, 2)
            self.assertFalse(memory.segment_changed)

    async def test_restored_json_checkpoint_keeps_old_system_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            base = '原始契约\n' + GUIDANCE
            legacy = (base + '\n' + GUIDANCE + '\n需要读取资料或共享事实时，只返回 '
                      '{"skill_requests":[{"name":"工具名","arguments":{}}]}；结果回灌后继续原 JSON 任务。统一工具契约：'
                      + json.dumps(SCHEMAS, ensure_ascii=False, separators=(',', ':')))
            memory = DecisionMemory()
            memory.blackboard, memory.agent_type = bb, AgentType.MASTER
            memory.prepare([{'role': 'system', 'content': legacy}, {'role': 'user', 'content': '{}'}])
            memory.commit('{"done":false}')
            checkpoint = copy.deepcopy(memory.messages)
            response = ChatResponse(content='{"done":true}', model='fixture', prompt_tokens=1,
                                    completion_tokens=1, cost_usd=0, latency_ms=0)
            gateway = SimpleNamespace(chat=AsyncMock(return_value=response))
            await chat_decision(gateway, [{'role': 'system', 'content': base}, {'role': 'user', 'content': '{}'}],
                                memory=memory, response_format='json')
            self.assertEqual(gateway.chat.call_args.args[0][:len(checkpoint)], checkpoint)
            self.assertEqual(memory.system, legacy)
            self.assertFalse(memory.segment_changed)
