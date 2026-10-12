from __future__ import annotations

import json
import unittest

from spiritscout.agents.business.agent import extract_surfaces
from spiritscout.config.settings import ModelEntry
from spiritscout.gateway.backends import MockBackend


class BusinessContextTests(unittest.TestCase):
    def test_extracts_observed_flow_without_values_or_external_links(self) -> None:
        html = """
        <html><head><title>商品中心</title></head><body>
          <h1>浏览商品</h1>
          <a href="/catalog">商品列表</a>
          <a href="https://outside.test/leave">外站</a>
          <form action="/search" method="get">
            <label>关键词</label>
            <input name="q" value="private term">
            <input name="csrf" value="secret-token" type="hidden">
          </form>
        </body></html>
        """

        surface = extract_surfaces(html, "https://shop.test/")

        self.assertEqual(surface["title"], "商品中心")
        self.assertEqual(surface["links"], [
            {"url": "https://shop.test/catalog", "text": "商品列表"},
        ])
        self.assertEqual(surface["forms"][0]["action"], "https://shop.test/search")
        self.assertEqual(
            [field["name"] for field in surface["forms"][0]["fields"]],
            ["q", "csrf"],
        )
        serialized = repr(surface)
        self.assertNotIn("private term", serialized)
        self.assertNotIn("secret-token", serialized)

    def test_redacts_query_values_and_url_userinfo(self) -> None:
        html = """
        <html><body>
          <a href="/reset?token=secret-token&amp;user=alice">Reset</a>
        </body></html>
        """

        surface = extract_surfaces(
            html, "https://viewer:password@shop.test/start?session=private")

        self.assertEqual(surface["url"], "https://shop.test/start?session=")
        self.assertEqual(surface["links"], [{
            "url": "https://shop.test/reset?token=&user=", "text": "Reset",
        }])
        serialized = repr(surface)
        for secret in ("viewer", "password", "secret-token", "alice", "private"):
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
