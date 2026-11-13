"""听风端口测绘必须由现场证据驱动，不能回退到固定常用端口表。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.hostops.agent import PortScanAgent, PostExploitAgent
from spiritscout.core.models import AgentType, Task
from spiritscout.tools.spec import ToolResult


class _Blackboard:
    def __init__(self, assets=None):
        self.assets = assets or []
        self.writes = []

    def query(self, layer, limit=0):
        return list(self.assets)

    def write(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        return 1


class PortScanPlanningTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def task(target: str) -> Task:
        return Task(
            task_id="port-task", task_type="port_scan", target=target,
            agent_type=AgentType.PORT_SCAN,
            payload={"mission": "根据现场证据测绘服务"},
        )

    @staticmethod
    def agent(assets=None) -> PortScanAgent:
        agent = object.__new__(PortScanAgent)
        agent.ctx = SimpleNamespace(
            blackboard=_Blackboard(assets),
            tools=SimpleNamespace(call=AsyncMock()),
            builtin_ctx={},
            convergence=Mock(),
        )
        agent.cache = Mock()
        agent.agent_id = "port-scan-test"
        agent._llm_chat_json = AsyncMock(return_value={})
        return agent

    async def test_explicit_url_port_is_the_only_fallback_probe(self):
        agent = self.agent()

        plans = await agent.plan_routes(self.task("https://example.test:8443/login"))

        self.assertEqual(plans[0].steps[0]["ports"], [8443])
        self.assertIn("任务目标显式指定端口", plans[0].steps[0]["basis"]["8443"][0])

    async def test_existing_service_observation_drives_port_choice(self):
        assets = [{"data": {"asset_type": "service", "value": "example.test:6379",
                             "port": 6379, "attrs": {"banner": "Redis"}}}]
        agent = self.agent(assets)

        plans = await agent.plan_routes(self.task("example.test"))

        self.assertEqual(plans[0].steps[0]["ports"], [6379])
        self.assertIn("黑板已有 service 观察", plans[0].steps[0]["basis"]["6379"][0])

    async def test_no_evidence_finishes_without_calling_tcp_scan(self):
        agent = self.agent()
        agent._llm_chat_json = AsyncMock(return_value={
            "ports": [], "reason": "没有显式端口或服务线索",
        })
        task = self.task("example.test")

        plans = await agent.plan_routes(task)
        result = await agent.execute_step(task, None, plans[0], plans[0].steps[0])

        self.assertTrue(result.ok)
        self.assertIn("未执行端口扫描", result.note)
        agent.ctx.tools.call.assert_not_awaited()

    async def test_tcp_scan_always_receives_validated_explicit_ports(self):
        agent = self.agent()
        agent.ctx.tools.call.return_value = ToolResult(
            tool="tcp_scan", ok=True,
            parsed={"open": [], "scanned": 1},
        )
        task = self.task("example.test")

        await agent._step_tcp_sweep(task, {
            "host": "example.test", "ports": [443, 0, 70000, "80"],
        })

        self.assertEqual(agent.ctx.tools.call.await_args.args,
                         ("tcp_scan", {"host": "example.test", "ports": [443]}))

    def test_invalid_or_unexplained_model_ports_are_rejected(self):
        selected = PortScanAgent._validated_model_ports([
            {"port": 443, "basis": []},
            {"port": 6379, "basis": ["响应横幅明确为 Redis"]},
            {"port": 70000, "basis": ["越界"]},
            {"port": "22", "basis": ["类型错误"]},
        ])

        self.assertEqual(selected, {6379: ["响应横幅明确为 Redis"]})


class PostExploitPortTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def agent(assets=None) -> PostExploitAgent:
        agent = object.__new__(PostExploitAgent)
        agent.agent_id = "post-exploit-test"
        agent.agent_type = AgentType.POST_EXPLOIT
        agent.ctx = SimpleNamespace(
            blackboard=_Blackboard(assets),
            tools=SimpleNamespace(call=AsyncMock()),
            builtin_ctx={},
            convergence=Mock(),
        )
        return agent

    @staticmethod
    def task(target: str = "example.test") -> Task:
        return Task(task_id="post-task", task_type="post_exploit", target=target,
                    agent_type=AgentType.POST_EXPLOIT)

    async def test_lateral_probe_without_port_evidence_sends_no_scan(self):
        agent = self.agent()

        result = await agent._step_lateral(self.task(), {"host": "peer.example.test"})

        self.assertTrue(result.ok)
        self.assertTrue(result.final_note)
        agent.ctx.tools.call.assert_not_awaited()

    async def test_lateral_probe_uses_only_observed_peer_ports(self):
        assets = [{"data": {"asset_type": "service", "value": "peer.example.test:6379",
                             "port": 6379, "attrs": {"banner": "Redis"}}}]
        agent = self.agent(assets)
        agent.ctx.tools.call.return_value = ToolResult(
            tool="tcp_scan", ok=True, parsed={"open": [], "scanned": 1})

        await agent._step_lateral(self.task(), {"host": "peer.example.test"})

        self.assertEqual(agent.ctx.tools.call.await_args.args,
                         ("tcp_scan", {"host": "peer.example.test", "ports": [6379]}))


if __name__ == "__main__":
    unittest.main()
