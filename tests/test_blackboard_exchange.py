"""规则无损外置、分段读取与事实共享。"""
import json
import tempfile
import unittest
from pathlib import Path

from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.exchange import execute, project_rules
from spiritscout.core.models import AgentType, BlackboardLayer


class BlackboardExchangeTests(unittest.TestCase):
    def test_shared_rejection_is_searchable_after_newer_facts_and_revisable_by_peer(self):
        from spiritscout.gateway.world_state import snapshot

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bb.db'
            bb = Blackboard(path)
            args = {'memory_id': 'response-check', 'base_version': 0, 'claim_kind': 'rejected',
                    'text': '该条件下候选输入未改变响应', 'evidence_refs': ['original-execution'],
                    'scope': '目标实例 A / 身份 X', 'conditions': '输入组 C，版本 1',
                    'revalidate_when': '实例、身份、版本变化或出现新输入依据',
                    'origin_assessment': 'mixed', 'origin_basis': '输出含本地产生的输入和目标响应'}
            first = execute(bb, 'blackboard_write', args, actor='solver-A', memory_enabled=True,
                            source={'task_id': 'task-A', 'route_id': 'route-A'})
            self.assertTrue(first['ok'])
            repeated = execute(bb, 'blackboard_write', {**args, 'base_version': first['blackboard_ref']['version']},
                               actor='solver-B', memory_enabled=True, source={'task_id': 'task-B', 'turn': 9})
            self.assertTrue(repeated['already_present'])
            self.assertEqual(repeated['blackboard_ref'], first['blackboard_ref'])
            for i in range(15):
                execute(bb, 'blackboard_write', {'text': f'后续观察 {i}', 'evidence_refs': ['execution']}, actor='peer')
            bb.close()
            bb = Blackboard(path)
            self.addCleanup(bb.close)
            index = snapshot(blackboard=bb, references=True)['shared_facts']
            row = next(r for r in index if r['blackboard_ref'] == first['blackboard_ref'])
            self.assertEqual(row['claim_kind'], 'rejected')
            self.assertEqual(row['origin_assessment'], 'mixed')
            found = execute(bb, 'blackboard_read', {'layer': 'topology', 'claim_kind': 'rejected',
                            'find': '候选输入'}, actor='solver-B', memory_enabled=True)
            self.assertEqual(found['total'], 1)
            read = execute(bb, 'blackboard_read', first['blackboard_ref'], actor='solver-B', memory_enabled=True)
            item = json.loads(read['content'])
            self.assertEqual(item['conditions'], args['conditions'])
            self.assertEqual(item['task_id'], 'task-A')
            self.assertEqual(item['evidence_state'], 'unverified')
            revision = {**args, 'base_version': first['blackboard_ref']['version'],
                        'claim_kind': 'hypothesis', 'scope': '实例 B / 身份 Y',
                        'text': '条件变化，重新验证'}
            updated = execute(bb, 'blackboard_write', revision, actor='solver-B', memory_enabled=True)
            self.assertTrue(updated['ok'])
            self.assertEqual(updated['blackboard_ref']['record_id'], first['blackboard_ref']['record_id'])
            self.assertGreater(updated['blackboard_ref']['version'], first['blackboard_ref']['version'])
            stale = execute(bb, 'blackboard_write', revision, actor='solver-A', memory_enabled=True)
            self.assertFalse(stale['ok'])
            self.assertEqual(stale['blackboard_ref'], updated['blackboard_ref'])
            self.assertFalse(execute(bb, 'blackboard_read', first['blackboard_ref'], actor='peer')['ok'])

    def test_shared_memory_validates_conditions_and_does_not_promote_execution_to_target(self):
        from spiritscout.blackboard.exchange import (
            MEMORY_SCHEMAS,
            SCHEMAS,
            ensure_memory_contract,
        )

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            args = {'memory_id': 'candidate', 'base_version': 0, 'claim_kind': 'fact',
                    'text': '脚本输出了候选值', 'evidence_refs': ['script-execution'],
                    'scope': '实例 A', 'conditions': '本地候选生成'}
            invalid = {**args, 'origin_assessment': 'target_response'}
            self.assertFalse(execute(bb, 'blackboard_write', invalid, actor='peer', memory_enabled=True)['ok'])
            self.assertFalse(execute(bb, 'blackboard_write', {**args, 'conditions': ''},
                                     actor='peer', memory_enabled=True)['ok'])
            self.assertFalse(execute(bb, 'blackboard_write', {**args, 'claim_kind': 'rejected'},
                                     actor='peer', memory_enabled=True)['ok'])
            self.assertFalse(execute(bb, 'blackboard_write', {k: v for k, v in args.items() if k != 'memory_id'},
                                     actor='peer', memory_enabled=True)['ok'])
            saved = execute(bb, 'blackboard_write', args, actor='peer', memory_enabled=True)
            item = json.loads(execute(bb, 'blackboard_read', saved['blackboard_ref'], actor='other')['content'])
            self.assertEqual(item['origin_assessment'], 'unknown')
            self.assertEqual(item['evidence_state'], 'unverified')
            self.assertNotIn('memory_id', SCHEMAS[1]['function']['parameters']['properties'])
            self.assertIn('memory_id', MEMORY_SCHEMAS[1]['function']['parameters']['properties'])
            prefix = [{'role': 'system', 'content': '原契约'}, {'role': 'user', 'content': '原任务'}]
            state = {'messages': prefix.copy()}
            ensure_memory_contract(state)
            first = state['messages'].copy()
            ensure_memory_contract(state)
            self.assertEqual(state['messages'], first)
            self.assertEqual(state['messages'][:-1], prefix)

    def test_inline_user_context_tracks_exact_version_without_truncating_large_rules(self) -> None:
        from spiritscout.gateway.world_state import snapshot

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            original = {'mission': '读取给定站点题面', 'standing_rules': '只测试本题'}
            bb.write(BlackboardLayer.TARGET_AUTH, 'user-context', original, updated_by='user')
            first = snapshot(blackboard=bb, references=True)['user_context']
            self.assertEqual(first['inline_content'], original)
            new = {**original, 'standing_rules': '只测试本题，保留所有原始记录'}
            bb.write(BlackboardLayer.TARGET_AUTH, 'user-context', new, updated_by='user')
            second = snapshot(blackboard=bb, references=True)['user_context']
            self.assertEqual(second['inline_content'], new)
            self.assertGreater(second['blackboard_ref']['version'], first['blackboard_ref']['version'])
            self.assertEqual(project_rules(bb, {'user_context': first})['user_context'], first)
            new['standing_rules'] *= 300
            bb.write(BlackboardLayer.TARGET_AUTH, 'user-context', new, updated_by='user')
            large = snapshot(blackboard=bb, references=True)['user_context']
            self.assertNotIn('inline_content', large)
            self.assertEqual(bb.read(BlackboardLayer.TARGET_AUTH, 'user-context'), new)

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
    async def test_shared_contract_upgrade_keeps_committed_system_and_message_prefix(self):
        import copy
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from spiritscout.gateway.context import DecisionMemory, decision_messages
        from spiritscout.gateway.gateway import ChatResponse, chat_decision

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            memory = DecisionMemory()
            memory.blackboard, memory.agent_type = bb, AgentType.MASTER
            gateway = SimpleNamespace(config=SimpleNamespace(lightweight_local=False),
                chat=AsyncMock(return_value=ChatResponse(content='{"done":true}', model='fixture',
                    prompt_tokens=1, completion_tokens=1, cost_usd=0, latency_ms=0)))
            original = decision_messages('固定系统', {'mission': 'fixture'})
            await chat_decision(gateway, original, memory=memory, response_format='json')
            prefix = copy.deepcopy(memory.messages)
            system = memory.system
            memory.shared_memory_enabled = True
            await chat_decision(gateway, original, memory=memory, response_format='json')
            sent = gateway.chat.call_args.args[0]
            self.assertEqual(sent[:len(prefix)], prefix)
            self.assertEqual(memory.system, system)
            patch = json.loads(sent[-1]['content'])['patch']
            self.assertEqual([(p['op'], p['path']) for p in patch], [('add', '/shared_memory_contract')])
            gateway.config.lightweight_local = True
            local = DecisionMemory()
            local.blackboard, local.shared_memory_enabled = bb, True
            await chat_decision(gateway, original, memory=local, response_format='json')
            self.assertNotIn('shared_memory_contract', local.snapshot)
            self.assertNotIn('execution_data_rules', local.snapshot)

    async def test_competition_agents_share_versioned_memory_through_existing_decision_channel(self):
        from types import SimpleNamespace

        from spiritscout.core.models import RunMode
        from spiritscout.gateway.context import decision_memory, decision_messages
        from spiritscout.gateway.gateway import ChatResponse, chat_decision
        from spiritscout.gateway.world_state import snapshot

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            owner = SimpleNamespace(agent_id='planner', agent_type=AgentType.MASTER,
                                    ctx=SimpleNamespace(mode=RunMode.COMPETITION, blackboard=bb))
            memory = decision_memory(owner, 'plan')
            self.assertTrue(memory.shared_memory_enabled)
            calls = []

            async def chat(messages, **kwargs):
                calls.append(copy_messages := json.loads(json.dumps(messages)))
                payload = memory.pending[1]
                self.assertIn('shared_memory_contract', payload)
                self.assertIn('execution_data_rules', payload)
                if len(calls) == 1:
                    result = {'skill_requests': [{'name': 'blackboard_write', 'arguments': {
                        'memory_id': 'constraint-A', 'base_version': 0, 'claim_kind': 'rejected',
                        'text': '条件 C 下该路径无效', 'scope': '实例 A', 'conditions': '条件 C',
                        'revalidate_when': '条件 C 改变', 'evidence_refs': ['source-execution'],
                        'origin_assessment': 'generated'}}]}
                else:
                    self.assertEqual(copy_messages[:len(calls[0])], calls[0])
                    self.assertTrue(payload['skill_observations'][0]['ok'])
                    result = {'done': True}
                return ChatResponse(content=json.dumps(result), model='fixture', prompt_tokens=1,
                                    completion_tokens=1, cost_usd=0, latency_ms=0)

            gateway = SimpleNamespace(config=SimpleNamespace(lightweight_local=False), chat=chat)
            await chat_decision(gateway, decision_messages('规划任务', {'mission': 'fixture'}),
                                memory=memory, response_format='json', actor='planner')
            index = snapshot(blackboard=bb, references=True)['shared_facts']
            self.assertEqual(index[0]['claim_kind'], 'rejected')
            peer_owner = SimpleNamespace(agent_id='peer', agent_type=AgentType.RECON, ctx=owner.ctx)
            peer = decision_memory(peer_owner, 'task-B:route-B')
            peer_calls = []

            async def peer_chat(messages, **kwargs):
                peer_calls.append(messages)
                payload = peer.pending[1]
                if len(peer_calls) == 1:
                    self.assertEqual(payload['world_state']['shared_facts'], index)
                    result = {'skill_requests': [{'name': 'blackboard_read', 'arguments': index[0]['blackboard_ref']}]}
                else:
                    item = json.loads(payload['skill_observations'][0]['content'])
                    self.assertEqual(item['conditions'], '条件 C')
                    self.assertEqual(item['origin_assessment'], 'generated')
                    result = {'reuse': '条件未变，按该结论选择其他路线'}
                return ChatResponse(content=json.dumps(result), model='fixture', prompt_tokens=1,
                                    completion_tokens=1, cost_usd=0, latency_ms=0)

            gateway.chat = peer_chat
            result = await chat_decision(gateway, decision_messages('执行任务', {
                'world_state': snapshot(blackboard=bb, references=True)}), memory=peer, response_format='json')
            self.assertIn('reuse', json.loads(result.content))
            self.assertEqual(len(peer_calls), 2)

    async def test_short_rules_are_complete_on_first_decision_with_versioned_reference(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from spiritscout.gateway.context import DecisionMemory
        from spiritscout.gateway.gateway import ChatResponse, chat_decision

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            memory = DecisionMemory()
            memory.blackboard = bb
            original = '仅测试本题，不能访问第三方。'
            projected = project_rules(bb, {'rules': original})
            self.assertEqual(projected['rules']['inline_content']['content'], original)
            self.assertEqual(project_rules(bb, projected), projected)
            gateway = SimpleNamespace(chat=AsyncMock(return_value=ChatResponse(
                content='{"done":true}', model='fixture', prompt_tokens=1,
                completion_tokens=1, cost_usd=0, latency_ms=0)))
            await chat_decision(gateway, [
                {'role': 'system', 'content': '规划并输出 JSON'},
                {'role': 'user', 'content': json.dumps(projected)}],
                memory=memory, response_format='json')
            gateway.chat.assert_awaited_once()
            sent = json.dumps(gateway.chat.call_args.args[0], ensure_ascii=False)
            self.assertIn(original, sent)
            self.assertIn('blackboard_ref', sent)

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
            original = '只测试租户 A，不能访问第三方' * 180
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
