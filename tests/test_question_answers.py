"""提问作答的真实 ASGI/Broker 链路；非法回答不得唤醒或隐式扩围。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.models import Scope
from spiritscout.policy.scope import ScopeGuard
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session


class QuestionAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)),
                           models_config_path=str(root / "models.yaml"))
        self.app = create_app(cfg, [])
        self.session = Session(session_id="question-test", mode="src", target="local",
                               workspace=root / "session", status="running")
        self.app.state.manager._sessions[self.session.session_id] = self.session
        self.guard = ScopeGuard(Scope(local_analysis=True))
        self.session.questions.bind_guard(self.guard)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="http://127.0.0.1")
        self.addAsyncCleanup(self.client.aclose)
        self.tasks: list[asyncio.Task] = []
        self.addAsyncCleanup(self.cleanup_questions)

    async def cleanup_questions(self) -> None:
        self.session.questions.cancel_all()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def ask(self, options: list[str] | None = None) -> tuple[str, asyncio.Task]:
        task = asyncio.create_task(self.session.questions.ask(
            "master", "下一步如何验证？", options if options is not None else ["只读验证", "等待补充"],
            ["https://scope-test.invalid"]))
        self.tasks.append(task)
        await asyncio.sleep(0)
        return self.session.questions.view()[0]["question_id"], task

    def url(self, qid: str) -> str:
        return f"/api/sessions/{self.session.session_id}/questions/{qid}"

    async def test_choice_resumes_branch_and_only_checked_targets_are_added(self) -> None:
        qid, waiting = await self.ask()
        result = await self.client.post(self.url(qid), json={
            "choice": 0, "add_targets": ["https://scope-test.invalid", "https://unoffered.invalid"]})
        self.assertEqual(result.status_code, 200, result.text)
        answer = await waiting
        self.assertEqual(answer["answer"], "只读验证")
        self.assertEqual(answer["scope_added"], ["https://scope-test.invalid/"])
        self.assertTrue(self.guard.is_allowed("https://scope-test.invalid"))
        self.assertFalse(self.guard.is_allowed("https://unoffered.invalid"))
        self.assertEqual(self.session.questions.view(), [])

    async def test_typed_answer_takes_priority_without_implicit_scope_expansion(self) -> None:
        qid, waiting = await self.ask()
        result = await self.client.post(self.url(qid), json={"choice": 1, "text": "  先看登录页\n再确认  "})
        self.assertEqual(result.status_code, 200)
        self.assertEqual((await waiting)["answer"], "先看登录页\n再确认")
        self.assertFalse(self.guard.is_allowed("https://scope-test.invalid"))

    async def test_invalid_input_keeps_question_pending_and_scope_unmodified(self) -> None:
        qid, waiting = await self.ask()
        for payload in [{}, {"text": "  "}, {"choice": True}, {"choice": "0"},
                        {"choice": -1}, {"choice": 99, "add_targets": ["https://scope-test.invalid"]},
                        {"text": 5}, {"text": "x" * 1001}, {"choice": 0, "add_targets": "bad"},
                        {"choice": 0, "add_targets": [3]}, {"choice": 0, "addTargets": []}]:
            with self.subTest(payload=payload):
                response = await self.client.post(self.url(qid), json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertFalse(waiting.done())
                self.assertEqual(len(self.session.questions.view()), 1)
                self.assertFalse(self.guard.is_allowed("https://scope-test.invalid"))
        self.assertEqual((await self.client.post(self.url(qid), json={"choice": 1})).status_code, 200)
        self.assertEqual((await waiting)["answer"], "等待补充")

    async def test_free_text_without_options_and_duplicate_submission(self) -> None:
        qid, waiting = await self.ask([])
        self.assertEqual((await self.client.post(self.url(qid), json={"choice": 0})).status_code, 422)
        self.assertEqual((await self.client.post(self.url(qid), json={"text": "自定义答复"})).status_code, 200)
        self.assertEqual((await waiting)["answer"], "自定义答复")
        self.assertEqual((await self.client.post(self.url(qid), json={"text": "再次提交"})).status_code, 409)

    async def test_other_session_cannot_answer_this_question(self) -> None:
        qid, waiting = await self.ask()
        other = Session(session_id="other", mode="src", target="local", workspace=Path(self.tmp.name) / "other")
        self.app.state.manager._sessions[other.session_id] = other
        response = await self.client.post(f"/api/sessions/other/questions/{qid}", json={"choice": 0})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(waiting.done())

    async def test_targets_carry_plain_language_why_and_answer_matches_by_target(self) -> None:
        task = asyncio.create_task(self.session.questions.ask(
            "master", "需要新增测试范围吗？", ["允许", "拒绝"],
            [{"target": "https://pass.test.invalid", "why": "登录接口域名，登录态走访需要"},
             "https://legacy.test.invalid"]))
        self.tasks.append(task)
        await asyncio.sleep(0)
        view = self.session.questions.view()[0]
        # 说明随目标一起进入前端渲染；裸字符串目标降级为空说明
        self.assertEqual(view["targets"], [
            {"target": "https://pass.test.invalid", "why": "登录接口域名，登录态走访需要"},
            {"target": "https://legacy.test.invalid", "why": ""}])
        response = await self.client.post(self.url(view["question_id"]), json={
            "text": "允许", "add_targets": ["https://pass.test.invalid"]})
        self.assertEqual(response.status_code, 200, response.text)
        answer = await task
        self.assertEqual(answer["scope_added"], ["https://pass.test.invalid/"])
        self.assertTrue(self.guard.is_allowed("https://pass.test.invalid"))
