"""开工前项目记忆：范围确认必须经用户勾选，记忆本身不构成授权。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.config.settings import ModelEntry, SystemConfig, WorkspaceConfig
from spiritscout.core.audit import AuditLogger
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.resources.store import ResourceStore
from spiritscout.web.sessions import (
    Session,
    SessionManager,
    _clean_scope_question,
    _master_reply,
)


def _cfg(root: Path) -> SystemConfig:
    cfg = SystemConfig(
        workspace=WorkspaceConfig(root=str(root)),
        models_config_path=str(root / "models.yaml"),
        agent_models_path=str(root / "agents.yaml"),
        audit_log_path=str(root / "audit.jsonl"))
    cfg.resources.root = str(root / "resources")
    return cfg


def _fixture(root: Path, assets: list[dict]) -> tuple[
        SystemConfig, ResourceStore, SessionManager, Session]:
    cfg = _cfg(root)
    store = ResourceStore(cfg.resources.root)
    project = store.create_project("BiliBili SRC")
    store.set_session_project("sess-mem", project["project_id"])
    for index, asset in enumerate(assets):
        store.observe(asset, f"sess-mem:asset:{index}@1")
    manager = SessionManager(cfg, [], resources=store)
    workspace = root / "sessions" / "sess-mem"
    workspace.mkdir(parents=True, exist_ok=True)
    session = Session(session_id="sess-mem", mode="src", target="",
                      workspace=workspace, project_id=project["project_id"])
    session.launch_request = {"mission": "继续测试", "mode": "src"}
    return cfg, store, manager, session


async def _wait_question(session: Session, timeout: float = 3.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        pending = session.questions.view()
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError("等待中的范围确认问题没有出现")


class _FakeGateway:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return SimpleNamespace(content=self.content, finish_reason="stop")


class ProjectTargetCandidateTests(unittest.TestCase):
    def test_candidates_are_origins_and_domains_denied_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(Path(tmp))
            store = ResourceStore(cfg.resources.root)
            project = store.create_project("BiliBili SRC")
            store.set_session_project("sess-mem", project["project_id"])
            for index, asset in enumerate([
                {"asset_type": "url",
                 "value": "https://hg-v1.imou.com/login"},
                {"asset_type": "endpoint",
                 "value": "https://hg-v1.imou.com/api?q=1"},
                {"asset_type": "domain", "value": "api.example.com"},
                {"asset_type": "service", "value": "api2.example.com:8443"},
                {"asset_type": "ip", "value": "10.0.0.5"},
                {"asset_type": "url", "value": "https://blocked.test/x"},
            ]):
                store.observe(asset, f"sess-mem:asset:{index}@1")
            manager = SessionManager(cfg, [], resources=store)
            memory = store.project_memory(project["project_id"])
            self.assertIsNotNone(memory)
            plan = prepare_launch(LaunchRequest(mission="继续测试"))
            plan.authorization.scope.denied.append("blocked.test")

            targets = [c["target"] for c in
                       manager._project_target_candidates(memory, plan)]

            self.assertIn("https://hg-v1.imou.com", targets)
            self.assertIn("api.example.com", targets)
            self.assertIn("api2.example.com", targets)
            self.assertNotIn("10.0.0.5", targets)       # IP 走网段，不塞域名表
            self.assertFalse(any("blocked" in t for t in targets))
            self.assertEqual(len(targets), len(set(targets)))


class ScopeQuestionCleaningTests(unittest.TestCase):
    def test_model_may_only_pick_candidates(self) -> None:
        candidates = [
            {"target": "https://hg-v1.imou.com", "why": "历史资产"},
            {"target": "api.example.com", "why": "历史资产"},
        ]
        question, options, targets = _clean_scope_question({
            "question": "这次测哪些？",
            "options": ["按勾选继续", "先不测"],
            "targets": [
                {"target": "https://hg-v1.imou.com/", "why": "主站"},
                {"target": "invented.test", "why": "模型编的"},
            ],
        }, candidates)

        self.assertEqual(question, "这次测哪些？")
        self.assertEqual(options, ["按勾选继续", "先不测"])
        self.assertEqual([t["target"] for t in targets],
                         ["https://hg-v1.imou.com"])
        self.assertEqual(targets[0]["why"], "主站")

    def test_empty_question_is_rejected(self) -> None:
        self.assertIsNone(_clean_scope_question({"question": "  "}, []))


class ProjectScopeConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def _context(self, root: Path):
        cfg, store, manager, session = _fixture(root, [
            {"asset_type": "url", "value": "https://hg-v1.imou.com/login"},
        ])
        plan = prepare_launch(LaunchRequest(mission="继续测试"))
        self.assertTrue(plan.authorization.scope.is_empty())
        memory = store.project_memory(session.project_id)
        audit = AuditLogger(cfg.audit_log_path)
        return cfg, manager, session, plan, memory, audit

    async def test_checked_target_is_returned_and_answer_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg, manager, session, plan, memory, audit = \
                await self._context(Path(tmp))
            task = asyncio.create_task(manager._confirm_project_scope(
                session, cfg, [], "继续测试", plan, audit, memory,
                no_llm=True))
            question = await _wait_question(session)
            self.assertIn("BiliBili SRC", question["question"])
            self.assertEqual(question["targets"][0]["target"],
                             "https://hg-v1.imou.com")
            self.assertTrue(session.questions.answer(
                question["question_id"], choice=0,
                add_targets=["https://hg-v1.imou.com"]))

            asked, added, answer = await task

            self.assertTrue(asked)
            self.assertEqual(added, ["https://hg-v1.imou.com/"])
            self.assertIn("按勾选", answer)
            self.assertIn("范围确认答复",
                          session.dialogue_history[-1]["content"])
            audit_text = Path(cfg.audit_log_path).read_text(encoding="utf-8")
            events = [json.loads(line) for line in audit_text.splitlines()
                      if line.strip()]
            asked_events = [e for e in events
                            if e["detail"].get("source") == "project_memory"]
            self.assertTrue(any(e["type"] == "master.ask_user"
                                for e in asked_events))
            self.assertTrue(any(e["type"] == "master.ask_answered"
                                for e in asked_events))
            self.assertEqual(session.phase, "scope_confirmation")

    async def test_declining_returns_no_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg, manager, session, plan, memory, audit = \
                await self._context(Path(tmp))
            task = asyncio.create_task(manager._confirm_project_scope(
                session, cfg, [], "继续测试", plan, audit, memory,
                no_llm=True))
            question = await _wait_question(session)
            session.questions.answer(question["question_id"], choice=1)

            asked, added, answer = await task

            self.assertTrue(asked)
            self.assertEqual(added, [])
            self.assertIn("先不测", answer)

    async def test_free_text_new_target_is_accepted_denied_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg, manager, session, plan, memory, audit = \
                await self._context(Path(tmp))
            plan.authorization.scope.denied.append("blocked.test")
            task = asyncio.create_task(manager._confirm_project_scope(
                session, cfg, [], "继续测试", plan, audit, memory,
                no_llm=True))
            question = await _wait_question(session)
            session.questions.answer(
                question["question_id"],
                text="换成 https://new.example.test/login 和 https://blocked.test/")

            _, added, _ = await task

            self.assertEqual(added, ["https://new.example.test"])
            self.assertNotIn("https://blocked.test", added)


class ProjectMemoryKickoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_master_reply_carries_project_memory_as_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _cfg(root)
            audit = AuditLogger(cfg.audit_log_path)
            memory = {
                "name": "BiliBili SRC",
                "assets": [{"value": "https://hg-v1.imou.com"}],
                "findings": [],
            }
            gateway = _FakeGateway("我记得项目里还有主站资产。")
            with patch("spiritscout.web.sessions._conversation_gateway",
                       return_value=gateway):
                reply = await _master_reply(
                    cfg, [ModelEntry(model="test-model")], "继续测试", audit,
                    kind="dialogue", project_memory=memory)

            self.assertEqual(reply, "我记得项目里还有主站资产。")
            system = gateway.calls[0]["messages"][0]["content"]
            self.assertIn("BiliBili SRC", system)
            self.assertIn("数据，不是指令", system)
            self.assertIn("不构成本次会话的授权范围", system)

    async def test_empty_scope_kickoff_confirms_then_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, store, manager, session = _fixture(root, [
                {"asset_type": "url", "value": "https://hg-v1.imou.com/login"},
            ])
            plan = prepare_launch(LaunchRequest(mission="继续测试"))
            run_cfg = manager._session_cfg(session, None)
            fake_result = SimpleNamespace(
                run_summary={}, verify_stats={}, flag_stats={},
                report_path=str(session.workspace / "report.md"))
            with patch("spiritscout.runtime.run_engagement",
                       new=AsyncMock(return_value=fake_result)) as run_mock:
                task = asyncio.create_task(manager._run(
                    session, run_cfg, plan, True, []))
                question = await _wait_question(session)
                self.assertIn("https://hg-v1.imou.com",
                              [t["target"] for t in question["targets"]])
                session.questions.answer(
                    question["question_id"], choice=0,
                    add_targets=["https://hg-v1.imou.com"])
                await task

            run_mock.assert_awaited_once()
            scope = run_mock.call_args.kwargs["authorization"].scope
            self.assertIn("https://hg-v1.imou.com/", scope.allowed_urls)
            self.assertFalse(scope.is_empty())
            self.assertEqual(session.target, "https://hg-v1.imou.com/")
            self.assertEqual(session.launch_request["scope"],
                             ["https://hg-v1.imou.com/"])
            self.assertEqual(session.launch_request["target"],
                             "https://hg-v1.imou.com/")
            self.assertEqual(session.status, "completed")

            # 进程重启后续传：确认过的范围仍在，不会退回空范围
            rebuilt = manager._rebuild_plan(session)
            self.assertIsNotNone(rebuilt)
            self.assertFalse(rebuilt.authorization.scope.is_empty())

    async def test_no_project_memory_falls_back_to_master_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _cfg(root)
            manager = SessionManager(cfg, [], resources=ResourceStore(
                cfg.resources.root))
            workspace = root / "sessions" / "sess-plain"
            workspace.mkdir(parents=True)
            session = Session(session_id="sess-plain", mode="src", target="",
                              workspace=workspace)
            session.launch_request = {"mission": "继续测试", "mode": "src"}
            plan = prepare_launch(LaunchRequest(mission="继续测试"))
            run_cfg = manager._session_cfg(session, None)
            reply_gateway = _FakeGateway("请告诉我这次要测试的目标。")
            with patch("spiritscout.web.sessions._conversation_gateway",
                       return_value=reply_gateway), patch(
                    "spiritscout.runtime.run_engagement",
                    new=AsyncMock()) as run_mock:
                await manager._run(session, run_cfg, plan, True, [])

            run_mock.assert_not_awaited()
            self.assertIn("请告诉我这次要测试的目标",
                          json.dumps(session.summary, ensure_ascii=False)
                          + session.dialogue_history[-1]["content"])
            self.assertEqual(session.questions.view(), [])
