from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from spiritscout.config.settings import SystemConfig, WebConfig, WorkspaceConfig
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.web.app import create_app


class WebAuthTests(unittest.TestCase):
    def test_remote_bind_without_password_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {}, clear=False):
            os.environ.pop("SPIRITSCOUT_SERVER_PASSWORD", None)
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=tmp),
                web=WebConfig(host="0.0.0.0"),
            )
            with self.assertRaises(ConfigurationError):
                create_app(cfg, [])

    def test_configured_password_protects_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"SPIRITSCOUT_SERVER_PASSWORD": "test-secret"}):
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=tmp),
                web=WebConfig(host="127.0.0.1"),
            )
            with TestClient(create_app(cfg, [])) as client:
                self.assertEqual(client.get("/").status_code, 401)
                response = client.get(
                    "/", auth=("spiritscout", "test-secret"))
                self.assertEqual(response.status_code, 200)

    def test_authenticated_cross_site_write_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"SPIRITSCOUT_SERVER_PASSWORD": "test-secret"}):
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=tmp),
                web=WebConfig(host="127.0.0.1"),
            )
            with TestClient(create_app(cfg, [])) as client:
                response = client.post(
                    "/api/sessions/missing/stop",
                    auth=("spiritscout", "test-secret"),
                    headers={"Origin": "https://attacker.example"},
                )
                self.assertEqual(response.status_code, 403)

    def test_loopback_without_password_still_rejects_cross_site_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {}, clear=False):
            os.environ.pop("SPIRITSCOUT_SERVER_PASSWORD", None)
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=tmp),
                web=WebConfig(host="127.0.0.1"),
            )
            with TestClient(create_app(cfg, [])) as client:
                response = client.post(
                    "/api/sessions/missing/stop",
                    headers={"Origin": "https://attacker.example"},
                )
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
