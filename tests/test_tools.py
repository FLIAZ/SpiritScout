from __future__ import annotations

import asyncio
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


class ToolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_target_is_blocked_before_execution(self) -> None:
        """禁打列表是唯一硬边界：命中即拒，不触碰执行器。"""
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

        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, -3)
        self.assertIn("禁打", result.error)
        self.assertEqual(executor.calls, 0)

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
                url, guard=ScopeGuard(Scope(allowed_urls=[url])))
        finally:
            server.shutdown()
            server.server_close()
            await asyncio.to_thread(thread.join, 2)

        self.assertTrue(result.ok)
        self.assertEqual(result.parsed["status_code"], 200)
        self.assertTrue(result.parsed["redirect_chain"])
        self.assertIn("授权范围之外", result.parsed["scope_note"])


if __name__ == "__main__":
    unittest.main()
