from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from spiritscout.config.settings import SystemConfig, WebConfig, WorkspaceConfig
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.web.app import create_app

_CREDENTIAL_ENVS = (
    "SPIRITSCOUT_ADMIN_USERNAME",
    "SPIRITSCOUT_ADMIN_PASSWORD",
    "SPIRITSCOUT_SERVER_PASSWORD",
    "SPIRITSCOUT_ADMIN_SYNC",
)


@contextmanager
def env_only(**values: str):
    """只保留本用例显式给出的凭据变量，免受开发机环境变量污染。"""
    with patch.dict(os.environ, values, clear=False):
        for key in _CREDENTIAL_ENVS:
            if key not in values:
                os.environ.pop(key, None)
        yield


def _app(root: str, host: str = "127.0.0.1"):
    cfg = SystemConfig(
        workspace=WorkspaceConfig(root=root),
        web=WebConfig(host=host),
    )
    return create_app(cfg, [])


def _login(client: TestClient, username: str = "spiritscout",
           password: str = "test-secret"):
    return client.post("/api/auth/login",
                       json={"username": username, "password": password})


class WebAuthTests(unittest.TestCase):
    def test_remote_bind_without_admin_credentials_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only():
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=tmp),
                web=WebConfig(host="0.0.0.0"),
            )
            with self.assertRaises(ConfigurationError) as ctx:
                create_app(cfg, [])
            self.assertIn("SPIRITSCOUT_ADMIN_PASSWORD", str(ctx.exception))

    def test_configured_password_redirects_browser_to_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_SERVER_PASSWORD="test-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                response = client.get("/")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"], "/login")
                self.assertNotIn("www-authenticate", response.headers)
                authed = client.get(
                    "/", auth=("spiritscout", "test-secret"))
                self.assertEqual(authed.status_code, 200)

    def test_authenticated_cross_site_write_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_SERVER_PASSWORD="test-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                basic = client.post(
                    "/api/sessions/missing/stop",
                    auth=("spiritscout", "test-secret"),
                    headers={"Origin": "https://attacker.example"},
                )
                self.assertEqual(basic.status_code, 403)

                self.assertEqual(_login(client).status_code, 200)
                cookie = client.post(
                    "/api/sessions/missing/stop",
                    headers={"Origin": "https://attacker.example"},
                )
                self.assertEqual(cookie.status_code, 403)

    def test_loopback_without_password_stays_open_but_blocks_cross_site(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only():
            with TestClient(_app(tmp),
                            base_url="http://127.0.0.1",
                            follow_redirects=False) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/api/sessions").status_code, 200)
                response = client.post(
                    "/api/sessions/missing/stop",
                    headers={"Origin": "https://attacker.example"},
                )
                self.assertEqual(response.status_code, 403)

    def test_non_loopback_host_is_rejected_without_password(self) -> None:
        """DNS 重绑定防护：恶意域名解析到 127.0.0.1 时 Host 是攻击者域名。"""
        with tempfile.TemporaryDirectory() as tmp, env_only():
            with TestClient(_app(tmp), base_url="http://evil.example",
                            follow_redirects=False) as client:
                self.assertEqual(client.get("/").status_code, 403)
                self.assertEqual(client.get("/api/sessions").status_code, 403)
                self.assertEqual(client.get("/login").status_code, 403)

    def test_password_mode_gates_credentials_not_hostname(self) -> None:
        """设了管理员后按凭据鉴权：反代/远程访问的 Host 不再受限。"""
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_SERVER_PASSWORD="test-secret"):
            with TestClient(_app(tmp), base_url="http://console.example",
                            follow_redirects=False) as client:
                self.assertEqual(client.get("/").status_code, 302)
                authed = client.get(
                    "/", auth=("spiritscout", "test-secret"))
                self.assertEqual(authed.status_code, 200)
                self.assertEqual(_login(client).status_code, 200)
                self.assertEqual(client.get("/").status_code, 200)

    # ------------------------------------------------------------ 登录面板

    def test_login_page_and_static_assets_are_public(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_PASSWORD="test-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                page = client.get("/login")
                self.assertEqual(page.status_code, 200)
                self.assertIn("login-form", page.text)
                self.assertIn("/static/css/login.css", page.text)
                for path in ("/static/css/base.css", "/static/css/login.css",
                             "/static/js/login.js"):
                    self.assertEqual(client.get(path).status_code, 200)

    def test_unauthenticated_api_returns_json_401_without_challenge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_PASSWORD="test-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                response = client.get("/api/sessions")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["detail"], "需要管理员登录")
                self.assertNotIn("www-authenticate", response.headers)

    def test_login_sets_httponly_cookie_and_unlocks_console(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_PASSWORD="test-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                response = _login(client)
                self.assertEqual(response.status_code, 200)
                cookie = response.headers["set-cookie"].lower()
                self.assertIn("httponly", cookie)
                self.assertIn("samesite=lax", cookie)
                self.assertIn("path=/", cookie)
                self.assertNotIn("secure", cookie)
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/api/sessions").status_code, 200)

    def test_secure_cookie_behind_https(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_PASSWORD="test-secret"):
            with TestClient(_app(tmp), base_url="https://console.example",
                            follow_redirects=False) as client:
                response = _login(client)
                self.assertIn("secure", response.headers["set-cookie"].lower())

            with TestClient(_app(tmp), follow_redirects=False) as client:
                response = _login(
                    client, password="test-secret")
                self.assertNotIn("secure", response.headers["set-cookie"].lower())
                forwarded = client.post(
                    "/api/auth/login",
                    json={"username": "spiritscout",
                          "password": "test-secret"},
                    headers={"X-Forwarded-Proto": "https"},
                )
                self.assertIn(
                    "secure", forwarded.headers["set-cookie"].lower())

    def test_wrong_credentials_are_rejected_without_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_PASSWORD="test-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                for username, password in (("spiritscout", "wrong"),
                                           ("nobody", "test-secret")):
                    response = _login(client, username, password)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json()["detail"],
                                     "用户名或密码错误")
                    self.assertNotIn("set-cookie", response.headers)

    def test_tampered_cookie_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_PASSWORD="test-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                self.assertEqual(_login(client).status_code, 200)
            with TestClient(_app(tmp), follow_redirects=False) as client:
                client.cookies.set("ss_session", "v1.abc.def")
                self.assertEqual(client.get("/").status_code, 302)
                self.assertEqual(client.get("/api/sessions").status_code, 401)

    def test_logout_clears_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_PASSWORD="test-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                self.assertEqual(_login(client).status_code, 200)
                self.assertEqual(
                    client.post("/api/auth/logout").status_code, 204)
                self.assertEqual(client.get("/").status_code, 302)

    def test_login_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_PASSWORD="test-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                for _ in range(5):
                    self.assertEqual(
                        _login(client, password="bad").status_code, 401)
                blocked = _login(client)
                self.assertEqual(blocked.status_code, 429)
                self.assertIn("retry-after", blocked.headers)

    # ------------------------------------------------------------ 凭据库与恢复

    def test_admin_env_names_and_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_USERNAME="admin",
                SPIRITSCOUT_ADMIN_PASSWORD="new-secret",
                SPIRITSCOUT_SERVER_PASSWORD="old-secret"):
            with TestClient(_app(tmp), follow_redirects=False) as client:
                self.assertEqual(
                    _login(client, "admin", "new-secret").status_code, 200)
                self.assertEqual(
                    _login(client, "spiritscout", "old-secret").status_code,
                    401)

    def test_password_is_hashed_in_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_PASSWORD="plain-text-secret"):
            with TestClient(_app(tmp)):
                pass
            db = Path(tmp) / "admin.db"
            self.assertTrue(db.is_file())
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT username, password_hash FROM admins").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "spiritscout")
            self.assertTrue(rows[0][1].startswith("scrypt$"))
            self.assertNotIn("plain-text-secret", rows[0][1])

    def test_credentials_persist_after_env_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with env_only(SPIRITSCOUT_ADMIN_PASSWORD="persist-secret"):
                with TestClient(_app(tmp),
                                follow_redirects=False) as first:
                    self.assertEqual(
                        _login(first, password="persist-secret").status_code,
                        200)
                    token = first.cookies.get("ss_session")
                    self.assertTrue(token)

            with env_only():
                with TestClient(_app(tmp),
                                follow_redirects=False) as second:
                    second.cookies.set("ss_session", token)
                    self.assertEqual(second.get("/").status_code, 200)
                    self.assertEqual(
                        _login(second, password="persist-secret").status_code,
                        200)

    def test_env_password_rotation_invalidates_old_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with env_only(SPIRITSCOUT_ADMIN_PASSWORD="old-secret"):
                with TestClient(_app(tmp),
                                follow_redirects=False) as first:
                    self.assertEqual(
                        _login(first, password="old-secret").status_code, 200)
                    token = first.cookies.get("ss_session")

            with env_only(SPIRITSCOUT_ADMIN_PASSWORD="new-secret"):
                with TestClient(_app(tmp),
                                follow_redirects=False) as second:
                    second.cookies.set("ss_session", token)
                    self.assertEqual(second.get("/").status_code, 302)
                    self.assertEqual(
                        _login(second, password="old-secret").status_code,
                        401)
                    self.assertEqual(
                        _login(second, password="new-secret").status_code,
                        200)

    def test_invalid_env_username_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only(
                SPIRITSCOUT_ADMIN_USERNAME="bad:name",
                SPIRITSCOUT_ADMIN_PASSWORD="secret"):
            with self.assertRaises(ConfigurationError):
                _app(tmp)

    def test_login_page_redirects_when_login_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, env_only():
            with TestClient(_app(tmp), base_url="http://127.0.0.1",
                            follow_redirects=False) as client:
                response = client.get("/login")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["location"], "/")


if __name__ == "__main__":
    unittest.main()
