"""Chrome kit 接入：运行环境网关、现有登录态及工具权限保持可验证。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from spiritscout.config.settings import (
    McpServerConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.models import Authorization, RunMode, Scope
from spiritscout.runtime import _build_infra
from spiritscout.tools.chrome_mcp import (
    MCP_PACKAGE,
    cdp_endpoint,
    command,
    default_host,
)
from spiritscout.tools.mcp_client import build_client
from spiritscout.tools.registry import AgentToolView


class ChromeEndpointTests(unittest.TestCase):
    def test_headless_command_is_isolated_and_does_not_attach_to_cdp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli = root / "cli.js"
            cli.touch()
            env = {"SPIRITSCOUT_CHROME_MCP_CLI": str(cli), "SPIRITSCOUT_CHROME_MODE": "headless",
                   "SPIRITSCOUT_CHROME_EXECUTABLE": "/installed/chromium"}
            with patch.object(Path, "cwd", return_value=root), patch("shutil.which", return_value="/bin/node"):
                argv = command("http://unavailable:9223", env)
            self.assertIn("--headless", argv)
            self.assertIn("--isolated", argv)
            self.assertEqual(Path(argv[argv.index("--executable-path") + 1]), Path("/installed/chromium"))
            self.assertNotIn("--cdp-endpoint", argv)
            self.assertNotIn("--user-data-dir", argv)

    def test_project_adapter_uses_running_venv_without_path_activation(self) -> None:
        with patch.dict(os.environ, {"SPIRITSCOUT_CHROME_CDP_URL": "http://127.0.0.1:9223",
                                    "SPIRITSCOUT_CHROME_MCP_CLI": "/installed/cli.js"}):
            client = build_client(McpServerConfig(name="chrome", command="python",
                args=["-m", "spiritscout.tools.chrome_mcp"]))
        self.assertEqual(client._params.command, sys.executable)
        self.assertEqual(client._params.env["SPIRITSCOUT_CHROME_MCP_CLI"], "/installed/cli.js")
        self.assertEqual(client._params.env["SPIRITSCOUT_CHROME_CDP_URL"], "http://127.0.0.1:9223")

    def test_explicit_docker_hostname_is_resolved_to_ip(self) -> None:
        with patch("spiritscout.tools.chrome_mcp.socket.gethostbyname", return_value="192.168.65.254"):
            self.assertEqual(cdp_endpoint("http://host.docker.internal:9223"),
                             "http://192.168.65.254:9223")

    def test_literal_addresses_do_not_need_dns(self) -> None:
        for value in ("http://127.0.0.1:9223", "http://localhost:9223", "http://[::1]:9222"):
            self.assertEqual(cdp_endpoint(value), value)
        for value in ("file:///private", "not-a-url", "http://name:bad", "http://user:secret@localhost:9223"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cdp_endpoint(value)

    def test_default_wsl_gateway_comes_from_its_own_route(self) -> None:
        def exists(path):
            return path.as_posix() != "/.dockerenv"

        def read(path):
            if "osrelease" in str(path):
                return "6.6.87.2-microsoft-standard-WSL2"
            return "Iface Destination Gateway Flags\neth0 00000000 016012AC 0003\n"

        with patch.object(Path, "exists", exists), patch.object(Path, "read_text", read):
            self.assertEqual(default_host(), "172.18.96.1")

    def test_default_docker_gateway_is_not_wsl_route(self) -> None:
        with patch.object(Path, "exists", return_value=True):
            self.assertEqual(default_host(), "host.docker.internal")

    def test_packaged_cli_attaches_with_cookie_tools_without_new_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cli = root / "cli.js"
            cli.touch()
            env = {"SPIRITSCOUT_CHROME_MCP_CLI": str(cli)}
            with patch.object(Path, "cwd", return_value=root), patch("shutil.which", return_value="/bin/node"):
                argv = command("http://127.0.0.1:9223", env)
            self.assertEqual(argv[:2], ["/bin/node", str(cli)])
            self.assertIn("storage", argv)
            self.assertIn("--cdp-endpoint", argv)
            self.assertNotIn("--headless", argv)
            self.assertNotIn("--isolated", argv)
            self.assertNotIn("--user-data-dir", argv)
            self.assertTrue(Path(env["npm_config_cache"]).is_relative_to(root))

    def test_source_install_uses_fixed_package_and_invalid_cli_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(Path, "cwd", return_value=root), \
                    patch("spiritscout.tools.chrome_mcp.PACKAGED_CLI", root / "missing.js"), \
                    patch("shutil.which", return_value="/bin/npx"):
                self.assertEqual(command("http://127.0.0.1:9223", {})[:3], ["/bin/npx", "--yes", MCP_PACKAGE])
                with self.assertRaises(ValueError):
                    command("http://127.0.0.1:9223", {"SPIRITSCOUT_CHROME_MCP_CLI": str(root / "missing.js")})


class ChromeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_competition_selects_headless_without_shared_login_guidance(self) -> None:
        client = SimpleNamespace(start=AsyncMock(return_value=[
            {"name": "browser_navigate", "inputSchema": {"type": "object", "properties": {}}}]),
            close=AsyncMock(), call=AsyncMock(return_value={"is_error": False, "text": "tabs ready"}))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=temp),
                audit_log_path=str(root / "audit.jsonl"), mcp_servers=[McpServerConfig(
                    name="chrome", command="python", args=["-m", "spiritscout.tools.chrome_mcp"])])
            cfg.blackboard.sqlite_path = str(root / "bb.db")
            cfg.knowledge.sqlite_path = str(root / "kb.db")
            for mode in (RunMode.CTF, RunMode.COMPETITION):
                with patch("spiritscout.tools.mcp_client.build_client", return_value=client) as build:
                    infra = await _build_infra(cfg, [ModelEntry(model="mock", provider="mock")],
                        Authorization(auth_id="headless-test", mode=mode, human_confirmed=True,
                                      scope=Scope(allowed_urls=["http://127.0.0.1"])))
                try:
                    self.assertEqual(build.call_args.args[0].env["SPIRITSCOUT_CHROME_MODE"], "headless")
                    description = infra.mcp_tools["mcp__chrome__browser_navigate"]["schema"]["description"]
                    self.assertIn("会话独立的无头浏览器", description)
                    self.assertNotIn("用户可见的现有浏览器", description)
                finally:
                    await infra.close()

    async def test_catalog_reaches_business_and_ctf_with_human_handoff_guidance(self) -> None:
        tools = [{"name": name, "inputSchema": {"type": "object", "properties": {}}}
                 for name in ("browser_navigate", "browser_cookie_list")]
        client = SimpleNamespace(start=AsyncMock(return_value=tools), close=AsyncMock(),
                                 call=AsyncMock(return_value={"text": "live-cookie-result", "is_error": False}))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=temp),
                audit_log_path=str(root / "audit.jsonl"), mcp_servers=[McpServerConfig(
                    name="chrome", command="python", args=["-m", "spiritscout.tools.chrome_mcp"],
                    default_risk="readonly", risk_by_action=True, allowed_agents=["business_context", "ctf_web"])])
            cfg.blackboard.sqlite_path = str(root / "bb.db")
            cfg.knowledge.sqlite_path = str(root / "kb.db")
            with patch("spiritscout.tools.mcp_client.build_client", return_value=client) as build:
                infra = await _build_infra(cfg, [ModelEntry(model="mock", provider="mock")],
                    Authorization(auth_id="chrome-test", mode=RunMode.SRC, human_confirmed=True,
                                  scope=Scope(allowed_urls=["http://127.0.0.1"])))
            try:
                self.assertEqual(build.call_args.args[0].env["SPIRITSCOUT_CHROME_MODE"], "cdp")
                name = "mcp__chrome__browser_cookie_list"
                for role in ("business_context", "ctf_web"):
                    self.assertTrue(AgentToolView(infra.tools, role).allows(name))
                for role in ("secretary", "report", "ctf_crypto"):
                    self.assertFalse(AgentToolView(infra.tools, role).allows(name))
                description = infra.mcp_tools[name]["schema"]["description"]
                self.assertIn("ask_user", description)
                self.assertIn("HttpOnly", description)
                self.assertIn("重新读取", description)
                response = await infra.tools.call(name, {"_safety_action": "analysis"}, **infra.builtin_ctx)
                self.assertTrue(response.ok)
                self.assertEqual(client.call.await_args_list, [
                    call("browser_tabs", {"action": "list"}, timeout=5.0),
                    call("browser_cookie_list", {}, timeout=180.0)])
            finally:
                await infra.close()
            client.close.assert_awaited_once()

    async def test_handshake_without_browser_connection_is_not_registered_as_ready(self) -> None:
        client = SimpleNamespace(start=AsyncMock(return_value=[{"name": "browser_navigate"}]),
            close=AsyncMock(), call=AsyncMock(return_value={"is_error": True,
                "text": "async initializeServer: connect ECONNREFUSED 127.0.0.1:9223"}))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=temp),
                audit_log_path=str(root / "audit.jsonl"), mcp_servers=[McpServerConfig(
                    name="chrome", command="python", args=["-m", "spiritscout.tools.chrome_mcp"])])
            cfg.blackboard.sqlite_path = str(root / "bb.db")
            cfg.knowledge.sqlite_path = str(root / "kb.db")
            with patch("spiritscout.tools.mcp_client.build_client", return_value=client):
                infra = await _build_infra(cfg, [ModelEntry(model="mock", provider="mock")],
                    Authorization(auth_id="chrome-unavailable", mode=RunMode.SRC, human_confirmed=True,
                                  scope=Scope(allowed_urls=["http://127.0.0.1"])))
            try:
                self.assertNotIn("mcp__chrome__browser_navigate", infra.mcp_tools)
                records = [json.loads(line) for line in (root / "audit.jsonl").read_text().splitlines()]
                failed = [r for r in records if r["detail"].get("action") == "mcp_server_failed"]
                self.assertEqual(len(failed), 1)
                self.assertIn("9223", failed[0]["detail"]["error"])
            finally:
                await infra.close()


@unittest.skipUnless(os.environ.get("SPIRITSCOUT_TEST_HEADLESS_MCP") == "1",
                     "需要本机 MCP CLI 与浏览器，可显式开启真实无头链路测试")
class ChromeManagedHeadlessLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_ctf_and_competition_runtime_navigate_without_cdp(self) -> None:
        async def handle(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                body = b"<!doctype html><title>SpiritScout isolated browser</title><h1>Local fixture</h1>"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                             + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        target = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            for mode in (RunMode.CTF, RunMode.COMPETITION):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    cfg = SystemConfig(workspace=WorkspaceConfig(root=temp),
                        audit_log_path=str(root / "audit.jsonl"), mcp_servers=[McpServerConfig(
                            name="chrome", command="python", args=["-m", "spiritscout.tools.chrome_mcp"],
                            risk_by_action=True,
                            env={"SPIRITSCOUT_CHROME_CDP_URL": "http://127.0.0.1:1"})])
                    cfg.blackboard.sqlite_path = str(root / "bb.db")
                    cfg.knowledge.sqlite_path = str(root / "kb.db")
                    infra = await _build_infra(cfg, [ModelEntry(model="mock", provider="mock")],
                        Authorization(auth_id="headless-live", mode=mode, human_confirmed=True,
                                      scope=Scope(allowed_urls=[target])))
                    try:
                        await asyncio.gather(*infra.mcp_startup_tasks)
                        name = "mcp__chrome__browser_navigate"
                        self.assertIn(name, infra.mcp_tools, (root / "audit.jsonl").read_text())
                        result = await infra.tools.call(name, {"url": target, "_safety_action": "analysis"},
                                                        **infra.builtin_ctx)
                        self.assertTrue(result.ok, result.error)
                        self.assertIn("SpiritScout isolated browser", result.stdout)
                    finally:
                        await infra.close()
        finally:
            server.close()
            await server.wait_closed()


_CDP = os.environ.get("SPIRITSCOUT_TEST_CHROME_CDP_URL")


@unittest.skipUnless(_CDP, "需要已启动的 chrome-mcp-kit CDP 链路")
class ChromeLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_wrapper_and_http_only_cookie(self) -> None:
        from spiritscout.tools.mcp_client import build_client

        # 创建一次性本机页面与带 HttpOnly 的夹具 Cookie，不读取现有站点凭据。
        cookie = "spiritscout_fixture_" + uuid.uuid4().hex[:12]

        async def handle(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                body = b"<!doctype html><title>SpiritScout Chrome MCP fixture</title><h1>Local fixture</h1>"
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                    + f"Set-Cookie: {cookie}=local-test-only; HttpOnly; Path=/\r\n".encode()
                    + b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "0.0.0.0", 0)
        # WSL 中的夹具需通过 WSL 的 eth0 地址供 Windows Chrome 访问。
        address = os.environ.get("SPIRITSCOUT_TEST_FIXTURE_HOST", "127.0.0.1")
        target = f"http://{address}:{server.sockets[0].getsockname()[1]}"
        client = build_client(McpServerConfig(name="chrome", command=os.sys.executable,
            args=["-m", "spiritscout.tools.chrome_mcp"], env={"SPIRITSCOUT_CHROME_CDP_URL": str(_CDP)}))
        tab = False
        try:
            tools = await client.start()
            names = {tool["name"] for tool in tools}
            self.assertIn("browser_cookie_list", names)
            response = await client.call("browser_tabs", {"action": "new"})
            self.assertFalse(response["is_error"], response["text"])
            tab = True
            response = await client.call("browser_navigate", {"url": target})
            self.assertFalse(response["is_error"], response["text"])
            self.assertIn("SpiritScout Chrome MCP fixture", response["text"])
            response = await client.call("browser_cookie_get", {"name": cookie})
            self.assertFalse(response["is_error"], response["text"])
            self.assertIn(cookie, response["text"])
            self.assertIn("local-test-only", response["text"])
            self.assertIn("httpOnly: true", response["text"])
            # 模拟登录状态更新，确认下一次查询实时读取同一上下文而不是历史快照。
            code = "async(page)=>{await page.context().addCookies([" + json.dumps({
                "name": cookie, "value": "updated-login-state", "url": target, "httpOnly": True})
            code += "]);return await page.title()}"
            code_tool = next(name for name in names if name in {"browser_run_code", "browser_run_code_unsafe"})
            response = await client.call(code_tool, {"code": code})
            self.assertFalse(response["is_error"], response["text"])
            response = await client.call("browser_cookie_get", {"name": cookie})
            self.assertIn("updated-login-state", response["text"])
        finally:
            if tab:
                await client.call("browser_cookie_delete", {"name": cookie})
                await client.call("browser_tabs", {"action": "close"})
            await client.close()
            server.close()
            await server.wait_closed()
