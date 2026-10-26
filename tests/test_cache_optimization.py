"""真实计量口径、上下文追加、按需工具目录与 DeepSeek 协议回归。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import RunMode, Scope
from spiritscout.gateway.cache import PrefixTracker, cache_summary
from spiritscout.gateway.context import append_instruction_events
from spiritscout.gateway.gateway import ChatResponse, ModelGateway
from spiritscout.gateway.pricing import estimate_deepseek_cny
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.gateway.usage import UsageLedger
from spiritscout.policy import PolicyEngine, ScopeGuard
from spiritscout.tools.catalog import search_mcp_tools
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.registry import AgentToolView, ToolRegistry
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec
from tests import test_steering_delivery as steering_fixture


class CacheOptimizationTests(unittest.IsolatedAsyncioTestCase):
    def test_instruction_events_keep_request_prefix_and_retire_old_ids(self) -> None:
        state = {'messages': [{'role': 'system', 'content': '规则原文'}]}
        rec = {'id': 's1', 'task_id': 't1', 'hint': '读取证据', 'feedback': {}}
        append_instruction_events(state, [rec], consumer_key='t1|r1')
        first = copy.deepcopy(state['messages'])
        state['messages'].append({'role': 'assistant', 'content': '已读取'})
        append_instruction_events(state, [rec], consumer_key='t1|r1')
        self.assertEqual(state['messages'][:len(first)], first)
        self.assertEqual(len(state['messages']), len(first) + 1)
        rec['feedback'] = {'t1|r2': {'status': 'completed'}}
        append_instruction_events(state, [rec], consumer_key='t1|r1')
        self.assertEqual(len(state['messages']), len(first) + 1, '其他路线反馈不应改写本路线上下文')
        newer = {**rec, 'id': 's2', 'hint': '改查新证据'}
        append_instruction_events(state, [newer], consumer_key='t1|r1')
        self.assertIn('"失效指令":["s1"]', state['messages'][-1]['content'])
        self.assertEqual(state['messages'][:len(first)], first)

    async def test_steering_requests_append_progress_instead_of_rebuilding_snapshot(self) -> None:
        master = object.__new__(MasterAgent)
        master.agent_id = 'master'
        reply = ChatResponse(content='{"steering":[]}', model='fixture', prompt_tokens=1,
                             completion_tokens=1, cost_usd=0, latency_ms=0)
        master.ctx = SimpleNamespace(audit=NullAuditLogger(), blackboard=None,
                                    extra={'competition_context': {'challenges': [{'id': 'c1'}]}},
                                    gateway=SimpleNamespace(chat=AsyncMock(return_value=reply)))
        master._mission = lambda: '同一场任务原文' * 100
        master._world_state = lambda: {'user_context': {'mission': '完整保留规则' * 1000}}
        master._system_prompt = lambda: '稳定系统契约'
        digest = {'tasks': [{'task_id': 't1', 'agent': 'ctf_web', 'challenge': {'id': 'c1'},
                             'routes': [{'notes': ['旧证据']}]}]}
        await master._llm_steer(digest)
        first = copy.deepcopy(master.ctx.gateway.chat.call_args.args[0])
        digest['tasks'][0]['routes'][0]['notes'] = ['新增证据']
        await master._llm_steer(digest)
        second = master.ctx.gateway.chat.call_args.args[0]
        self.assertEqual(second[:len(first)], first)
        delta = json.loads(second[-1]['content'])['patch']
        self.assertTrue(any('/progress/tasks/t1/' in row['path'] for row in delta))
        self.assertNotIn('完整保留规则', second[-1]['content'])

    async def test_large_mcp_catalog_is_deferred_and_calls_original_registry(self) -> None:
        agent, state, task, route, plan = steering_fixture.SteeringExecutionTests()._solver()
        catalog = {f'mcp__fixture__tool_{i}': {
            'server': 'fixture', 'tool': f'tool_{i}', 'schema': {
                'description': '完整工具说明' * 2000,
                'inputSchema': {'type': 'object', 'required': ['path'],
                                'properties': {'path': {'type': 'string'}}}}} for i in range(30)}
        agent.ctx.mcp_tools = catalog
        original = copy.deepcopy(agent.tool_schemas)
        agent._merge_mcp_schemas()
        self.assertEqual(agent.tool_schemas, original)
        self.assertLess(len(json.dumps(agent.tool_schemas)), len(json.dumps(catalog)) / 10)
        result = search_mcp_tools(catalog, 'mcp__fixture__tool_17')
        self.assertEqual(result['tools'][0]['inputSchema'], catalog['mcp__fixture__tool_17']['schema']['inputSchema'])
        agent.ctx.tools = SimpleNamespace(call=AsyncMock(return_value=ToolResult(tool='fixture', ok=True, stdout='evidence')))
        out = await agent._run_tool_call(task, route, state, {
            'name': 'mcp_call', 'arguments': {'name': 'mcp__fixture__tool_17',
                                             'arguments': {'path': '/fixture'}}})
        self.assertTrue(out.ok)
        self.assertEqual(agent.ctx.tools.call.call_args.args, ('mcp__fixture__tool_17', {'path': '/fixture'}))
        catalog['mcp__late__ready'] = {'server': 'late', 'tool': 'ready', 'schema': {}}
        self.assertEqual(search_mcp_tools(catalog, 'mcp__late__ready')['total'], 1)
        self.assertEqual(agent.tool_schemas, original)
        agent.ctx.tools.call.reset_mock()
        denied = await agent._run_tool_call(task, route, state, {
            'name': 'mcp_call', 'arguments': {'name': 'mcp__other__hidden', 'arguments': {}}})
        self.assertFalse(denied.ok)
        agent.ctx.tools.call.assert_not_awaited()

    async def test_reasoning_is_preserved_across_tool_turns(self) -> None:
        agent, state, task, route, plan = steering_fixture.SteeringExecutionTests()._solver()
        response = SimpleNamespace(content='继续', reasoning='供应商要求回传的推理字段', tool_calls=[
            {'id': 'ack', 'name': 'steering_feedback', 'arguments': {
                'instruction_id': 's1', 'status': 'blocked', 'detail': '缺证据'}}])
        agent.ctx.gateway.chat.return_value = response
        await agent._solve_turn(task, route, plan)
        first = copy.deepcopy(agent.ctx.gateway.chat.call_args.args[0])
        await agent._solve_turn(task, route, plan)
        second = agent.ctx.gateway.chat.call_args.args[0]
        self.assertEqual(second[:len(first)], first)
        self.assertTrue(any(m.get('reasoning_content') == response.reasoning for m in second))
        self.assertTrue(agent.ctx.gateway.chat.call_args.kwargs['preserve_context'])

    async def test_ctf_proxy_auto_approves_original_dangerous_action(self) -> None:
        agent, state, task, route, plan = steering_fixture.SteeringExecutionTests()._solver()
        approval = AsyncMock(return_value=False)
        policy = PolicyEngine(RunMode.CTF, ScopeGuard(Scope()), approval_gate=approval)
        registry = ToolRegistry(LocalExecutor(), policy=policy)
        name = 'mcp__yakit__fixture_delete'
        execute = AsyncMock(return_value=ToolResult(tool=name, ok=True))
        registry.register_builtin(name, execute, ToolSpec(
            name=name, kind=ToolKind.MCP, risk=RiskLevel.INTRUSIVE, action_required=True,
            allowed_params={'action', 'path'}))
        agent.ctx.tools = AgentToolView(registry, 'ctf_web')
        agent.ctx.mcp_tools = {name: {'server': 'yakit', 'tool': 'fixture_delete', 'schema': {}}}
        out = await agent._run_tool_call(task, route, state, {
            'name': 'mcp_call', 'arguments': {'name': name, 'arguments': {
                'action': 'delete_files', 'path': 'fixture'}}})
        self.assertTrue(out.ok)
        approval.assert_not_awaited()
        execute.assert_awaited_once()
        self.assertEqual(execute.call_args.kwargs["action"], "delete_files")

    async def test_newly_discovered_schema_is_not_truncated_or_folded(self) -> None:
        agent, state, task, route, plan = steering_fixture.SteeringExecutionTests()._solver()
        agent.ctx.mcp_tools = {'mcp__yakit__fixture': {
            'server': 'yakit', 'tool': 'fixture', 'schema': {
                'description': '完整参数契约说明' * 10000,
                'inputSchema': {'type': 'object', 'required': ['tail_field'],
                                'properties': {'tail_field': {'type': 'string'}}}}}}
        agent.ctx.gateway.chat.return_value = SimpleNamespace(content='', tool_calls=[{
            'id': 'find', 'name': 'mcp_search', 'arguments': {'query': 'fixture'}}])
        await agent._solve_turn(task, route, plan)
        result = json.loads(state['messages'][-1]['content'])
        self.assertEqual(result['tools'][0]['inputSchema']['required'], ['tail_field'])
        await agent._solve_turn(task, route, plan)
        sent = agent.ctx.gateway.chat.call_args.args[0]
        found = next(m for m in sent if m.get('role') == 'tool' and m.get('tool_call_id') == 'find')
        self.assertEqual(json.loads(found['content']), result)
        self.assertNotIn('_context_contract', found)

    async def test_gateway_does_not_rewrite_caller_managed_history(self) -> None:
        entry = ModelEntry(model='fixture', provider='mock', context_window=2000)
        gateway = ModelGateway(ModelRegistry([entry]))
        messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'rules'},
                    {'role': 'assistant', 'content': '旧记录' * 1000},
                    {'role': 'user', 'content': 'new'}]
        original = copy.deepcopy(messages)
        backend = SimpleNamespace(complete=AsyncMock(return_value={
            'content': 'ok', 'prompt_tokens': 100, 'completion_tokens': 1}))
        from unittest.mock import patch
        with patch('spiritscout.gateway.gateway.backend_for', return_value=backend):
            await gateway.chat(messages, preserve_context=True)
        self.assertEqual(backend.complete.call_args.args[1], original)
        self.assertEqual(messages, original)

    def test_rmb_estimates_preserve_unknown_usage_and_dollar_ledger(self) -> None:
        entry = ModelEntry(model='deepseek-flash', base_url='https://api.deepseek.com')
        ts = datetime(2026, 10, 23, 0, 30, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        price = estimate_deepseek_cny(entry, ts, 1_000_000, 900_000, 100_000)
        self.assertAlmostEqual(price['estimated_cost_cny_min'], .518)
        self.assertEqual(price['estimated_cost_cny_min'], price['estimated_cost_cny_max'])
        peak = ts + 10 * 3600
        bounds = estimate_deepseek_cny(entry, peak, 1_000_000, 900_000, 100_000)
        self.assertAlmostEqual(bounds['estimated_cost_cny_max'], 1.036)
        self.assertEqual(estimate_deepseek_cny(entry, ts, 100, None, 10), {})
        unofficial = entry.model_copy(update={'base_url': 'https://proxy.fixture'})
        self.assertEqual(estimate_deepseek_cny(unofficial, ts, 100, 90, 10), {})
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(Path(tmp) / 'usage.db')
            ledger.append({'ts': ts, 'call_id': 'one', 'cost_usd': .1,
                           'context_parts': {'tools': 200}, 'cache_scope': 'collab_judge', **price})
            snap = ledger.read()
            self.assertEqual(snap['used_usd'], .1)
            self.assertEqual(snap['call_series'][0]['context_parts'], {'tools': 200})
            self.assertEqual(snap['cache']['priced_calls'], 1)
            self.assertAlmostEqual(snap['cache']['estimated_cost_cny_min'], .518)

    def test_prefix_diagnostics_measure_parts_without_claiming_cache_hits(self) -> None:
        metrics = PrefixTracker().observe(('role',), [{'role': 'user', 'content': 'private data'}], [])
        self.assertGreater(metrics['context_parts']['user'], 0)
        self.assertNotIn('private data', json.dumps(metrics))
        cache = cache_summary([{'input_tokens': 100, 'context_parts': metrics['context_parts'],
                                'reused_prefix_bytes': 999999}])
        self.assertIsNone(cache['hit_rate'])
        self.assertEqual(cache['unmeasured_calls'], 1)
