"""提问作答的真实 ASGI/Broker 链路；非法回答不得唤醒或隐式扩围。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.audit import AuditLogger, read_record
from spiritscout.core.models import Scope
from spiritscout.gateway import world_state
from spiritscout.policy.scope import ScopeGuard
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session


class QuestionAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_dependencies_block_only_declared_work_and_cancellation_preserves_question(self):
        from spiritscout.policy.questions import QuestionBroker
        from spiritscout.tools.invocation import ExecutionScope, current_execution_scope

        for mode in ("src", "ctf", "competition"):
            broker = QuestionBroker(interactive=mode != "competition")
            token = current_execution_scope.set(ExecutionScope(task_id="owner", route_id="route"))
            try:
                ask = asyncio.create_task(broker.ask("worker", "fixture", [],
                    blocking_task_ids=["dependent"]))
            finally:
                current_execution_scope.reset(token)
            await asyncio.sleep(0)
            if mode == "competition":
                self.assertTrue((await ask)["autonomous"])
                self.assertEqual(broker.view(), [])
                continue
            owner = asyncio.create_task(broker.wait_for_task("owner"))
            dependent = asyncio.create_task(broker.wait_for_task("dependent"))
            await broker.wait_for_task("independent")
            await asyncio.sleep(0)
            self.assertFalse(owner.done())
            self.assertFalse(dependent.done())
            dependent.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await dependent
            qid = broker.view()[0]["question_id"]
            self.assertEqual(set(broker.view()[0]["blocking_task_ids"]), {"owner", "dependent"})
            broker._resolve(qid, {"answer": "fixture", "scope_added": []})
            await asyncio.wait_for(owner, 1)
            self.assertEqual((await ask)["answer"], "fixture")

    async def test_multiple_team_questions_all_need_resolution(self):
        from spiritscout.policy.questions import QuestionBroker

        broker = QuestionBroker()
        asks = [asyncio.create_task(broker.ask("master", text, [], block_all_tasks=True))
                for text in ("first", "second")]
        await asyncio.sleep(0)
        blocked = asyncio.create_task(broker.wait_for_task("new-task"))
        await asyncio.sleep(0)
        first, second = [item["question_id"] for item in broker.view()]
        broker._resolve(first, {"answer": "one"})
        await asyncio.sleep(0)
        self.assertFalse(blocked.done())
        broker._resolve(second, {"answer": "two"})
        await asyncio.wait_for(blocked, 1)
        await asyncio.gather(*asks)

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
        self.session.questions._audit = AuditLogger(self.session.audit_path)
        # 真实端点/Broker，授权语义测试不访问收费模型。
        self.scope_resolver = AsyncMock(return_value=[])
        resolver_patch = patch("spiritscout.web.session_assistant._llm_answer_scope", self.scope_resolver)
        resolver_patch.start()
        self.addCleanup(resolver_patch.stop)
        self.guard = ScopeGuard(Scope(local_analysis=True))
        self.session.questions.bind_guard(self.guard)
        self.bb = Blackboard(root / "bb.db")
        self.addCleanup(self.bb.close)
        self.session.questions.bind_context(self.bb)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="http://127.0.0.1")
        self.addAsyncCleanup(self.client.aclose)
        self.tasks: list[asyncio.Task] = []
        self.addAsyncCleanup(self.cleanup_questions)

    async def cleanup_questions(self) -> None:
        self.session.questions.cancel_all()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def ask(self, options: list[str] | None = None, *,
                  multiple: bool = False, targets: list | None = None) -> tuple[str, asyncio.Task]:
        task = asyncio.create_task(self.session.questions.ask(
            "master", "下一步如何验证？", options if options is not None else ["只读验证", "等待补充"],
            targets if targets is not None else ["https://scope-test.invalid"], multiple=multiple))
        self.tasks.append(task)
        await asyncio.sleep(0)
        return self.session.questions.view()[0]["question_id"], task

    def url(self, qid: str) -> str:
        return f"/api/sessions/{self.session.session_id}/questions/{qid}"

    async def test_answer_original_is_shared_before_waiting_agent_resumes(self) -> None:
        qid, waiting = await self.ask([])
        answer = "先看登录页\n" * 90 + "末尾不要访问生产环境"
        response = await self.client.post(self.url(qid), json={"text": answer})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await waiting)["answer"], answer)
        context = world_state.snapshot(blackboard=self.bb)["user_context"]
        self.assertEqual(context["statements"], [{"kind": "answer", "question": "下一步如何验证？",
                                                 "answer": answer, "scope_added": [], "statement_id": qid}])

    async def test_full_answer_is_durable_and_linked_to_question(self) -> None:
        qid, waiting = await self.ask([], targets=[])
        answer = "允许只读测试，不修改真实数据。\n" * 50 + "完整末尾"
        result = await self.client.post(self.url(qid), json={"text": answer})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((await waiting)["answer"], answer)
        records = [read_record(line) for line in self.session.audit_path.read_text().splitlines()]
        answered = next(row for row in records if row["type"] == "user.answered")
        self.assertEqual(answered["detail"]["answer"], answer)
        self.assertEqual(answered["detail"]["question_id"], qid)
        self.assertEqual(answered["detail"]["question"], "下一步如何验证？")
        page = await self.client.get(f"/api/sessions/{self.session.session_id}/audit-page")
        self.assertEqual(page.status_code, 200)
        event = next(item["event"] for item in page.json()["items"] if item["event"]["type"] == "user.answered")
        self.assertEqual(event["detail"]["answer"], answer)
        self.scope_resolver.assert_not_awaited()

    async def test_choice_alone_approves_only_associated_target_without_model_call(self) -> None:
        options = ["仅允许测试 A", "仅允许测试 B", "不允许扩围"]
        targets = [{"target": "https://a.invalid", "approved_by": [options[0]]},
                   {"target": "https://b.invalid", "approved_by": [options[1]]}]
        for index in [0, 2]:
            qid, waiting = await self.ask(options, targets=targets)
            response = await self.client.post(self.url(qid), json={"choice": index})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual((await waiting)["scope_added"], ["https://a.invalid/"] if index == 0 else [])
        self.assertFalse(self.guard.is_allowed("https://b.invalid"))
        self.scope_resolver.assert_not_awaited()

    async def test_typed_partial_authorization_and_override_use_one_semantic_resolution(self) -> None:
        options = ["允许全部", "不允许"]
        targets = [{"target": "https://a.invalid", "approved_by": [options[0]]},
                   {"target": "https://b.invalid", "approved_by": [options[0]]}]
        qid, waiting = await self.ask(options, targets=targets)
        self.scope_resolver.return_value = ["https://b.invalid"]
        response = await self.client.post(self.url(qid), json={"choice": 0, "text": "只允许 B，不允许 A"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await waiting)["scope_added"], ["https://b.invalid/"])
        self.assertFalse(self.guard.is_allowed("https://a.invalid"))
        self.scope_resolver.assert_awaited_once()
        self.assertEqual(self.scope_resolver.call_args.args[-1], "只允许 B，不允许 A")

    async def test_concurrent_answer_requests_share_resolution_and_release_question_lock(self) -> None:
        qid, waiting = await self.ask()
        entered, release = asyncio.Event(), asyncio.Event()
        async def resolve(*args):
            entered.set()
            await release.wait()
            return ["https://scope-test.invalid"]
        self.scope_resolver.side_effect = resolve
        first = asyncio.create_task(self.client.post(self.url(qid), json={"text": "同意"}))
        await entered.wait()
        second = asyncio.create_task(self.client.post(self.url(qid), json={"text": "同意"}))
        release.set()
        responses = await asyncio.gather(first, second)
        self.assertEqual(sorted(result.status_code for result in responses), [200, 409])
        self.scope_resolver.assert_awaited_once()
        self.assertEqual((await waiting)["scope_added"], ["https://scope-test.invalid/"])
        self.assertIsNone(self.session.questions.answer_lock(qid))
        events = [read_record(line) for line in self.session.audit_path.read_text().splitlines()]
        self.assertEqual(sum(event["type"] == "user.answered" for event in events), 1)

    async def test_failed_or_cancelled_scope_resolution_does_not_consume_answer(self) -> None:
        from spiritscout.core.exceptions import SpiritScoutError
        qid, waiting = await self.ask()
        self.scope_resolver.side_effect = SpiritScoutError("fixture unavailable")
        response = await self.client.post(self.url(qid), json={"text": "同意"})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertFalse(waiting.done())
        self.assertFalse(self.guard.is_allowed("https://scope-test.invalid"))
        self.scope_resolver.side_effect = None
        async def stop(*args):
            self.session.questions.cancel_all()
            return ["https://scope-test.invalid"]
        self.scope_resolver.side_effect = stop
        response = await self.client.post(self.url(qid), json={"text": "同意"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue((await waiting)["cancelled"])
        self.assertFalse(self.guard.is_allowed("https://scope-test.invalid"))

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

    async def test_checked_targets_alone_are_a_valid_answer(self) -> None:
        """只勾选扩围目标（范围确认场景）也要能提交，不被当成空答复。"""
        qid, waiting = await self.ask()
        result = await self.client.post(self.url(qid), json={
            "add_targets": ["https://scope-test.invalid"]})
        self.assertEqual(result.status_code, 200, result.text)
        answer = await waiting
        self.assertEqual(answer["scope_added"], ["https://scope-test.invalid/"])
        self.assertIn("已确认目标", answer["answer"])
        self.assertTrue(self.guard.is_allowed("https://scope-test.invalid"))

    async def test_multi_select_joins_picked_options_and_supplement(self) -> None:
        qid, waiting = await self.ask(["复测登录", "检查上传", "先看接口"], multiple=True)
        self.assertTrue(self.session.questions.view()[0]["multiple"])
        response = await self.client.post(self.url(qid), json={
            "choices": [0, 2], "text": "另外留意越权",
            "add_targets": ["https://scope-test.invalid"]})
        self.assertEqual(response.status_code, 200, response.text)
        answer = await waiting
        self.assertEqual(answer["answer"], "复测登录、先看接口；另外留意越权")
        self.assertEqual(answer["scope_added"], ["https://scope-test.invalid/"])
        self.assertTrue(self.guard.is_allowed("https://scope-test.invalid"))

    async def test_multi_select_deduplicates_and_accepts_text_only(self) -> None:
        qid, waiting = await self.ask(["甲", "乙"], multiple=True)
        response = await self.client.post(self.url(qid), json={"choices": [1, 1]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await waiting)["answer"], "乙")

        qid, waiting = await self.ask(["甲", "乙"], multiple=True)
        response = await self.client.post(self.url(qid), json={"text": "都不选，改看日志"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await waiting)["answer"], "都不选，改看日志")

    async def test_multi_select_rejects_exclusive_and_invalid_payloads(self) -> None:
        qid, waiting = await self.ask(["甲", "乙"], multiple=True)
        for payload in [{"choice": 0, "choices": [1]}, {"choices": [2]},
                        {"choices": [0, -1]}, {"choices": []},
                        {"choices": [0], "text": "x" * 1001}]:
            with self.subTest(payload=payload):
                response = await self.client.post(self.url(qid), json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertFalse(waiting.done())
        self.assertEqual((await self.client.post(self.url(qid), json={"choices": [1]})).status_code, 200)
        self.assertEqual((await waiting)["answer"], "乙")

    async def test_single_select_question_rejects_multiple_choices(self) -> None:
        qid, waiting = await self.ask()
        response = await self.client.post(self.url(qid), json={"choices": [0, 1]})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(waiting.done())
        self.assertEqual((await self.client.post(self.url(qid), json={"choices": [0]})).status_code, 200)
        self.assertEqual((await waiting)["answer"], "只读验证")

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

    async def test_concrete_input_question_removes_deferred_promise_options(self) -> None:
        waiting = asyncio.create_task(self.session.questions.ask(
            "master", "请在下方输入可登录的测试账号和密码；如果没有账号请选择替代方案。",
            ["我可以提供账号（稍后在对话里给出）", "没有账号，只做未登录测试",
             "我先注册好再把账号给你"]))
        self.tasks.append(waiting)
        await asyncio.sleep(0)
        question = self.session.questions.view()[0]
        self.assertEqual(question["options"], ["没有账号，只做未登录测试"])
        response = await self.client.post(self.url(question["question_id"]), json={
            "text": "使用本次测试专用账号，凭据已在当前输入框提交"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("当前输入框提交", (await waiting)["answer"])

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


class ScopeAnswerModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_interpretation_uses_selected_model_and_validates_result(self) -> None:
        import json
        from types import SimpleNamespace

        from spiritscout.config.settings import ModelEntry
        from spiritscout.web.session_assistant import _llm_answer_scope

        with tempfile.TemporaryDirectory() as tmp:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            model = ModelEntry(model="fixture-selected")
            session = Session(session_id="scope-model", mode="src", target="local",
                              model=model.key, workspace=Path(tmp) / "session")
            question = {"question": "允许测试 A 和 B 吗？", "options": ["允许全部", "不允许"],
                        "targets": [{"target": "a.invalid"}, {"target": "b.invalid"}]}
            gateway = SimpleNamespace(chat=AsyncMock())
            with patch("spiritscout.config.load_model_config", return_value=[model]), \
                 patch("spiritscout.web.vault.apply_keys", side_effect=lambda entries: entries), \
                 patch("spiritscout.web.session_assistant._conversation_gateway", return_value=gateway):
                for output, expected in [('{"approved_targets":["b.invalid"]}', ["b.invalid"]),
                                         ('{"approved_targets":[]}', []),
                                         ('{"approved_targets":["unoffered.invalid"]}', None),
                                         ('{}', None)]:
                    gateway.chat.reset_mock()
                    gateway.chat.return_value = SimpleNamespace(content=output)
                    from spiritscout.core.exceptions import SpiritScoutError
                    if expected is None:
                        with self.assertRaises(SpiritScoutError):
                            await _llm_answer_scope(cfg, session, question, "只允许 B，不允许 A")
                    else:
                        self.assertEqual(await _llm_answer_scope(cfg, session, question, "只允许 B，不允许 A"), expected)
                    gateway.chat.assert_awaited_once()
                    self.assertEqual(gateway.chat.call_args.kwargs["model"], model.key)
                    data = json.loads(gateway.chat.call_args.args[0][1]["content"])
                    self.assertEqual(data["answer"], "只允许 B，不允许 A")
                    self.assertEqual(data["offered_targets"], ["a.invalid", "b.invalid"])
