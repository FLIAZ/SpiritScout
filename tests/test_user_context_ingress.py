"""用户边界原话接收即落盘，停止、预检和重启不依赖滚动对话史保留。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import RunMode
from spiritscout.gateway import world_state
from spiritscout.web.app import create_app
from spiritscout.web.sessions import DIALOGUE_HISTORY_MAX, Session, SessionManager


class UserContextIngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)),
                                models_config_path=str(root / "models.yaml"))
        self.app = create_app(self.cfg, [])
        self.manager = self.app.state.manager
        self.session = Session(session_id="sess-context", mode="src", target="local",
                               workspace=self.manager.root / "sess-context", status="stopped")
        self.manager._sessions[self.session.session_id] = self.session
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="http://127.0.0.1")
        self.addAsyncCleanup(self.client.aclose)

    def context(self) -> dict:
        blackboard = Blackboard(self.session.bb_path, readonly=True)
        try:
            return world_state.snapshot(blackboard=blackboard)["user_context"]
        finally:
            blackboard.close()

    async def test_stopped_message_is_persisted_before_resume_even_when_resume_fails(self) -> None:
        old = self.session.remember_user_message("仅测测试环境")
        message = "别碰生产，等我确认"

        async def fail_resume(_session_id: str) -> None:
            self.assertEqual(self.context()["statements"][-1]["text"], message)
            self.assertTrue(self.session.meta_path.exists())
            raise SpiritScoutError("模型尚未就绪")

        with patch.object(self.manager, "resume", side_effect=fail_resume):
            response = await self.client.post(f"/api/sessions/{self.session.session_id}/messages",
                                              json={"content": message})
        self.assertEqual(response.status_code, 409)
        restored = SessionManager(self.cfg, []).get(self.session.session_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.dialogue_history[-1]["content"], message)
        self.assertEqual(self.context()["statements"][0], old)
        self.assertEqual(self.context()["statements"][-1]["statement_id"],
                         restored.dialogue_history[-1]["statement_id"])

    async def test_originals_survive_rolling_history_and_restart_without_consumption(self) -> None:
        self.session.status = "running"
        count = DIALOGUE_HISTORY_MAX + 5
        for _ in range(count):
            response = await self.client.post(f"/api/sessions/{self.session.session_id}/messages",
                                              json={"content": "仅测 A，不测 B"})
            self.assertEqual(response.status_code, 200, response.text)
        restored = SessionManager(self.cfg, []).get(self.session.session_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.status, "interrupted")
        self.assertEqual(restored.user_inbox, [])
        self.assertEqual(len(restored.dialogue_history), DIALOGUE_HISTORY_MAX)
        blackboard = Blackboard(restored.bb_path)
        try:
            world_state.sync_user_context(blackboard, dialogue_history=restored.dialogue_history,
                                          mission="继续", standing_rules="")
        finally:
            blackboard.close()
        statements = self.context()["statements"]
        self.assertEqual(len(statements), count)
        self.assertEqual(len({item["statement_id"] for item in statements}), count)
        self.assertTrue(all(item["text"] == "仅测 A，不测 B" for item in statements))

    async def test_preflight_answer_survives_before_runtime_blackboard_is_bound(self) -> None:
        self.assertFalse(self.session.bb_path.exists())
        waiting = asyncio.create_task(self.session.questions.ask("master", "是否允许扩围？", []))
        try:
            await asyncio.sleep(0)
            qid = self.session.questions.view()[0]["question_id"]
            answer = "不允许扩围，原来的声明继续有效"
            response = await self.client.post(f"/api/sessions/{self.session.session_id}/questions/{qid}",
                                              json={"text": answer})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.context()["statements"], [
                {"kind": "answer", "question": "是否允许扩围？", "answer": answer,
                 "scope_added": [], "statement_id": qid}])
            self.assertEqual((await waiting)["answer"], answer)
            self.assertFalse(self.session.has_runtime_state())
        finally:
            self.session.questions.cancel_all()
            await asyncio.gather(waiting, return_exceptions=True)

    async def test_context_only_database_does_not_freeze_preflight_mode_routing(self) -> None:
        self.session.launch_request = {"mission": "CTF Web 赛题，附件里找 flag",
                                       "mode": "src", "mode_auto": True}
        self.session.remember_user_message("只做这道赛题")
        self.assertTrue(self.session.bb_path.exists())
        plan = self.manager._rebuild_plan(self.session)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.authorization.mode, RunMode.CTF)
        self.assertTrue(plan.needs_mode_routing)

    async def test_invalid_message_never_enters_boundary_context(self) -> None:
        with patch.object(self.manager, "resume", new=AsyncMock()) as resume:
            response = await self.client.post(f"/api/sessions/{self.session.session_id}/messages",
                                              json={"content": "x" * 4001})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.session.bb_path.exists())
        resume.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
