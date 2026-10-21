"""MCP 环境变更提示不等同于 DoS；可信单工具配置不扩大审批许可。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from spiritscout.config.settings import (
    McpServerConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.models import Authorization, RunMode, Scope
from spiritscout.runtime import _build_infra
from spiritscout.tools.mcp_client import resolve_mcp_risk
from spiritscout.tools.spec import RiskLevel


class McpRiskTests(unittest.TestCase):
    def test_destructive_hint_requires_approval_instead_of_permanent_rejection(self) -> None:
        server = McpServerConfig(name="browser", default_risk="readonly")
        self.assertEqual(resolve_mcp_risk(server, {"name": "browser_click", "annotations": {
            "readOnlyHint": False, "destructiveHint": True}}), RiskLevel.INTRUSIVE)

    def test_readonly_hint_makes_destructive_hint_irrelevant_as_defined_by_sdk(self) -> None:
        server = McpServerConfig(name="browser")
        for hints in ({"readOnlyHint": True, "destructiveHint": True},
                            {"read_only_hint": True, "destructive_hint": True}):
            self.assertEqual(resolve_mcp_risk(server, {"name": "snapshot", "annotations": hints}),
                             RiskLevel.READONLY)

    def test_exact_navigation_override_does_not_allow_other_browser_actions(self) -> None:
        server = McpServerConfig(name="browser", tool_risk_overrides={"browser_navigate": "readonly"})
        annotations = {"readOnlyHint": False, "destructiveHint": True}
        self.assertEqual(resolve_mcp_risk(server, {"name": "browser_navigate", "annotations": annotations}),
                         RiskLevel.READONLY)
        self.assertEqual(resolve_mcp_risk(server, {"name": "browser_run_code", "annotations": annotations}),
                         RiskLevel.INTRUSIVE)

    def test_explicit_host_destructive_classification_stays_forbidden(self) -> None:
        server = McpServerConfig(name="host", default_risk="destructive")
        self.assertEqual(resolve_mcp_risk(server, {"name": "tool", "annotations": {"readOnlyHint": True}}),
                         RiskLevel.DESTRUCTIVE)
        server = McpServerConfig(name="host", tool_risk_overrides={"tool": "destructive"})
        self.assertEqual(resolve_mcp_risk(server, {"name": "tool", "annotations": {"readOnlyHint": True}}),
                         RiskLevel.DESTRUCTIVE)

    def test_unknown_hint_uses_host_default_and_invalid_override_is_rejected(self) -> None:
        self.assertEqual(resolve_mcp_risk(McpServerConfig(name="host"), {"name": "unknown"}),
                         RiskLevel.INTRUSIVE)
        with self.assertRaises(ValidationError):
            McpServerConfig(name="host", tool_risk_overrides={"browser_navigate": "allow_all"})


class McpAssemblyRiskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.gate = AsyncMock(return_value=False)
        server = McpServerConfig(name="playwright", default_risk="intrusive",
            tool_risk_overrides={"browser_navigate": "readonly", "host_forbidden": "destructive"})
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)),
                           audit_log_path=str(root / "audit.jsonl"), mcp_servers=[server])
        cfg.blackboard.sqlite_path = str(root / "bb.db")
        cfg.knowledge.sqlite_path = str(root / "kb.db")
        props = {"url": {"type": "string"}, "action": {"type": "string"}}
        listed = [{"name": name, "annotations": {"readOnlyHint": False, "destructiveHint": True},
                   "inputSchema": {"type": "object", "properties": props}}
                  for name in ("browser_navigate", "browser_click", "host_forbidden")]
        self.client = SimpleNamespace(start=AsyncMock(return_value=listed), close=AsyncMock(),
                                       call=AsyncMock(return_value={"text": "returned", "is_error": False}))
        auth = Authorization(auth_id="audit", mode=RunMode.SRC, human_confirmed=True,
                             scope=Scope(allowed_urls=["https://allowed.invalid"], denied=["https://denied.invalid"]))
        with patch("spiritscout.tools.mcp_client.build_client", return_value=self.client):
            self.infra = await _build_infra(cfg, [ModelEntry(model="test", provider="mock")],
                                            auth, approval_gate=self.gate)
        self.addAsyncCleanup(self.infra.close)

    async def test_navigation_reaches_mcp_without_destructive_error(self) -> None:
        result = await self.infra.tools.call("mcp__playwright__browser_navigate",
                                            {"url": "https://allowed.invalid"}, **self.infra.builtin_ctx)
        self.assertTrue(result.ok, result.error)
        self.gate.assert_not_awaited()
        self.client.call.assert_awaited_once()

    async def test_navigation_legacy_denied_target_reaches_mcp(self) -> None:
        result = await self.infra.tools.call("mcp__playwright__browser_navigate",
                                            {"url": "https://denied.invalid"}, **self.infra.builtin_ctx)
        self.assertTrue(result.ok, result.error)
        self.client.call.assert_awaited_once()

    async def test_mcp_error_text_is_preserved_without_identical_retry(self) -> None:
        self.client.call.return_value = {"text": "Invalid arguments: url is required", "is_error": True}
        result = await self.infra.tools.call("mcp__playwright__browser_navigate", {},
                                            **self.infra.builtin_ctx)
        self.assertFalse(result.ok)
        self.assertIn("url is required", result.error)
        self.assertEqual(result.attempts, 1)
        self.client.call.assert_awaited_once()

    async def test_empty_mcp_error_still_has_tool_diagnostic(self) -> None:
        self.client.call.return_value = {"text": "", "is_error": True}
        result = await self.infra.tools.call("mcp__playwright__browser_navigate", {},
                                            **self.infra.builtin_ctx)
        self.assertIn("browser_navigate", result.error)
        self.assertIn("isError=true", result.error)

    async def test_invasive_mcp_tool_requires_declaration_and_gates_only_dangerous_actions(self) -> None:
        # 缺声明：不执行、不打扰用户
        result = await self.infra.tools.call("mcp__playwright__browser_click",
                                            {"url": "https://allowed.invalid"}, **self.infra.builtin_ctx)
        self.assertFalse(result.ok)
        self.assertIn("_safety_action", result.error)
        self.gate.assert_not_awaited()
        self.client.call.assert_not_awaited()
        # 普通写入类声明：如实注明即放行，不打断
        result = await self.infra.tools.call(
            "mcp__playwright__browser_click",
            {"url": "https://allowed.invalid", "_safety_action": "intrusive_write"},
            **self.infra.builtin_ctx)
        self.assertTrue(result.ok)
        self.gate.assert_not_awaited()
        self.client.call.assert_awaited_once()
        # 危险语义声明：逐次即时请示
        result = await self.infra.tools.call(
            "mcp__playwright__browser_click",
            {"url": "https://allowed.invalid", "_safety_action": "delete_files"},
            **self.infra.builtin_ctx)
        self.assertFalse(result.ok)
        self.gate.assert_awaited_once()
        self.client.call.assert_awaited_once()
        self.gate.return_value = True
        result = await self.infra.tools.call(
            "mcp__playwright__browser_click",
            {"url": "https://allowed.invalid", "_safety_action": "delete_files"},
            **self.infra.builtin_ctx)
        self.assertTrue(result.ok)
        self.assertEqual(self.gate.await_count, 2)
        self.assertEqual(self.client.call.await_count, 2)

    async def test_readonly_navigation_override_cannot_approve_sensitive_declared_action(self) -> None:
        result = await self.infra.tools.call("mcp__playwright__browser_navigate",
            {"url": "https://allowed.invalid", "action": "delete_files"}, **self.infra.builtin_ctx)
        self.assertFalse(result.ok)
        self.gate.assert_awaited_once()
        self.client.call.assert_not_awaited()

    async def test_explicitly_forbidden_tool_stays_forbidden_even_with_approving_gate(self) -> None:
        self.gate.return_value = True
        result = await self.infra.tools.call("mcp__playwright__host_forbidden",
                                            {"url": "https://allowed.invalid"}, **self.infra.builtin_ctx)
        self.assertFalse(result.ok)
        self.gate.assert_not_awaited()
        self.client.call.assert_not_awaited()
