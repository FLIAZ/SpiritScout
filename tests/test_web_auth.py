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
            with TestClient(create_app(cfg, []),
                            base_url="http://127.0.0.1") as client:
                response = client.post(
                    "/api/sessions/missing/stop",
                    headers={"Origin": "https://attacker.example"},
                )
                self.assertEqual(response.status_code, 403)

    def test_non_loopback_host_is_rejected_without_password(self) -> None:
        """DNS 重绑定防护：恶意域名解析到 127.0.0.1 时 Host 是攻击者域名。

        同源自洽检查（Origin 由 Host 派生）分辨不出重绑定；无密码的本机
        模式只接受回环 Host——远程访问应设置管理密码。
        """
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {}, clear=False):
            os.environ.pop("SPIRITSCOUT_SERVER_PASSWORD", None)
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=tmp),
                web=WebConfig(host="127.0.0.1"),
            )
            with TestClient(create_app(cfg, []),
                            base_url="http://evil.example") as client:
                self.assertEqual(client.get("/").status_code, 403)
                self.assertEqual(client.get("/api/sessions").status_code, 403)

    def test_password_mode_gates_credentials_not_hostname(self) -> None:
        """设了管理密码后按凭据鉴权：反代/远程访问的 Host 不再受限。"""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"SPIRITSCOUT_SERVER_PASSWORD": "test-secret"}):
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=tmp),
                web=WebConfig(host="127.0.0.1"),
            )
            with TestClient(create_app(cfg, []),
                            base_url="http://console.example") as client:
                self.assertEqual(client.get("/").status_code, 401)
                authed = client.get("/", auth=("spiritscout", "test-secret"))
                self.assertEqual(authed.status_code, 200)


if __name__ == "__main__":
    unittest.main()
