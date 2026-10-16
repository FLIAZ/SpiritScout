"""执行台账、单次审批、SQLite 原子性与旧库升级的集成回归。"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from spiritscout.blackboard import Blackboard
from spiritscout.bus.inprocess import InProcessBus
from spiritscout.core.models import BlackboardLayer, RunMode, Scope
from spiritscout.policy.approvals import ApprovalBroker
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.journal import ExecutionJournal, read_execution_page
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bb = Blackboard(self.root / "bb.db")
        self.addAsyncCleanup(self.bb.aclose)
        self.journal = ExecutionJournal(self.bb, self.root, "run-test")

    async def test_real_cli_result_survives_reopen_without_plaintext(self) -> None:
        registry = ToolRegistry(LocalExecutor(), journal=self.journal)
        registry.register(ToolSpec(name="echo", kind=ToolKind.CLI,
                                  command_template=["/bin/echo", "{value}"]))
        result = await registry.call("echo", {"value": "private-result-marker"})
        self.assertEqual(result.execution_status, "succeeded")
        reopened = ExecutionJournal(self.bb, self.root, "run-next")
        self.assertEqual(reopened.result(result.invocation_id).stdout, "private-result-marker\n")
        self.assertEqual((self.root / "execution.key").stat().st_mode & 0o777, 0o600)
        with self.bb.transaction() as conn:
            row = conn.execute("SELECT result_cipher FROM tool_invocations").fetchone()
        self.assertNotIn(b"private-result-marker", row[0])
        page = read_execution_page(self.root / "bb.db")
        self.assertEqual(page["items"][0]["invocation_id"], result.invocation_id)
        self.assertNotIn("private-result-marker", json.dumps(page))
        events = read_execution_page(self.root / "bb.db", events=True, limit=1)
        self.assertTrue(events["has_more"])
        after = read_execution_page(self.root / "bb.db", events=True, after=events["next_cursor"])
        self.assertEqual([x["kind"] for x in after["items"]],
                         ["invocation.running", "invocation.succeeded"])

    async def test_two_sensitive_calls_need_two_decisions(self) -> None:
        gate = AsyncMock(side_effect=[True, False])
        guard = ScopeGuard(Scope())
        # 旧会话级许可不能绕过新的调用门。
        guard.approve_sensitive("intrusive_write")
        policy = PolicyEngine(RunMode.SRC, guard, approval_gate=gate)
        registry = ToolRegistry(LocalExecutor(), journal=self.journal, policy=policy)
        execute = AsyncMock(return_value=ToolResult(tool="write", ok=True))
        registry.register_builtin("write", execute, ToolSpec(
            name="write", kind=ToolKind.BUILTIN, risk=RiskLevel.INTRUSIVE))
        first = await registry.call("write", {"value": "first"})
        second = await registry.call("write", {"value": "second"})
        self.assertTrue(first.ok)
        self.assertEqual(second.execution_status, "denied")
        self.assertEqual(first.effect_status, "applied")
        self.assertEqual(second.effect_status, "none")
        self.assertEqual(gate.await_count, 2)
        execute.assert_awaited_once()
        with self.bb.transaction() as conn:
            decisions = conn.execute("SELECT allow FROM approval_records ORDER BY decided_at").fetchall()
        self.assertEqual([row[0] for row in decisions], [1, 0])

    async def test_concurrent_same_action_has_distinct_approvals_and_parameter_snapshot(self) -> None:
        broker = ApprovalBroker()
        entered = asyncio.Event()
        calls: list[str] = []

        async def gate(action: str, detail: str) -> bool:
            entered.set()
            return await broker.ask(action, detail)

        async def execute(**params) -> ToolResult:
            calls.append(params["value"])
            return ToolResult(tool="write", ok=True)

        registry = ToolRegistry(LocalExecutor(), journal=self.journal, policy=PolicyEngine(
            RunMode.SRC, ScopeGuard(Scope()), approval_gate=gate))
        registry.register_builtin("write", execute, ToolSpec(
            name="write", kind=ToolKind.BUILTIN, risk=RiskLevel.INTRUSIVE))
        params = {"value": "original"}
        first = asyncio.create_task(registry.call("write", params))
        await entered.wait()
        entered.clear()
        params["value"] = "mutated"
        second = asyncio.create_task(registry.call("write", {"value": "second"}))
        await entered.wait()
        try:
            pending = broker.view()
            self.assertEqual(len(pending), 2)
            self.assertNotEqual(pending[0]["invocation_id"], pending[1]["invocation_id"])
            self.assertNotEqual(pending[0]["params_digest"], pending[1]["params_digest"])
            broker.decide(pending[0]["approval_id"], True)
            broker.decide(pending[1]["approval_id"], False)
            await asyncio.gather(first, second)
            self.assertEqual(calls, ["original"])
        finally:
            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

    async def test_cancelled_running_write_retains_unknown_effect(self) -> None:
        entered = asyncio.Event()

        async def execute(**_) -> ToolResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("不能到达")

        registry = ToolRegistry(LocalExecutor(), journal=self.journal, policy=PolicyEngine(
            RunMode.SRC, ScopeGuard(Scope()), approval_gate=AsyncMock(return_value=True)))
        registry.register_builtin("write", execute, ToolSpec(
            name="write", kind=ToolKind.BUILTIN, risk=RiskLevel.INTRUSIVE))
        task = asyncio.create_task(registry.call("write", {}))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = self.journal.list_recent()[0]
        self.assertEqual((row["execution_status"], row["effect_status"], row["replay_policy"]),
                         ("cancelled", "unknown", "never_automatic"))

    async def test_failed_outbox_insert_rolls_back_blackboard(self) -> None:
        with self.bb.transaction() as conn:
            conn.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON bb_event_outbox "
                         "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.bb.write(BlackboardLayer.TASK_STATE, "task", {"value": 1})
        self.assertIsNone(self.bb.read(BlackboardLayer.TASK_STATE, "task"))

    async def test_outbox_failure_then_reopen_delivers_same_event(self) -> None:
        bus = AsyncMock()
        bus.publish.side_effect = RuntimeError("offline")
        bb = Blackboard(self.root / "outbox.db", bus=bus)
        bb.write(BlackboardLayer.TASK_STATE, "task", {"value": 1})
        await asyncio.sleep(0)
        await bb.aclose()
        self.assertIsNotNone(bb)
        bus.publish.side_effect = None
        reopened = Blackboard(self.root / "outbox.db", bus=bus)
        try:
            self.assertEqual(await reopened.flush_outbox(), 1)
            self.assertEqual(await reopened.flush_outbox(), 0)
            ids = [call.args[0].event_id for call in bus.publish.await_args_list]
            self.assertGreaterEqual(len(ids), 2)
            self.assertEqual(len(set(ids)), 1)
        finally:
            await reopened.aclose()

    async def test_identical_event_replay_does_not_break_priority_queue(self) -> None:
        from spiritscout.core.models import Event

        bus = InProcessBus(queue_maxsize=2)
        await bus.start()
        try:
            event = Event(topic="test", event_id="same")
            await bus.publish(event)
            await bus.publish(event)
            self.assertEqual(bus.stats()["published"], 2)
        finally:
            await bus.stop()

    async def test_missing_key_is_reported_without_replacing_existing_cipher_key(self) -> None:
        from spiritscout.core.exceptions import ConfigurationError

        self.journal.begin("private", "echo", "digest", "safe")
        self.journal.transition("private", "succeeded", result=ToolResult(tool="echo", ok=True))
        (self.root / "execution.key").unlink()
        with self.assertRaises(ConfigurationError):
            ExecutionJournal(self.bb, self.root, "next")
        self.assertFalse((self.root / "execution.key").exists())

    async def test_actual_sandbox_cannot_read_or_overwrite_execution_key(self) -> None:
        from spiritscout.tools.sandbox import sandbox_available, sandbox_exec

        if not sandbox_available():
            self.skipTest("需要 bubblewrap 验证实际密钥遮蔽")
        key = self.root / "execution.key"
        original = key.read_bytes()
        script = ("from pathlib import Path\n"
                  f"p = Path({str(key)!r})\n"
                  "try:\n print('visible_bytes', len(p.read_bytes()))\n"
                  "except OSError:\n print('read_blocked')\n"
                  "try:\n p.write_bytes(b'changed')\n"
                  "except OSError:\n print('write_blocked')\n"
                  f"host = Path({('/proc/' + str(os.getpid()) + '/root' + str(key))!r})\n"
                  "try:\n print('host_visible_bytes', len(host.read_bytes()))\n"
                  "except OSError:\n print('host_read_blocked')\n")
        result = await sandbox_exec(script, language="python", workspace_root=str(self.root))
        self.assertTrue(result.ok, result.error + result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue("visible_bytes 0" in result.stdout or "read_blocked" in result.stdout,
                        result.stdout)
        self.assertIn("write_blocked", result.stdout)
        self.assertIn("host_read_blocked", result.stdout)
        self.assertEqual(key.read_bytes(), original)
        # PID 隔离不能破坏会话停止时的进程清理。
        marker = self.root / "late-write"
        task = asyncio.create_task(sandbox_exec(
            "import time\nfrom pathlib import Path\ntime.sleep(0.2)\n"
            f"Path({str(marker)!r}).write_text('late')", language="python",
            workspace_root=str(self.root)))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.25)
        self.assertFalse(marker.exists())

    async def test_full_inprocess_queue_uses_backpressure_without_dropping_events(self) -> None:
        from spiritscout.core.models import Event

        bus = InProcessBus(queue_maxsize=1)
        entered = asyncio.Event()
        release = asyncio.Event()
        received: list[str] = []

        async def receive(event: Event) -> None:
            entered.set()
            await release.wait()
            received.append(event.data_id)

        bus.subscribe("*", receive)
        await bus.start()
        blocked = None
        try:
            await bus.publish(Event(topic="test", data_id="first"))
            await entered.wait()
            await bus.publish(Event(topic="test", data_id="second"))
            blocked = asyncio.create_task(bus.publish(Event(topic="test", data_id="third")))
            await asyncio.sleep(0)
            self.assertFalse(blocked.done())
            release.set()
            await asyncio.wait_for(blocked, 2)
            await asyncio.wait_for(bus._queue.join(), 2)
            self.assertEqual(received, ["first", "second", "third"])
            self.assertEqual(bus.stats()["dropped"], 0)
        finally:
            release.set()
            if blocked is not None:
                blocked.cancel()
                await asyncio.gather(blocked, return_exceptions=True)
            await bus.stop()


class MigrationTests(unittest.TestCase):
    def test_legacy_backup_includes_committed_wal_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bb.db"
            old = sqlite3.connect(path)
            old.execute("PRAGMA journal_mode=WAL")
            old.execute("CREATE TABLE bb_records(layer TEXT,record_id TEXT,version INTEGER,"
                        "dedup_key TEXT,data TEXT,updated_by TEXT,updated_at TEXT,"
                        "PRIMARY KEY(layer,record_id))")
            old.execute("INSERT INTO bb_records VALUES('task_state','old',1,'','{}','','now')")
            old.commit()
            bb = Blackboard(path)
            bb.close()
            old.close()
            backup = sqlite3.connect(str(path) + ".pre-runtime-v1.bak")
            try:
                self.assertEqual(backup.execute("SELECT record_id FROM bb_records").fetchone()[0], "old")
                self.assertIsNone(backup.execute("SELECT 1 FROM sqlite_master "
                                                 "WHERE name='bb_event_outbox'").fetchone())
            finally:
                backup.close()


class ExecutionApiTests(unittest.TestCase):
    def test_metadata_pagination_session_boundary_and_legacy_reads(self) -> None:
        from fastapi.testclient import TestClient

        from spiritscout.config.settings import SystemConfig, WorkspaceConfig
        from spiritscout.web.app import create_app
        from spiritscout.web.sessions import Session

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(SystemConfig(workspace=WorkspaceConfig(root=tmp),
                                          models_config_path=str(root / "models.yaml")), [])
            with TestClient(app) as client:
                workspace = root / "sessions" / "test-session"
                workspace.mkdir(parents=True)
                session = Session(session_id="test-session", mode="src", target="local",
                                  workspace=workspace, status="completed")
                app.state.manager._sessions[session.session_id] = session
                url = "/api/sessions/test-session/executions"
                self.assertEqual(client.get(url).json()["items"], [])
                self.assertFalse(session.bb_path.exists())
                bb = Blackboard(session.bb_path)
                try:
                    journal = ExecutionJournal(bb, workspace, "run-api")
                    for index in range(2):
                        journal.begin(f"inv-{index}", "echo", f"digest-{index}", "safe")
                        journal.transition(f"inv-{index}", "succeeded", result=ToolResult(
                            tool="echo", ok=True, stdout="private-api-marker"))
                    first = client.get(url, params={"limit": 1}).json()
                    self.assertTrue(first["has_more"])
                    second = client.get(url, params={"after": first["next_cursor"]}).json()
                    self.assertEqual(second["items"][0]["invocation_id"], "inv-1")
                    self.assertFalse(second["has_more"])
                    self.assertNotIn("private-api-marker", json.dumps(first))
                    self.assertNotIn("result_cipher", json.dumps(first))
                    events = client.get("/api/sessions/test-session/execution-events").json()
                    self.assertEqual(events["cursor_version"], "execution-v1")
                    self.assertEqual(len(events["items"]), 4)
                    self.assertEqual(client.get(url, params={"limit": 501}).status_code, 422)
                    self.assertEqual(client.get(url, params={"after": -1}).status_code, 422)
                    self.assertEqual(client.get("/api/sessions/absent/executions").status_code, 404)
                finally:
                    bb.close()


class CrashTests(unittest.TestCase):
    def test_process_exits_after_side_effect_retains_unknown_record_without_replay(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            script = '''
import asyncio, os, sys
from pathlib import Path
from spiritscout.blackboard import Blackboard
from spiritscout.core.models import Scope, RunMode
from spiritscout.policy.scope import ScopeGuard
from spiritscout.policy.engine import PolicyEngine
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.spec import ToolSpec, ToolKind, RiskLevel
root = Path(sys.argv[1])
bb = Blackboard(root / 'bb.db')
async def approve(*args): return True
async def execute(**kwargs):
    (root / 'effect.txt').write_text('once')
    os._exit(0)
registry = ToolRegistry(LocalExecutor(), journal=ExecutionJournal(bb, root, 'crashed'),
    policy=PolicyEngine(RunMode.SRC, ScopeGuard(Scope()), approval_gate=approve))
registry.register_builtin('write', execute, ToolSpec(name='write', kind=ToolKind.BUILTIN,
    risk=RiskLevel.INTRUSIVE))
asyncio.run(registry.call('write', {}))
'''
            subprocess.run([sys.executable, "-c", script, tmp], check=True, timeout=10)
            root = Path(tmp)
            bb = Blackboard(root / "bb.db")
            try:
                journal = ExecutionJournal(bb, root, "reopened")
                record = journal.list_recent()[0]
                self.assertEqual(record["execution_status"], "running")
                self.assertEqual(record["effect_status"], "unknown")
                self.assertEqual(record["replay_policy"], "never_automatic")
                self.assertIsNone(journal.result(record["invocation_id"]))
                self.assertEqual((root / "effect.txt").read_text(), "once")
                self.assertEqual(len(journal.list_recent()), 1)
            finally:
                bb.close()
