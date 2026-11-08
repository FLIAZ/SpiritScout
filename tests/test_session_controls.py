"""暂停保留运行实例，审批目的不透传远端，裁决后仍遵守暂停门。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.models import RunMode, Scope
from spiritscout.core.pause import PauseGate
from spiritscout.policy.approvals import ApprovalBroker
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session, SessionManager


class SessionControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_unpaused_releases_existing_waiter(self):
        gate = PauseGate()
        gate.pause()
        waiter = asyncio.create_task(gate.wait())
        await asyncio.sleep(0)
        gate.restore(False)
        await asyncio.wait_for(waiter, 1)
        self.assertFalse(gate.paused)

    async def test_continue_restored_paused_session_releases_rebuilt_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = SessionManager(SystemConfig(workspace=WorkspaceConfig(root=tmp)), [])
            workspace = manager.root / "sess-restored"
            workspace.mkdir()
            session = Session(session_id="sess-restored",mode="ctf",target="fixture",workspace=workspace,status="interrupted")
            session.pause_gate.restore(True)
            manager._sessions[session.session_id] = session
            async def rebuild(sid):
                session.pause_gate = PauseGate()
                session.pause_gate.restore(True)
                session.status = "running"
                return session
            manager.resume = AsyncMock(side_effect=rebuild)
            await manager.set_paused(session.session_id,False)
            manager.resume.assert_awaited_once_with(session.session_id)
            await asyncio.wait_for(session.pause_gate.wait(), 1)
            self.assertFalse(session.pause_gate.paused)

    async def test_pause_resume_keeps_task_and_route_state_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = SessionManager(SystemConfig(workspace=WorkspaceConfig(root=tmp)), [])
            workspace = manager.root / "sess-pause"
            workspace.mkdir()
            session = Session(session_id="sess-pause", mode="ctf", target="fixture",
                              workspace=workspace, status="running")
            manager._sessions[session.session_id] = session
            manager._run = AsyncMock()
            gate = session.pause_gate
            registry = ToolRegistry(SimpleNamespace(), pause_gate=gate)
            entered, finish = asyncio.Event(), asyncio.Event()
            state = {"step": 0, "results": []}
            async def execute():
                if state["step"] == 0:
                    entered.set()
                    await finish.wait()
                state["results"].append(state["step"])
                state["step"] += 1
                return ToolResult(tool="fixture", ok=True)
            registry.register_builtin("fixture", execute, ToolSpec("fixture", ToolKind.BUILTIN))
            async def run():
                for _ in range(2):
                    await registry.call("fixture", {})
            task = asyncio.create_task(run())
            session.task = task
            try:
                await entered.wait()
                await manager.set_paused(session.session_id, True)
                finish.set()
                for _ in range(4):
                    await asyncio.sleep(0)
                self.assertEqual(state["results"], [0])
                self.assertIs(session.task, task)
                self.assertFalse(task.done())
                await manager.set_paused(session.session_id, False)
                await asyncio.wait_for(task, 1)
                self.assertEqual(state["results"], [0, 1])
                self.assertIs(session.pause_gate, gate)
                manager._run.assert_not_called()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_mcp_missing_purpose_returns_to_agent_before_asking_user(self) -> None:
        broker = ApprovalBroker()
        policy = PolicyEngine(RunMode.SRC, ScopeGuard(Scope()), approval_gate=broker.ask)
        registry = ToolRegistry(SimpleNamespace(), policy=policy)
        execute = AsyncMock(return_value=ToolResult("remote", True))
        registry.register_builtin("mcp__yakit__execute", execute, ToolSpec(
            "mcp__yakit__execute", ToolKind.MCP, risk=RiskLevel.INTRUSIVE,
            action_required=True, approval_purpose_param="_approval_purpose"))
        for purpose in ("", "mcp__yakit__execute", "delete_files"):
            result = await registry.call("mcp__yakit__execute", {
                "action": "delete_files", "_approval_purpose": purpose})
            self.assertFalse(result.ok)
            self.assertIn("自然语言", result.error)
            self.assertEqual(broker.view(), [])
        execute.assert_not_awaited()

    async def test_approval_displays_purpose_and_resume_executes_only_the_approved_call(self) -> None:
        broker, gate = ApprovalBroker(), PauseGate()
        policy = PolicyEngine(RunMode.SRC, ScopeGuard(Scope()), approval_gate=broker.ask)
        registry = ToolRegistry(SimpleNamespace(), policy=policy, pause_gate=gate)
        execute = AsyncMock(return_value=ToolResult("remote", True))
        registry.register_builtin("remote", execute, ToolSpec("remote", ToolKind.MCP,
            description="测试夹具文件操作", risk=RiskLevel.INTRUSIVE, action_required=True,
            allowed_params={"action", "path"}, approval_purpose_param="_approval_purpose"))
        purpose = "删除隔离测试目录中本次生成的临时文件，以验证清理行为；这些临时文件将无法恢复。"
        task = asyncio.create_task(registry.call("remote", {
            "action": "delete_files", "path": "fixture.tmp", "_approval_purpose": purpose}))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            pending = broker.view()[0]
            self.assertEqual(pending["purpose"], purpose)
            self.assertEqual(pending["tool"], "remote")
            self.assertIn(purpose, pending["detail"])
            self.assertIn("删除文件", pending["detail"])
            gate.pause()
            broker.decide(pending["approval_id"], True)
            for _ in range(4):
                await asyncio.sleep(0)
            execute.assert_not_awaited()
            gate.resume()
            self.assertTrue((await asyncio.wait_for(task, 1)).ok)
            execute.assert_awaited_once_with(action="delete_files", path="fixture.tmp")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_ctf_remains_preapproved_and_mcp_purpose_is_removed(self) -> None:
        ask = AsyncMock()
        registry = ToolRegistry(SimpleNamespace(), policy=PolicyEngine(
            RunMode.CTF, ScopeGuard(Scope()), approval_gate=ask))
        execute = AsyncMock(return_value=ToolResult("remote", True))
        registry.register_builtin("remote", execute, ToolSpec("remote", ToolKind.MCP,
            risk=RiskLevel.INTRUSIVE, action_required=True, approval_purpose_param="_approval_purpose"))
        self.assertTrue((await registry.call("remote", {"action": "delete_files"})).ok)
        ask.assert_not_awaited()
        execute.assert_awaited_once_with(action="delete_files")


class PauseApiTests(unittest.TestCase):
    def test_ended_session_rejects_pause_instead_of_reporting_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(SystemConfig(workspace=WorkspaceConfig(root=tmp)), [])
            workspace = app.state.manager.root / "sess-finished"
            workspace.mkdir()
            session = Session(session_id="sess-finished", mode="ctf", target="fixture", workspace=workspace,
                              status="completed")
            app.state.manager._sessions[session.session_id] = session
            with TestClient(app, base_url="http://127.0.0.1") as client:
                for action in ("pause", "resume"):
                    self.assertEqual(client.post(f"/api/sessions/sess-finished/{action}").status_code, 409)
                self.assertEqual(session.status, "completed")
