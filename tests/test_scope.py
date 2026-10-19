from __future__ import annotations

import unittest

from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.models import RunMode, Scope
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.runtime import _mcp_embedded_scope_targets
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import ToolKind, ToolSpec


class ScopeGuardTests(unittest.TestCase):
    def test_url_prefix_does_not_expand_to_entire_host(self) -> None:
        guard = ScopeGuard(Scope(allowed_urls=["https://example.test/app"]))

        self.assertEqual(
            guard.check_url("https://example.test/app/users"),
            "https://example.test/app/users",
        )
        with self.assertRaises(ScopeViolation):
            guard.check_url("https://example.test/admin")
        with self.assertRaises(ScopeViolation):
            guard.check_url("https://example.test/application")

    def test_explicit_domain_scope_allows_entire_host(self) -> None:
        guard = ScopeGuard(Scope(allowed_domains=["example.test"]))
        self.assertEqual(
            guard.check_url("https://example.test/admin"),
            "https://example.test/admin",
        )

    def test_url_origin_includes_port(self) -> None:
        guard = ScopeGuard(Scope(allowed_urls=["http://127.0.0.1:8000/app"]))
        with self.assertRaises(ScopeViolation):
            guard.check_url("http://127.0.0.1:8001/app")

    def test_encoded_parent_path_cannot_escape_prefix(self) -> None:
        guard = ScopeGuard(Scope(allowed_urls=["https://example.test/app"]))
        with self.assertRaises(ScopeViolation):
            guard.check_url("https://example.test/app/%2e%2e/admin")
        with self.assertRaises(ScopeViolation):
            guard.check_url("https://example.test/app/%252e%252e/admin")

    def test_denied_host_is_case_insensitive_and_has_priority(self) -> None:
        guard = ScopeGuard(Scope(
            allowed_domains=["*.example.test"],
            denied=["admin.example.test"],
        ))
        with self.assertRaises(ScopeViolation):
            guard.check_url("https://ADMIN.EXAMPLE.TEST/dashboard")

    def test_denied_url_uses_path_boundary(self) -> None:
        guard = ScopeGuard(Scope(
            allowed_domains=["example.test"],
            denied=["https://example.test/private"],
        ))
        with self.assertRaises(ScopeViolation):
            guard.check_url("https://example.test/private/keys")
        self.assertEqual(
            guard.check_url("https://example.test/privately"),
            "https://example.test/privately",
        )

    def test_url_scope_still_allows_dns_resolution_of_its_host(self) -> None:
        guard = ScopeGuard(Scope(allowed_urls=["https://example.test/app"]))
        self.assertEqual(guard.check_target("example.test"), "example.test")

    def test_launch_keeps_target_as_url_prefix(self) -> None:
        plan = prepare_launch(LaunchRequest(
            mode=RunMode.SRC, target="https://example.test/app"))
        self.assertEqual(plan.authorization.scope.allowed_urls,
                         ["https://example.test/app"])
        self.assertEqual(plan.authorization.scope.allowed_domains, [])


class ToolScopeGateTests(unittest.TestCase):
    """工具门 fail-closed（scope_required）与 MCP 复合入参目标提取。"""

    @staticmethod
    def _registry() -> ToolRegistry:
        guard = ScopeGuard(Scope(allowed_domains=["ok.test"], denied=["denied.test"]))
        return ToolRegistry(LocalExecutor(), policy=PolicyEngine(RunMode.SRC, guard))

    def test_composite_target_tool_without_any_target_is_denied(self) -> None:
        registry = self._registry()
        spec = ToolSpec(name="mcp__yak__http_fuzzer", kind=ToolKind.MCP,
                        target_params=(), scope_required=True,
                        scope_target_extractor=_mcp_embedded_scope_targets)
        with self.assertRaises(ScopeViolation):
            registry._check_scope(spec, {})
        with self.assertRaises(ScopeViolation):
            registry._check_scope(spec, {"isHttps": True})

    def test_optional_or_absent_target_params_do_not_break_calls(self) -> None:
        # schema 无目标参数（快照/编解码）、目标参数可选（浏览器 tabs/click
        # 的缺省调用）都不在误杀范围；提供了目标就照常校验
        registry = self._registry()
        registry._check_scope(ToolSpec(name="mcp__browser__snapshot", kind=ToolKind.MCP,
                                       target_params=(), scope_required=True), {})
        tabs = ToolSpec(name="mcp__browser__tabs", kind=ToolKind.MCP,
                        target_params=("url",), scope_required=True)
        registry._check_scope(tabs, {"action": "list"})
        with self.assertRaises(ScopeViolation):
            registry._check_scope(tabs, {"url": "https://denied.test"})

    def test_embedded_target_with_path_is_still_checked(self) -> None:
        registry = self._registry()
        spec = ToolSpec(name="mcp__yak__http_fuzzer", kind=ToolKind.MCP,
                        target_params=(), scope_required=True,
                        scope_target_extractor=_mcp_embedded_scope_targets)
        with self.assertRaises(ScopeViolation):
            registry._check_scope(spec, {"actualAddr": "denied.test/app"})

    def test_embedded_extraction_keeps_path_and_port_values(self) -> None:
        # 含 / 的裸值此前被静默跳过（禁打门被绕过）——现在补 scheme 后校验
        self.assertEqual(
            _mcp_embedded_scope_targets({"actualAddr": "denied.test/app"}),
            ["http://denied.test/app/"])
        self.assertEqual(
            _mcp_embedded_scope_targets({"batchTarget": ["a.test:8080", "b.test/x"]}),
            ["http://a.test:8080/", "http://b.test/x/"])
        targets = _mcp_embedded_scope_targets({
            "isHttps": True,
            "request": "GET /admin HTTP/1.1\r\nHost: h.test\r\n\r\n"})
        self.assertIn("https://h.test/admin", targets)

    def test_request_host_and_actual_addr_are_both_checked(self) -> None:
        registry = self._registry()
        spec = ToolSpec(name="mcp__yak__http_fuzzer", kind=ToolKind.MCP,
                        target_params=(), scope_required=True,
                        scope_target_extractor=_mcp_embedded_scope_targets)
        with self.assertRaises(ScopeViolation):
            registry._check_scope(spec, {
                "actualAddr": "denied.test:8080", "isHttps": False,
                "request": "GET / HTTP/1.1\r\nHost: ok.test\r\n\r\n"})


if __name__ == "__main__":
    unittest.main()
