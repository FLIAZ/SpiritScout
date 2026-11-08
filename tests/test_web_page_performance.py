"""页面资源传输回归：静态压缩与实时接口彼此隔离，不调用模型。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from spiritscout import __version__
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session


class PageDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict("os.environ", {
            "SPIRITSCOUT_ADMIN_USERNAME": "", "SPIRITSCOUT_ADMIN_PASSWORD": "",
            "SPIRITSCOUT_SERVER_PASSWORD": "", "SPIRITSCOUT_ADMIN_SYNC": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.cfg = SystemConfig(workspace=WorkspaceConfig(root=temporary.name))
        self.app = create_app(self.cfg, [])
        # 不启动后台服务；这里仅验证 HTTP 路由和实际中间件。
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.addCleanup(self.client.close)

    def test_static_compression_preserves_content_and_revalidation(self) -> None:
        for path in ("/static/js/core.js?v=fixture", "/static/css/views.css?v=fixture"):
            with self.subTest(path=path):
                plain = self.client.get(path, headers={"Accept-Encoding": "identity"})
                compressed = self.client.get(path, headers={"Accept-Encoding": "gzip"})
                self.assertEqual(compressed.status_code, 200)
                self.assertEqual(compressed.headers["content-encoding"], "gzip")
                self.assertIn("accept-encoding", compressed.headers["vary"].lower())
                self.assertNotIn("content-encoding", plain.headers)
                self.assertEqual(compressed.content, plain.content)
                self.assertEqual(compressed.headers["x-content-type-options"], "nosniff")
                cached = self.client.get(path, headers={
                    "Accept-Encoding": "gzip", "If-None-Match": compressed.headers["etag"],
                })
                self.assertEqual(cached.status_code, 304)
                self.assertEqual(cached.content, b"")
                self.assertEqual(self.client.head(path).content, b"")
        self.assertEqual(self.client.get("/static/missing.js").status_code, 404)

    def test_home_includes_runtime_version_in_both_deployment_modes(self) -> None:
        for light in (False, True):
            with self.subTest(light=light):
                self.cfg.gateway.lightweight_local = light
                page = self.client.get("/")
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.headers["cache-control"], "no-store")
                self.assertIn(f'name="spiritscout-version" content="{__version__}"', page.text)
                self.assertNotIn("__SPIRITSCOUT_VERSION__", page.text)
                self.assertEqual("window.__SPIRITSCOUT_LIGHT__=true" in page.text, light)

    def test_api_and_sse_are_not_compressed(self) -> None:
        session = Session("sess-perf", "ctf", "fixture", self.root, status="completed")
        session.audit_path.write_text('{"type":"master.reply","detail":{"text":"fixture"}}\n')
        self.app.state.manager._sessions[session.session_id] = session
        response = self.client.get("/api/sessions", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("content-encoding", response.headers)
        stream = self.client.get(f"/api/sessions/{session.session_id}/events?cursor=bytes",
                                 headers={"Accept-Encoding": "gzip"})
        self.assertEqual(stream.status_code, 200)
        self.assertTrue(stream.headers["content-type"].startswith("text/event-stream"))
        self.assertNotIn("content-encoding", stream.headers)
        self.assertEqual(stream.headers["x-accel-buffering"], "no")
        self.assertIn("event: audit", stream.text)
        self.assertIn("event: done", stream.text)
