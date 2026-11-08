"""真实 SQLite、审计游标及恢复链的无损压缩回归；不调用模型或远端工具。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import StorageConfig, SystemConfig, WorkspaceConfig
from spiritscout.core.audit import AuditLogger, read_record
from spiritscout.core.models import BlackboardLayer
from spiritscout.core.storage_codec import (
    compress_bytes,
    decompress_bytes,
    is_compressed,
    pack_json,
    unpack_json,
)
from spiritscout.monitor.panel import export_audit
from spiritscout.resources.store import ResourceStore
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.tools.run_lock import ExecutionRunLock
from spiritscout.tools.spec import ToolResult
from spiritscout.web.app import create_app
from spiritscout.web.conversation import ConversationResources
from spiritscout.web.deliverables import read_record_history
from spiritscout.web.events import read_audit_page, stream_audit
from spiritscout.web.sessions import Session, SessionManager
from spiritscout.web.storage import StorageCleaner, clean_database
from spiritscout.web.tool_parameters import read_tool_parameters

BODY = "完整证据\nHTTP/1.1 200 OK\r\n参数=原文\x00😀\n" * 2500


class CodecTests(unittest.TestCase):
    def test_legacy_and_compressed_json_roundtrip_without_loss(self) -> None:
        value = {"output": BODY, "nested": [False, None, 1, {"code": "a\\b\n"}]}
        raw = json.dumps(value, ensure_ascii=False).encode()
        packed = compress_bytes(raw)
        self.assertLess(len(packed), len(raw) // 10)
        self.assertEqual(decompress_bytes(packed), raw)
        for encoded in (raw, raw.decode(), packed, pack_json(value)):
            self.assertEqual(unpack_json(encoded), value)
        self.assertEqual(decompress_bytes(b'{"ok":true}'), b'{"ok":true}')

    def test_corruption_truncation_and_wrong_length_fail_explicitly(self) -> None:
        packed = pack_json({"text": BODY})
        for broken in (packed[:-1], packed + b"extra", packed[:8], packed[:5] + b"\0" * 8 + packed[13:]):
            with self.subTest(size=len(broken)), self.assertRaises(ValueError):
                unpack_json(broken)


class CompressedStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "sessions" / "sess-compressed"
        self.workspace.mkdir(parents=True)
        self.bb = Blackboard(self.workspace / "bb.db")
        self.addAsyncCleanup(self.bb.aclose)
        self.journal = ExecutionJournal(self.bb, self.workspace, "run")

    async def test_tool_result_and_full_checkpoint_survive_reopen_and_maintenance(self) -> None:
        params = {"command": BODY, "scope": ["授权目标", "离线样本"]}
        digest = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.journal.begin("call", "fixture", digest, "never_automatic", params=params)
        result = ToolResult(tool="fixture", ok=True, stdout=BODY, stderr="完整 stderr", parsed={"evidence": BODY})
        self.journal.transition("call", "succeeded", effect="applied", result=result)
        messages = [{"role": "user", "content": BODY}]
        for i in range(36):
            messages.append({"role": "tool", "content": f"{i}: {BODY[:100]}"})
            self.journal.checkpoint("ctf_conversation", "main", "saved", {
                "messages": list(messages), "step": i, "rules": params["scope"]})
        expected = self.journal.latest_checkpoint("ctf_conversation", "main")["state"]
        with self.bb.transaction() as conn:
            row = conn.execute("SELECT params_cipher,result_cipher FROM tool_invocations").fetchone()
            self.assertLess(sum(map(len, row)), len(BODY.encode()) // 5)
            self.assertTrue(is_compressed(self.journal._cipher.decrypt(row[1])))
        self.bb.close()
        cleaned = clean_database(self.workspace, lambda _: None)
        self.assertEqual(cleaned["warnings"], [])
        reopened = Blackboard(self.workspace / "bb.db")
        try:
            journal = ExecutionJournal(reopened, self.workspace, "resumed")
            self.assertEqual(journal.latest_checkpoint("ctf_conversation", "main")["state"], expected)
            self.assertEqual(asdict(journal.result("call")), asdict(result))
            self.assertEqual(journal.parameters("call"), params)
            self.assertEqual(read_tool_parameters(self.workspace, "fixture", digest)["parameters"], params)
            self.assertEqual(journal.list_recent()[0]["effect_status"], "applied")
        finally:
            reopened.close()

    async def test_legacy_reencoding_preserves_all_facts_parameters_and_pending_events(self) -> None:
        value = {"asset_id": "asset-1", "value": "https://fixture.test", "asset_type": "url", "body": BODY}
        for i in range(3):
            self.bb.write(BlackboardLayer.ASSET, "asset-1", {**value, "observation": i})
        before_history = read_record_history(self.workspace, "asset", "asset-1")
        params = {"command": BODY}
        self.journal.begin("legacy", "fixture", "digest", "never_automatic", params=params)
        self.journal.checkpoint("ctf_conversation", "legacy", "saved", {"messages": [params]})
        with self.bb.transaction() as conn:
            for row in conn.execute("SELECT rowid,data FROM bb_record_history").fetchall():
                conn.execute("UPDATE bb_record_history SET data=? WHERE rowid=?",
                             (json.dumps(unpack_json(row[1])), row[0]))
            for row in conn.execute("SELECT rowid,event_json FROM bb_event_outbox").fetchall():
                conn.execute("UPDATE bb_event_outbox SET event_json=? WHERE rowid=?",
                             (json.dumps(unpack_json(row[1])), row[0]))
            conn.execute("UPDATE tool_invocations SET params_cipher=?", (
                self.journal._cipher.encrypt(json.dumps(params).encode()),))
            conn.execute("UPDATE runtime_checkpoints SET state_cipher=?", (
                self.journal._cipher.encrypt(json.dumps({"messages": [params]}).encode()),))
            conn.execute("DELETE FROM execution_schema_migrations WHERE version=4")
        catalog = ResourceStore(self.root / "catalog")
        catalog.import_session(self.workspace)
        self.bb.close()
        before_size = (self.workspace / "bb.db").stat().st_size
        result = clean_database(self.workspace, lambda _: None)
        self.assertEqual(result["warnings"], [])
        self.assertGreaterEqual(result["records_compressed"], 8)
        self.assertLess((self.workspace / "bb.db").stat().st_size, before_size // 2)
        self.assertEqual(read_record_history(self.workspace, "asset", "asset-1"), before_history)
        # VACUUM 后导入高水位仍可靠；独立目录可完整读取压缩历史。
        catalog.import_session(self.workspace)
        ResourceStore(self.root / "another-catalog").import_session(self.workspace)
        bus = AsyncMock()
        reopened = Blackboard(self.workspace / "bb.db", bus)
        try:
            journal = ExecutionJournal(reopened, self.workspace, "next")
            self.assertEqual(journal.parameters("legacy"), params)
            self.assertEqual(journal.latest_checkpoint("ctf_conversation", "legacy")["state"], {"messages": [params]})
            self.assertEqual(await reopened.flush_outbox(), 3)
            self.assertEqual([call.args[0].payload["body"] for call in bus.publish.await_args_list], [BODY] * 3)
            with reopened.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT MAX(version) FROM execution_schema_migrations").fetchone()[0], 4)
        finally:
            await reopened.aclose()

    async def test_corrupt_cipher_rolls_back_reencoding_without_losing_original(self) -> None:
        self.bb.write(BlackboardLayer.TASK_STATE, "fact", {"body": BODY})
        self.journal.begin("broken", "fixture", "digest", "never_automatic")
        with self.bb.transaction() as conn:
            raw = json.dumps({"body": BODY})
            conn.execute("UPDATE bb_record_history SET data=?", (raw,))
            conn.execute("UPDATE tool_invocations SET result_cipher=?", (b"broken" * 1000,))
        self.bb.close()
        result = clean_database(self.workspace, lambda _: None)
        self.assertTrue(result["warnings"])
        with closing(sqlite3.connect(self.workspace / "bb.db")) as conn:
            self.assertEqual(conn.execute("SELECT data FROM bb_record_history").fetchone()[0], raw)
            self.assertEqual(conn.execute("SELECT result_cipher FROM tool_invocations").fetchone()[0], b"broken" * 1000)

    async def test_audit_pagination_sse_agent_reads_and_exports_return_original_content(self) -> None:
        path = self.workspace / "audit.jsonl"
        legacy = {"seq": 1, "type": "user.message", "actor": "user", "detail": {"text": "旧格式"}}
        path.write_text(json.dumps(legacy) + "\n")
        cursor = path.stat().st_size
        audit = AuditLogger(path)
        saved = audit.log("agent.tool_result", "agent", {"result": BODY, "ok": True})
        audit.log("agent.reply", "agent", {"text": BODY})
        lines = path.read_bytes().splitlines()
        self.assertIn(b'"_detail_zlib"', lines[1])
        self.assertLess(path.stat().st_size, len(BODY.encode()) // 5)
        self.assertEqual(read_record(lines[1]), saved)
        page = read_audit_page(path, limit=2)
        self.assertEqual(page["items"][0]["event"], saved)
        self.assertEqual(page["older_cursor"], cursor)
        self.assertEqual(read_audit_page(path, before=cursor)["items"][0]["event"], legacy)
        frames = [frame async for frame in stream_audit(path, byte_offset=cursor, max_frames=2)]
        self.assertEqual(json.loads(frames[0].split("data: ", 1)[1]), saved)
        session = Session(session_id="sess-compressed", workspace=self.workspace, mode="ctf", target="fixture")
        reader = ConversationResources(session, audit, {})
        self.assertEqual(reader._read({"source": "events"})["items"][1], saved)
        output = self.root / "export.jsonl"
        export_audit(path, output)
        self.assertEqual(json.loads(output.read_text().splitlines()[1]), saved)
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)), storage=StorageConfig(auto_maintenance=False))
        app = create_app(cfg, [])
        app.state.manager._sessions[session.session_id] = session
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get(f"/api/sessions/{session.session_id}/audit/download")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.text.splitlines()[1]), saved)

    async def test_multiple_audit_writers_keep_sequence_and_contents(self) -> None:
        path = self.workspace / "audit.jsonl"
        writers = [AuditLogger(path), AuditLogger(path)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: writers[i % 2].log("tool.result", "fixture", {"i": i, "body": BODY}), range(40)))
        rows = [read_record(line) for line in path.read_bytes().splitlines()]
        self.assertEqual([row["seq"] for row in rows], list(range(1, 41)))
        self.assertEqual({row["detail"]["i"] for row in rows}, set(range(40)))
        self.assertTrue(all(row["detail"]["body"] == BODY for row in rows))

    async def test_automatic_maintenance_skips_live_and_locked_sessions_and_is_idempotent(self) -> None:
        self.bb.write(BlackboardLayer.TASK_STATE, "fact", {"body": BODY})
        self.bb.close()
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)),
                           storage=StorageConfig(idle_seconds=0, maintenance_interval_seconds=1))
        manager = SessionManager(cfg, [])
        session = Session(session_id="sess-compressed", workspace=self.workspace, mode="ctf", target="fixture",
                          status="running", finished_at=time.time() - 60)
        manager._sessions[session.session_id] = session
        cleaner = StorageCleaner(manager)
        self.addAsyncCleanup(cleaner.close)
        await cleaner.maintain_once()
        self.assertFalse(cleaner.jobs)
        session.status = "completed"
        lock = ExecutionRunLock(self.workspace)
        lock.acquire()
        try:
            await cleaner.maintain_once()
            self.assertEqual(cleaner.jobs[session.session_id]["status"], "failed")
            self.assertFalse((self.workspace / ".storage-maintenance.json").exists())
        finally:
            lock.close()
        cleaner._attempted.clear()
        await cleaner.maintain_once()
        self.assertEqual(cleaner.jobs[session.session_id]["status"], "completed")
        self.assertNotIn(session.session_id, manager.storage_busy)
        # 新进程也使用完成标记跳过未变化的库。
        another = StorageCleaner(manager)
        self.addAsyncCleanup(another.close)
        with patch.object(another, "start", side_effect=AssertionError("未变化的库不应再次维护")):
            await another.maintain_once()
        cfg.storage.auto_maintenance = False
        with patch.object(another, "start", side_effect=AssertionError("禁用维护")):
            await another.maintain_once()

    async def test_shutdown_waits_for_started_maintenance_worker(self) -> None:
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)),
                           storage=StorageConfig(idle_seconds=0, maintenance_interval_seconds=1))
        manager = SessionManager(cfg, [])
        session = Session(session_id="sess-compressed", workspace=self.workspace, mode="ctf", target="fixture",
                          status="completed", finished_at=time.time() - 60)
        manager._sessions[session.session_id] = session
        cleaner = StorageCleaner(manager)
        cleaner._maintenance_task = asyncio.create_task(cleaner.maintain_once())
        await asyncio.sleep(0)
        await cleaner.close()
        self.assertNotIn(session.session_id, manager.storage_busy)
        self.assertTrue(cleaner._task is None or cleaner._task.done())

    async def test_unwritable_marker_does_not_block_other_sessions(self) -> None:
        self.bb.close()
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)), storage=StorageConfig(idle_seconds=0))
        manager = SessionManager(cfg, [])
        sessions = []
        for index in range(2):
            workspace = self.workspace.parent / f"sess-marker-{index}"
            workspace.mkdir()
            Blackboard(workspace / "bb.db").close()
            session = Session(session_id=workspace.name, workspace=workspace, mode="ctf", target="fixture",
                              status="completed", finished_at=time.time() - 60)
            manager._sessions[session.session_id] = session
            sessions.append(session)
        cleaner = StorageCleaner(manager)
        self.addAsyncCleanup(cleaner.close)
        # 路径被目录占用时无法原子发布标记，但其他数据库仍应完成维护。
        for session in sessions:
            (session.workspace / ".storage-maintenance.json").mkdir()
        with self.assertLogs("spiritscout.web.storage", level="WARNING") as logs:
            await cleaner.maintain_once()
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(all(cleaner.jobs[session.session_id]["status"] == "completed" for session in sessions))
        self.assertFalse(manager.storage_busy)
