from __future__ import annotations

import unittest

from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.models import AgentType, RunMode, Scope
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.executor import ToolExecutor
from spiritscout.tools.registry import AgentToolView, ToolRegistry
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec


class _Executor(ToolExecutor):
    async def run(self, spec: ToolSpec, params: dict) -> ToolResult:
        return ToolResult(tool=spec.name, ok=True)


class CommonSrcToolsTests(unittest.IsolatedAsyncioTestCase):
    def test_hybrid_scope_tracks_candidate_and_asset_level_exclusion(self) -> None:
        guard = ScopeGuard(Scope(
            scope_mode="hybrid", organization_domains=["*.example.test"],
            asset_rules=[{"pattern": "admin.example.test", "status": "blocked", "reason": "管理面排除"}],
        ))
        self.assertEqual(guard.asset_decision("api.example.test")["status"], "candidate_only")
        with self.assertRaises(ScopeViolation):
            guard.check_execution_target("api.example.test")
        guard.register_asset_decision("api.example.test", "allowed_test", reason="归属已核验")
        self.assertEqual(guard.check_execution_target("api.example.test"), "api.example.test")
        self.assertEqual(guard.asset_decision("admin.example.test")["status"], "blocked")

    def test_src_roles_share_common_tool_directory(self) -> None:
        registry = ToolRegistry(_Executor(), policy=PolicyEngine(
            RunMode.SRC, ScopeGuard(Scope(allowed_domains=["example.test"]))))
        registry.register(ToolSpec(name="http_probe", kind=ToolKind.CLI))
        registry.register(ToolSpec(name="sandbox_exec", kind=ToolKind.CLI))
        for role in (AgentType.RECON, AgentType.JS_REVERSE, AgentType.BUSINESS_CONTEXT,
                     AgentType.WEB_VULN, AgentType.VERIFIER):
            view = AgentToolView(registry, role)
            self.assertIn("http_probe", view.names())
            self.assertIn("sandbox_exec", view.names())

    async def test_required_parameters_fail_as_repairable_tool_result(self) -> None:
        registry = ToolRegistry(_Executor())
        registry.register(ToolSpec(name="http_probe", kind=ToolKind.BUILTIN,
                                   required_params=("url",)))
        result = await registry.call("http_probe", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.parsed["missing"], ["url"])
        self.assertIn("请按工具目录补充", result.error)
