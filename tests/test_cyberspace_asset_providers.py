"""空间测绘 Provider 采用统一候选契约。"""

from __future__ import annotations

import base64
import unittest

import httpx

from spiritscout.assets.providers.cyberspace import (
    HunterProvider,
    QuakeProvider,
    ZoomEyeProvider,
)
from spiritscout.config.settings import AssetApiProviderConfig


def config(url: str) -> AssetApiProviderConfig:
    return AssetApiProviderConfig(
        enabled=True, api_url=url, api_key_env="TEST_ASSET_KEY",
        vault_key_name="test:asset", page_size=25,
    )


class CyberspaceAssetProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_hunter_uses_bounded_domain_query(self) -> None:
        captured = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, json={"code": 200, "data": {
                "total": 1, "arr": [{"domain": "api.example.test", "ip": "203.0.113.1"}],
            }})

        result = await HunterProvider(config(
            "https://hunter.qianxin.com/openApi/search"), api_key="fixture").search_domain(
                "example.test", transport=httpx.MockTransport(handle))

        query = base64.urlsafe_b64decode(captured["request"].url.params["search"]).decode()
        self.assertEqual(query, 'domain.suffix="example.test"')
        self.assertEqual(result.candidates[0].value, "api.example.test")

    async def test_quake_and_zoomeye_map_results_to_unverified_candidates(self) -> None:
        quake = await QuakeProvider(config(
            "https://quake.360.net/api/v3/search/quake_service"), api_key="fixture").search_domain(
                "example.test", transport=httpx.MockTransport(lambda request: httpx.Response(
                    200, json={"code": 0, "data": [{
                        "ip": "203.0.113.2", "port": 443,
                        "service": {"name": "https", "http": {"host": "web.example.test"}},
                    }]})))
        zoomeye = await ZoomEyeProvider(config(
            "https://api.zoomeye.hk/host/search"), api_key="fixture").search_domain(
                "example.test", transport=httpx.MockTransport(lambda request: httpx.Response(
                    200, json={"total": 1, "matches": [{
                        "ip": "203.0.113.3", "portinfo": {"hostname": "vpn.example.test"},
                    }]})))

        self.assertEqual(quake.candidates[0].value, "web.example.test")
        self.assertEqual(zoomeye.candidates[0].value, "vpn.example.test")
        self.assertEqual(quake.candidates[0].observation["verification_status"], "unverified")
        self.assertEqual(zoomeye.candidates[0].observation["verification_status"], "unverified")


if __name__ == "__main__":
    unittest.main()
