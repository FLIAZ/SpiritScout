from __future__ import annotations

import unittest

from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.models import RunMode, Scope
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.policy.scope import ScopeGuard


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


if __name__ == "__main__":
    unittest.main()
