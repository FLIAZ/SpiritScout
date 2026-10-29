"""在临时工作区验证存储精简、恢复链、运行互斥及真实 SQLite 文件收缩。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import BlackboardLayer
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.tools.run_lock import ExecutionRunLock
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session, SessionManager
from spiritscout.web.storage import (
    StorageCleaner,
    cache_paths,
    clean_database,
    database_preview,
    tree_bytes,
)


class DatabaseCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bb = Blackboard(self.root / "bb.db")
        self.addCleanup(self.bb.close)
        self.journal = ExecutionJournal(self.bb, self.root, "run")

    def legacy_history(self) -> None:
        self.bb.write(BlackboardLayer.TASK_STATE, "dag-snapshot", {"dag_snapshot": []})
        with self.bb.transaction() as conn:
            conn.executemany("INSERT INTO bb_record_history VALUES('task_state','dag-snapshot',?,?,?,?)",
                             [(i, json.dumps({"data": "x" * 32000, "n": i}), "master", "now")
                              for i in range(2, 102)])
            conn.executemany("INSERT INTO bb_event_outbox(event_json,delivered) VALUES(?,1)",
                             [(json.dumps({"payload": "x" * 32000}),) for _ in range(100)])

    def test_cleanup_shrinks_real_database_and_preserves_current_state_and_restore_chain(self) -> None:
        self.legacy_history()
        self.bb.write(BlackboardLayer.TASK_STATE, "task-kept", {"notes": "original task"})
        self.bb.write(BlackboardLayer.TASK_STATE, "task-kept", {"notes": "updated task"})
        for i in range(5):
            self.journal.checkpoint("ctf_conversation", "route-a", "saved", {
                "messages": [{"role": "user", "content": f"turn-{n}"} for n in range(i + 1)]})
        self.journal.checkpoint("route", "route-b", "saved", {"result": "keep"})
        # 模拟旧版的多余完整检查点；最新恢复链不得引用它。
        with self.bb.transaction() as conn:
            conn.execute("INSERT INTO runtime_checkpoints SELECT -1,run_id,scope,checkpoint_key,phase,"
                         "schema_version,last_event_id,state_cipher,created_at FROM runtime_checkpoints "
                         "WHERE checkpoint_id=1")
        expected = self.journal.latest_checkpoint("ctf_conversation", "route-a")["state"]
        original = self.bb.read(BlackboardLayer.TASK_STATE, "task-kept")
        preview = database_preview(self.root)
        self.assertEqual(preview["old_dag_versions"], 37)
        self.assertEqual(preview["delivered_events"], 100)
        self.bb.close()
        before = (self.root / "bb.db").stat().st_size
        result = clean_database(self.root, lambda _: None)
        self.assertEqual(result["warnings"], [])
        self.assertTrue(result["compacted"])
        self.assertEqual(result["events_removed"], 100)
        self.assertEqual(result["dag_versions_removed"], 37)
        self.assertEqual(result["checkpoints_removed"], 1)
        self.assertLess((self.root / "bb.db").stat().st_size, before)
        reopened = Blackboard(self.root / "bb.db")
        try:
            journal = ExecutionJournal(reopened, self.root, "new-run")
            self.assertEqual(journal.latest_checkpoint("ctf_conversation", "route-a")["state"], expected)
            self.assertEqual(journal.latest_checkpoint("route", "route-b")["state"], {"result": "keep"})
            self.assertEqual(reopened.read(BlackboardLayer.TASK_STATE, "task-kept"), original)
            with reopened.transaction() as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM bb_record_history WHERE record_id='task-kept'").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM bb_event_outbox WHERE delivered=0").fetchone()[0], 3)
        finally:
            reopened.close()

    def test_invalid_checkpoint_chain_is_not_partially_pruned(self) -> None:
        self.journal.checkpoint("ctf_conversation", "a", "saved", {"messages": []})
        self.journal.checkpoint("ctf_conversation", "a", "saved", {"messages": [{"role": "user", "content": "hello"}]})
        with self.bb.transaction() as conn:
            conn.execute("UPDATE runtime_checkpoints SET state_cipher=? WHERE checkpoint_id=2", (b"broken",))
        self.bb.close()
        result = clean_database(self.root, lambda _: None)
        self.assertTrue(result["warnings"])
        with closing(sqlite3.connect(self.root / "bb.db")) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runtime_checkpoints").fetchone()[0], 2)

    def test_insufficient_disk_space_preserves_database_and_reports_skipped_compaction(self) -> None:
        self.legacy_history()
        self.bb.close()
        with patch("spiritscout.web.storage.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            result = clean_database(self.root, lambda _: None)
        self.assertNotIn("compacted", result)
        self.assertTrue(result["warnings"])
        self.assertNotIn("events_removed", result)
        with closing(sqlite3.connect(self.root / "bb.db")) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bb_event_outbox WHERE delivered=1").fetchone()[0], 100)

    def test_future_dag_and_checkpoint_growth_is_bounded_without_changing_facts(self) -> None:
        for i in range(90):
            version = self.bb.write(BlackboardLayer.TASK_STATE, "dag-snapshot", {"dag_snapshot": [i]})
            self.assertEqual(self.bb.write(BlackboardLayer.TASK_STATE, "dag-snapshot", {"dag_snapshot": [i]}), version)
            self.journal.checkpoint("ctf_conversation", "a", "saved", {"messages": list(range(i + 1))})
        with self.bb.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bb_record_history").fetchone()[0], 64)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bb_event_outbox").fetchone()[0], 90)
            self.assertLessEqual(conn.execute("SELECT COUNT(*) FROM runtime_checkpoints").fetchone()[0], 32)
        self.assertEqual(self.journal.latest_checkpoint("ctf_conversation", "a")["state"]["messages"], list(range(90)))

    def test_symlink_cache_and_database_are_not_followed(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "keep"
        marker.write_text("preserve")
        sandbox = self.root / "sandbox" / "a" / ".cache"
        sandbox.mkdir(parents=True)
        (sandbox / "pip").symlink_to(outside, target_is_directory=True)
        self.assertEqual(cache_paths(self.root), [])
        (sandbox / "uv").mkdir()
        (sandbox / "uv" / "link").symlink_to(marker)
        self.assertEqual(tree_bytes(sandbox / "uv"), 0)
        self.bb.close()
        db = self.root / "bb.db"
        target = self.root / "actual.db"
        db.rename(target)
        db.symlink_to(target)
        with self.assertRaises(SpiritScoutError):
            clean_database(self.root, lambda _: None)
        self.assertEqual(marker.read_text(), "preserve")


class CleanerJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SystemConfig(workspace=WorkspaceConfig(root=self.tmp.name))
        self.manager = SessionManager(self.cfg, [])
        self.workspace = self.manager.root / "sess-clean"
        self.workspace.mkdir()
        self.session = Session(session_id="sess-clean", mode="ctf", target="fixture", workspace=self.workspace,
                               status="completed")
        self.manager._sessions[self.session.session_id] = self.session
        self.cleaner = StorageCleaner(self.manager)
        self.addAsyncCleanup(self.cleaner.close)

    async def test_job_removes_only_download_cache_and_keeps_artifacts(self) -> None:
        for name in ("sandbox/a/.cache/pip/blob", "sandbox/a/pylibs/keep", "reports/keep.md", "execution.key"):
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("retained" * 1000)
        self.cleaner.start("sess-clean", database=False, caches=True)
        await self.cleaner.close()
        job = self.cleaner.jobs["sess-clean"]
        self.assertEqual(job["status"], "completed")
        self.assertGreater(job["released_bytes"], 0)
        self.assertFalse((self.workspace / "sandbox/a/.cache/pip").exists())
        self.assertTrue((self.workspace / "sandbox/a/pylibs/keep").exists())
        self.assertTrue((self.workspace / "reports/keep.md").exists())
        self.assertTrue((self.workspace / "execution.key").exists())

    async def test_active_report_running_session_and_execution_lock_block_cleanup(self) -> None:
        self.session.status = "running"
        with self.assertRaises(SpiritScoutError):
            self.cleaner.start("sess-clean", database=True, caches=True)
        self.session.status = "completed"
        (self.workspace / "report_job.json").write_text('{"status":"running"}')
        with self.assertRaises(SpiritScoutError):
            self.cleaner.start("sess-clean", database=True, caches=True)
        (self.workspace / "report_job.json").unlink()
        lock = ExecutionRunLock(self.workspace)
        lock.acquire()
        try:
            self.cleaner.start("sess-clean", database=True, caches=True)
            await self.cleaner.close()
            self.assertEqual(self.cleaner.jobs["sess-clean"]["status"], "failed")
        finally:
            lock.close()

    async def test_cleanup_blocks_resume_delete_and_duplicate_job_until_thread_finishes(self) -> None:
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(5)
            return {}
        with patch("spiritscout.web.storage.clean_database", side_effect=blocked):
            self.cleaner.start("sess-clean", database=True, caches=False)
            await asyncio.to_thread(entered.wait, 2)
            try:
                with self.assertRaises(SpiritScoutError):
                    await self.manager.resume("sess-clean")
                with self.assertRaises(SpiritScoutError):
                    await self.manager.delete("sess-clean", confirm=True)
                with self.assertRaises(SpiritScoutError):
                    self.cleaner.start("sess-clean", database=True, caches=False)
            finally:
                release.set()
                await self.cleaner.close()
        self.assertNotIn("sess-clean", self.manager.storage_busy)

    async def test_delivered_event_is_removed_only_after_successful_publish(self) -> None:
        bus = AsyncMock()
        bb = Blackboard(self.workspace / "bb.db", bus=bus)
        try:
            # 不让后台投递协程竞争测试中显式调用。
            with patch.object(bb, "_wake_outbox"):
                bb.write(BlackboardLayer.TASK_STATE, "t", {"x": 1})
            bus.publish.side_effect = RuntimeError("offline")
            with self.assertRaises(RuntimeError):
                await bb.flush_outbox()
            with bb.transaction() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM bb_event_outbox").fetchone()[0], 1)
            bus.publish.side_effect = None
            await bb.flush_outbox()
            with bb.transaction() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM bb_event_outbox").fetchone()[0], 0)
        finally:
            await bb.aclose()


class StorageApiTests(unittest.TestCase):
    def test_preview_confirmation_validation_and_message_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            app = create_app(cfg, [])
            manager = app.state.manager
            workspace = manager.root / "sess-test"
            workspace.mkdir()
            session = Session(session_id="sess-test", mode="ctf", target="fixture", workspace=workspace,
                              status="completed")
            manager._sessions[session.session_id] = session
            with TestClient(app, base_url="http://127.0.0.1") as client:
                self.assertEqual(client.get('/api/storage/sessions').status_code, 200)
                self.assertEqual(client.get('/api/sessions/sess-test/storage').status_code, 200)
                path = '/api/sessions/sess-test/storage/cleanup'
                for payload in ({}, {"confirm": "true"}, {"confirm": True, "database": "false"}):
                    self.assertEqual(client.post(path, json=payload).status_code, 422)
                self.assertEqual(client.post(path, json={"confirm": True, "database": False, "caches": False}).status_code, 409)
                manager.storage_busy.add("sess-test")
                before = list(session.dialogue_history)
                response = client.post('/api/sessions/sess-test/messages', json={"content": "continue"})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(session.dialogue_history, before)
                manager.storage_busy.clear()
                self.assertEqual(client.get('/api/sessions/missing/storage').status_code, 404)
                response = client.post(path, json={"confirm": True, "database": False, "caches": True})
                self.assertEqual(response.status_code, 200, response.text)
