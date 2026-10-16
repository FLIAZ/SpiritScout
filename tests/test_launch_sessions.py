from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.models import AgentType, BlackboardLayer, RunMode
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.web.app import _read_state
from spiritscout.web.sessions import Session, SessionManager

CTF_MISSION = (
    "一道ctf题目，拿flag："
    "http://8000-d310a0ca-737b-403d-82a7-000eb4beb8d3.challenge.ctfplus.cn/"
)


class LaunchRoutingTests(unittest.TestCase):
    def test_explicit_ctf_mission_is_deterministic(self) -> None:
        plan = prepare_launch(LaunchRequest(mission=CTF_MISSION))

        self.assertEqual(plan.authorization.mode, RunMode.CTF)
        self.assertTrue(plan.mode_auto)
        self.assertTrue(plan.needs_mode_routing)
        self.assertEqual(len(plan.challenges), 1)

    def test_ambiguous_web_mission_still_uses_semantic_router(self) -> None:
        plan = prepare_launch(LaunchRequest(
            mission="检查 https://example.test 的登录流程"))

        self.assertEqual(plan.authorization.mode, RunMode.SRC)
        self.assertTrue(plan.needs_mode_routing)


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_runtime_error_cannot_leave_session_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
            manager = SessionManager(cfg, [])
            workspace = root / "sessions" / "sess-test"
            workspace.mkdir(parents=True)
            session = Session(
                session_id="sess-test", mode="invalid", target="test",
                workspace=workspace, status="running", phase="starting")
            plan = prepare_launch(LaunchRequest(
                mode=RunMode.SRC, target="https://example.test"))
            # 故障注入：runtime 入口抛普通异常，验证会话故障隔离落终态
            # （不依赖真实装配链的失败点——那个会随实现漂移）
            with patch("spiritscout.runtime.run_engagement",
                       new=AsyncMock(side_effect=ValueError("注入的启动前故障"))):
                await manager._run(session, cfg, plan, True, [])

            self.assertEqual(session.status, "failed")
            self.assertEqual(session.phase, "failed")
            self.assertIn("ValueError", session.error)
            self.assertTrue(session.meta_path.exists())

    async def test_unstarted_auto_session_rebuilds_from_original_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
            manager = SessionManager(cfg, [])
            workspace = root / "sessions" / "sess-recover"
            workspace.mkdir(parents=True)
            session = Session(
                session_id="sess-recover", mode="src", target="old",
                workspace=workspace,
                launch_request={
                    "mission": CTF_MISSION, "mode": "src", "mode_auto": True,
                },
            )

            plan = manager._rebuild_plan(session)

            self.assertIsNotNone(plan)
            self.assertEqual(plan.authorization.mode, RunMode.CTF)
            self.assertTrue(plan.needs_mode_routing)

    async def test_state_exposes_startup_phase_without_blackboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "sess-phase"
            workspace.mkdir()
            session = Session(
                session_id="sess-phase", mode="ctf", target="challenge",
                workspace=workspace, status="running", phase="model_preflight")

            state = _read_state(session)

            self.assertEqual(state["session"]["phase"], "model_preflight")
            self.assertIsNone(state["business_context"])

    async def test_resume_cancels_stale_running_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
            manager = SessionManager(cfg, [])
            workspace = root / "sessions" / "sess-stale"
            workspace.mkdir(parents=True)
            session = Session(
                session_id="sess-stale", mode="ctf", target="challenge",
                workspace=workspace, status="stopped")
            bb = Blackboard(session.bb_path, bus=None)
            bb.write(
                BlackboardLayer.TASK_STATE, "task-old",
                {"task": {"task_id": "task-old", "status": "running"},
                 "summary": {"live": True},
                 "routes": [{"name": "solve", "status": "running"}]},
                agent_type=AgentType.MASTER)
            bb.write(
                BlackboardLayer.TASK_STATE, "dag-snapshot",
                {"dag_snapshot": [{"node_id": "old", "status": "running"}]},
                agent_type=AgentType.MASTER)
            bb.close()

            changed = manager._cancel_stale_tasks(session)

            bb = Blackboard(session.bb_path, bus=None)
            self.assertEqual(changed, 1)
            self.assertEqual(
                bb.read(BlackboardLayer.TASK_STATE, "task-old")["task"]["status"],
                "cancelled")
            self.assertEqual(
                bb.read(BlackboardLayer.TASK_STATE, "dag-snapshot")
                ["dag_snapshot"][0]["status"], "cancelled")
            bb.close()

    async def test_state_hides_tasks_outside_current_dag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "sess-current"
            workspace.mkdir()
            session = Session(
                session_id="sess-current", mode="ctf", target="challenge",
                workspace=workspace, status="completed", phase="completed",
                summary={"run": {"dag_nodes": [{
                    "node_id": "new", "task_ids": ["task-new"]}]}})
            bb = Blackboard(session.bb_path, bus=None)
            for task_id, status in (("task-old", "running"), ("task-new", "done")):
                bb.write(
                    BlackboardLayer.TASK_STATE, task_id,
                    {"task": {"task_id": task_id, "status": status},
                     "summary": {}, "routes": []},
                    agent_type=AgentType.MASTER)
            bb.close()

            state = _read_state(session)

            self.assertEqual(
                [task["task_id"] for task in state["tasks"]], ["task-new"])


if __name__ == "__main__":
    unittest.main()
