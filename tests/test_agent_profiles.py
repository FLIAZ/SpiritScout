from __future__ import annotations

import unittest
from types import SimpleNamespace

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.profiles import PROFILE_BY_TYPE, get_profile
from spiritscout.agents.sub.base import SubAgentContext
from spiritscout.core.models import AgentType
from spiritscout.tools.executor import ToolExecutor
from spiritscout.tools.registry import AgentToolView, ToolRegistry
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec


class _Executor(ToolExecutor):
    async def run(self, spec: ToolSpec, params: dict) -> ToolResult:
        return ToolResult(tool=spec.name, ok=True)


class AgentProfileTests(unittest.IsolatedAsyncioTestCase):
    def test_every_agent_type_has_an_explicit_profile(self) -> None:
        self.assertEqual(set(AgentType), set(PROFILE_BY_TYPE))
        # SRC/渗透链路角色全部可用可指派（听风/贯甲/深耕已实装）
        self.assertTrue(get_profile(AgentType.PORT_SCAN).configurable)
        self.assertEqual(get_profile(AgentType.PORT_SCAN).status, "available")
        self.assertTrue(get_profile(AgentType.HOST_VULN).configurable)
        self.assertTrue(get_profile(AgentType.POST_EXPLOIT).configurable)

    async def test_runtime_tool_view_denies_cross_role_tool(self) -> None:
        registry = ToolRegistry(_Executor())
        registry.register(ToolSpec(name="http_probe", kind=ToolKind.CLI))
        registry.register(ToolSpec(name="sandbox_exec", kind=ToolKind.CLI))
        view = AgentToolView(registry, AgentType.RECON)

        allowed = await view.call("http_probe", {})
        denied = await view.call("sandbox_exec", {})

        self.assertTrue(allowed.ok)
        self.assertFalse(denied.ok)
        self.assertEqual(denied.returncode, -5)
        self.assertEqual(view.names(), ["http_probe"])

    def test_context_filters_mcp_tools_by_specialty(self) -> None:
        ctx = SubAgentContext(
            bus=SimpleNamespace(), blackboard=SimpleNamespace(),
            convergence=SimpleNamespace(), gateway=SimpleNamespace(),
            tools=ToolRegistry(_Executor()), policy=SimpleNamespace(),
            mcp_tools={
                "mcp__ida__decompile": {},
                "mcp__yakit__scan": {},
            },
        )

        pwn = ctx.for_agent(AgentType.CTF_PWN.value)
        web = ctx.for_agent(AgentType.CTF_WEB.value)

        self.assertEqual(set(pwn.mcp_tools), {"mcp__ida__decompile"})
        self.assertEqual(set(web.mcp_tools), {"mcp__yakit__scan"})

    def test_master_forces_business_context_before_other_src_nodes(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(
            agents={AgentType.BUSINESS_CONTEXT: object()},
            tactic_map=None,
        )

        nodes = master._ensure_business_gate([
            {"tactic": "TA0043", "title": "recon", "depends_on": []},
            {"tactic": "TA0001", "title": "web", "depends_on": ["TA0043"]},
        ])

        self.assertEqual(nodes[0]["tactic"], "BUSINESS-CONTEXT")
        self.assertIn("BUSINESS-CONTEXT", nodes[1]["depends_on"])
        self.assertIn("BUSINESS-CONTEXT", nodes[2]["depends_on"])


if __name__ == "__main__":
    unittest.main()
