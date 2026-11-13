"""FOFA 资源接口只暴露配置状态，凭据加密保存且不回显。"""

from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.web.app import create_app


class FofaSettingsTests(unittest.TestCase):
    def test_save_restart_and_clear_never_echo_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            endpoint = "/api/resources/assets/fofa"
            secret = "fofa-secret-fixture"
            email = "fixture@example.test"
            app = create_app(cfg, [])
            with TestClient(app, base_url="http://127.0.0.1") as client:
                initial = client.get(endpoint).json()
                self.assertFalse(initial["key_configured"])
                response = client.put(endpoint, json={
                    "email": email, "api_key_value": secret,
                    "enabled": True, "expected_version": 0,
                })
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["stored_credentials"])
                self.assertNotIn(secret, response.text)
                self.assertNotIn(email, response.text)
                stale = client.put(endpoint, json={
                    "email": email, "api_key_value": secret,
                    "enabled": True, "expected_version": 0,
                })
                self.assertEqual(stale.status_code, 409)
                self.assertNotIn(secret, stale.text)
            restarted = create_app(cfg, [])
            with TestClient(restarted, base_url="http://127.0.0.1") as client:
                view = client.get(endpoint).json()
                self.assertTrue(view["key_configured"])
                cleared = client.put(endpoint, json={
                    "clear_credentials": True, "enabled": False,
                    "expected_version": 1,
                })
                self.assertEqual(cleared.status_code, 200)
                self.assertFalse(cleared.json()["key_configured"])

    def test_partial_or_unknown_payload_never_echoes_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(SystemConfig(workspace=WorkspaceConfig(root=tmp)), [])
            with TestClient(app, base_url="http://127.0.0.1") as client:
                for payload in (
                    {"email": "fixture@example.test"},
                    {"email": "fixture@example.test", "api_key_value": "never-echo",
                     "unexpected": "never-echo"},
                ):
                    response = client.put("/api/resources/assets/fofa", json=payload)
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn("never-echo", response.text)


if __name__ == "__main__":
    unittest.main()
