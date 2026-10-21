"""浏览器按本次实际行为审批，原生 action 与安全声明互不覆盖。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.config.settings import (
    McpServerConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.models import Authorization, RunMode, Scope
from spiritscout.policy.engine import DANGEROUS_ACTIONS
from spiritscout.runtime import _build_infra
from spiritscout.tools.spec import ActionCategory


class BrowserActionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        props = {"url": {"type": "string"}, "target": {"type": "string"},
                 "function": {"type": "string"}, "action": {"type": "string", "enum": ["list", "new", "close"]}}
        tools = [{"name": name, "inputSchema": {"type": "object", "properties": props},
                  "annotations": {"readOnlyHint": False, "destructiveHint": True}}
                 for name in ("browser_navigate", "browser_click", "browser_tabs", "browser_evaluate", "forbidden")]
        self.client = SimpleNamespace(start=AsyncMock(return_value=tools), close=AsyncMock(),
            call=AsyncMock(return_value={"text": "ok", "is_error": False}))
        self.gate = AsyncMock(return_value=False)
        spec = McpServerConfig(name="playwright", default_risk="readonly", risk_by_action=True,
                               tool_risk_overrides={"forbidden": "destructive"})
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)),
                           audit_log_path=str(root / "audit.jsonl"), mcp_servers=[spec])
        cfg.blackboard.sqlite_path = str(root / "bb.db")
        cfg.knowledge.sqlite_path = str(root / "kb.db")
        auth = Authorization(auth_id="test-browser", mode=RunMode.SRC, human_confirmed=True,
                             scope=Scope(allowed_domains=["allowed.test"], denied=["https://denied.test"]))
        with patch("spiritscout.tools.mcp_client.build_client", return_value=self.client):
            self.infra = await _build_infra(cfg, [ModelEntry(model="test", provider="mock")], auth,
                                            approval_gate=self.gate)
        self.addAsyncCleanup(self.infra.close)

    async def call(self, name: str, args: dict):
        return await self.infra.tools.call("mcp__playwright__" + name, args, **self.infra.builtin_ctx)

    async def test_normal_click_and_native_tab_action_require_no_human_approval(self) -> None:
        self.assertTrue((await self.call("browser_click", {"target": "e247", "_safety_action": "analysis"})).ok)
        self.assertTrue((await self.call("browser_tabs", {"action": "list", "_safety_action": "analysis"})).ok)
        self.assertEqual(self.client.call.await_args.args[1], {"action": "list"})
        self.gate.assert_not_awaited()

    async def test_dangerous_actions_require_approval_but_ordinary_writes_do_not(self) -> None:
        # 只有危险语义逐次审批：普通浏览与 intrusive_write 如实声明即放行
        for action in ActionCategory:
            result = await self.call("browser_evaluate", {
                "function": "() => 1", "_safety_action": action.value})
            self.assertEqual(result.ok, action.value not in DANGEROUS_ACTIONS,
                             f"类别 {action.value} 的放行判定不符")
        self.assertEqual(self.gate.await_count, len(DANGEROUS_ACTIONS))
        self.gate.return_value = True
        self.assertTrue((await self.call("browser_evaluate", {"function": "() => 1", "_safety_action": "delete_files"})).ok)
        self.gate.return_value = False
        self.assertFalse((await self.call("browser_evaluate", {"function": "() => 1", "_safety_action": "delete_files"})).ok)
        self.assertEqual(self.client.call.await_count, 3)

    async def test_missing_or_invalid_classification_is_not_assumed_safe(self) -> None:
        for args in ({"action": "list"}, {"_safety_action": "unknown"}):
            self.assertFalse((await self.call("browser_tabs", args)).ok)
        self.gate.assert_not_awaited()
        self.client.call.assert_not_awaited()

    async def test_legacy_denied_target_executes_but_forbidden_tool_stays_blocked(self) -> None:
        self.assertTrue((await self.call("browser_navigate", {"url": "https://denied.test", "_safety_action": "analysis"})).ok)
        self.assertFalse((await self.call("forbidden", {"_safety_action": "analysis"})).ok)
        self.gate.assert_not_awaited()
        self.client.call.assert_awaited_once()

    async def test_failed_stateful_action_is_never_automatically_replayed(self) -> None:
        self.client.call.side_effect = TimeoutError("transport interrupted")
        result = await self.call("browser_click", {"target": "e247", "_safety_action": "analysis"})
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 1)
        self.client.call.assert_awaited_once()

    def test_model_schema_requires_separate_safety_declaration(self) -> None:
        schema = self.infra.mcp_tools["mcp__playwright__browser_tabs"]["schema"]["inputSchema"]
        self.assertIn("_safety_action", schema["required"])
        self.assertIn("analysis", schema["properties"]["_safety_action"]["enum"])
        self.assertEqual(schema["properties"]["action"]["enum"], ["list", "new", "close"])
