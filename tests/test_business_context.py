from __future__ import annotations

import json
import unittest

from spiritscout.config.settings import ModelEntry
from spiritscout.gateway.backends import MockBackend


class UrlVerbatimTests(unittest.TestCase):
    """入黑板 URL 原文保留：会话页/拓扑监控的是 agent 真实足迹。"""

    def test_business_agent_module_no_longer_redacts_urls(self) -> None:
        from spiritscout.agents.business import agent as business_agent
        self.assertFalse(hasattr(business_agent, "_redacted_url"))


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
