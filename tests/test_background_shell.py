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
