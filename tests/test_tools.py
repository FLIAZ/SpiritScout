from __future__ import annotations

import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from spiritscout.core.models import RunMode, Scope
from spiritscout.policy import PolicyEngine, ScopeGuard
from spiritscout.tools.builtin import http_probe
from spiritscout.tools.executor import LocalExecutor, ToolExecutor
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec


class _RecordingExecutor(ToolExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def run(self, spec: ToolSpec, params: dict) -> ToolResult:
        self.calls += 1
        return ToolResult(tool=spec.name, ok=True)


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", "/landing")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"landing")

    def log_message(self, format: str, *args) -> None:
        return


class _EchoHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        payload = json.dumps({
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


class ToolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_denied_target_executes_without_backend_boundary_gate(self) -> None:
        """旧排除声明只传模型，执行层不做禁打匹配。"""
        executor = _RecordingExecutor()
        policy = PolicyEngine(
            RunMode.SRC,
            ScopeGuard(Scope(allowed_urls=["https://example.test/app"],
                             denied=["https://example.test/admin"])),
        )
        registry = ToolRegistry(executor, policy=policy)
        registry.register(ToolSpec(
            name="external_probe", kind=ToolKind.CLI,
            command_template=["probe", "{url}"], allowed_params={"url"},
            target_params=("url",), risk=RiskLevel.READONLY,
        ))

        result = await registry.call(
            "external_probe", {"url": "https://example.test/admin"}

        )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(executor.calls, 1)

    async def test_out_of_scope_target_executes_without_hard_block(self) -> None:
        """授权白名单不拦截：范围外目标照常执行（扩围由用户决定）。"""
        executor = _RecordingExecutor()
        policy = PolicyEngine(
            RunMode.SRC,
            ScopeGuard(Scope(allowed_urls=["https://example.test/app"])),
        )
        registry = ToolRegistry(executor, policy=policy)
        registry.register(ToolSpec(
            name="external_probe", kind=ToolKind.CLI,
            command_template=["probe", "{url}"], allowed_params={"url"},
            target_params=("url",), risk=RiskLevel.READONLY,
        ))

        result = await registry.call(
            "external_probe", {"url": "https://other.test/x"})

        self.assertTrue(result.ok)
        self.assertEqual(executor.calls, 1)

    async def test_local_executor_preserves_success_return_code(self) -> None:
        executor = LocalExecutor()
        spec = ToolSpec(
            name="success", kind=ToolKind.CLI,
            command_template=[sys.executable, "-c", "raise SystemExit(0)"],
            target_params=(),
        )
        result = await executor.run(spec, {})
        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)

    async def test_http_redirect_follows_and_marks_out_of_scope(self) -> None:
        """重定向照常跟随；最终落点在白名单外时结果带范围标注（不拦截）。"""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/start"
            result = await http_probe(
                url, guard=ScopeGuard(Scope(allowed_urls=[url],
                                            denied=["127.0.0.1", url.replace("/start", "/outside")])))
        finally:
            server.shutdown()
            server.server_close()
            await asyncio.to_thread(thread.join, 2)

        self.assertTrue(result.ok)
        self.assertEqual(result.parsed["status_code"], 200)
        self.assertTrue(result.parsed["redirect_chain"])
        self.assertIn("未匹配初始范围列表", result.parsed["scope_note"])
        self.assertIn("不代表该目标未经授权", result.parsed["scope_note"])
        self.assertIn("无需重复确认", result.parsed["scope_note"])
        self.assertNotIn("继续测试该目标前先用", result.parsed["scope_note"])

    async def test_http_body_gets_default_content_type(self) -> None:
        """表单串/JSON 原文自动补 Content-Type：服务端按表单解析能读到字段。

        回归：raw content 不带类型头时 Flask request.form 为空 → 400，
        模型拿不到可纠正的信息只能反复试值（实测白烧多个回合）。
        """
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/echo"
        guard = ScopeGuard(Scope(allowed_urls=[url]))
        try:
            form = await http_probe(
                url, method="POST",
                data="username=whale01&password=test123456", guard=guard)
            body = json.loads(form.stdout)
            self.assertEqual(body["content_type"],
                             "application/x-www-form-urlencoded")
            self.assertEqual(body["body"],
                             "username=whale01&password=test123456")

            as_json = await http_probe(
                url, method="POST", data='{"username": "whale01"}', guard=guard)
            self.assertEqual(json.loads(as_json.stdout)["content_type"],
                             "application/json")

            explicit = await http_probe(
                url, method="POST", data="raw",
                headers={"content-type": "text/plain"}, guard=guard)
            self.assertEqual(json.loads(explicit.stdout)["content_type"],
                             "text/plain")
        finally:
            server.shutdown()
            server.server_close()
            await asyncio.to_thread(thread.join, 2)


class DnsResolveDeniedAddressTests(unittest.IsolatedAsyncioTestCase):
    """真实 localhost 解析不因上下文中的旧排除声明被后端拒绝。"""

    async def test_legacy_denied_dns_address_still_resolves(self) -> None:
        from spiritscout.tools.builtin import dns_resolve

        guard = ScopeGuard(Scope(denied=["127.0.0.1", "::1"]))
        result = await dns_resolve("localhost", guard=guard)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.parsed["ips"])

    async def test_normal_host_still_resolves(self) -> None:
        from spiritscout.tools.builtin import dns_resolve

        guard = ScopeGuard(Scope(allowed_domains=["localhost"]))
        result = await dns_resolve("localhost", guard=guard)

        self.assertTrue(result.ok)
        self.assertTrue(result.parsed["ips"])


if __name__ == "__main__":
    unittest.main()
