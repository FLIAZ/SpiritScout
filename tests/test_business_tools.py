"""业务走访工具契约、错误回灌和页面证据可信性回归。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.business.agent import BusinessContextAgent
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, Task
from spiritscout.tools.spec import ToolResult


class BusinessToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(BusinessContextAgent)
        self.agent.agent_id = "business-test"
        self.agent._explore = {}
        self.agent.ctx = SimpleNamespace(audit=NullAuditLogger(), builtin_ctx={},
            tools=SimpleNamespace(call=AsyncMock()), convergence=Mock(), mcp_tools={
                "mcp__playwright__browser_navigate": {"tool": "browser_navigate", "schema": {
                    "name": "browser_navigate", "description": "Navigate to a URL",
                    "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}},
                                    "required": ["url"]},
                    "annotations": {"destructiveHint": True}}},
                "mcp__playwright__browser_tabs": {"tool": "browser_tabs", "schema": {
                    "description": "Manage tabs", "inputSchema": {"type": "object", "properties": {
                        "action": {"type": "string", "enum": ["list", "new", "close", "select"]}},
                        "required": ["action"]}}}})
        self.task = Task(task_id="business-task", task_type="business_context",
                         agent_type=AgentType.BUSINESS_CONTEXT, target="https://example.test")

    def test_mcp_catalog_exposes_input_schema_and_description(self) -> None:
        schemas = {s["function"]["name"]: s["function"] for s in self.agent._tool_schemas()}
        nav = schemas["mcp__playwright__browser_navigate"]
        self.assertEqual(nav["parameters"]["required"], ["url"])
        self.assertEqual(nav["parameters"]["properties"]["url"], {"type": "string"})
        self.assertNotIn("inputSchema", nav["parameters"])
        self.assertNotIn("annotations", nav["parameters"])
        self.assertEqual(nav["description"], "Navigate to a URL")
        self.assertEqual(schemas["mcp__playwright__browser_tabs"]["parameters"]["properties"]["action"]["enum"][0], "list")

    def test_ask_user_is_exposed_to_explorer_tool_loop(self) -> None:
        schemas = {s["function"]["name"]: s["function"] for s in self.agent._tool_schemas()}
        self.assertIn("ask_user", schemas)
        self.assertEqual(schemas["ask_user"]["parameters"]["required"], ["question"])

    async def test_ask_user_answer_and_scope_flow_back_to_agent(self) -> None:
        broker = SimpleNamespace(ask=AsyncMock(return_value={
            "answer": "允许自行注册测试账号", "scope_added": ["hg-v1.imou.com"]}))
        self.agent.ctx.questions = broker
        text = await self.agent._run_tool_call(self.task, {
            "id": "ask-1", "name": "ask_user",
            "arguments": {"question": "请提供手机号与收到的验证码", "options": ["稍后提供"]}})
        self.assertIn("允许自行注册测试账号", text)
        self.assertIn("hg-v1.imou.com", text)
        broker.ask.assert_awaited_once()
        self.assertEqual(broker.ask.await_args.args[0], "business_context")
        self.assertIn("手机号", broker.ask.await_args.args[1])

    async def test_remote_error_stdout_reaches_agent_and_failed_page_is_not_evidence(self) -> None:
        self.agent.ctx.tools.call.return_value = ToolResult(tool="browser_navigate", ok=False,
            stdout='Invalid arguments for tool "browser_navigate": url is required', returncode=1,
            invocation_id="inv-failed")
        text = await self.agent._run_tool_call(self.task, {"id": "call-1",
            "name": "mcp__playwright__browser_navigate", "arguments": {"url": self.task.target}})
        self.assertIn("url is required", text)
        self.assertNotIn("未知错误", text)
        self.agent.ctx.convergence.merge_asset.assert_not_called()
        event = self.agent.ctx.audit.records[-1]["detail"]
        self.assertEqual(event["invocation_id"], "inv-failed")
        self.assertEqual(event["tool"], "mcp__playwright__browser_navigate")

    async def test_successful_navigation_records_page_once(self) -> None:
        self.agent.ctx.tools.call.return_value = ToolResult(tool="browser_navigate", ok=True, stdout="Page Title: local")
        call = {"id": "call-2", "name": "mcp__playwright__browser_navigate", "arguments": {"url": self.task.target}}
        await self.agent._run_tool_call(self.task, call)
        await self.agent._run_tool_call(self.task, call)
        self.agent.ctx.convergence.merge_asset.assert_called_once()

    async def test_browser_cause_reaches_short_audit_preview(self) -> None:
        summary = "目标元素已找到，但位于可视区域外；自动滚动后仍无法点击。"
        self.agent.ctx.tools.call.return_value = ToolResult(tool="browser_click", ok=False,
            error=summary + "\n" + "waiting for locator\n" * 40,
            parsed={"failure_kind": "target_outside_viewport", "failure_summary": summary})
        text = await self.agent._run_tool_call(self.task, {
            "id": "click", "name": "mcp__playwright__browser_click", "arguments": {"target": "e247"}})
        self.assertIn(summary, text[:300])
        self.assertEqual(self.agent.ctx.audit.records[-1]["detail"]["failure_summary"], summary)

    async def test_malformed_json_never_calls_tool(self) -> None:
        text = await self.agent._run_tool_call(self.task, {"id": "bad",
            "name": "mcp__playwright__browser_navigate", "parse_error": "invalid JSON", "arguments": {}})
        self.assertIn("参数解析失败", text)
        self.agent.ctx.tools.call.assert_not_awaited()

    def test_failure_reason_uses_stderr_or_exit_code_when_text_is_absent(self) -> None:
        self.assertIn("connection refused", ToolResult(tool="x", ok=False, stderr="connection refused").failure_reason())
        self.assertIn("退出码 7", ToolResult(tool="x", ok=False, returncode=7).failure_reason())
        self.assertIn("超时", ToolResult(tool="x", ok=False, timed_out=True).failure_reason())
