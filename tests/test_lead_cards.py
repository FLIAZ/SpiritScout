"""稳定 Lead Card、真实性消歧与软 200 回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from spiritscout.agents.recon.agent import ReconAgent
from spiritscout.agents.sub.cache import LocalSharedCache
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.blackboard.leads import assess_authenticity, response_profile
from spiritscout.core.models import AgentType, Asset, BlackboardLayer, Task
from spiritscout.gateway.world_state import investigation_frontier
from spiritscout.tools.spec import ToolResult


def _response(body: str, *, url: str, content_type: str = "text/html") -> ToolResult:
    return ToolResult(tool="http_probe", ok=True, stdout=body, parsed={
        "status_code": 200, "content_length": len(body), "final_url": url,
        "headers": {"content-type": content_type}, "redirect_chain": [],
    })


class LeadCardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bb = Blackboard(Path(self.tmp.name) / "bb.db")
        self.addCleanup(self.bb.close)

    def test_asset_merge_creates_one_persistent_lead_card(self) -> None:
        engine = ConvergenceEngine(self.bb)
        asset = Asset(asset_id="candidate", asset_type="endpoint",
                      value="https://a.test/swagger-ui/",
                      attrs={"method": "GET", "source": "model_generated_test"},
                      source_agent="recon")
        engine.merge_asset(asset, AgentType.RECON)
        engine.merge_asset(asset.model_copy(update={"asset_id": "candidate-2"}), AgentType.RECON)

        leads = [row for row in self.bb.query(BlackboardLayer.TOPOLOGY, limit=20)
                 if row["data"].get("kind") == "lead_card"]
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["data"]["status"], "needs_disambiguation")
        self.assertEqual(leads[0]["data"]["origin_class"], "generated_candidate")
        self.assertEqual(leads[0]["data"]["observation_count"], 1)
        self.assertEqual(leads[0]["data"]["source_refs"], ["asset:candidate@2"])
        self.assertNotEqual(leads[0]["data"].get("evidence_state"), "expired")

    def test_ordinary_assets_and_observed_business_endpoints_do_not_create_cards(self) -> None:
        engine = ConvergenceEngine(self.bb)
        engine.merge_asset(Asset(
            asset_id="ip", asset_type="ip", value="192.0.2.1", source_agent="recon"),
            AgentType.RECON)
        engine.merge_asset(Asset(
            asset_id="observed", asset_type="endpoint", value="https://a.test/api/news",
            attrs={"method": "GET", "sources": ["page_network", "target_response"]},
            source_agent="business"), AgentType.BUSINESS_CONTEXT)

        leads = [row for row in self.bb.query(BlackboardLayer.TOPOLOGY, limit=20)
                 if row["data"].get("kind") == "lead_card"]
        self.assertEqual(leads, [])

    def test_later_asset_merge_does_not_regress_authenticity_resolution(self) -> None:
        engine = ConvergenceEngine(self.bb)
        asset = Asset(asset_id="api", asset_type="endpoint", value="https://a.test/openapi.json",
                      attrs={"method": "GET", "source": "model_generated_test"},
                      source_agent="recon")
        engine.merge_asset(asset, AgentType.RECON)
        from spiritscout.blackboard.leads import upsert_lead

        upsert_lead(
            self.bb, subject=asset.value, origins=["model_generated_test"],
            status="actionable", authenticity={"verdict": "authenticity_confirmed"},
            unknowns=[], replace_unknowns=True, actor="recon", agent_type=AgentType.RECON,
        )
        engine.merge_asset(asset.model_copy(update={"asset_id": "api-again"}), AgentType.RECON)

        lead = next(row["data"] for row in self.bb.query(BlackboardLayer.TOPOLOGY, limit=20)
                    if row["data"].get("kind") == "lead_card")
        self.assertEqual(lead["status"], "actionable")
        self.assertEqual(lead["authenticity"]["verdict"], "authenticity_confirmed")
        self.assertEqual(lead["unknowns"], [])

    def test_authenticity_classifier_distinguishes_soft_200_and_json_api(self) -> None:
        homepage = response_profile("<html><title>Home</title><main>Welcome 12345</main></html>", {
            "status_code": 200, "headers": {"content-type": "text/html"},
            "final_url": "https://a.test/",
        })
        soft = response_profile("<html><title>Home</title><main>Welcome 67890</main></html>", {
            "status_code": 200, "headers": {"content-type": "text/html"},
            "final_url": "https://a.test/",
        })
        api = response_profile('{"openapi":"3.0.0","paths":{"/users":{}}}', {
            "status_code": 200, "headers": {"content-type": "application/json"},
            "final_url": "https://a.test/openapi.json",
        })

        self.assertEqual(assess_authenticity(
            profile=soft, baseline=homepage, origin="model_generated_test")["verdict"],
            "soft_200_suspected")
        self.assertEqual(assess_authenticity(
            profile=api, baseline=homepage, origin="model_generated_test")["verdict"],
            "authenticity_confirmed")

    async def test_recon_soft_200_updates_lead_without_claiming_real_entry(self) -> None:
        homepage = _response("<html><title>Home</title><main>Welcome 12345</main></html>",
                             url="https://a.test/")
        candidate = _response("<html><title>Home</title><main>Welcome 67890</main></html>",
                              url="https://a.test/")
        missing = _response("<html><title>Home</title><main>Welcome 24680</main></html>",
                            url="https://a.test/")
        tools = SimpleNamespace(call=AsyncMock(side_effect=[candidate, missing]))
        agent = object.__new__(ReconAgent)
        agent.agent_id = "recon-a"
        agent.agent_type = AgentType.RECON
        agent.cache = LocalSharedCache()
        agent.cache.put("recon", "fingerprint|https://a.test", {
            "response_profile": response_profile(homepage.stdout, homepage.parsed)})
        agent.ctx = SimpleNamespace(
            tools=tools, builtin_ctx={}, blackboard=self.bb,
            convergence=ConvergenceEngine(self.bb),
        )
        task = Task(task_id="t1", task_type="recon", target="https://a.test",
                    agent_type=AgentType.RECON)

        result = await agent._step_path_probe(task, {
            "path": "/swagger-ui/", "origin": "model_generated_test"})

        self.assertEqual(result.value, 0.0)
        self.assertIn("疑似软 200", result.note)
        frontier = investigation_frontier(self.bb)
        cards = [item for item in frontier["needs_disambiguation"]
                 if item["subject"] == "https://a.test/swagger-ui/"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["authenticity"]["verdict"], "soft_200_suspected")
        self.assertEqual(cards[0]["unknowns"], ["该候选是否对应独立真实入口"])
        self.assertEqual(tools.call.await_count, 2)
        self.assertEqual(self.bb.query(BlackboardLayer.RECON_CLUE, limit=20), [])

        # 同一 Agent/目标后续候选复用不存在路径基线，只新增候选本身一次请求。
        second = _response("<html><title>Home</title><main>Welcome 13579</main></html>",
                           url="https://a.test/")
        tools.call.side_effect = [second]
        await agent._step_path_probe(task, {
            "path": "/another-docs/", "origin": "model_generated_test"})
        self.assertEqual(tools.call.await_count, 3)

    async def test_non_200_and_derived_candidates_do_not_fetch_missing_baseline(self) -> None:
        not_found = ToolResult(tool="http_probe", ok=True, stdout="missing", parsed={
            "status_code": 404, "content_length": 7, "final_url": "https://a.test/missing",
            "headers": {"content-type": "text/html"}, "redirect_chain": [],
        })
        derived = _response('{"message":"ok"}', url="https://a.test/from-robots",
                            content_type="application/json")
        tools = SimpleNamespace(call=AsyncMock(side_effect=[not_found, derived]))
        agent = object.__new__(ReconAgent)
        agent.agent_id = "recon-a"
        agent.agent_type = AgentType.RECON
        agent.cache = LocalSharedCache()
        agent.ctx = SimpleNamespace(
            tools=tools, builtin_ctx={}, blackboard=self.bb,
            convergence=ConvergenceEngine(self.bb),
        )
        task = Task(task_id="t1", task_type="recon", target="https://a.test",
                    agent_type=AgentType.RECON)

        await agent._step_path_probe(task, {
            "path": "/missing", "origin": "model_generated_test"})
        await agent._step_path_probe(task, {
            "path": "/from-robots", "origin": "robots.txt"})

        self.assertEqual(tools.call.await_count, 2)


if __name__ == "__main__":
    unittest.main()
