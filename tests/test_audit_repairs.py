"""补充审计回归：真实沙箱/HTTP/子进程与任务结果、并发提交边界。"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent, SubAgentContext
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.bus.inprocess import InProcessBus
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.audit import AuditLogger, NullAuditLogger, sanitize_record
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    RunMode,
    Scope,
    Task,
    TaskStatus,
)
from spiritscout.ctf.flags import FlagManager, SubmitResult
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.builtin import _response_window, http_probe
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.journal import ExecutionJournal, read_recovery_view
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.run_lock import ExecutionRunLock
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import ToolKind, ToolSpec
from spiritscout.web.app import create_app
from spiritscout.web.events import stream_audit
from spiritscout.web.sessions import Session


class _ProbeAgent(SubAgent):
    health_prune_enabled = False

    def route_templates(self, task: Task) -> list[RoutePlan]:
        return []

    async def plan_routes(self, task: Task) -> list[RoutePlan]:
        return [RoutePlan("audit", "negative-or-failure", task.payload.get("steps", []))]

    async def execute_step(self, task: Task, route, plan, step: dict) -> StepResult:
        return StepResult(ok=step["ok"], note="有效阴性结果" if step["ok"] else "适配器启动失败")


class AuditTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bb = Blackboard(self.root / "bb.db")
        self.addAsyncCleanup(self.bb.aclose)
        self.bus = InProcessBus(rate_limit_per_second=0)
        await self.bus.start()
        self.addAsyncCleanup(self.bus.stop)
        policy = PolicyEngine(RunMode.CTF, ScopeGuard(Scope(local_analysis=True)))
        ctx = SubAgentContext(self.bus, self.bb, ConvergenceEngine(self.bb),
                              SimpleNamespace(), ToolRegistry(LocalExecutor()), policy)
        self.agent = _ProbeAgent(ctx, enable_llm_planning=False)

    def task(self, steps: list[dict]) -> Task:
        return Task(task_id="audit-task", task_type="recon", target="local",
                    agent_type=AgentType.RECON, payload={"steps": steps})

    async def test_all_failed_steps_do_not_unlock_downstream(self) -> None:
        task = self.task([{"ok": False}])
        dag = TaskDAG()
        first = dag.add_node("FIRST", "前置任务")
        dag.add_node("NEXT", "依赖任务", depends_on=[first.node_id])
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(pause_gate=None, enable_llm_planning=False,
            agents={AgentType.RECON: self.agent}, ring=None, blackboard=self.bb,
            audit=NullAuditLogger())
        result = await master._dispatch({"dag": dag.to_dict(), "round_no": 0,
            "tasks_dispatched": [{"node_id": first.node_id, "task": task.model_dump(mode="json")}]})
        updated = TaskDAG.from_dict(result["dag"])
        self.assertEqual(updated.get(first.node_id).status, TaskStatus.FAILED)
        self.assertEqual(updated.ready_nodes(), [])
        saved = self.bb.read(BlackboardLayer.TASK_STATE, task.task_id)
        self.assertEqual(saved["task"]["status"], "failed")
        self.assertEqual(saved["summary"]["routes_done"], 0)

    async def test_valid_negative_result_is_done(self) -> None:
        task = self.task([{"ok": True}])
        summary = await self.agent.run(task)
        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertEqual(summary["routes_done"], 1)

    async def test_published_route_conclusion_not_repeated_at_task_level(self) -> None:
        """路线已单独发过 agent.conclude 时，任务级收尾不再重复拼接同一结论。"""
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            ctx = SubAgentContext(
                self.bus, self.bb, ConvergenceEngine(self.bb),
                SimpleNamespace(), ToolRegistry(LocalExecutor()),
                PolicyEngine(RunMode.CTF, ScopeGuard(Scope(local_analysis=True))),
                audit=AuditLogger(audit_path))

            class _ConcludingAgent(_ProbeAgent):
                publish = False

                async def execute_step(self, task, route, plan, step):
                    if self.publish:
                        # 模拟 CTF 解题环：结论已随路线级 agent.conclude 上时间线
                        route.conclusion_published = True
                    return StepResult(ok=True, note="已拿到 Flag", final_note=True)

            def conclude_events() -> list[dict]:
                return [json.loads(line) for line in
                        audit_path.read_text(encoding="utf-8").splitlines()
                        if line.strip() and json.loads(line)["type"] == "agent.conclude"]

            # 未置位（SRC 侧形态）：任务级收尾照常拼一条汇总结论
            agent = _ConcludingAgent(ctx, enable_llm_planning=False)
            agent.publish = False
            await agent.run(self.task([{"ok": True}]))
            self.assertEqual(len(conclude_events()), 1)
            self.assertIn("audit：已拿到 Flag", conclude_events()[0]["detail"]["summary"])

            # 已置位（CTF 解题环形态）：同文本不再上第二遍时间线
            audit_path.write_text("", encoding="utf-8")
            agent = _ConcludingAgent(ctx, enable_llm_planning=False)
            agent.publish = True
            await agent.run(self.task([{"ok": True}]))
            self.assertEqual(conclude_events(), [])

    async def test_empty_route_is_cancelled_instead_of_success(self) -> None:
        task = self.task([])
        summary = await self.agent.run(task)
        self.assertEqual(task.status, TaskStatus.CANCELLED)
        self.assertEqual(summary["routes_cancelled"], 1)

    async def test_model_can_stop_with_blocked_dependencies(self) -> None:
        dag = TaskDAG()
        parent = dag.add_node("PARENT", "失败前置")
        child = dag.add_node("CHILD", "不再需要的后续", depends_on=[parent.node_id])
        dag.mark(parent.node_id, TaskStatus.FAILED)
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(audit=NullAuditLogger(), enable_llm_planning=True,
                                     extra={"user_inbox": []}, pause_gate=None)
        master._final_verdicts = Mock(return_value=[])
        master._llm_review = AsyncMock(return_value={"continue_iteration": False,
                                                    "reason": "目标已结束"})
        result = await master._review({"round_no": 0, "dag": dag.to_dict(),
                                       "round_views": [{"flags": {}}]})
        self.assertFalse(result["continue_iteration"])
        cancelled = TaskDAG.from_dict(result["dag"]).get(child.node_id)
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        self.assertIn("目标已结束", cancelled.meta["cancel_reason"])

    async def test_invalid_model_review_does_not_cancel_pending_task(self) -> None:
        dag = TaskDAG()
        node = dag.add_node("PENDING", "未执行任务")
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(audit=NullAuditLogger(), enable_llm_planning=True,
                                     extra={"user_inbox": []}, pause_gate=None)
        master._final_verdicts = Mock(return_value=[])
        for invalid in ({"reason": "缺少决策"}, {"continue_iteration": "false"}):
            master._llm_review = AsyncMock(return_value=invalid)
            result = await master._review({"round_no": 0, "dag": dag.to_dict(),
                                           "round_views": [{"flags": {}}]})
            self.assertTrue(result["continue_iteration"])
            self.assertEqual(TaskDAG.from_dict(result["dag"]).get(node.node_id).status, TaskStatus.PENDING)


class AuditProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_cli_reaps_background_with_or_without_inherited_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for inherited in (False, True):
                with self.subTest(inherited=inherited):
                    marker = Path(tmp) / f"marker-{inherited}"
                    code = ("import time; from pathlib import Path; time.sleep(.4); "
                            f"Path({str(marker)!r}).write_text('late')")
                    redirects = "" if inherited else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
                    parent = ("import subprocess,sys; "
                              f"subprocess.Popen([sys.executable,'-c',{code!r}]{redirects}); print('done')")
                    result = await LocalExecutor().run_argv(
                        ToolSpec("audit", ToolKind.CLI, timeout_seconds=2),
                        [sys.executable, "-c", parent])
                    self.assertTrue(result.ok, result.error)
                    self.assertIn("done", result.stdout)
                    self.assertLess(result.duration_ms, 1200)
                    await asyncio.sleep(.5)
                    self.assertFalse(marker.exists(), "工具返回后不得继续产生副作用")

    async def test_cancelled_cli_reaps_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "late"
            ready = Path(tmp) / "ready"
            child = ("import time; from pathlib import Path; time.sleep(.5); "
                     f"Path({str(marker)!r}).write_text('late')")
            parent = ("import subprocess,sys,time; from pathlib import Path; "
                      f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                      f"Path({str(ready)!r}).write_text('ready'); time.sleep(30)")
            task = asyncio.create_task(LocalExecutor().run_argv(
                ToolSpec("audit", ToolKind.CLI), [sys.executable, "-c", parent]))
            async with asyncio.timeout(3):
                while not ready.exists():
                    await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(.6)
            self.assertFalse(marker.exists())

    async def test_sandbox_hides_control_and_preserves_artifacts_and_readonly_attachments(self) -> None:
        if not sandbox_available():
            self.skipTest("需要 bubblewrap")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "attachments").mkdir()
            (root / "attachments" / "input").write_text("attachment")
            for name in ("bb.db", "audit.jsonl", "meta.json", "execution.key", "vault.key"):
                (root / name).write_text("host-control")
            code = f"""from pathlib import Path
