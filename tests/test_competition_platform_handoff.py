"""平台供给回执、旧列题状态与专项执行者上下文必须保持一致。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.sub.base import RoutePlan
from spiritscout.blackboard import Blackboard
from spiritscout.core.models import AgentType, Task
from spiritscout.ctf.platform import PlatformAdapter, parse_platform_profile
from spiritscout.tools.spec import ToolResult
from tests import test_project_injection
from tests.test_platform_adapter import PROFILE


class PlatformHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_receipt_supersedes_stopped_snapshot_without_claiming_ready(self) -> None:
        calls: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json={'container_addr': '127.0.0.1:8080'})

        adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(adapter.aclose)
        original = {'id': 'c1', 'container_status': 'stopped', 'description': '完整题面'}
        started = await adapter.ensure_instance(original)
        self.assertEqual(original['container_status'], 'stopped')
        self.assertNotIn('container_status', started)
        receipt = started['instance_provisioning']
        self.assertTrue(receipt['start_accepted'])
        self.assertEqual(receipt['listing_snapshot']['container_status'], 'stopped')
        self.assertEqual(receipt['service_readiness'], 'not_checked')
        self.assertEqual(receipt['entry_url'], started['url'])
        cached = await adapter.ensure_instance(original)
        self.assertEqual(cached, started)
        self.assertEqual(len(calls), 1)
        await adapter.close_instance('c1')
        self.assertNotIn('c1', adapter._instance_receipts)

    async def test_session_receipts_survive_rolling_fact_window(self) -> None:
        adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)
        adapter._record('precheck', ok=True, status=200, detail='不应复制到执行者上下文')
        adapter._record('list_refreshed', count=63)
        for i in range(50):
            adapter._record('instance_started', challenge=str(i))
        self.assertFalse(any(f['action'] == 'precheck' for f in adapter.facts))
        adapter._listed['c1'] = {'id': 'c1'}
        handoff = MasterAgent._platform_handoff(adapter, {'id': 'c1', 'url': 'http://fixture.test'})
        self.assertTrue(handoff['completed_operations']['precheck']['ok'])
        self.assertEqual(handoff['completed_operations']['list_refreshed']['count'], 63)
        self.assertNotIn('detail', handoff['completed_operations']['precheck'])

    async def test_failed_initial_http_request_is_not_reported_as_stopped_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            agent = test_project_injection.CTFProjectMemorySeedTests()._agent(bb)
            agent.ctx.ring = None
            agent.ctx.tools = SimpleNamespace(call=AsyncMock(return_value=ToolResult(
                tool='http_probe', ok=False, error='HTTP 错误: All connection attempts failed')))
            task = Task(task_id='t1', task_type='ctf_solve', target='http://fixture.test',
                        agent_type=AgentType.CTF_WEB)
            state = {'seed': []}
            _, note = await agent._inv_bootstrap(task, RoutePlan(name='solve', strategy=''), state)
            self.assertIn('All connection attempts failed', state['seed'][0])
            self.assertIn('一次请求', state['seed'][0])
            self.assertNotIn('入口页不可达', note)
            self.assertIn('未证明容器停止', note)

    async def test_handoff_and_original_rules_survive_large_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(bb.close)
            agent = test_project_injection.CTFProjectMemorySeedTests()._agent(bb)
            handoff = {'coordinator': 'master', 'challenge_id': 'c1',
                       'completed_operations': {'precheck': {'ok': True, 'status': 200}},
                       'instance_provisioning': {'start_accepted': True,
                                                'service_readiness': 'not_checked'}}
            rules = '全队原始约定：必须首先预检 VPN，然后列题和启动容器。' * 1000
            task = Task(task_id='t1', task_type='ctf_solve', target='http://fixture.test',
                        agent_type=AgentType.CTF_WEB,
                        payload={'platform_handoff': handoff, 'rules': rules})
            state = {'seed': ['观察' * 40000]}
            plan = RoutePlan(name='solve', strategy='')
            agent._seed_messages(task, plan, plan, state)
            content = state['messages'][1]['content']
            self.assertIn(rules, content)
            self.assertIn(json.dumps(handoff, ensure_ascii=False), content)
            self.assertIn('全队分工执行', content)
            self.assertIn('submit_flag', content)
            self.assertIn('不能用来推翻启动回执', content)
