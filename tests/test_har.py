from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import BlackboardLayer, Scope
from spiritscout.policy.scope import ScopeGuard
from spiritscout.traffic import import_har_directory, parse_har


def _har(entries: list[dict]) -> dict:
    return {"log": {"version": "1.2", "entries": entries}}


class HarImportTests(unittest.TestCase):
    def test_parser_extracts_names_without_values_or_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.har"
            path.write_text(json.dumps(_har([{
                "request": {
                    "method": "GET",
                    "url": "https://example.test/app/users/12345?id=secret&q=term",
                    "headers": [
                        {"name": "Authorization", "value": "Bearer secret-token"},
                        {"name": "Cookie", "value": "session=secret-cookie"},
                    ],
                    "queryString": [
                        {"name": "id", "value": "secret"},
                        {"name": "q", "value": "term"},
                    ],
                },
                "response": {"status": 200,
                             "content": {"text": "private response"}},
            }])), encoding="utf-8")

            endpoints = parse_har(path)

        self.assertEqual(len(endpoints), 1)
        endpoint = endpoints[0]
        self.assertEqual(endpoint.params, ("id", "q"))
        self.assertTrue(endpoint.template.endswith("/users/{id}"))
        self.assertEqual(endpoint.first_seen_index, 0)
        self.assertEqual(endpoint.status_code, 200)
        serialized = repr(endpoint)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("secret-cookie", serialized)
        self.assertNotIn("private response", serialized)

    def test_parser_removes_basic_auth_userinfo_from_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.har"
            path.write_text(json.dumps(_har([{
                "request": {
                    "method": "GET",
                    "url": "https://alice:secret@example.test:8443/private?q=value",
                },
            }])), encoding="utf-8")

            endpoints = parse_har(path)

        self.assertEqual(endpoints[0].url,
                         "https://example.test:8443/private")
        self.assertNotIn("alice", repr(endpoints[0]))
        self.assertNotIn("secret", repr(endpoints[0]))

    def test_import_keeps_only_authorized_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "capture.har").write_text(json.dumps(_har([
                {"request": {"method": "GET",
                              "url": "https://example.test/app/search?q=x"}},
                {"request": {"method": "GET",
                              "url": "https://outside.test/app?q=x"}},
            ])), encoding="utf-8")
            bb = Blackboard(root / "bb.db")
            try:
                stats = import_har_directory(
                    root, blackboard=bb,
                    guard=ScopeGuard(Scope(
                        allowed_urls=["https://example.test/app"])),
                    audit=NullAuditLogger(),
                )
                assets = bb.query(BlackboardLayer.ASSET)
            finally:
                bb.close()

        self.assertEqual(stats["endpoints"], 1)
        self.assertEqual(stats["out_of_scope"], 1)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["data"]["attrs"]["params"], ["q"])


if __name__ == "__main__":
    unittest.main()
