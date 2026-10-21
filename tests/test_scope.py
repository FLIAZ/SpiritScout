from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.models import RunMode, Scope
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec


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

    def test_legacy_denied_host_does_not_override_scope_annotation(self) -> None:
        guard = ScopeGuard(Scope(
            allowed_domains=["*.example.test"], denied=["admin.example.test"]))
        self.assertTrue(guard.is_allowed("https://ADMIN.EXAMPLE.TEST/dashboard"))

    def test_legacy_denied_url_does_not_block_or_authorize(self) -> None:
        guard = ScopeGuard(Scope(
            allowed_domains=["example.test"],
            denied=["https://example.test/private", "outside.test"]))
        self.assertTrue(guard.is_allowed("https://example.test/private/keys"))
        self.assertFalse(guard.is_allowed("https://outside.test/"))

    def test_legacy_denied_ip_does_not_override_cidr_annotation(self) -> None:
        guard = ScopeGuard(Scope(allowed_cidrs=["127.0.0.0/8"], denied=["127.0.0.1"]))
        self.assertEqual(guard.check_ip("127.0.0.1"), "127.0.0.1")

    def test_url_scope_still_allows_dns_resolution_of_its_host(self) -> None:
        guard = ScopeGuard(Scope(allowed_urls=["https://example.test/app"]))
        self.assertEqual(guard.check_target("example.test"), "example.test")

    def test_launch_keeps_target_as_url_prefix(self) -> None:
        plan = prepare_launch(LaunchRequest(
            mode=RunMode.SRC, target="https://example.test/app"))
        self.assertEqual(plan.authorization.scope.allowed_urls,
                         ["https://example.test/app"])
        self.assertEqual(plan.authorization.scope.allowed_domains, [])


class ToolContextBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """旧目标校验配置不再是执行门，工具调用不会由后端推断测试边界。"""

    async def test_required_and_embedded_target_metadata_do_not_block(self) -> None:
        guard = ScopeGuard(Scope(allowed_domains=["ok.test"], denied=["denied.test"]))
        registry = ToolRegistry(LocalExecutor(), policy=PolicyEngine(RunMode.SRC, guard))
        extractor = AsyncMock(side_effect=AssertionError("不应调用旧目标提取器"))
        execute = AsyncMock(return_value=ToolResult(tool="composite_probe", ok=True))
        spec = ToolSpec(name="composite_probe", kind=ToolKind.BUILTIN,
                        scope_required=True, scope_target_extractor=extractor)
        registry.register_builtin(spec.name, execute, spec)
        for params in ({}, {"isHttps": True}, {"target": "denied.test"},
                       {"actualAddr": "denied.test/app"},
                       {"request": "GET / HTTP/1.1\r\nHost: denied.test\r\n\r\n"}):
            result = await registry.call(spec.name, params)
            self.assertTrue(result.ok, result.error)
        self.assertEqual(execute.await_count, 5)
        extractor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
