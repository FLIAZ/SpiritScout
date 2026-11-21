"""真实 Linux 沙箱持续输出、条件等待、定时唤醒及进程组回收。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.pause import PauseGate
from spiritscout.tools.background import SPECS, ShellJobs, seconds_until
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available


@unittest.skipUnless(sandbox_available(), "需要 Linux bubblewrap")
class BackgroundShellTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gate = PauseGate()
        self.jobs = ShellJobs(
            str(self.root), audit=NullAuditLogger(), pause_gate=self.gate
        )

    async def asyncTearDown(self):
        await self.jobs.aclose()
        self.tmp.cleanup()

    async def start(self, code, scope=""):
        result = await self.jobs.start(code, workspace_scope=scope)
        self.assertTrue(result.ok, result.error)
        return result.parsed["job_id"]

    async def test_split_output_wakes_without_waiting_for_process_exit(self):
        job = await self.start("printf RE; sleep .1; printf ADY; sleep 10")
        result = await asyncio.wait_for(self.jobs.wait(job, contains="READY"), 5)
        self.assertTrue(result.parsed["matched"])
        self.assertEqual(result.parsed["status"], "running")
        await self.jobs.stop(job)
        self.assertEqual((await self.jobs.status(job)).parsed["status"], "stopped")
        self.assertEqual(self.jobs.archive.get(job)["archive_state"], "stopped", "停止记录不冒充完整的退出输出")

    async def test_stderr_exit_without_match_and_wait_timeout(self):
        job = await self.start("printf SIGNAL >&2; sleep .1; exit 7")
        self.assertTrue(
            (
                await self.jobs.wait(job, contains="SIGNAL", stream="stderr", timeout=5)
            ).parsed["matched"]
        )
        ended = await self.jobs.wait(job, contains="never", timeout=5)
        self.assertFalse(ended.parsed["matched"])
        self.assertEqual(ended.parsed["returncode"], 7)
        job = await self.start("sleep 10")
        result = await self.jobs.wait(job, contains="READY", timeout=0.02)
        self.assertTrue(result.parsed["wait_timed_out"])
        self.assertEqual(self.jobs.get(job).status, "running")

    async def test_condition_cannot_bypass_manual_pause_or_scope(self):
        job = await self.start("sleep .1; printf READY; sleep 10", scope="a")
        self.gate.pause()
        waiter = asyncio.create_task(
            self.jobs.wait(job, contains="READY", workspace_scope="a")
        )
        await asyncio.sleep(0.3)
        self.assertFalse(waiter.done())
        self.gate.resume()
        self.assertTrue((await asyncio.wait_for(waiter, 5)).parsed["matched"])
        with self.assertRaises(ValueError):
            await self.jobs.status(job, workspace_scope="b")
        other = ShellJobs(str(self.root), audit=NullAuditLogger())
        with self.assertRaises(ValueError):
            await other.status(job)
        await other.aclose()

    async def test_nonblocking_watch_does_not_block_other_jobs_and_notifies_owner_once(
        self,
    ):
        token = current_execution_scope.set(ExecutionScope(agent_id="agent-1"))
        try:
            first = await self.start(
                "sleep .2; printf RE; sleep .05; printf ADY; sleep 1", scope="a"
            )
            watched = await self.jobs.watch(first, "READY", workspace_scope="a")
            self.assertFalse(watched.parsed["blocking"])
        finally:
            current_execution_scope.reset(token)
        token = current_execution_scope.set(ExecutionScope(agent_id="agent-2"))
        try:
            second = await self.start("printf INDEPENDENT", scope="a")
        finally:
            current_execution_scope.reset(token)
        await asyncio.wait_for(self.jobs.get(second, "a").task, 5)
        self.assertEqual(self.jobs.get(first, "a").status, "running")
        self.assertTrue(self.jobs.drain("agent-2", "a"))
        self.assertEqual(self.jobs.drain("agent-2", "a"), [])
        await self.jobs.wait(first, contains="READY", workspace_scope="a", timeout=5)
        notices = self.jobs.drain("agent-1", "a")
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0]["matched"])
        self.assertEqual(self.jobs.drain("agent-1", "a"), [])
        self.assertEqual(self.jobs.drain("agent-1", "b"), [])

    async def test_closing_runtime_kills_actual_sandbox_process(self):
        job = await self.start("echo $$ > background.pid; sleep 20")
        path = self.root / "sandbox" / "background.pid"
        for _ in range(100):
            if path.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(path.exists())
        pid = self.jobs.get(job).pid
        await self.jobs.aclose()
        self.assertEqual(self.jobs.get(job).status, "stopped")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_timer_and_registry_reject_identity_override(self):
        registry = ToolRegistry(SimpleNamespace(), pause_gate=self.gate)
        registry.register_builtin("shell_start", self.jobs.start, SPECS["shell_start"])
        result = await registry.call(
            "shell_start",
            {"code": "true", "action": "analysis", "workspace_root": "/tmp/foreign"},
        )
        self.assertFalse(result.ok)
        self.assertEqual(self.jobs.jobs, {})
        registry.register_builtin("shell_events",self.jobs.events,SPECS["shell_events"])
        denied=await registry.call("shell_events",{"workspace_scope":"foreign"})
        self.assertFalse(denied.ok,"零参数工具也不能注入其他任务的可信范围")
        self.assertTrue((await self.jobs.delay(seconds=0.01)).ok)
        for args in (
            {"seconds": -1},
            {"seconds": float("nan")},
            {"until": "2030-01-01T00:00:00"},
            {"seconds": 1, "until": "2030-01-01T00:00:00Z"},
        ):
            with self.assertRaises(ValueError):
                seconds_until(**args)


    async def test_complete_archive_survives_output_rotation_and_keeps_model_tail_contract(self):
        from spiritscout.tools.background_archive import BackgroundArchive
        code = "printf ARCHIVE-BEGIN; head -c 23000000 /dev/zero; printf ARCHIVE-END; printf separate-error >&2"
        job = await self.start(code)
        await asyncio.wait_for(self.jobs.get(job).task, 15)
        status = await self.jobs.status(job)
        self.assertEqual(status.parsed["status"], "completed")
        self.assertTrue(status.ok)
        self.assertEqual(status.parsed["output_window_chars"], 65536)
        self.assertNotIn("command", status.parsed, "完整档案不进入模型状态请求")
        self.assertLessEqual(len(__import__('json').loads(status.stdout)["stdout"]), 2000)
        await self.jobs.aclose()
        archive = BackgroundArchive(self.root)
        try:
            item = archive.get(job)
            self.assertEqual(item["command"], code)
            self.assertEqual(item["archive_state"], "complete")
            self.assertIsNotNone(item["finished_at"])
            self.assertEqual(item["stdout_bytes"], 23000024)
            chunks = archive.chunks(job, "stdout", end=item["stdout_bytes"])
            first = next(chunks)
            self.assertTrue(first.startswith(b"ARCHIVE-BEGIN"))
            total = len(first)
            last = first
            for last in chunks:
                total += len(last)
            self.assertEqual(total, item["stdout_bytes"])
            self.assertTrue(last.endswith(b"ARCHIVE-END"))
            self.assertEqual(archive.output(job, "stderr")["text"], "separate-error")
        finally:
            archive.close()

    async def test_archive_write_failure_does_not_break_output_condition_or_command(self):
        from unittest.mock import patch

        from spiritscout.tools.background_archive import BackgroundArchive
        with self.assertLogs("spiritscout.tools.background", level="WARNING"), patch.object(BackgroundArchive, "append", side_effect=OSError("disk full")):
            job = await self.start("printf READY; sleep .05; printf DONE")
            waited = await self.jobs.wait(job, contains="READY", timeout=5)
            self.assertTrue(waited.parsed["matched"])
            await asyncio.wait_for(self.jobs.get(job).task, 5)
        status = await self.jobs.status(job)
        self.assertEqual(status.parsed["status"], "completed")
        self.assertTrue(status.ok)
        self.assertIn("READYDONE", status.stdout)
        self.assertTrue(self.jobs.get(job).archive_error)
        self.assertEqual(self.jobs.archive.get(job)["archive_state"], "error")


@unittest.skipUnless(sandbox_available(), "需要真实 Linux bubblewrap")
class BackgroundAPITests(unittest.TestCase):
    def test_parallel_task_list_output_stop_and_session_isolation(self):
        import time
        from contextlib import asynccontextmanager

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from spiritscout.tools.background import task_rows
        from spiritscout.web.background_tasks import mount_background_tasks

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b = root / "a", root / "b"
            a.mkdir()
            b.mkdir()
            sessions = {
                "a": SimpleNamespace(workspace=a),
                "b": SimpleNamespace(workspace=b),
            }

            @asynccontextmanager
            async def lifespan(app):
                jobs = ShellJobs(str(a), audit=NullAuditLogger())
                app.state.jobs = jobs
                await jobs.start(
                    "printf READY; sleep 20",
                    label="listener",
                    workspace_scope="challenge-a",
                )
                await jobs.start(
                    "printf SECOND; sleep 20",
                    label="independent",
                    workspace_scope="challenge-b",
                )
                yield
                await jobs.aclose()

            app = FastAPI(lifespan=lifespan)
            mount_background_tasks(app, SimpleNamespace(get=sessions.get))
            with TestClient(app) as client:
                for _ in range(100):
                    response = client.get("/api/sessions/a/background-tasks")
                    rows = response.json()["items"]
                    if len(rows) == 2 and all(row["stdout"] for row in rows):
                        break
                    time.sleep(0.02)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(
                    {r["label"] for r in rows}, {"listener", "independent"}
                )
                self.assertEqual(
                    client.get("/api/sessions/b/background-tasks").json()["items"], []
                )
                job = next(row for row in rows if row["label"] == "listener")
                self.assertEqual(
                    client.post(
                        f"/api/sessions/b/background-tasks/{job['job_id']}/stop"
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.post(
                        f"/api/sessions/a/background-tasks/{job['job_id']}/stop"
                    ).json()["status"],
                    "stopped",
                )
                remaining = next(
                    row
                    for row in client.get("/api/sessions/a/background-tasks").json()[
                        "items"
                    ]
                    if row["label"] == "independent"
                )
                self.assertEqual(remaining["status"], "running")
            self.assertTrue(all(row["status"] == "stopped" for row in task_rows(a)))


class BackgroundArchiveTests(unittest.TestCase):
    """档案契约独立于模型尾窗；旧索引、完整输出和损坏状态不混淆。"""

    def setUp(self):
        from spiritscout.tools.background_archive import BackgroundArchive
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = BackgroundArchive(self.root, writable=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def row(job="one", status="completed"):
        return {"job_id": job, "created_at": 1.0, "status": status, "command": "printf '原文'", "label": job, "returncode": 0}

    def test_full_history_restart_paging_search_and_exact_download(self):
        from spiritscout.tools.background_archive import BackgroundArchive
        stdout = ("起点\n" + "正文🙂\n" * 12000 + "关键内容\n").encode() + b"\x00\xff"
        stderr = b"separate error\n"
        self.store.save(self.row(), new=True)
        # 切分恰好落在字符/搜索词中间，下载仍保持字节原貌。
        for raw in (stdout[:5], stdout[5:-10], stdout[-10:]):
            self.store.append("one", "stdout", raw)
        self.store.append("one", "stderr", stderr)
        for index in range(60):
            self.store.save({**self.row(str(index)), "created_at": index + 2}, new=True)
        self.store.close()
        self.store = BackgroundArchive(self.root)
        first = self.store.listing()
        self.assertEqual(first["total"], 61)
        self.assertEqual(len(first["items"]), 50)
        self.assertEqual(len(self.store.listing(50)["items"]), 11)
        self.assertEqual(self.store.get("one")["command"], self.row()["command"])
        self.assertEqual(b"".join(self.store.chunks("one", "stdout", end=len(stdout))), stdout)
        self.assertEqual(b"".join(self.store.chunks("one", "stderr", end=len(stderr))), stderr)
        tail = self.store.output("one", "stdout", limit=4096)
        self.assertTrue(tail["has_earlier"])
        previous = self.store.output("one", "stdout", before=tail["start"], limit=4096)
        self.assertEqual(previous["end"], tail["start"])
        self.assertNotIn("�", previous["text"], "分页不破坏正常 UTF-8 字符")
        match = self.store.output("one", "stdout", query="关键内容")
        self.assertEqual(match["match"], stdout.find("关键内容".encode()))
        self.assertIn("关键内容", match["text"])
        self.assertIsNone(self.store.output("one", "stdout", query="关键内容", after=match["match"] + 1)["match"])
        self.assertLess(first["stored_bytes"], len(stdout), "压缩存档不重复保存大段文本")
        self.assertEqual((self.root / 'background-tasks/archive.db').stat().st_mode & 0o777, 0o600)

    def test_quota_and_manual_clear_do_not_delete_other_history_or_replay(self):
        import os

        from spiritscout.tools.background_archive import BackgroundArchive
        self.store.close()
        self.store = BackgroundArchive(self.root, writable=True, limit=4096)
        self.store.save(self.row(), new=True)
        self.store.append("one", "stdout", b"preserved")
        self.store.append("one", "stdout", os.urandom(9000))
        self.assertEqual(self.store.get("one")["archive_state"], "limited")
        self.assertEqual(b"".join(self.store.chunks("one", "stdout", end=9)), b"preserved")
        self.store.save(self.row("active", "running"), new=True)
        with self.assertRaisesRegex(ValueError, "运行中"):
            self.store.clear("active")
        self.store.clear("one")
        self.store.save(self.row())  # 晚到的状态刷新不能恢复已清理的命令或输出。
        self.assertEqual(self.store.get("one")["archive_state"], "cleared")
        self.assertNotIn("command", self.store.get("one"))
        self.assertEqual(self.store.get("active")["status"], "running")
        self.store.append("active", "stdout", b"new output")
        self.assertEqual(self.store.get("active")["stdout_bytes"], 10)

    def test_legacy_and_corrupt_blocks_never_look_like_complete_empty_output(self):
        import json

        from spiritscout.tools.background_archive import BackgroundArchive
        self.store.close()
        folder = self.root / "background-tasks"
        (folder / "jobs.json").write_text(json.dumps([{**self.row("legacy"), "stdout": "old tail"}]))
        self.store = BackgroundArchive(self.root, writable=True)
        self.assertEqual(self.store.get("legacy")["archive_state"], "legacy")
        self.assertEqual(self.store.get("legacy")["stdout"], "old tail")
        self.store.save(self.row(), new=True)
        self.store.append("one", "stdout", b"original")
        with self.store.conn:
            self.store.conn.execute("UPDATE chunks SET data=? WHERE job='one'", (b"invalid",))
        with self.assertRaises((ValueError, __import__('zlib').error)):
            self.store.output("one", "stdout")
        self.store.close()
        (folder / "jobs.json").write_text("broken legacy index")
        self.store = BackgroundArchive(self.root, writable=True)
        self.assertEqual(self.store.get("one")["command"], self.row()["command"], "旧副本损坏不能阻止读取已有权威档案")

    def test_archive_paths_cannot_follow_symlinks(self):
        from spiritscout.tools.background_archive import BackgroundArchive
        other = self.root / "other"
        other.mkdir()
        (other / "background-tasks").symlink_to(self.root / "background-tasks", target_is_directory=True)
        with self.assertRaises(OSError):
            BackgroundArchive(other, writable=True)

    def test_api_full_output_is_session_scoped_and_cleanup_is_explicit(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from spiritscout.web.background_tasks import mount_background_tasks
        self.store.save(self.row(), new=True)
        stdout = b"original command output\n" * 5000
        self.store.append("one", "stdout", stdout)
        self.store.save(self.row("orphan", "running"), new=True)
        self.store.append("orphan", "stderr", b"before crash")
        other = self.root / "other"
        other.mkdir()
        app = FastAPI()
        mount_background_tasks(app, SimpleNamespace(get={"a": SimpleNamespace(workspace=self.root), "b": SimpleNamespace(workspace=other)}.get))
        with TestClient(app) as client:
            url = "/api/sessions/a/background-tasks"
            data = client.get(url).json()
            self.assertEqual(data["counts"].get("running"), 0)
            self.assertEqual(data["counts"]["interrupted"], 1)
            self.assertNotIn("command", data["items"][0], "列表轮询不带命令全文")
            self.assertEqual(client.get(url + "/one").json()["command"], self.row()["command"])
            output = client.get(url + "/one/output", params={"q": "command output"})
            self.assertEqual(output.status_code, 200)
            self.assertIn("command output", output.json()["text"])
            self.assertEqual(client.get(url + "/one/download").content, stdout)
            self.assertEqual(client.get(url + "/one/download?stream=command").content.decode(), self.row()["command"])
            self.assertEqual(client.get("/api/sessions/b/background-tasks/one").status_code, 404)
            self.assertEqual(client.get("/api/sessions/b/background-tasks/one/download").status_code, 404)
            self.assertEqual(client.get(url + "/one/output?stream=../../etc/passwd").status_code, 422)
            self.assertEqual(client.delete(url + "/orphan/archive").status_code, 200, "重启遗留任务可显式清理")
            self.assertEqual(client.delete(url + "/one/archive").status_code, 200)
            self.assertEqual(client.get(url + "/one").json()["archive_state"], "cleared")
            self.assertEqual(client.get(url + "/one/download").status_code, 409)
