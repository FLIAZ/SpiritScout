"""可选真实 MCP + Chromium 导航回归，只访问本机随机端口。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from spiritscout.agents.business.agent import BusinessContextAgent
from spiritscout.agents.sub.base import SubAgentContext
from spiritscout.config.settings import (
    McpServerConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.models import (
    AgentType,
    Authorization,
    BlackboardLayer,
    RunMode,
    Scope,
    Task,
)
from spiritscout.runtime import _build_infra

_MCP_CLI = os.environ.get("SPIRITSCOUT_TEST_MCP_CLI")
_BROWSER = os.environ.get("SPIRITSCOUT_TEST_BROWSER")


@unittest.skipUnless(_MCP_CLI and _BROWSER, "需要隔离 Playwright MCP CLI 与 Chromium 路径")
class LiveNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_navigation_through_registry_and_policy_then_deny_without_request(self) -> None:
        requests: list[str] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                requests.append(request.split(b" ", 2)[1].decode())
                page = (b"<!doctype html><title>SpiritScout navigation regression</title>"
                        b"<link rel='icon' href='data:,'><h1>Local MCP navigation works</h1>")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                             + str(len(page)).encode() + b"\r\nConnection: close\r\n\r\n" + page)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                spec = McpServerConfig(name="playwright", command=shutil.which("node") or "node",
                    args=[str(_MCP_CLI), "--headless", "--isolated", "--executable-path", str(_BROWSER),
                          "--output-dir", str(root / "browser-output")],
                    default_risk="intrusive", tool_risk_overrides={"browser_navigate": "readonly"})
                cfg = SystemConfig(workspace=WorkspaceConfig(root=temporary),
                    audit_log_path=str(root / "audit.jsonl"), mcp_servers=[spec])
                cfg.blackboard.sqlite_path = str(root / "bb.db")
                cfg.knowledge.sqlite_path = str(root / "kb.db")
                gate = AsyncMock(return_value=False)
                auth = Authorization(auth_id="local-audit", mode=RunMode.SRC, human_confirmed=True,
                    scope=Scope(allowed_urls=[base], denied=[base + "/blocked"]))
                infra = await _build_infra(cfg, [ModelEntry(model="test", provider="mock")],
                                            auth, approval_gate=gate)
                try:
                    name = "mcp__playwright__browser_navigate"
                    self.assertIn(name, infra.tools.names(), "真实 MCP 握手/工具发现失败")
                    result = await infra.tools.call(name, {"url": base + "/probe"}, **infra.builtin_ctx)
                    self.assertTrue(result.ok, result.error + result.stdout)
                    self.assertIn("Page Title: SpiritScout navigation regression", result.stdout)
                    self.assertIn("/probe", requests)
                    denied = await infra.tools.call(name, {"url": base + "/blocked"}, **infra.builtin_ctx)
                    self.assertFalse(denied.ok)
                    self.assertNotIn("/blocked", requests)
                    gate.assert_not_awaited()

                    # 使用生产业务 Agent 工具环复验：旧测试只覆盖了 Registry，
                    # 会漏掉模型收到的 parameters 被错误包在整份描述中的问题。
                    responses = 0

                    async def decide(messages, **kwargs):
                        nonlocal responses
                        catalog = {t["function"]["name"]: t["function"] for t in kwargs["tools"]}
                        parameters = catalog[name]["parameters"]
                        self.assertEqual(parameters["type"], "object")
                        self.assertIn("url", parameters["required"])
                        self.assertNotIn("inputSchema", parameters)
                        if responses:
                            self.assertIn("url", messages[-1]["content"])
                            self.assertIn("Invalid arguments", messages[-1]["content"])
                            self.assertNotIn("未知错误", messages[-1]["content"])
                        arguments = {} if responses == 0 else {"url": base + "/business"}
                        responses += 1
                        return SimpleNamespace(content="", tool_calls=[{
                            "id": f"call-{responses}", "name": name, "arguments": arguments,
                            "raw": json.dumps(arguments)}])

                    agent = BusinessContextAgent(SubAgentContext(
                        bus=infra.bus, blackboard=infra.blackboard, convergence=infra.convergence,
                        gateway=SimpleNamespace(chat=AsyncMock(side_effect=decide)),
                        tools=infra.tools, policy=infra.policy, audit=infra.audit,
                        builtin_ctx=infra.builtin_ctx, mcp_tools=infra.mcp_tools))
                    task = Task(task_id="business-local", task_type="business_context",
                                agent_type=AgentType.BUSINESS_CONTEXT, target=base)
                    plan = agent.route_templates(task)[0]
                    await agent._explore_turn(task, None, plan)
                    self.assertNotIn("/business", requests)
                    self.assertEqual(infra.blackboard.count(BlackboardLayer.ASSET), 0)
                    failed = infra.audit.read_all()[-1]["detail"]
                    self.assertEqual(failed["attempts"], 1)
                    self.assertIn("url", failed["note"])
                    await agent._explore_turn(task, None, plan)
                    self.assertIn("/business", requests)
                    self.assertEqual(infra.blackboard.count(BlackboardLayer.ASSET), 1)
                    self.assertIn("Page Title: SpiritScout navigation regression",
                                  agent._explore[task.task_id]["messages"][-1]["content"])
                    gate.assert_not_awaited()
                finally:
                    await infra.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_offscreen_carousel_click_reports_cause_then_visible_click_opens_tab(self) -> None:
        requests: list[str] = []
        page = b'''<!doctype html><title>Local carousel</title><link rel="icon" href="data:,">
        <div style="position:relative;overflow:hidden;width:240px;height:80px">
          <a id="offscreen" href="/landed" target="_blank"
             style="position:absolute;left:-1500px;top:0;width:120px;height:40px">Carousel target</a>
        </div><button id="show" onclick="document.querySelector('#offscreen').style.left='0px'">Show slide</button>'''

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                path = request.split(b" ", 2)[1].decode()
                requests.append(path)
                body = b"<!doctype html><title>Opened carousel link</title>" if path == "/landed" else page
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                spec = McpServerConfig(name="playwright", command=shutil.which("node") or "node",
                    args=[str(_MCP_CLI), "--headless", "--isolated", "--executable-path", str(_BROWSER),
                          "--timeout-action", "500", "--snapshot-boxes", "--output-dir", str(root / "browser-output")],
                    default_risk="intrusive", tool_risk_overrides={"browser_navigate": "readonly"})
                cfg = SystemConfig(workspace=WorkspaceConfig(root=temporary),
                    audit_log_path=str(root / "audit.jsonl"), mcp_servers=[spec])
                cfg.blackboard.sqlite_path = str(root / "bb.db")
                cfg.knowledge.sqlite_path = str(root / "kb.db")
                # 隔离测试审批回调，每次有状态调用各请示一次；不设置自动批准配置。
                gate = AsyncMock(return_value=True)
                auth = Authorization(auth_id="local-carousel", mode=RunMode.SRC, human_confirmed=True,
                                     scope=Scope(allowed_urls=[base]))
                infra = await _build_infra(cfg, [ModelEntry(model="test", provider="mock")], auth,
                                           approval_gate=gate)
                try:
                    nav = await infra.tools.call("mcp__playwright__browser_navigate", {"url": base}, **infra.builtin_ctx)
                    self.assertTrue(nav.ok, nav.error)
                    failed = await infra.tools.call("mcp__playwright__browser_click", {"target": "#offscreen"}, **infra.builtin_ctx)
                    self.assertFalse(failed.ok)
                    self.assertEqual(failed.attempts, 1)
                    self.assertEqual(failed.parsed["failure_kind"], "target_outside_viewport")
                    self.assertIn("可视区域外", failed.error[:100])
                    self.assertNotIn("\x1b", failed.error)
                    self.assertNotIn("/landed", requests)
                    show = await infra.tools.call("mcp__playwright__browser_click", {"target": "#show"}, **infra.builtin_ctx)
                    self.assertTrue(show.ok, show.error)
                    clicked = await infra.tools.call("mcp__playwright__browser_click", {"target": "#offscreen"}, **infra.builtin_ctx)
                    self.assertTrue(clicked.ok, clicked.error)
                    self.assertEqual(requests.count("/landed"), 1)
                    self.assertEqual(gate.await_count, 3)
                finally:
                    await infra.close()
        finally:
            server.close()
            await server.wait_closed()