r = Path({str(root)!r})
for name in ('bb.db', 'audit.jsonl', 'meta.json', 'execution.key', 'vault.key'):
    assert not (r / name).exists(), name
    try:
        (r / name).write_text('changed')
    except OSError:
        pass
    else:
        raise RuntimeError('control write permitted')
assert (r / 'attachments/input').read_text() == 'attachment'
try:
    (r / 'attachments/input').write_text('changed')
except OSError:
    pass
else:
    raise RuntimeError('attachment write permitted')
Path('persistent').write_text('saved')
(r / 'artifacts/result').write_text('artifact')
print('isolated')
"""
            result = await sandbox_exec(code, language="python", workspace_root=str(root))
            self.assertEqual(result.returncode, 0, result.stderr + result.error)
            self.assertIn("isolated", result.stdout)
            followup = await sandbox_exec("cat persistent ../artifacts/result",
                                           workspace_root=str(root))
            self.assertEqual(followup.stdout, "savedartifact")
            self.assertEqual((root / "bb.db").read_text(), "host-control")

    async def test_sandbox_rejects_symlink_shared_directory(self) -> None:
        if not sandbox_available():
            self.skipTest("需要 bubblewrap")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sandbox").symlink_to(root, target_is_directory=True)
            result = await sandbox_exec("true", workspace_root=str(root))
            self.assertFalse(result.ok)
            self.assertIn("符号链接", result.error)

    async def test_sandbox_workspace_scope_isolates_challenge_products(self) -> None:
        if not sandbox_available():
            self.skipTest("需要 bubblewrap")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = await sandbox_exec("echo A > marker", workspace_root=str(root),
                                       workspace_scope="e1-01")
            second = await sandbox_exec("echo B > marker", workspace_root=str(root),
                                        workspace_scope="e1-02")
            self.assertEqual(first.returncode, 0, first.stderr + first.error)
            self.assertEqual(second.returncode, 0, second.stderr + second.error)
            self.assertTrue(first.parsed["workdir"].endswith("sandbox/e1-01"),
                            first.parsed)
            self.assertTrue(second.parsed["workdir"].endswith("sandbox/e1-02"),
                            second.parsed)
            # 同名脚本产物与默认日志按题隔离，互不覆盖
            self.assertEqual((root / "sandbox" / "e1-01" / "marker").read_text(), "A\n")
            self.assertEqual((root / "sandbox" / "e1-02" / "marker").read_text(), "B\n")
            for result in (first, second):
                output = Path(result.parsed["output_file"])
                self.assertTrue(output.exists())
                self.assertEqual(output.parent, Path(result.parsed["workdir"]))


class AuditHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_compressed_http_window_is_bounded_without_httpx_auto_decode(self) -> None:
        plain = b"A" * (8 * 1024 * 1024)
        raw_deflate = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        for encoding, payload in (("gzip", gzip.compress(plain)),
                                   ("deflate", zlib.compress(plain)),
                                   ("deflate", raw_deflate.compress(plain) + raw_deflate.flush())):
            with self.subTest(encoding=encoding, size=len(payload)):
                async def serve(reader, writer, encoding=encoding, payload=payload):
                    await reader.readuntil(b"\r\n\r\n")
                    writer.write((f"HTTP/1.1 200 OK\r\nContent-Encoding: {encoding}\r\n"
                                  f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload)
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()

                server = await asyncio.start_server(serve, "127.0.0.1", 0)
                try:
                    port = server.sockets[0].getsockname()[1]
                    decoded_sizes = []
                    make_decoder = zlib.decompressobj

                    class Decoder:
                        def __init__(self, *args, _factory=make_decoder, **kwargs):
                            self.inner = _factory(*args, **kwargs)

                        def __getattr__(self, name):
                            return getattr(self.inner, name)

                        def decompress(self, data: bytes, max_length: int = 0,
                                       _sizes=decoded_sizes) -> bytes:
                            output = self.inner.decompress(data, max_length)
                            _sizes.append(len(output))
                            return output

                    # 禁止自动解压路径，验证真实请求只能走有界解压器。
                    with (patch.object(httpx.Response, "aiter_bytes", side_effect=AssertionError("auto decode")),
                          patch("spiritscout.tools.builtin.zlib.decompressobj", Decoder)):
                        result = await http_probe(f"http://127.0.0.1:{port}",
                            guard=ScopeGuard(Scope(local_analysis=True)), max_output_chars=1000)
                    self.assertTrue(result.ok, result.error)
                    self.assertTrue(result.parsed["body_truncated"])
                    self.assertLessEqual(max(decoded_sizes), 4001)
                    self.assertLess(len(result.stdout), 1200)
                finally:
                    server.close()
                    await server.wait_closed()

    async def test_short_identity_and_gzip_are_complete(self) -> None:
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield gzip.compress(b"short")

        response = httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Stream())
        self.assertEqual(await _response_window(response, 100), (b"short", False))

    async def test_invalid_compression_is_not_silently_accepted(self) -> None:
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield gzip.compress(b"short")[:-4]

        response = httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Stream())
        with self.assertRaises(httpx.DecodingError):
            await _response_window(response, 100)


class AuditOwnershipTests(unittest.TestCase):
    def test_same_key_other_workspace_still_cannot_reconcile_active_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / "bb.db")
            lock = ExecutionRunLock(root / "one")
            lock.acquire()
            other_lock = ExecutionRunLock(root / "two")
            other_lock.acquire()
            try:
                first = ExecutionJournal(bb, root / "one", "first")
                first.activate_run(lock)
                (root / "two").mkdir()
                (root / "two" / "execution.key").write_bytes((root / "one" / "execution.key").read_bytes())
                second = ExecutionJournal(bb, root / "two", "second")
                with self.assertRaises(ConfigurationError):
                    second.activate_run(other_lock)
                self.assertEqual(read_recovery_view(root / "bb.db")["runs"][0]["status"], "running")
            finally:
                other_lock.close()
                lock.close()
                bb.close()

    def test_legacy_database_rejects_existing_wrong_key_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / "bb.db")
            try:
                first = ExecutionJournal(bb, root / "one", "first")
                first.begin("private", "echo", "digest", "safe", params={"private": "saved"})
                with bb.transaction() as conn:
                    conn.execute("DROP TABLE execution_owner")
                    conn.execute("DELETE FROM execution_schema_migrations WHERE version=3")
                (root / "two").mkdir()
                (root / "two" / "execution.key").write_bytes(Fernet.generate_key())
                with self.assertRaises(ConfigurationError):
                    ExecutionJournal(bb, root / "two", "second")
                with bb.transaction() as conn:
                    self.assertEqual(conn.execute("SELECT MAX(version) FROM execution_schema_migrations").fetchone()[0], 2)
                self.assertEqual(first.parameters("private"), {"private": "saved"})
            finally:
                bb.close()

    def test_same_database_aliases_cannot_take_second_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "bb.db"
            first = ExecutionRunLock(root / "one", db)
            first.acquire()
            try:
                alias = root / "alias.db"
                alias.symlink_to(db)
                hardlink = root / "hardlink.db"
                os.link(db, hardlink)
                for path in (db, alias, hardlink):
                    second = ExecutionRunLock(root / "two", path)
                    with self.assertRaises(ConfigurationError):
                        second.acquire()
                    self.assertFalse(second.owned)
                    second.close()
            finally:
                first.close()
            second = ExecutionRunLock(root / "two", db)
            second.acquire()
            second.close()

    def test_other_workspace_cannot_reconcile_live_run_or_get_replacement_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / "bb.db")
            lock = ExecutionRunLock(root / "one")
            lock.acquire()
            try:
                first = ExecutionJournal(bb, root / "one", "first")
                first.activate_run(lock)
                with self.assertRaises(ConfigurationError):
                    ExecutionJournal(bb, root / "two", "second")
                self.assertFalse((root / "two" / "execution.key").exists())
                self.assertEqual(read_recovery_view(root / "bb.db")["runs"][0]["status"], "running")
            finally:
                lock.close()
                bb.close()


class AuditFlagTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_final_verdict_submits_once_for_success_and_rejection(self) -> None:
        for accepted in (True, False):
            calls = []

            async def submit(value: str, challenge: str, calls=calls, accepted=accepted) -> SubmitResult:
                calls.append(value)
                await asyncio.sleep(.01)
                return SubmitResult(accepted, "judged")

            flags = FlagManager(submit_fn=submit, rate_limit_per_minute=600000)
            candidate = flags.register("flag{audit}", challenge_id="one", found_by="test")
            await asyncio.gather(*(flags.submit(candidate) for _ in range(20)))
            self.assertEqual(len(calls), 1)
            self.assertEqual(candidate.accepted, accepted)


class AuditPlaintextTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_and_historical_audit_stay_verbatim_on_disk_stream_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            log = AuditLogger(path)
            record = {"type": "agent.tool_call", "actor": "test", "detail": {"calls": [
                {"arguments": {"code": "UNMARKED_SECRET_VALUE", "url": "https://x/?token=URL_SECRET"}}],
                "note": "Authorization: Bearer HEADER_SECRET", "url": "https://user:PASS_SECRET@x"}}
            log.log(record["type"], record["actor"], record["detail"])
            # 原文落盘：会话页实时监控 agent 的真实数据流，不做键值脱敏
            for secret in ("UNMARKED_SECRET_VALUE", "URL_SECRET", "HEADER_SECRET", "PASS_SECRET"):
                self.assertIn(secret, path.read_text())
            path.write_text(json.dumps(record) + "\n")
            frames = "".join([frame async for frame in stream_audit(path, max_frames=1)])
            exported = log.export(Path(tmp) / "export.jsonl").read_text()
            self.assertEqual(sanitize_record(sanitize_record(record)), sanitize_record(record))
            for secret in ("UNMARKED_SECRET_VALUE", "URL_SECRET", "HEADER_SECRET", "PASS_SECRET"):
                self.assertIn(secret, frames + exported + json.dumps(log.read_all()))

    async def test_historical_audit_download_returns_records_as_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp),
                               models_config_path=str(root / "models.yaml"))
            app = create_app(cfg, [])
            with TestClient(app, base_url="http://127.0.0.1") as client:
                session = Session(session_id="audit-test", mode="src", target="local",
                                  workspace=root / "sessions" / "audit-test", status="completed")
                session.workspace.mkdir(parents=True)
                session.audit_path.write_text(json.dumps({"type": "tool.call", "detail": {
                    "params": {"command": "UNMARKED_SECRET_VALUE"}}}) + "\n")
                app.state.manager._sessions[session.session_id] = session
                response = client.get("/api/sessions/audit-test/audit/download")
                self.assertEqual(response.status_code, 200)
                self.assertIn("UNMARKED_SECRET_VALUE", response.text)
                self.assertIn("attachment", response.headers["content-disposition"])
