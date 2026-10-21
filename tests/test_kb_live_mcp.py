"""kb_search 直接检索会话当前注册的 MCP 工具目录，无需预先入库。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.knowledge.store import KnowledgeStore


class McpLiveDirectorySearchTests(unittest.IsolatedAsyncioTestCase):
    """用户经网页端自装的 MCP 服务器无需入库：kb_search 直接翻活工具目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.kb = KnowledgeStore(Path(self._tmp.name) / "kb.db", auto_seed=True)

    def tearDown(self) -> None:
        self.kb.close()
        self._tmp.cleanup()

    def _agent(self) -> CTFBaseAgent:
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = "a"
        agent.agent_type = SimpleNamespace(value="ctf_web")
        agent.direction = "web"
        # 模拟会话装配后的活目录：内置 yakit + 用户自装的 sqlmap 服务器
        agent.ctx = SimpleNamespace(
            mcp_tools={
                "mcp__yakit__require_dnslog_domain": {
                    "server": "yakit", "tool": "require_dnslog_domain",
                    "schema": {"description":
                               "Request DNSLog subdomain and token for OOB detection"}},
                "mcp__sqlmap__run_scan": {
                    "server": "sqlmap", "tool": "run_scan",
                    "schema": {"description":
                               "Run sqlmap scan against a target URL for SQL injection"}},
            },
            knowledge=self.kb)
        return agent

    async def test_user_installed_server_surfaces_in_kb_search(self) -> None:
        agent = self._agent()
        out = await agent._tool_kb_search({"query": "sqlmap 注入"})
        self.assertTrue(out.ok)
        self.assertIn("mcp__sqlmap__run_scan", out.text)

    async def test_dnslog_tool_surfaces_from_live_directory(self) -> None:
        agent = self._agent()
        out = await agent._tool_kb_search({"query": "dnslog"})
        self.assertTrue(out.ok)
        self.assertIn("mcp__yakit__require_dnslog_domain", out.text)

    async def test_no_hit_anywhere_reports_miss(self) -> None:
        agent = self._agent()
        out = await agent._tool_kb_search({"query": "zzqxvvn wqkj"})
        self.assertFalse(out.ok)


if __name__ == "__main__":
    unittest.main()
