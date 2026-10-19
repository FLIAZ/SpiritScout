"""运行排他、中断核对、加密检查点与真实工具链路的回归。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from spiritscout.agents.master.agent import MasterAgent, MasterContext
from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent, SubAgentContext
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.bus.inprocess import InProcessBus
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.models import AgentType, Authorization, RunMode, Scope
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope
from spiritscout.tools.journal import (
    ExecutionJournal,
    read_execution_page,
    read_recovery_view,
)
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.run_lock import ExecutionRunLock
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "session"
        self.bb = Blackboard(self.root / "bb.db")
        self.addAsyncCleanup(self.bb.aclose)
        self.journal = ExecutionJournal(self.bb, self.root, "run-old")

    async def test_concurrent_scope_and_encrypted_parameters_are_isolated(self) -> None:
        registry = ToolRegistry(LocalExecutor(), journal=self.journal)
        registry.register(ToolSpec(name="echo", kind=ToolKind.CLI,
                                  command_template=["/bin/echo", "{value}"]))

        async def run(index: int) -> ToolResult:
            token = current_execution_scope.set(ExecutionScope(
                task_id=f"task-{index}", route_id=f"route-{index}", agent_id="test"))
            try:
                return await registry.call("echo", {"value": f"private-{index}"})
            finally:
                current_execution_scope.reset(token)

        first, second = await asyncio.gather(run(1), run(2))
        self.assertIsNone(current_execution_scope.get())
        page = read_execution_page(self.root / "bb.db")
        self.assertEqual([(r["task_id"], r["route_id"]) for r in page["items"]],
                         [("task-1", "route-1"), ("task-2", "route-2")])
        for index, result in enumerate((first, second), 1):
            self.assertEqual(self.journal.parameters(result.invocation_id), {"value": f"private-{index}"})
        with self.bb.transaction() as conn:
            values = conn.execute("SELECT params_cipher FROM tool_invocations").fetchall()
        self.assertTrue(all(b"private-" not in row[0] for row in values))
        self.assertNotIn("private-", json.dumps(page))

    async def test_interrupted_run_is_reconciled_without_replaying_or_reusing_approval(self) -> None:
        lock = ExecutionRunLock(self.root)
        lock.acquire()
        self.addCleanup(lock.close)
        self.journal.activate_run(lock)
        self.journal.begin("write", "write", "one", "never_automatic", params={"value": "private"})
        self.journal.transition("write", "running", effect="unknown")
        self.journal.begin("waiting", "write", "two", "never_automatic")
        self.journal.transition("waiting", "awaiting_approval")
        self.journal.begin("done", "read", "three", "safe")
        self.journal.transition("done", "succeeded", result=ToolResult(tool="read", ok=True, stdout="saved"))
        self.journal.checkpoint("master", "master", "before_dispatch", {"secret": "snapshot-private"})
        lock.close()
        new_lock = ExecutionRunLock(self.root)
        new_lock.acquire()
        self.addCleanup(new_lock.close)
        resumed = ExecutionJournal(self.bb, self.root, "run-new")
        resumed.activate_run(new_lock)
        rows = {r["invocation_id"]: r for r in resumed.list_recent()}
        self.assertEqual(rows["write"]["execution_status"], "unknown")
        self.assertEqual(rows["write"]["effect_status"], "unknown")
        self.assertEqual(rows["waiting"]["execution_status"], "cancelled")
        self.assertEqual(resumed.result("done").stdout, "saved")
        self.assertEqual(len(rows), 3)
        self.assertEqual(resumed.latest_checkpoint("master", "master")["state"],
                         {"secret": "snapshot-private"})
        view = read_recovery_view(self.root / "bb.db")
        self.assertEqual(view["unresolved_count"], 1)
        self.assertEqual({r["run_id"]: r["status"] for r in view["runs"]},
                         {"run-old": "interrupted", "run-new": "running"})
        self.assertNotIn("snapshot-private", json.dumps(view))
        resumed.finish_run("cancelled")
        self.assertEqual(read_recovery_view(self.root / "bb.db")["runs"][0]["status"], "cancelled")

    async def test_reconciliation_requires_ownership_of_the_correct_workspace(self) -> None:
        lock = ExecutionRunLock(self.root)
        with self.assertRaises(ConfigurationError):
            self.journal.activate_run(lock)
        other = ExecutionRunLock(self.root.parent / "other")
        other.acquire()
        self.addCleanup(other.close)
        with self.assertRaises(ConfigurationError):
            self.journal.activate_run(other)
        self.assertEqual(read_recovery_view(self.root / "bb.db")["runs"], [])

    async def test_checkpoint_failure_rolls_back_its_event(self) -> None:
        with self.bb.transaction() as conn:
            conn.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON runtime_checkpoints "
                         "BEGIN SELECT RAISE(ABORT, 'injected'); END")
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.checkpoint("master", "master", "before_dispatch", {"value": "secret"})
        events = read_execution_page(self.root / "bb.db", events=True)
        self.assertEqual(events["items"], [])
        self.assertIsNone(self.journal.latest_checkpoint("master", "master"))

    async def test_checkpoint_only_missing_key_fails(self) -> None:
        self.journal.checkpoint("master", "master", "after_review", {"round_no": 1})
        (self.root / "execution.key").unlink()
        with self.assertRaises(ConfigurationError):
            ExecutionJournal(self.bb, self.root, "new")
        self.assertFalse((self.root / "execution.key").exists())

    async def test_parameters_only_missing_key_fails(self) -> None:
        self.journal.begin("pending", "write", "digest", "never_automatic", params={"token": "secret"})
        (self.root / "execution.key").unlink()
        with self.assertRaises(ConfigurationError):
            ExecutionJournal(self.bb, self.root, "new")
        self.assertFalse((self.root / "execution.key").exists())

    async def test_master_and_route_checkpoint_real_cli_and_reducer_state(self) -> None:
        bus = InProcessBus()
        await bus.start()
        self.addAsyncCleanup(bus.stop)
        registry = ToolRegistry(LocalExecutor(), journal=self.journal)
        # 角色目录只暴露已声明工具；此隔离场景用本地 echo 作为只读适配器。
        registry.register(ToolSpec(name="http_probe", kind=ToolKind.CLI,
                                  command_template=["/bin/echo", "{value}"]))
        policy = PolicyEngine(RunMode.CTF, ScopeGuard(Scope(local_analysis=True)))
        convergence = ConvergenceEngine(self.bb)
        context = SubAgentContext(bus=bus, blackboard=self.bb, convergence=convergence,
                                  gateway=SimpleNamespace(), tools=registry, policy=policy,
                                  journal=self.journal)

        class LocalAgent(SubAgent):
            agent_type = AgentType.RECON

            def route_templates(self, task):
                return []

            async def plan_routes(self, task):
                return [RoutePlan(name="local", strategy="验证记录", steps=[{"action": "read"}])]

            async def execute_step(self, task, route, plan, step):
                result = await self.ctx.tools.call("http_probe", {"value": "local-evidence"})
                return StepResult(ok=result.ok, note=result.stdout)

        agent = LocalAgent(context, enable_llm_planning=False)
        master = MasterAgent(MasterContext(
            bus=bus, blackboard=self.bb, convergence=convergence,
            gateway=SimpleNamespace(), policy=policy, agents={AgentType.RECON: agent},
            enable_llm_planning=False, journal=self.journal,
            tactic_map={"LOCAL": (AgentType.RECON, "local")},
            plan_override=lambda _: {"dag_nodes": [{"tactic": "LOCAL", "title": "验证本地输出"}]}))
        summary = await master.run(Authorization(auth_id="local", mode=RunMode.CTF,
                                                scope=Scope(local_analysis=True)), "local://fixture")
        saved = self.journal.latest_checkpoint("master", "master")
        self.assertEqual(saved["phase"], "after_review")
        self.assertEqual(len(saved["state"]["tasks_dispatched"]), 1)
        self.assertEqual(saved["state"]["round_views"], summary["round_views"])
        invocation = self.journal.list_recent()[0]
        self.assertTrue(invocation["task_id"])
        self.assertTrue(invocation["route_id"])
        self.assertEqual(invocation["agent_id"], agent.agent_id)
        route = self.journal.latest_checkpoint("route", invocation["route_id"])
        self.assertEqual(route["phase"], "after_step")
        self.assertEqual(route["state"]["step_index"], 0)
        self.assertEqual(route["state"]["result"]["note"], "local-evidence\n")
        planning = self.journal.planning_context(read_recovery_view(self.root / "bb.db"))
        self.assertEqual(planning["mode"], "checkpoint_replan")
        self.assertEqual(planning["checkpoint"]["phase"], "after_review")
        self.assertEqual(planning["checkpoint"]["nodes"][0]["status"], "done")
        self.assertNotIn("local-evidence", json.dumps(planning))


class LockProcessTests(unittest.TestCase):
    def test_second_process_is_rejected_until_owner_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "session"
            script = ("import sys\nfrom pathlib import Path\n"
                      "from spiritscout.tools.run_lock import ExecutionRunLock\n"
                      "lock=ExecutionRunLock(Path(sys.argv[1])); lock.acquire()\n"
                      "print('ready', flush=True)\nsys.stdin.readline()\n")
            with subprocess.Popen([sys.executable, "-c", script, str(workspace)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                lock = ExecutionRunLock(workspace)
                try:
                    with self.assertRaises(ConfigurationError):
                        lock.acquire()
                    child.kill()
                    child.wait(timeout=3)
                    lock.acquire()
                    self.assertTrue(lock.owned)
                finally:
                    lock.close()
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=3)


class JournalMigrationTests(unittest.TestCase):
    def test_v1_upgrade_keeps_result_and_creates_consistent_backup(self) -> None:
        from dataclasses import asdict

        from cryptography.fernet import Fernet

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / "bb.db")
            try:
                key = Fernet.generate_key()
                (root / "execution.key").write_bytes(key)
                old_result = ToolResult(tool="echo", ok=True, stdout="old-private-evidence")
                encrypted = Fernet(key).encrypt(json.dumps(asdict(old_result)).encode())
                with bb.transaction() as conn:
                    conn.execute("CREATE TABLE execution_schema_migrations(version INTEGER PRIMARY KEY,"
                                 "applied_at REAL NOT NULL)")
                    conn.execute("INSERT INTO execution_schema_migrations VALUES(1,1)")
                    conn.execute("CREATE TABLE tool_invocations(invocation_id TEXT PRIMARY KEY,"
                                 "run_id TEXT,tool_name TEXT,params_digest TEXT,execution_status TEXT,"
                                 "effect_status TEXT,replay_policy TEXT,result_cipher BLOB,"
                                 "created_at REAL,updated_at REAL)")
                    conn.execute("INSERT INTO tool_invocations VALUES('old','run-old','echo','digest',"
                                 "'succeeded','none','safe',?,1,1)", (encrypted,))
                legacy = read_execution_page(root / "bb.db")
                self.assertEqual(legacy["items"][0]["task_id"], "")
                self.assertNotIn("old-private", json.dumps(legacy))
                journal = ExecutionJournal(bb, root, "new")
                self.assertEqual(journal.result("old").stdout, "old-private-evidence")
                self.assertIsNone(journal.parameters("old"))
                import sqlite3

                backup = sqlite3.connect(root / "bb.db.pre-execution-v2.bak")
                try:
                    self.assertNotIn("params_cipher", {r[1] for r in backup.execute(
                        "PRAGMA table_info(tool_invocations)")})
                    self.assertEqual(backup.execute("SELECT count(*) FROM tool_invocations").fetchone()[0], 1)
                finally:
                    backup.close()
            finally:
                bb.close()

    def test_future_version_is_rejected_without_overwriting_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / "bb.db")
            try:
                journal = ExecutionJournal(bb, root, "old")
                journal.checkpoint("master", "master", "after_review", {"round_no": 1})
                with bb.transaction() as conn:
                    conn.execute("INSERT INTO execution_schema_migrations VALUES(99,1)")
                with self.assertRaises(ConfigurationError):
                    ExecutionJournal(bb, root, "new")
                self.assertEqual(journal.latest_checkpoint("master", "master")["state"]["round_no"], 1)
            finally:
                bb.close()

    def test_empty_history_read_does_not_create_a_database_or_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(read_recovery_view(root / "absent.db")["unresolved_count"], 0)
            self.assertEqual(list(root.iterdir()), [])


class InfraLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_runtime_start_is_rejected_and_clean_close_releases_owner(self) -> None:
        from spiritscout.config.settings import (
            ModelEntry,
            SystemConfig,
            WorkspaceConfig,
        )
        from spiritscout.runtime import _build_infra

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "session"
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)),
                               audit_log_path=str(root / "audit.jsonl"))
            cfg.blackboard.sqlite_path = str(root / "bb.db")
            cfg.knowledge.sqlite_path = str(root / "kb.db")
            auth = Authorization(auth_id="local", mode=RunMode.CTF,
                                 scope=Scope(local_analysis=True), human_confirmed=True)
            models = [ModelEntry(model="fixture", provider="mock")]
            infra = await _build_infra(cfg, models, auth)
            try:
                with self.assertRaises(ConfigurationError):
                    await _build_infra(cfg, models, auth)
                infra.outcome = "completed"
            finally:
                await infra.close()
            self.assertEqual(read_recovery_view(root / "bb.db")["runs"][0]["status"], "completed")
            next_infra = await _build_infra(cfg, models, auth)
            next_infra.outcome = "cancelled"
            await next_infra.close()
            self.assertEqual(read_recovery_view(root / "bb.db")["runs"][0]["status"], "cancelled")


class RouteLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_route_cancels_and_joins_its_siblings(self) -> None:
        from spiritscout.core.audit import NullAuditLogger
        from spiritscout.core.models import Task

        class TestAgent(SubAgent):
            agent_type = AgentType.RECON

            def route_templates(self, task):
                return []

            async def plan_routes(self, task):
                return [RoutePlan("bad", ""), RoutePlan("waiting", "")]

            async def execute_step(self, task, route, plan, step):
                return StepResult(ok=True)

        agent = object.__new__(TestAgent)
        agent.ctx = SimpleNamespace(audit=NullAuditLogger())
        agent.agent_id = "test"
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def run_route(task, plan, route, routes):
            if plan.name == "bad":
                await entered.wait()
                raise RuntimeError("fixture checkpoint failure")
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        agent._run_route = run_route
        task = Task(task_id="fixture", task_type="local", target="local://fixture",
                    agent_type=AgentType.RECON)
        with self.assertRaises(ExceptionGroup):
            await agent.run(task)
        self.assertTrue(cancelled.is_set())


class RecoveryApiTests(unittest.TestCase):
    def test_stopped_session_inflight_calls_are_visible_without_plaintext_or_db_mutation(self) -> None:
        from fastapi.testclient import TestClient

        from spiritscout.config.settings import SystemConfig, WorkspaceConfig
        from spiritscout.web.app import create_app
        from spiritscout.web.sessions import Session

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(SystemConfig(workspace=WorkspaceConfig(root=tmp),
                                          models_config_path=str(root / "models.yaml")), [])
            workspace = root / "sessions" / "interrupted"
            workspace.mkdir(parents=True)
            session = Session(session_id="interrupted", mode="src", target="local",
                              workspace=workspace, status="interrupted")
            app.state.manager._sessions[session.session_id] = session
            bb = Blackboard(session.bb_path)
            try:
                journal = ExecutionJournal(bb, workspace, "old")
                journal.begin("inflight", "write", "digest", "never_automatic", params={"key": "private"})
                journal.transition("inflight", "running", effect="unknown")
                journal.checkpoint("master", "master", "before_dispatch", {"secret": "private"})
                with TestClient(app, base_url="http://127.0.0.1") as client:
                    response = client.get("/api/sessions/interrupted/recovery")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["unresolved_count"], 1)
                    self.assertNotIn("private", response.text)
                    self.assertEqual(client.get("/api/sessions/absent/recovery").status_code, 404)
                    self.assertEqual(client.get("/api/sessions/interrupted/state").json()["recovery"],
                                     response.json())
                # 查看不能把 running 偷改成 unknown，接管必须先持有运行锁。
                self.assertEqual(journal.list_recent()[0]["execution_status"], "running")
            finally:
                bb.close()
