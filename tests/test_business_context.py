from __future__ import annotations

import json
import unittest

from spiritscout.agents.business.agent import _redacted_url
from spiritscout.config.settings import ModelEntry
from spiritscout.gateway.backends import MockBackend


class UrlRedactionTests(unittest.TestCase):
    """入黑板 URL 的脱敏保证：结构保留、参数值与用户信息不落盘。"""

    def test_strips_query_values_but_keeps_structure(self) -> None:
        self.assertEqual(
            _redacted_url("https://shop.test/reset?token=secret-token&user=alice"),
            "https://shop.test/reset?token=&user=",
        )

    def test_strips_url_userinfo(self) -> None:
        self.assertEqual(
            _redacted_url("https://viewer:password@shop.test/start?session=private"),
            "https://shop.test/start?session=",
        )

    def test_non_http_passthrough(self) -> None:
        self.assertEqual(_redacted_url("javascript:alert(1)"), "javascript:alert(1)")

    def test_no_secret_survives(self) -> None:
        serialized = repr(_redacted_url(
            "https://alice:hunter2@shop.test/api?session=private-token&q=x"))
        for secret in ("alice", "hunter2", "private-token"):
            self.assertNotIn(secret, serialized)


class BusinessContextMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_does_not_describe_html_forms_as_har_transactions(self) -> None:
        backend = MockBackend()
        response = await backend.complete(
            ModelEntry(model="mock", provider="mock"),
            [
                {"role": "system", "content": "ROLE: business_analyst"},
                {"role": "user", "content": json.dumps({"evidence": [
                    {"id": "E1", "kind": "page"},
                    {"id": "E2", "kind": "observed_form"},
                ]})},
            ],
        )

        data = json.loads(response["content"])
        self.assertEqual(data["workflows"][0]["name"], "公开页面访问路径")
        self.assertNotIn("HAR", response["content"])


if __name__ == "__main__":
    unittest.main()
