"""统一资产 Provider 配置只暴露状态，API Key 与 Cookie 均留在 Vault。"""

from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.web import vault
from spiritscout.web.app import create_app


class AssetProviderSettingsTests(unittest.TestCase):
    def test_provider_catalog_and_native_enterprise_cookie_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            cookie = "QCCSESSID=secret-cookie-fixture"
            app = create_app(cfg, [])
            with TestClient(app, base_url="http://127.0.0.1") as client:
                catalog = client.get("/api/resources/assets/providers").json()["providers"]
                self.assertEqual(
                    {item["provider"] for item in catalog},
                    {"fofa", "hunter", "quake", "zoomeye", "aiqicha", "qichacha"},
                )
                qichacha = next(item for item in catalog if item["provider"] == "qichacha")
                self.assertFalse(qichacha["key_configured"])
                saved = client.put("/api/resources/assets/providers/qichacha", json={
                    "enabled": True, "cookie_value": cookie,
                    "expected_version": qichacha["version"],
                })
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertTrue(saved.json()["key_configured"])
                self.assertNotIn(cookie, saved.text)
                self.assertNotIn("QCCSESSID", saved.text)
                stale = client.put("/api/resources/assets/providers/qichacha", json={
                    "enabled": True, "cookie_value": "QCCSESSID=stale-overwrite",
                    "expected_version": 0,
                })
                self.assertEqual(stale.status_code, 409)
                self.assertNotIn("stale-overwrite", stale.text)
                self.assertEqual(vault.get_key("asset-provider:qichacha"), cookie)

            restarted = create_app(cfg, [])
            with TestClient(restarted, base_url="http://127.0.0.1") as client:
                view = client.get("/api/resources/assets/providers/qichacha").json()
                self.assertTrue(view["key_configured"])
                cleared = client.put("/api/resources/assets/providers/qichacha", json={
                    "enabled": False, "clear_credentials": True,
                    "expected_version": view["version"],
                })
                self.assertEqual(cleared.status_code, 200, cleared.text)
                self.assertFalse(cleared.json()["key_configured"])

    def test_provider_credential_types_are_not_interchangeable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(SystemConfig(workspace=WorkspaceConfig(root=tmp)), [])
            with TestClient(app, base_url="http://127.0.0.1") as client:
                wrong_cookie = client.put("/api/resources/assets/providers/hunter", json={
                    "cookie_value": "never-echo-cookie", "expected_version": 0,
                })
                self.assertEqual(wrong_cookie.status_code, 422)
                self.assertNotIn("never-echo-cookie", wrong_cookie.text)
                wrong_key = client.put("/api/resources/assets/providers/aiqicha", json={
                    "api_key_value": "never-echo-key", "expected_version": 0,
                })
                self.assertEqual(wrong_key.status_code, 422)
                self.assertNotIn("never-echo-key", wrong_key.text)


if __name__ == "__main__":
    unittest.main()
