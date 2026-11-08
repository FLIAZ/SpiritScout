"""真实 tshark 离线解析 / 重组与后台生命周期，不假称具备实时抓包权限。"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import socket
import struct
import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.core.models import Scope
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.builtin import dns_resolve, tcp_scan
from spiritscout.traffic import packets, recorder
from spiritscout.traffic.capture import (
    CaptureError,
    CaptureManager,
    _command,
    display_numbers,
    follow_stream,
)
from spiritscout.web.traffic import mount_traffic


def pcap_fixture() -> bytes:
    frames = []

    def ip(payload, proto, src="10.20.0.1", dst="10.20.0.2"):
        header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 1, 0, 64, proto, 0,
                             socket.inet_aton(src), socket.inet_aton(dst))
        frames.append(b"\x00" * 12 + b"\x08\x00" + header + payload)

    def tcp(payload, seq, ack, reverse=False, flags=0x18):
        sport, dport = (8080, 42000) if reverse else (42000, 8080)
        raw = struct.pack("!HHIIBBHHH", sport, dport, seq, ack, 0x50, flags, 65535, 0, 0) + payload
        ip(raw, 6, "10.20.0.2" if reverse else "10.20.0.1", "10.20.0.1" if reverse else "10.20.0.2")

    request = (b"GET /chat HTTP/1.1\r\nHost: fixture.invalid\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
    response = (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n")
    tcp(b"", 1000, 0, flags=2)
    tcp(b"", 9000, 1001, True, flags=0x12)
    tcp(b"", 1001, 9001, flags=0x10)
    tcp(request, 1001, 9001)
    tcp(response, 9001, 1001 + len(request), True)
    mask = b"1234"
    frame = b"\x81\x85" + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(b"hello"))
    tcp(frame, 1001 + len(request), 9001 + len(response))
    tcp(b"\x81\x05world", 9001 + len(response), 1001 + len(request) + len(frame), True)
    # 重传同一帧；follow 应按 TCP 序号消除重复。
    tcp(b"\x81\x05world", 9001 + len(response), 1001 + len(request) + len(frame), True)
    query = struct.pack("!6H", 1234, 0x100, 1, 0, 0, 0) + b"\x07fixture\x07invalid\x00\x00\x01\x00\x01"
    udp = struct.pack("!4H", 53000, 53, len(query) + 8, 0) + query
    ip(udp, 17)
    answer = struct.pack("!6H", 1234, 0x8180, 1, 1, 0, 0) + query[12:] + b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("10.20.0.2")
    ip(struct.pack("!4H", 53, 53000, len(answer) + 8, 0) + answer, 17, "10.20.0.2", "10.20.0.1")
    data = bytearray(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
    for i, frame in enumerate(frames):
        data.extend(struct.pack("<IIII", 1700000000, i * 1000, len(frame), len(frame)))
        data.extend(frame)
    return bytes(data)


@unittest.skipUnless(shutil.which("tshark"), "真实离线解析需要 tshark")
class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = SimpleNamespace(workspace=self.root)
        self.manager = SimpleNamespace(get=lambda sid: self.session if sid == "a" else None)
        self.monitor = CaptureManager(self.manager)
        self.monitor.start_supervisor()

    async def asyncTearDown(self):
        await self.monitor.aclose()
        self.temp.cleanup()

    async def import_fixture(self, data=None):
        async def chunks():
            raw = pcap_fixture() if data is None else data
            yield raw[:10]
            yield raw[10:]
        result = await self.monitor.import_file("a", chunks(), "fixture.pcap")
        await self.monitor.queue.join()
        await self.monitor.jobs["a"]
        return result["capture_id"]

    async def test_real_protocols_raw_bytes_and_reassembled_stream(self):
        capture_id = await self.import_fixture()
        row = packets.get_capture(self.root, capture_id)
        self.assertEqual(row["status"], "completed", row["error"])
        self.assertEqual(row["packet_count"], 10)
        rows = packets.packet_page(self.root, capture_id)["items"]
        self.assertTrue({"TCP", "HTTP", "WEBSOCKET", "DNS"}.issubset({r["protocol"] for r in rows}))
        ws = next(r for r in rows if r["protocol"] == "WEBSOCKET")
        detail = packets.packet_detail(self.root, ws["id"])
        self.assertIn("websocket", detail["layers"])
        self.assertEqual(len(bytes.fromhex(detail["raw_hex"])), detail["captured_length"])
        self.assertEqual(detail["source_port"], 8080)
        followed = await follow_stream(self.root, capture_id, 0, "tcp")
        joined = "".join(s["text"] for s in followed["segments"])
        self.assertIn("GET /chat", joined)
        self.assertEqual(joined.count("world"), 1, "TCP 重传不应重复显示为应用数据")
        self.assertEqual(len(followed["nodes"]), 2)
        udp = await follow_stream(self.root, capture_id, 0, "udp")
        self.assertIn("fixture", "".join(s["text"] for s in udp["segments"]))

    async def test_real_display_filters_and_keyset_pagination(self):
        capture_id = await self.import_fixture()
        numbers = await display_numbers(self.root, capture_id, "dns && udp.port == 53")
        self.assertEqual(numbers, [9, 10])
        self.assertEqual(len(packets.packet_page(self.root, capture_id, numbers=numbers)["items"]), 2)
        self.assertEqual(packets.packet_page(self.root, capture_id, numbers=[])["items"], [])
        first = packets.packet_page(self.root, capture_id, limit=3)
        second = packets.packet_page(self.root, capture_id, before=first["next_before"], limit=3)
        self.assertFalse({r["number"] for r in first["items"]} & {r["number"] for r in second["items"]})
        self.assertEqual(packets.packet_summary(self.root)["packet_count"], 10)
        with self.assertRaises(CaptureError):
            await display_numbers(self.root, capture_id, "not-a-real-field === !")

    async def test_real_pcapng_decode_and_write_path(self):
        original, converted = self.root / "input.pcap", self.root / "converted.pcapng"
        original.write_bytes(pcap_fixture())
        converted.touch(mode=0o600)
        code, _, error = await _command([shutil.which("tshark"), "-n", "-r", str(original),
            "-w", str(converted), "-P", "-T", "ek", "-x"], cap=1024 * 1024)
        self.assertEqual(code, 0, error)
        self.assertEqual(converted.read_bytes()[:4], b"\x0a\x0d\x0d\x0a")
        capture_id = await self.import_fixture(converted.read_bytes())
        self.assertEqual(packets.get_capture(self.root, capture_id)["packet_count"], 10)
        self.assertEqual(len(await display_numbers(self.root, capture_id, "websocket")), 2)

    async def test_invalid_upload_truncation_missing_and_symlinks(self):
        with self.assertRaises(CaptureError):
            await self.import_fixture(b"not a capture file" * 2)
        self.assertEqual(packets.captures(self.root), [])
        # 文件头有效但包记录残缺：保留真实 tshark 错误，不报告成功。
        capture_id = await self.import_fixture(pcap_fixture()[:-10])
        row = packets.get_capture(self.root, capture_id)
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["error"])
        self.assertIsNone(packets.get_capture(self.root, "0" * 32))
        with self.assertRaises(ValueError):
            packets.capture_path(self.root, "../../outside")
        folder = packets.capture_path(self.root, capture_id)
        source = folder / "source.pcap"
        source.unlink()
        source.symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            await follow_stream(self.root, capture_id, 0, "tcp")

    async def test_upload_limit_and_cancel_do_not_leave_writers(self):
        with patch("spiritscout.traffic.capture.MAX_FILE_BYTES", 30), self.assertRaises(CaptureError):
            await self.import_fixture()
        self.assertFalse(list((self.root / "traffic" / "captures").glob("*/source.pcap")))
        entered = asyncio.Event()
        async def slow():
            yield pcap_fixture()[:24]
            entered.set()
            await asyncio.Event().wait()
        upload = asyncio.create_task(self.monitor.import_file("a", slow(), "slow.pcap"))
        await entered.wait()
        await self.monitor.stop("a", deleting=True)
        await asyncio.gather(upload, return_exceptions=True)
        self.assertFalse(list((self.root / "traffic" / "captures").glob("*/source.pcap")))
        with self.assertRaises(CaptureError):
            await self.import_fixture()

    async def test_missing_live_permission_and_process_bounds(self):
        with self.assertRaises(CaptureError):
            await self.monitor.start("a", "1", "", 60)
        with patch("spiritscout.traffic.capture.capabilities", return_value={"live_available": False, "error": "权限不足"}):
            with self.assertRaisesRegex(CaptureError, "权限不足"):
                await self.monitor.start("a", "1", "tcp port 12345", 60)
        with self.assertRaises(TimeoutError):
            await _command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05)
        with self.assertRaises(CaptureError):
            await _command([sys.executable, "-c", "print('x'*10000)"], cap=100)

    async def test_restart_marks_unfinished_capture_interrupted(self):
        capture_id = await self.import_fixture()
        packets.update_capture(self.root, capture_id, status="running")
        self.monitor.jobs.clear()
        self.assertEqual(self.monitor.catalog("a")[0]["status"], "interrupted")


@unittest.skipUnless(shutil.which("tshark"), "真实 API 离线解析需要 tshark")
class PacketAPITests(unittest.TestCase):
    def test_background_import_survives_response_and_session_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = []
            for sid in ("a", "b"):
                workspace = Path(temp) / sid
                workspace.mkdir()
                sessions.append(SimpleNamespace(session_id=sid, workspace=workspace, title=sid,
                    status="completed", project_id="", display_title=lambda sid=sid: sid))
            manager = SimpleNamespace(get=lambda sid: next((s for s in sessions if s.session_id == sid), None), list=lambda: sessions)
            @asynccontextmanager
            async def lifespan(app):
                monitor.start_supervisor()
                yield
                await monitor.aclose()
            app = FastAPI(lifespan=lifespan)
            monitor = mount_traffic(app, SimpleNamespace(projects=lambda: {"items": []}), manager)
            with TestClient(app) as client:
                response = client.post("/api/sessions/a/traffic/capture/import?name=fixture.pcap",
                    content=pcap_fixture(), headers={"Content-Type": "application/octet-stream"})
                self.assertEqual(response.status_code, 202, response.text)
                capture_id = response.json()["capture_id"]
                for _ in range(100):
                    row = client.get("/api/sessions/a/traffic/captures").json()["items"][0]
                    if row["status"] not in {"starting", "running"}:
                        break
                    time.sleep(0.02)
                self.assertEqual(row["status"], "completed", row)
                endpoint = f"/api/sessions/a/traffic/packets?capture_id={capture_id}"
                page = client.get(endpoint + "&display_filter=dns").json()
                self.assertEqual(len(page["items"]), 2)
                detail_id = page["items"][0]["id"]
                self.assertEqual(client.get(f"/api/sessions/b/traffic/packets/{detail_id}").status_code, 404)
                self.assertEqual(client.get(f"/api/sessions/b/traffic/captures/{capture_id}/download").status_code, 404)
                download = client.get(f"/api/sessions/a/traffic/captures/{capture_id}/download")
                self.assertEqual(hashlib.sha256(download.content).digest(), hashlib.sha256(pcap_fixture()).digest())
                self.assertEqual(download.headers["cache-control"], "no-store")
                self.assertEqual(client.get(endpoint + "&display_filter=bad==?").status_code, 422)
                self.assertEqual(client.get(f"/api/sessions/a/traffic/captures/{capture_id}/follow?stream=0").status_code, 200)
                self.assertEqual(client.get("/api/traffic").json()["projects"][0]["packet_count"], 10)
                attachments = sessions[0].workspace / "attachments"
                attachments.mkdir()
                original = pcap_fixture()
                (attachments / "offline.pcap").write_bytes(original)
                preview = "/api/sessions/a/artifacts/packets?path=attachments/offline.pcap"
                imported = client.post(preview)
                self.assertEqual(imported.status_code, 200, imported.text)
                offline_id = imported.json()["capture_id"]
                for _ in range(100):
                    row = next(r for r in monitor.catalog("a") if r["capture_id"] == offline_id)
                    if row["status"] not in {"starting", "running"}:
                        break
                    time.sleep(.02)
                self.assertEqual(row["status"], "completed", row)
                self.assertEqual(client.post(preview).json()["capture_id"], offline_id)
                self.assertEqual(len(monitor.catalog("a")), 2, "重复打开附件复用索引")
                page = client.get(f"/api/sessions/a/traffic/packets?capture_id={offline_id}&order=asc&limit=4").json()
                self.assertEqual([r["number"] for r in page["items"]], [1,2,3,4])
                following = client.get(f"/api/sessions/a/traffic/packets?capture_id={offline_id}&order=asc&before={page['next_before']}&limit=4").json()
                self.assertEqual([r["number"] for r in following["items"]], [5,6,7,8])
                http = client.get(f"/api/sessions/a/traffic/packets/{page['items'][3]['id']}").json()
                self.assertIn("GET /chat HTTP/1.1", http["payload_text"])
                self.assertIn("fixture.invalid", http["payload_text"])
                self.assertTrue(http["raw_hex"])
                self.assertIn("http", http["layers"])
                self.assertEqual((attachments / "offline.pcap").read_bytes(), original)
                self.assertEqual(client.post("/api/sessions/b/artifacts/packets?path=attachments/offline.pcap").status_code, 422)
                self.assertEqual(client.post("/api/sessions/a/artifacts/packets?path=../offline.pcap").status_code, 422)
                (attachments / "fake.pcap").write_bytes(b"not a pcap" * 10)
                self.assertEqual(client.post("/api/sessions/a/artifacts/packets?path=attachments/fake.pcap").status_code, 422)
                self.assertEqual(client.get("/api/sessions/a/traffic").json()["summary"]["packet_count"], 20)


class NativeObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_dns_and_refused_tcp_are_recorded_without_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            guard = ScopeGuard(Scope(local_analysis=True))
            with patch("socket.getaddrinfo", side_effect=socket.gaierror("fixture")):
                result = await dns_resolve("fixture.invalid", guard=guard, workspace_root=str(root))
            self.assertFalse(result.ok)
            # 绑定但不监听，保留端口所有权同时产生真实本机拒绝连接。
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                result = await tcp_scan("127.0.0.1", ports=[reserved.getsockname()[1]], guard=guard,
                                        workspace_root=str(root), timeout=0.2)
            self.assertTrue(result.ok)
            self.assertEqual(result.parsed["open"], [])
            rows = recorder.page(root)["items"]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["error"] for row in rows))
            self.assertEqual(recorder.summary(root)["errors"], 2)

    async def test_delete_waits_for_capture_cleanup_even_when_request_is_cancelled(self):
        from spiritscout.config.settings import SystemConfig, WorkspaceConfig
        from spiritscout.core.exceptions import SpiritScoutError
        from spiritscout.web.sessions import Session, SessionManager

        with tempfile.TemporaryDirectory() as temp:
            manager = SessionManager(SystemConfig(workspace=WorkspaceConfig(root=temp)), [])
            workspace = manager.root / "sess-delete-capture"
            workspace.mkdir()
            session = Session(session_id=workspace.name, mode="ctf", target="local", workspace=workspace,
                              status="completed")
            manager._sessions[session.session_id] = session
            entered, release = asyncio.Event(), asyncio.Event()
            async def stop(sid, *, deleting=False):
                self.assertTrue(deleting)
                self.assertIsNone(manager.get(sid), "删除期间必须拒绝新采集")
                entered.set()
                await release.wait()
                (workspace / "last-write").write_text("finished")
            manager._traffic_capture = SimpleNamespace(stop=stop)
            with self.assertRaises(SpiritScoutError):
                await manager.delete(session.session_id)
            self.assertFalse(entered.is_set(), "未确认删除不能停止采集")
            deletion = asyncio.create_task(manager.delete(session.session_id, confirm=True))
            await entered.wait()
            deletion.cancel()
            await asyncio.sleep(0)
            self.assertTrue(workspace.exists())
            release.set()
            result = await asyncio.gather(deletion, return_exceptions=True)
            self.assertIsInstance(result[0], asyncio.CancelledError)
            self.assertFalse(workspace.exists(), "等最后一次写入结束后才删目录，不复活已删除会话")
            await manager.aclose()

    async def test_real_dns_and_tcp_banner_preserve_tool_behavior(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            async def serve(reader, writer):
                writer.write(b"SSH-2.0-fixture\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            server = await asyncio.start_server(serve, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            # 仅本机地址，系统解析可能从 hosts/cache 返回；记录不得声称真实 DNS 包。
            guard = ScopeGuard(Scope(local_analysis=True))
            try:
                dns = await dns_resolve("localhost", guard=guard, workspace_root=str(root))
                scanned = await tcp_scan("127.0.0.1", ports=[port], guard=guard, workspace_root=str(root))
            finally:
                server.close()
                await server.wait_closed()
            self.assertTrue(dns.ok)
            self.assertTrue(scanned.ok)
            self.assertEqual(scanned.parsed["open"][0]["banner"], "SSH-2.0-fixture")
            rows = recorder.page(root)["items"]
            self.assertEqual({row["method"] for row in rows}, {"DNS", "TCP"})
            tcp = recorder.detail(root, rows[0]["id"])
            self.assertEqual(tcp["observations"]["destination_port"], port)
            self.assertIn("SSH-2.0", tcp["response_body"]["text"])
            self.assertIn("API 观测", tcp["note"])
            self.assertEqual(packets.packet_summary(root)["packet_count"], 0)
