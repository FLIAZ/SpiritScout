"""企业资产 Provider 生成归属证据与 ICP 候选，不直接扩大测试范围。"""

from __future__ import annotations

import unittest

import httpx

from spiritscout.assets.providers.enterprise import AiqichaProvider, QichachaProvider
from spiritscout.config.settings import EnterpriseProviderConfig


class EnterpriseAssetProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_aiqicha_recurses_qualified_investments_and_branches(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            path, pid = request.url.path, request.url.params.get("pid", "")
            if path.endswith("advanceFilterAjax"):
                return httpx.Response(200, json={
                    "ddw": 0, "data": {"resultList": [
                        {"pid": "root", "entName": "Example Holdings", "regAddr": "Zhejiang"}
                    ]},
                })
            if path.endswith("basicAllDataAjax"):
                return httpx.Response(200, json={"data": {"basicData": {
                    "entName": {"root": "Example Holdings", "child": "Example Cloud",
                                "branch": "Example Branch"}.get(pid, pid),
                    "taxNo": "91330000FIXTURE", "regAddr": "Zhejiang Hangzhou",
                    "openStatus": "开业", "legalPerson": "Fixture",
                }}})
            if path.endswith("icpinfoAjax"):
                rows = {
                    "root": [{"siteName": "Root", "domain": ["www.example.test"],
                              "icpNo": "浙ICP备1"}],
                    "child": [{"siteName": "Cloud", "domain": ["api.example.test"],
                               "icpNo": "浙ICP备2"}],
                    "branch": [],
                }.get(pid, [])
                return httpx.Response(200, json={"data": {"list": rows}})
            if path.endswith("investajax"):
                rows = ([{"pid": "child", "entName": "Example Cloud", "regRate": "60%"},
                         {"pid": "minor", "entName": "Minor Co", "regRate": "20%"}]
                        if pid == "root" else [])
                return httpx.Response(200, json={"data": {"list": rows}})
            if path.endswith("branchajax"):
                rows = ([{"pid": "branch", "entName": "Example Branch"}]
                        if pid == "root" else [])
                return httpx.Response(200, json={"data": {"list": rows}})
            return httpx.Response(404)

        provider = AiqichaProvider(EnterpriseProviderConfig(
            enabled=True, base_url="https://aiqicha.baidu.com",
            cookie_env="TEST_AQC_COOKIE", vault_key_name="test:aqc",
        ), cookie="BAIDUID=fixture")
        result = await provider.search_organization(
            "Example Holdings", regions=["Zhejiang"], ownership_rule="控股比例不低于 50%",
            deep=2, transport=httpx.MockTransport(handle))

        self.assertEqual(result.status, "ok")
        self.assertEqual({item.value for item in result.candidates},
                         {"www.example.test", "api.example.test"})
        child = next(item for item in result.candidates if item.value == "api.example.test")
        evidence = child.observation["ownership_evidence"]
        self.assertEqual(evidence["relation_type"], "investment")
        self.assertEqual(evidence["ownership_percentage"], 60.0)
        self.assertEqual(evidence["verification_status"], "unverified")

    async def test_qichacha_extracts_only_explicit_domain_fields(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/web/search":
                return httpx.Response(200, text='{"KeyNo":"abc123","Name":"Example Co"}')
            return httpx.Response(200, text=(
                '{"Domain":"portal.example.test","Logo":"cdn.qcc.com/logo.png",'
                '"Noise":"noise.invalid.test"}'))

        provider = QichachaProvider(EnterpriseProviderConfig(
            enabled=True, base_url="https://www.qcc.com",
            cookie_env="TEST_QCC_COOKIE", vault_key_name="test:qcc",
        ), cookie="QCCSESSID=fixture")
        result = await provider.search_organization(
            "Example Co", transport=httpx.MockTransport(handle))

        self.assertEqual(result.status, "ok")
        self.assertEqual([item.value for item in result.candidates], ["portal.example.test"])
        self.assertEqual(result.candidates[0].observation["verification_status"], "unverified")

    async def test_login_redirect_is_reported_not_hidden_as_empty_success(self) -> None:
        provider = QichachaProvider(EnterpriseProviderConfig(
            enabled=True, base_url="https://www.qcc.com",
            cookie_env="TEST_QCC_COOKIE", vault_key_name="test:qcc",
        ), cookie="expired")
        result = await provider.search_organization(
            "Example", transport=httpx.MockTransport(
                lambda request: httpx.Response(302, headers={"location": "/user_login"})))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "credentials_rejected")
        self.assertEqual(result.candidates, [])


if __name__ == "__main__":
    unittest.main()
