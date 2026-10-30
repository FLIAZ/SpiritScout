"""单题本地答案完整性、前台收敛与后台报告。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import Authorization, RunMode, Scope, Task
from spiritscout.ctf import report_jobs
from spiritscout.ctf.agents import CTFWebAgent
from spiritscout.ctf.flags import FlagManager
from spiritscout.launch import LaunchRequest, prepare_launch


class LocalAnswersTests(unittest.TestCase):
    def test_answer_option_survives_session_plan_rebuild(self) -> None:
        from spiritscout.web.app import SessionCreatePayload
        from spiritscout.web.sessions import Session, SessionManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = SessionManager(SystemConfig(workspace=WorkspaceConfig(root=tmp)), [])
            for multiple in (False, True):
                payload = SessionCreatePayload(mode="ctf", mission="解题 http://fixture.test",
                                               multiple_answers=multiple).model_dump()
                session = Session(session_id="local", mode="ctf", target="http://fixture.test",
                                  workspace=root / "local", launch_request=payload)
                rebuilt = manager._rebuild_plan(session)
                self.assertIs(rebuilt.challenges[0]["multiple_answers"], multiple)
            self.assertFalse(SessionCreatePayload().multiple_answers)

    def test_multiple_answers_require_explicit_complete_set_and_survive_restart(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "flags.json"
            manager = FlagManager(local_only=True, store_path=store)
            first = manager.register("answer-one", challenge_id="one", found_by="agent")
            self.assertFalse(
                manager.confirm_complete(
                    "one", [first.flag_id], "还有第二个答案", expected=2
                )
            )
            second = manager.register(
                "answer-two", challenge_id="one", found_by="agent"
            )
            self.assertFalse(manager.confirm_complete("one", [first.flag_id], "已完成"))
            self.assertFalse(
                manager.confirm_complete("one", [first.flag_id, second.flag_id], "")
            )
            self.assertTrue(
                manager.confirm_complete(
                    "one",
                    [first.flag_id, second.flag_id],
                    "两个子目标都有验证依据",
                    expected=2,
                )
            )
            loaded = FlagManager(local_only=True, store_path=store)
            self.assertEqual(loaded.completed_ids(), {"one"})
            loaded.register("third-answer", challenge_id="one", found_by="agent")
            self.assertFalse(
                FlagManager(local_only=True, store_path=store).completed_ids()
            )

    def test_finish_does_not_submit_or_stop_after_first_of_multiple_answers(
        self,
    ) -> None:
        agent = object.__new__(CTFWebAgent)
        agent.flag_manager = FlagManager(local_only=True, multiple_answers=True)
        task = Task(
            task_id="task-local", agent_type="ctf_web", task_type="ctf_web",
            target="http://fixture.test",
            payload={"challenge": {"id": "one", "flag_count": 2}},
        )
        first = agent.flag_manager.register("one", challenge_id="one", found_by="agent")
        outcome = agent._tool_finish_challenge(
            task, {"answer_ids": [first.flag_id], "reason": "核对题面"}
        )
        self.assertFalse(outcome.stop)
        second = agent.flag_manager.register(
            "two", challenge_id="one", found_by="agent"
        )
        outcome = agent._tool_finish_challenge(
            task,
            {"answer_ids": [first.flag_id, second.flag_id], "reason": "全部目标有依据"},
        )
        self.assertTrue(outcome.stop)
        self.assertNotIn("等待人工提交", outcome.text)
        self.assertFalse(any(flag.submitted for flag in agent.flag_manager.all()))


class BackgroundReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_choice_controls_answer_count_and_single_answer_finishes_immediately(self) -> None:
        for multiple in (False, True):
            plan = prepare_launch(LaunchRequest(mode=RunMode.CTF,
                mission="解题 http://fixture.test", multiple_answers=multiple))
            self.assertIs(plan.challenges[0]["multiple_answers"], multiple)
            agent = object.__new__(CTFWebAgent)
            agent.flag_manager = FlagManager(local_only=True, multiple_answers=multiple)
            agent.agent_id = "fixture-agent"
            agent.ctx = SimpleNamespace(blackboard=None)
            agent._record_flag = Mock()
            task = Task(task_id="local", agent_type="ctf_web", task_type="ctf_web",
                        target="http://fixture.test",
                        payload={"challenge": {"id": "one"}})
            outcome = await agent._tool_record_answer(task, SimpleNamespace(name="route"),
                {"answer": "valid-answer", "evidence_refs": ["inv-fixture"]})
            self.assertTrue(outcome.ok)
            self.assertIs(outcome.stop, not multiple)
            self.assertEqual(bool(agent.flag_manager.completed_ids()), not multiple)
            if not multiple:
                again = await agent._tool_record_answer(task, SimpleNamespace(name="route"),
                    {"answer": "unneeded-answer", "evidence_refs": ["inv-other"]})
                self.assertTrue(again.stop)
                self.assertEqual(len(agent.flag_manager.all()), 1)

    async def test_report_is_persisted_nonblocking_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entered, release = asyncio.Event(), asyncio.Event()

            async def writer(*args, **kwargs):
                entered.set()
                await release.wait()
                return "# 完整解题报告\n已验证的过程"

            with patch("spiritscout.ctf.report_jobs.writeup_body", side_effect=writer):
                task = report_jobs.enqueue(
                    root,
                    root / "report.md",
                    {"flags": [], "chronology": []},
                    Mock(),
                    NullAuditLogger(),
                )
                await entered.wait()
                self.assertFalse((root / "report.md").exists())
                self.assertEqual(report_jobs.status(root)["status"], "running")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(report_jobs.status(root)["status"], "queued")
                resumed = report_jobs.resume(root, Mock(), NullAuditLogger())
                release.set()
                await resumed
                self.assertEqual(report_jobs.status(root)["status"], "done")
                self.assertIn("完整解题报告", (root / "report.md").read_text())
                self.assertIsNone(report_jobs.resume(root, Mock(), NullAuditLogger()))
                self.assertIsNone(report_jobs.enqueue(root, root / "report.md",
                    {"flags": [], "chronology": []}, Mock(), NullAuditLogger()))

    async def test_standalone_runtime_returns_before_report_and_distill_finish(
        self,
    ) -> None:
        from spiritscout import runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=tmp),
                audit_log_path=str(root / "audit.jsonl"),
            )
            board = Mock(query=Mock(return_value=[]))
            auth = Authorization(
                auth_id="local-test",
                mode=RunMode.CTF,
                human_confirmed=True,
                scope=Scope(local_analysis=True),
            )
            infra = SimpleNamespace(
                cfg=cfg,
                workspace=root,
                authorization=auth,
                blackboard=board,
                gateway=Mock(), bus=None,
                audit=NullAuditLogger(),
                agent_models={},
                experience=Mock(),
                close=AsyncMock(),
                outcome="failed",
            )

            def assemble(mode, **kwargs):
                return SimpleNamespace(flag_manager=kwargs["flag_manager"], notes=[])

            async def run_master(infra, assembly, target, **kwargs):
                manager = assembly.flag_manager
                answer = manager.register(
                    "plain-answer", challenge_id="one", found_by="worker"
                )
                manager.confirm_complete("one", [answer.flag_id], "全部目标已有依据")
                return {"rounds_executed": 1, "dag_nodes": []}

            entered, release = asyncio.Event(), asyncio.Event()

            async def writer(*args, **kwargs):
                entered.set()
                await release.wait()
                return "# 后台报告"

            distill_entered = asyncio.Event()

            async def distill():
                distill_entered.set()
                await release.wait()

            distills = []

            def schedule(*args, **kwargs):
                task = asyncio.create_task(distill())
                distills.append(task)
                return task

            with (
                patch.object(
                    runtime, "_build_infra", new=AsyncMock(return_value=infra)
                ),
                patch.object(
                    runtime,
                    "_make_assembler",
                    return_value=SimpleNamespace(assemble=assemble),
                ),
                patch.object(runtime, "_run_master", side_effect=run_master),
                patch.object(
                    runtime, "_schedule_background_distill", side_effect=schedule
                ),
                patch("spiritscout.ctf.report_jobs.writeup_body", side_effect=writer),
            ):
                result = await runtime.run_ctf_engagement(
                    cfg=cfg, models=[], authorization=auth, challenges=[{"id": "one"}]
                )
                self.assertTrue(result.run_summary["answers_complete"])
                await entered.wait()
                await distill_entered.wait()
                self.assertFalse(Path(result.report_path).exists())
                infra.close.assert_awaited_once()
                release.set()
                await asyncio.gather(*distills, *list(report_jobs._TASKS.values()))
