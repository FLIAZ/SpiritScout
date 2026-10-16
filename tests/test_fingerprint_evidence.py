"""技术栈识别以标签和头部为依据；证据不足是正常阴性结果。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from spiritscout.agents.recon.agent import ReconAgent
from spiritscout.core.models import AgentType, Task
from spiritscout.tools.fingerprint import web_fingerprint


class FingerprintEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def test_html_framework_assets_are_recognized_without_server_headers(self) -> None:
        result = web_fingerprint({"Content-Type": "text/html"}, '<script src="/_next/static/main.js"></script>')
        self.assertEqual(result["technologies"][0]["name"], "Next.js")
        self.assertEqual(result["technologies"][0]["source"], "resource_path")

    def test_comments_and_json_strings_are_not_framework_evidence(self) -> None:
        self.assertEqual(web_fingerprint({"Content-Type": "text/html"}, '<!-- <script src="/_next/static/x.js"></script> -->'), {})
        self.assertEqual(web_fingerprint({"Content-Type": "application/json"}, '"<script src=\"/_next/static/x.js\"></script>"'), {})

    def test_generator_and_headers_keep_observed_evidence(self) -> None:
        result = web_fingerprint({"Server": "nginx", "Content-Type": "text/html"},
                                 '<meta name="generator" content="Hugo 0.123">')
        self.assertEqual(result["server"], "nginx")
        self.assertEqual(result["technologies"][0]["name"], "Hugo 0.123")

    async def test_no_identifiable_technology_is_not_an_execution_failure(self) -> None:
        agent = object.__new__(ReconAgent)
        agent.cache = SimpleNamespace(get=Mock(return_value={"fingerprint": {}, "title": "site"}))
        agent._write_clue = Mock()
        task = Task(task_id="fp", task_type="recon", agent_type=AgentType.RECON, target="https://example.test")
        result = await agent._step_fingerprint(task, {})
        self.assertTrue(result.ok)
        self.assertIn("未提供可确认指纹", result.note)
        agent._write_clue.assert_not_called()
