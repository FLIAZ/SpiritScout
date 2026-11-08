"""流量旁路采集：真实本机 HTTP、隔离、脱敏、分页、失败与只读 API。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.core.models import Scope
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.builtin import http_probe
from spiritscout.tools.invocation import (
    ExecutionScope,
    InvocationContext,
    current_execution_scope,
    current_invocation,
)
from spiritscout.traffic import recorder
from spiritscout.web.traffic import mount_traffic


class TrafficTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.requests = []

        async def serve(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                               if line.lower().startswith(b"content-length:")), 0)
                body = await reader.readexactly(length) if length else b""
                self.requests.append((head, body))
                path = head.split(b" ")[1]
                status, extra, payload = "200 OK", "", b'{"result":"ok","token":"response-secret"}'
                if path == b"/redirect":
                    status, extra, payload = "302 Found", "Location: /final\r\n", b"not-read"
                if path == b"/bad":
                    status = "500 Error"
                if path == b"/large":
                    payload = b"a" * (recorder.BODY_LIMIT * 2)
                ctype = "text/plain" if path == b"/large" else "application/json"
                writer.write((f"HTTP/1.1 {status}\r\n{extra}Content-Type: {ctype}\r\n"
                              f"Set-Cookie: session=server-secret\r\nContent-Length: {len(payload)}\r\n"
                              "Connection: close\r\n\r\n").encode() + payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.temp.cleanup()

    async def fetch(self, path="/", **kwargs):
        return await http_probe(self.base + path, guard=ScopeGuard(Scope(local_analysis=True)),
                                workspace_root=str(self.root), **kwargs)

    async def test_real_request_redaction_and_context_without_mutating_request(self):
        invocation = current_invocation.set(InvocationContext("inv-fixture", "digest"))
        scope = current_execution_scope.set(ExecutionScope(agent_id="web-1", task_id="task-1", tool_call_id="call-1"))
        try:
            result = await self.fetch("/?token=query-secret&empty=", method="POST",
                headers={"Authorization": "Bearer header-secret", "Cookie": "session=cookie-secret",
                         "Content-Type": "Application/JSON"},
                data='{"password":"body-secret","nested":{"api_key":"nested-secret"},"input":"value"}')
        finally:
            current_invocation.reset(invocation)
            current_execution_scope.reset(scope)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.requests), 1)
        wire = b"".join(self.requests[0])
        self.assertIn(b"query-secret", wire)
        self.assertIn(b"header-secret", wire)
        self.assertIn(b"body-secret", wire)
        row = recorder.detail(self.root, 1)
        serialized = json.dumps(row)
        for secret in ("query-secret", "header-secret", "cookie-secret", "body-secret", "nested-secret", "response-secret", "server-secret"):
            self.assertNotIn(secret, serialized)
        self.assertIn("value", row["request_body"]["text"])
        self.assertEqual((row["agent_id"], row["task_id"], row["invocation_id"], row["tool_call_id"]),
                         ("web-1", "task-1", "inv-fixture", "call-1"))
        self.assertTrue(row["response_complete"])
        self.assertEqual(recorder.summary(self.root)["total"], 1)

    async def test_redirect_each_hop_once_and_capture_cap_does_not_change_download(self):
        self.assertTrue((await self.fetch("/redirect")).ok)
        rows = recorder.page(self.root)["items"]
        self.assertEqual([r["status"] for r in rows], [200, 302])
        self.assertFalse(recorder.detail(self.root, rows[1]["id"])["response_complete"])
        self.assertEqual(len(self.requests), 2)
        result = await self.fetch("/large")
        row = recorder.detail(self.root, 3)
        self.assertEqual(result.parsed["saved_body_bytes"], recorder.BODY_LIMIT * 2)
        self.assertEqual(len(row["response_body"]["text"]), recorder.BODY_LIMIT)
        self.assertTrue(row["response_body"]["truncated"])
        self.assertTrue(row["response_complete"])

    async def test_download_limit_and_redirect_limit_are_preserved(self):
        result = await self.fetch("/large", http_max_body_bytes=1024)
        self.assertTrue(result.parsed["body_truncated"])
        row = recorder.detail(self.root, 1)
        self.assertEqual(row["response_bytes"], 1024)
        self.assertFalse(row["response_complete"])
        result = await self.fetch("/redirect", max_redirects=0)
        self.assertFalse(result.ok)
        self.assertEqual([r["status"] for r in recorder.page(self.root)["items"]], [302, 200])

    async def test_concurrent_recording_pagination_and_literal_filter(self):
        await asyncio.gather(*(self.fetch(f"/item-{n}") for n in range(8)))
        await self.fetch("/bad")
        first = recorder.page(self.root, limit=3)
        await self.fetch("/new")
        second = recorder.page(self.root, before=first["next_before"], limit=3)
        self.assertFalse({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})
        self.assertEqual(recorder.summary(self.root)["total"], 10)
        self.assertEqual(recorder.summary(self.root)["errors"], 1)
        self.assertEqual(len(recorder.page(self.root, errors_only=True)["items"]), 1)
        self.assertEqual(len(recorder.page(self.root, query="item-3")["items"]), 1)
        self.assertEqual(recorder.page(self.root, query="%'")["items"], [])

    async def test_http_failure_and_recording_failure_do_not_retry(self):
        async def fail(*args, **kwargs):
            raise httpx.ConnectError("sensitive diagnostic")
        with patch("httpx.AsyncClient.send", fail):
            result = await self.fetch("/?token=secret")
        self.assertFalse(result.ok)
        row = recorder.detail(self.root, 1)
        self.assertEqual(row["error"], "ConnectError")
        self.assertIsNone(row["status"])
        self.assertNotIn("sensitive diagnostic", json.dumps(row))
        with patch.object(recorder, "_append", side_effect=OSError("disk full")), self.assertLogs(recorder._LOG, level="WARNING"):
            result = await self.fetch()
        self.assertTrue(result.ok)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(recorder.summary(self.root)["total"], 1)

    async def test_empty_isolation_readonly_and_symlinks(self):
        empty = self.root / "other"
        empty.mkdir()
        self.assertEqual(recorder.summary(empty)["total"], 0)
        self.assertEqual(recorder.page(empty)["items"], [])
        self.assertFalse((empty / "traffic").exists())
        await self.fetch()
        self.assertIsNone(recorder.detail(empty, 1))
        (empty / "traffic").symlink_to(self.root / "traffic", target_is_directory=True)
        with self.assertRaises(ValueError):
            recorder.page(empty)

    async def test_binary_and_oversize_structured_body(self):
        req = httpx.Request("POST", "https://test.invalid", content=b'\x00\xff')
        await recorder.record_http(str(self.root), req, None, started=time.perf_counter())
        row = recorder.detail(self.root, 1)
        self.assertEqual(row["request_body"]["encoding"], "base64")
        data = json.dumps({"password": "secret", "padding": "a" * recorder.BODY_LIMIT}).encode()
        req = httpx.Request("POST", "https://test.invalid", content=data, headers={"Content-Type": "application/json"})
        await recorder.record_http(str(self.root), req, None, started=time.perf_counter())
        row = recorder.detail(self.root, 2)
        self.assertNotIn("secret", row["request_body"]["text"])
        self.assertTrue(row["request_body"]["truncated"])


class TrafficAPITests(unittest.TestCase):
    def test_project_classification_validation_isolation_and_no_store(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = []
            for sid, project in (("a", "p1"), ("b", "deleted"), ("c", "")):
                root = Path(temp) / sid
                root.mkdir()
                sessions.append(SimpleNamespace(session_id=sid, project_id=project, workspace=root,
                    title=sid, status="completed", display_title=lambda sid=sid: sid))
            req = httpx.Request("GET", "https://test.invalid/path")
            asyncio.run(recorder.record_http(str(sessions[0].workspace), req, None, started=time.perf_counter(), error="ConnectError"))
            store = SimpleNamespace(projects=lambda: {"items": [{"project_id": "p1", "name": "项目一"}]})
            manager = SimpleNamespace(list=lambda: sessions, get=lambda sid: next((s for s in sessions if s.session_id == sid), None))
            app = FastAPI()
            mount_traffic(app, store, manager)
            with TestClient(app) as client:
                response = client.get("/api/traffic")
                self.assertEqual(response.headers["cache-control"], "no-store")
                data = response.json()
                self.assertEqual([(p["session_count"], p["total"]) for p in data["projects"]], [(3, 1), (1, 1), (2, 0)])
                self.assertEqual(client.get("/api/sessions/a/traffic").json()["summary"]["total"], 1)
                self.assertEqual(client.get("/api/sessions/b/traffic").json()["items"], [])
                self.assertEqual(client.get("/api/sessions/b/traffic/1").status_code, 404)
                self.assertEqual(client.get("/api/sessions/a/traffic/1").status_code, 200)
                self.assertEqual(client.get("/api/sessions/missing/traffic").status_code, 404)
                self.assertEqual(client.get("/api/sessions/a/traffic?limit=10000").status_code, 422)
                self.assertEqual(client.get("/api/sessions/a/traffic?before=0").status_code, 422)
                (sessions[1].workspace / "traffic").symlink_to(sessions[0].workspace / "traffic", target_is_directory=True)
                self.assertEqual(client.get("/api/sessions/b/traffic").status_code, 503)
                self.assertFalse(client.get("/api/traffic").json()["sessions"][1]["available"])

    def test_all_filters_combinations_pagination_and_fts_fallback(self):
        """HTTP 接口确实应用各条件，而非仅接收后静默忽略。"""
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = SimpleNamespace(session_id="filters", workspace=root)
            app = FastAPI()
            mount_traffic(app, SimpleNamespace(), SimpleNamespace(get=lambda sid: session if sid == "filters" else None))
            async def seed():
                for method, path, status, body in (
                    ("GET", "/ok", 200, {"message":"alpha 中文", "password":"private-filter-secret"}),
                    ("POST", "/missing", 404, {"message":"beta"}),
                    ("PATCH", "/bad", 500, {"message":"alpha"}),
                    ("GET", "/redirect", 302, {"message":"gamma"}),
                    ("DELETE", "/offline", None, {}),
                    ("POST", "/created", 201, {"message":"alpha"}),
                ):
                    request = httpx.Request(method, "https://fixture.invalid" + path)
                    response = httpx.Response(status, headers={"content-type":"application/json"}) if status else None
                    await recorder.record_http(str(root), request, response, started=time.perf_counter(),
                        content=json.dumps(body, ensure_ascii=False).encode(), complete=bool(status), error="" if status else "ConnectError")
            asyncio.run(seed())
            with TestClient(app) as client:
                endpoint = "/api/sessions/filters/traffic"
                cases = [
                    ({"query":"missing"}, [2]), ({"query":"%'"}, []),
                    ({"method":"post"}, [6,2]), ({"status":"200"}, [1]),
                    ({"status":"2xx"}, [6,1]), ({"status":"4xx"}, [2]),
                    ({"errors_only":"true"}, [5,3,2]),
                    ({"body":"alpha"}, [6,3,1]), ({"body":"中文"}, [1]),
                    ({"body":"private-filter-secret"}, []), ({"body":'" OR *'}, []),
                    ({"method":"POST","status":"2xx","body":"alpha"}, [6]),
                    ({"method":"POST","status":"200"}, []),
                    ({"status":"200","errors_only":"true"}, []),
                ]
                for filters, expected in cases:
                    with self.subTest(filters=filters):
                        result = client.get(endpoint, params=filters)
                        self.assertEqual(result.status_code, 200)
                        self.assertEqual([r["id"] for r in result.json()["items"]], expected)
                        self.assertIn("filters", result.json())
                ids, before = [], None
                for _ in range(3):
                    params = {"errors_only":"true","limit":1}
                    if before:
                        params["before"] = before
                    page = client.get(endpoint, params=params).json()
                    ids.extend(r["id"] for r in page["items"])
                    before = page["next_before"]
                self.assertEqual(ids, [5,3,2])
                self.assertFalse(page["has_more"])
                for params in ({"method":"GET OR"}, {"status":"600"}, {"status":"20"}, {"body":"x"*201}):
                    self.assertEqual(client.get(endpoint, params=params).status_code, 422)
                with sqlite3.connect(root / "traffic" / "records.db") as conn:
                    conn.execute("DROP TABLE IF EXISTS exchange_fts")
                self.assertEqual([r["id"] for r in client.get(endpoint, params={"body":"alpha"}).json()["items"]], [6,3,1])
