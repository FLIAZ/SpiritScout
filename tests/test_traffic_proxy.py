"""真实 Linux Go 代理、本机 TLS/HTTP/WS、沙箱、隔离及证据读取。"""
from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import ipaddress
import json
import os
import ssl
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.tools.invocation import (
    ExecutionScope,
    InvocationContext,
    current_execution_scope,
    current_invocation,
)
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.traffic import recorder
from spiritscout.traffic.proxy import (
    ProxyManager,
    executable,
    raw_evidence,
    tool_environment,
)
from spiritscout.traffic.tools import traffic_body_read, traffic_get, traffic_search
from spiritscout.web.traffic import mount_traffic


def tls_fixture(root: Path) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SpiritScout local test CA")])
    now = datetime.now(timezone.utc)
    ca = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(
        x509.random_serial_number()).not_valid_before(now-timedelta(hours=1)).not_valid_after(now+timedelta(days=1)).add_extension(
        x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])).issuer_name(
        name).public_key(leaf_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(
        now-timedelta(hours=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
        x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False).sign(key, hashes.SHA256())
    (root / "origin-ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (root / "origin-cert.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (root / "origin-key.pem").write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return root / "origin-ca.pem"


@unittest.skipUnless(executable(), "需要先运行 scripts/build-traffic-proxy.sh，验证真实 Go 代理")
class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = {}
        for sid in ("a", "b"):
            path = self.root / sid
            path.mkdir()
            self.sessions[sid] = SimpleNamespace(workspace=path)
        self.manager = ProxyManager(SimpleNamespace(get=self.sessions.get))
        self.manager.start_supervisor()
        self.requests = []
        self.large = b"large-evidence\n" * 40000
        self.ca = tls_fixture(self.root)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(self.root / "origin-cert.pem", self.root / "origin-key.pem")
        async def serve(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")), 0)
                body = await reader.readexactly(length) if length else b""
                self.requests.append((head, body))
                path = head.split(b" ")[1].split(b"?", 1)[0]
                payload, status, extra = b'{"message":"local proxy evidence","password":"response-secret"}', "200 OK", ""
                content_type = "application/json"
                if path == b"/redirect":
                    status, extra, payload = "302 Found", "Location: /final\r\n", b""
                if path == b"/large":
                    payload, content_type = self.large, "text/plain"
                if path == b"/gzip":
                    payload, extra = gzip.compress(payload), "Content-Encoding: gzip\r\n"
                writer.write((f"HTTP/1.1 {status}\r\nContent-Length: {len(payload)}\r\nContent-Type: {content_type}\r\n"
                              f"Set-Cookie: fixture=secret\r\n{extra}Connection: close\r\n\r\n").encode() + payload)
                await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
        self.http = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.https = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=server_ctx)
        self.http_url = f"http://127.0.0.1:{self.http.sockets[0].getsockname()[1]}"
        self.https_url = f"https://127.0.0.1:{self.https.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await self.manager.aclose()
        self.http.close()
        self.https.close()
        await self.http.wait_closed()
        await self.https.wait_closed()
        self.temp.cleanup()

    async def enable(self, sid="a", trust_origin=False):
        if trust_origin:
            with patch.dict(os.environ, {"SSL_CERT_FILE": str(self.ca)}):
                await self.manager.start(sid)
        else:
            await self.manager.start(sid)
        return await self.manager.sessions[sid].environment()

    async def wait_records(self, count, sid="a"):
        for _ in range(100):
            if recorder.summary(self.sessions[sid].workspace)["total"] >= count:
                return recorder.page(self.sessions[sid].workspace)["items"]
            await asyncio.sleep(0.02)
        self.fail(f"代理证据未落盘：{self.manager.status(sid)}")

    async def test_http_auth_context_redaction_and_raw_body(self):
        await self.manager.start("a")
        token = current_invocation.set(InvocationContext("inv-proxy-fixture", "digest"))
        scope = current_execution_scope.set(ExecutionScope(agent_id="web-fixture", task_id="task-fixture", tool_call_id="call-fixture"))
        try:
            env = await tool_environment(self.sessions["a"].workspace)
        finally:
            current_execution_scope.reset(scope)
            current_invocation.reset(token)
        async with httpx.AsyncClient(proxy=env["HTTP_PROXY"], trust_env=False) as client:
            result = await client.post(self.http_url + "/submit?token=query-secret", content=b'{"password":"request-secret","input":"test"}',
                headers={"Authorization": "Bearer origin-secret", "Content-Type": "application/json"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(b"proxy-authorization", self.requests[0][0].lower())
        self.assertIn(b"Bearer origin-secret", self.requests[0][0])
        rows = await self.wait_records(1)
        row = recorder.detail(self.sessions["a"].workspace, rows[0]["id"])
        self.assertEqual((row["agent_id"], row["task_id"], row["invocation_id"]), ("web-fixture", "task-fixture", "inv-proxy-fixture"))
        self.assertNotIn("request-secret", json.dumps(row))
        self.assertNotIn("response-secret", json.dumps(row))
        self.assertNotIn("origin-secret", json.dumps(row))
        body, metadata = raw_evidence(self.sessions["a"].workspace, row["id"], "response")
        self.assertIn(b"response-secret", body.read_bytes())
        self.assertEqual(hashlib.sha256(body.read_bytes()).hexdigest(), metadata["response"]["sha256"])

    async def test_https_verification_with_session_ca_and_origin_ca(self):
        env = await self.enable(trust_origin=True)
        context = ssl.create_default_context(cafile=env["SSL_CERT_FILE"])
        async with httpx.AsyncClient(proxy=env["HTTPS_PROXY"], verify=context, trust_env=False) as client:
            response = await client.get(self.https_url + "/secure")
        self.assertEqual(response.status_code, 200)
        rows = await self.wait_records(1)
        self.assertTrue(rows[0]["url"].startswith("https://"))
        self.assertEqual(self.manager.status("a")["error"], "")
        self.assertFalse(list(self.manager.sessions["a"].root.glob("*key*")), "代理 CA 私钥不应落盘")

    async def test_untrusted_origin_does_not_disable_certificate_verification(self):
        env = await self.enable()
        context = ssl.create_default_context(cafile=env["SSL_CERT_FILE"])
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        def expected_tls_rejection(loop, details):
            if not isinstance(details.get("exception"), ssl.SSLError):
                (previous or (lambda loop, details: loop.default_exception_handler(details)))(loop, details)
        loop.set_exception_handler(expected_tls_rejection)
        try:
            async with httpx.AsyncClient(proxy=env["HTTPS_PROXY"], verify=context, trust_env=False) as client:
                response = await client.get(self.https_url + "/secure")
        finally:
            loop.set_exception_handler(previous)
        self.assertEqual(response.status_code, 502)
        rows = await self.wait_records(1)
        self.assertEqual(rows[0]["error"], "UpstreamRequestError")
        self.assertEqual(self.requests, [])

    async def test_large_response_blob_dedup_and_gzip_preview(self):
        env = await self.enable()
        async with httpx.AsyncClient(proxy=env["HTTP_PROXY"], trust_env=False) as client:
            for _ in range(2):
                response = await client.get(self.http_url + "/large")
                self.assertEqual(response.content, self.large)
            response = await client.get(self.http_url + "/gzip")
            self.assertIn("local proxy evidence", response.text)
        rows = await self.wait_records(3)
        root = self.sessions["a"].workspace
        first, _ = raw_evidence(root, rows[1]["id"], "response")
        second, _ = raw_evidence(root, rows[2]["id"], "response")
        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), self.large)
        gzip_row = recorder.detail(root, rows[0]["id"])
        self.assertIn("local proxy evidence", gzip_row["response_body"]["text"])
        self.assertNotIn("response-secret", gzip_row["response_body"]["text"])

    async def test_redirects_remain_client_controlled_and_unauthenticated_proxy_rejected(self):
        env = await self.enable()
        async with httpx.AsyncClient(proxy=env["HTTP_PROXY"], follow_redirects=True, trust_env=False) as client:
            self.assertEqual((await client.get(self.http_url + "/redirect")).status_code, 200)
        rows = await self.wait_records(2)
        self.assertEqual(sorted(r["status"] for r in rows), [200, 302])
        async with httpx.AsyncClient(proxy=self.manager.status("a")["address"], trust_env=False) as client:
            self.assertEqual((await client.get(self.http_url + "/private")).status_code, 407)
        self.assertEqual(len(self.requests), 2)

    async def test_two_session_isolation_stop_and_restart(self):
        a, b = await self.enable("a"), await self.enable("b")
        self.assertNotEqual(a["HTTP_PROXY"], b["HTTP_PROXY"])
        async with httpx.AsyncClient(proxy=a["HTTP_PROXY"], trust_env=False) as client:
            await client.get(self.http_url + "/a")
        await self.wait_records(1)
        self.assertEqual(recorder.summary(self.sessions["b"].workspace)["total"], 0)
        old_pid = self.manager.sessions["a"].proc.pid
        await self.manager.stop("a")
        self.assertEqual(await tool_environment(self.sessions["a"].workspace), {})
        self.assertEqual(self.manager.status("a")["status"], "stopped")
        with self.assertRaises(ProcessLookupError):
            os.kill(old_pid, 0)
        self.assertEqual((await self.manager.start("a"))["status"], "running")

    async def test_parent_control_pipe_eof_stops_proxy(self):
        await self.enable()
        proxy = self.manager.sessions["a"]
        proxy.proc.stdin.close()
        await asyncio.wait_for(asyncio.shield(proxy.task), timeout=8)
        self.assertEqual(proxy.proc.returncode, 0)
        self.assertEqual(self.manager.status("a")["status"], "stopped")
        self.assertEqual(await tool_environment(self.sessions["a"].workspace), {})

    async def test_upstream_http_proxy_chain_preserves_target_request(self):
        upstream = await self.enable("b")
        await self.manager.start("a", upstream=upstream["HTTP_PROXY"])
        env = await self.manager.sessions["a"].environment()
        async with httpx.AsyncClient(proxy=env["HTTP_PROXY"], trust_env=False) as client:
            result = await client.post(self.http_url + "/through-chain", content=b"once")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][1], b"once")
        self.assertNotIn(b"proxy-authorization", self.requests[0][0].lower())
        await self.wait_records(1, "a")
        await self.wait_records(1, "b")

    async def test_local_search_read_tools_do_not_request_target(self):
        env = await self.enable()
        async with httpx.AsyncClient(proxy=env["HTTP_PROXY"], trust_env=False) as client:
            await client.get(self.http_url + "/one")
        rows = await self.wait_records(1)
        workspace = str(self.sessions["a"].workspace)
        found = await traffic_search(workspace_root=workspace, body="proxy evidence", method="GET", status="2xx")
        self.assertTrue(found.ok, found.error)
        self.assertEqual(len(json.loads(found.stdout)["items"]), 1)
        got = await traffic_get(workspace_root=workspace, record_id=rows[0]["id"])
        self.assertTrue(got.ok)
        raw = await traffic_body_read(workspace_root=workspace, record_id=rows[0]["id"], limit=10)
        self.assertTrue(raw.ok)
        self.assertTrue(json.loads(raw.stdout)["has_more"])
        self.assertEqual(len(self.requests), 1)
        wrong = await traffic_get(workspace_root=str(self.sessions["b"].workspace), record_id=rows[0]["id"])
        self.assertFalse(wrong.ok)

    async def test_tool_cannot_override_workspace_and_literal_fts_search(self):
        from spiritscout.tools.registry import ToolRegistry
        from spiritscout.traffic.tools import FUNCTIONS, SPECS

        env = await self.enable()
        async with httpx.AsyncClient(proxy=env["HTTP_PROXY"], trust_env=False) as client:
            await client.get(self.http_url + "/one")
        await self.wait_records(1)
        tools = ToolRegistry(SimpleNamespace(), max_retries=0)
        for name, function in FUNCTIONS.items():
            tools.register_builtin(name, function, SPECS[name])
        result = await tools.call("traffic_get", {"record_id": 1, "workspace_root": str(self.sessions["b"].workspace)},
                                  workspace_root=str(self.sessions["a"].workspace))
        self.assertFalse(result.ok)
        for term in ('" OR *', 'response-secret', 'nonexistent', '100%'):
            result = await traffic_search(workspace_root=str(self.sessions["a"].workspace), body=term)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(json.loads(result.stdout)["items"], [])

    @unittest.skipUnless(sandbox_available(), "真实 Linux bubblewrap 不可用")
    async def test_real_sandbox_curl_and_ca_isolation(self):
        await self.enable(trust_origin=True)
        result = await sandbox_exec(f"curl -sS --max-time 5 {self.https_url}/sandbox", workspace_root=str(self.sessions["a"].workspace),
                                    guard_root=str(self.root))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local proxy evidence", result.stdout)
        await self.wait_records(1)
        hidden = await sandbox_exec('test -r "$SSL_CERT_FILE" && test ! -e ../traffic && echo isolated',
            workspace_root=str(self.sessions["a"].workspace), guard_root=str(self.root))
        self.assertIn("isolated", hidden.stdout)

    async def test_local_executor_inherits_session_proxy(self):
        from spiritscout.tools.executor import LocalExecutor
        from spiritscout.tools.spec import ToolKind, ToolSpec

        await self.enable(trust_origin=True)
        executor = LocalExecutor()
        executor.traffic_workspace = self.sessions["a"].workspace
        result = await executor.run_argv(ToolSpec(name="curl-fixture", kind=ToolKind.CLI),
            ["curl", "-sS", "--max-time", "5", self.https_url + "/cli"])
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("local proxy evidence", result.stdout)
        self.assertEqual(len(self.requests), 1)
        await self.wait_records(1)

    async def test_websocket_connect_tunnel_preserves_headers_and_subprotocol(self):
        try:
            import websockets
            from websockets.asyncio.server import serve
        except ImportError:
            self.skipTest("本机未安装 websockets，未覆盖真实 WS")
        env = await self.enable(trust_origin=True)
        seen = []
        async def echo(ws):
            seen.append(ws.request.headers.get("Authorization"))
            async for message in ws:
                await ws.send(message)
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(self.root / "origin-cert.pem", self.root / "origin-key.pem")
        for secure in (False, True):
            async with serve(echo, "127.0.0.1", 0, ssl=server_ssl if secure else None, subprotocols=["fixture"]) as server:
                address = f"{'wss' if secure else 'ws'}://127.0.0.1:{server.sockets[0].getsockname()[1]}/echo"
                options = {"ssl": ssl.create_default_context(cafile=env["SSL_CERT_FILE"])} if secure else {}
                async with websockets.connect(address, proxy=env["HTTP_PROXY"], subprotocols=["fixture"],
                        additional_headers={"Authorization": "Bearer target"}, **options) as ws:
                    self.assertEqual(ws.subprotocol, "fixture")
                    await ws.send("hello-through-proxy")
                    self.assertEqual(await ws.recv(), "hello-through-proxy")
        self.assertEqual(seen, ["Bearer target", "Bearer target"])
        rows = await self.wait_records(2)
        row = recorder.detail(self.sessions["a"].workspace, rows[0]["id"])
        self.assertEqual(row["transport"], "WS")
        self.assertEqual(row["status"], 101)


@unittest.skipUnless(executable(), "需要先构建真实 Linux Go 代理")
class ProxyAPITests(unittest.TestCase):
    def test_real_proxy_api_lifecycle_filters_and_original_download(self):
        payload = b'{"message":"api proxy evidence","password":"fixture-private"}'
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404 if self.path == "/missing" else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        origin = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=origin.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                sessions = []
                for sid in ("a", "b"):
                    workspace = Path(temp) / sid
                    workspace.mkdir()
                    sessions.append(SimpleNamespace(session_id=sid, workspace=workspace, project_id="", status="completed",
                        display_title=lambda sid=sid: sid))
                manager = SimpleNamespace(list=lambda: sessions, get=lambda sid: next((s for s in sessions if s.session_id == sid), None))
                @asynccontextmanager
                async def lifespan(app):
                    monitor.start_supervisor()
                    yield
                    await monitor.aclose()
                app = FastAPI(lifespan=lifespan)
                monitor = mount_traffic(app, SimpleNamespace(projects=lambda: {"items": []}), manager)
                with TestClient(app) as client:
                    base = "/api/sessions/a/traffic"
                    self.assertEqual(client.post(base + "/proxy", json={}).status_code, 200)
                    status = client.get(base + "/proxy").json()
                    self.assertEqual(status["status"], "running")
                    self.assertNotIn(monitor.proxies.sessions["a"].secret, json.dumps(status))
                    response = client.get(base + "/proxy/environment")
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    env = response.json()["environment"]
                    with httpx.Client(proxy=env["HTTP_PROXY"], trust_env=False) as target:
                        self.assertEqual(target.get(f"http://127.0.0.1:{origin.server_port}/once").content, payload)
                        self.assertEqual(target.get(f"http://127.0.0.1:{origin.server_port}/missing").status_code, 404)
                    for _ in range(100):
                        data = client.get(base).json()
                        if len(data["items"]) == 2:
                            break
                        time.sleep(0.01)
                    record_id = data["items"][0]["id"]
                    self.assertEqual(len(client.get(base + "?body=proxy%20evidence&method=GET&status=2xx").json()["items"]), 1)
                    for selected in ("200", "404"):
                        filtered = client.get(base + f"?status={selected}").json()
                        self.assertEqual([r["status"] for r in filtered["items"]], [int(selected)])
                        self.assertEqual(filtered["filters"]["status"], selected)
                    self.assertEqual(client.get(base + "?status=wrong").status_code, 422)
                    preview = client.get(base + f"/{record_id}").json()
                    self.assertNotIn("fixture-private", json.dumps(preview))
                    original = client.get(base + f"/{record_id}/original")
                    self.assertEqual(original.content, payload)
                    self.assertEqual(original.headers["cache-control"], "no-store")
                    self.assertEqual(client.get(f"/api/sessions/b/traffic/{record_id}/original").status_code, 404)
                    certificate = client.get(base + "/proxy/ca")
                    self.assertIn(b"BEGIN CERTIFICATE", certificate.content)
                    self.assertNotIn(b"PRIVATE KEY", certificate.content)
                    self.assertEqual(client.delete(base + "/proxy").json()["status"], "stopped")
                    self.assertEqual(client.post(base + "/proxy", json={"upstream":"file:///etc/passwd"}).status_code, 409)
        finally:
            origin.shutdown()
            origin.server_close()
            thread.join(timeout=2)
