"""外部资产情报必须先成为候选，再由本地证据晋级。"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from spiritscout.assets.cache import ProviderCache
from spiritscout.assets.models import AssetStage
from spiritscout.assets.providers.fofa import FofaProvider, effective_fofa
from spiritscout.assets.service import AssetCandidateService
from spiritscout.blackboard.store import Blackboard
from spiritscout.config.settings import FofaConfig
from spiritscout.core.models import BlackboardLayer


class AssetCandidateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bb = Blackboard(":memory:")
        self.addCleanup(self.bb.close)
        self.service = AssetCandidateService(self.bb)

    def test_external_candidate_stays_out_of_asset_layer(self) -> None:
        candidate = self.service.upsert(
            "api.example.test", provider="fofa", source_ref="fofa:row-1",
            observation={"provider_claim": {"port": "443"}},
        )

        self.assertEqual(candidate.stage, AssetStage.EXTERNAL_CANDIDATE)
        self.assertEqual(self.bb.query(BlackboardLayer.ASSET), [])
        stored = self.bb.query(BlackboardLayer.RECON_CLUE)[0]["data"]
        self.assertEqual(stored["clue_type"], "asset_candidate")
        self.assertIn("unverified_penalty",
                      {item["name"] for item in stored["priority_components"]})

    def test_stage_and_sources_merge_with_explainable_priority(self) -> None:
        self.service.upsert("api.example.test", provider="crt.sh")
        candidate = self.service.upsert(
            "api.example.test", stage=AssetStage.DNS_VERIFIED,
            provider="dns_resolve", source_ref="inv-dns",
            observation={"ips": ["203.0.113.10"]}, unknowns=["待 HTTP 核验"],
        )

        self.assertEqual(candidate.stage, AssetStage.DNS_VERIFIED)
        self.assertEqual(candidate.providers, ["crt.sh", "dns_resolve"])
        self.assertGreater(candidate.priority_score, 0)
        names = {item["name"] for item in candidate.priority_components}
        self.assertIn("dns_evidence", names)
        self.assertNotIn("unverified_penalty", names)


class FofaProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_resource_setting_overrides_default_enablement(self) -> None:
        resources = type("Resources", (), {
            "settings": lambda self, kind: [
                {"id": "fofa", "data": {"enabled": True}, "version": 1}
            ] if kind == "asset_provider" else []
        })()

        selected = effective_fofa(FofaConfig(enabled=False), resources)

        self.assertTrue(selected.enabled)

    async def test_missing_credentials_skips_without_request(self) -> None:
        provider = FofaProvider(FofaConfig(enabled=True), credentials={})
        transport = httpx.MockTransport(
            lambda request: self.fail(f"unexpected request: {request.url}"))
        result = await provider.search_domain("example.test", transport=transport)

        self.assertEqual(result.status, "not_configured")

    async def test_query_is_bounded_and_results_remain_unverified(self) -> None:
        captured = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, json={
                "error": False, "size": 2,
                "results": [
                    ["https://api.example.test", "203.0.113.10", "443", "https",
                     "API", "api.example.test"],
                    ["https://evil.test", "198.51.100.2", "443", "https",
                     "Other", "evil.test"],
                ],
            })

        provider = FofaProvider(
            FofaConfig(enabled=True, page_size=25),
            credentials={"email": "fixture@example.test", "key": "secret"},
        )
        result = await provider.search_domain(
            "example.test", transport=httpx.MockTransport(handle))

        request = captured["request"]
        query = base64.b64decode(request.url.params["qbase64"]).decode()
        self.assertEqual(query, 'domain="example.test"')
        self.assertEqual(request.url.params["size"], "25")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.candidates[0].observation["verification_status"], "unverified")
        self.assertNotIn("secret", json.dumps(result.summary()))

    async def test_environment_credentials_override_vault_without_persisting(self) -> None:
        config = FofaConfig(enabled=True)
        with patch.dict(os.environ, {config.email_env: "env@example.test",
                                     config.api_key_env: "env-secret"}, clear=False), \
                patch("spiritscout.web.vault.get_key") as get_key:
            provider = FofaProvider(config)

        self.assertEqual(provider.credentials,
                         {"email": "env@example.test", "key": "env-secret"})
        get_key.assert_not_called()


class ProviderCacheTests(unittest.IsolatedAsyncioTestCase):
    def test_successful_candidates_are_reused_without_credentials(self) -> None:
        from spiritscout.assets.providers.base import ProviderCandidate, ProviderResult

        with tempfile.TemporaryDirectory() as tmp:
            cache = ProviderCache(Path(tmp) / "provider.sqlite3", ttl_seconds=60)
            cache.put("fofa:domain:example.test", ProviderResult(
                "fofa", "ok", candidates=[ProviderCandidate(
                    "api.example.test", source_ref="fofa:row-1",
                    observation={"verification_status": "unverified"},
                )], returned=1,
            ))
            result = cache.get("fofa:domain:example.test")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "cached")
        self.assertTrue(result.cached)
        self.assertEqual(result.candidates[0].value, "api.example.test")
        self.assertEqual(result.summary()["cached"], True)

    def test_failures_and_expired_entries_are_not_reused(self) -> None:
        from spiritscout.assets.providers.base import ProviderResult

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider.sqlite3"
            cache = ProviderCache(path, ttl_seconds=60)
            cache.put("failed", ProviderResult("fofa", "failed"))
            self.assertIsNone(cache.get("failed"))

            cache.put("expired", ProviderResult("fofa", "ok"))
            with sqlite3.connect(path) as db:
                db.execute("UPDATE provider_cache SET created=0 WHERE cache_key='expired'")
            self.assertIsNone(cache.get("expired"))

    async def test_disabled_provider_does_not_consume_cached_candidates(self) -> None:
        provider = FofaProvider(FofaConfig(enabled=False), credentials={})
        result = await provider.search_domain("example.test")

        self.assertEqual(result.status, "disabled")
        self.assertFalse(result.cached)


if __name__ == "__main__":
    unittest.main()
