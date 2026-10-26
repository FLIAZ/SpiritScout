from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import AgentType, TaskStatus


class MasterReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_steering_keeps_observing_after_three_decisions_and_refill(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(audit=Mock(), ring=Mock())
        master._STEER_INTERVAL = 0.001
        ids = ["first"]
        observations = []
        enough = asyncio.Event()

        def digest(task_ids: list[str]) -> dict:
            observations.append(list(task_ids))
            return {"tasks": [{"agent": "ctf_web", "turn": len(observations)}]}

        async def decide(progress: dict, queue: object) -> list:
            if len(observations) == 3:
                ids.append("refill")
            if len(observations) >= 5:
                enough.set()
            return []

        master._progress_digest = digest
        master._llm_steer = AsyncMock(side_effect=decide)
        watcher = asyncio.create_task(master._steering_watch(ids))
        try:
            await asyncio.wait_for(enough.wait(), 1)
            self.assertGreaterEqual(master._llm_steer.await_count, 5)
            self.assertIn("refill", observations[-1])
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def test_reference_only_plan_dispatches_complete_original_challenge(self) -> None:
        original = {"id": "e1-01", "title": "真实赛题", "description": "完整题面" * 100,
                    "url": "http://challenge.test", "attachments": [{"path": "sample.bin"}],
                    "flag_count": 2, "custom_rule": {"new_format": True}}
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(
            audit=Mock(), pause_gate=None, plan_override=None, enable_llm_planning=True,
            agents={AgentType.CTF_WEB: object()},
            tactic_map={"CTF-WEB": (AgentType.CTF_WEB, "ctf_solve")},
            extra={"mode_prompt": "动态规划", "competition_context": {"challenges": [original]}})
        master._llm_plan = AsyncMock(return_value={"dag_nodes": [
            {"tactic": "CTF-WEB", "challenge_id": "e1-01", "title": "首批开工"}]})
        master._ensure_business_gate = lambda nodes: nodes
        master._write_dag_snapshot = Mock()
        result = await master._meta_plan({
            "target": "https://bench.test", "round_no": 0, "dag": TaskDAG().to_dict()})
        task = result["tasks_dispatched"][0]["task"]
        self.assertEqual(task["target"], original["url"])
        self.assertEqual(task["payload"]["challenge"], original)
        task["payload"]["challenge"]["attachments"][0]["path"] = "changed"
        self.assertEqual(original["attachments"][0]["path"], "sample.bin")

    def test_unknown_challenge_reference_is_rejected_and_legacy_plan_still_works(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(extra={})
        with self.assertRaisesRegex(SpiritScoutError, "不在当前赛题清单"):
            master._resolve_plan_challenges([{"tactic": "CTF-WEB", "challenge_id": "wrong"}])
        legacy = [{"tactic": "CTF-WEB", "meta": {"challenge": {"id": "legacy"}}}]
        self.assertEqual(master._resolve_plan_challenges(legacy), legacy)

    async def test_invalid_target_returns_reason_for_model_replanning(self) -> None:
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(
            audit=Mock(), pause_gate=None, plan_override=None,
            enable_llm_planning=True, agents={}, tactic_map={},
            extra={"mode_prompt": "按任务规则动态规划"})
        master._llm_plan = AsyncMock(return_value={"dag_nodes": [
            {"tactic": "TA0043", "target": "http://10.0.100.58, https://bench.test"}]})
        master._ensure_business_gate = lambda nodes: nodes
        master._write_dag_snapshot = Mock()
        result = await master._meta_plan({
            "target": "https://bench.test", "round_no": 0, "dag": TaskDAG().to_dict()})
        self.assertEqual(result["tasks_dispatched"], [])
        self.assertIn("target 必须是单个目标", master.ctx.extra["last_plan_result"]["rejected"])

    async def test_competition_with_pending_unsolved_skips_review_llm(self) -> None:
        dag = TaskDAG()
        dag.add_node(
            "CTF-WEB", "[web] next", meta={
                "challenge": {"id": "c2", "title": "next"}})
        audit = Mock()
        platform = SimpleNamespace(progress=Mock(return_value={
            "total_challenges": 2, "solved_ids": ["c1"],
            "unsolved_ids": ["c2"]}))
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(
            audit=audit, enable_llm_planning=True,
            extra={"mode_prompt": "竞赛策略", "platform": platform},
            plan_override=lambda state: {"dag_nodes": []},
            tactic_map={"CTF-WEB": (AgentType.CTF_WEB, "ctf_solve")},
            pause_gate=None)
        master._final_verdicts = Mock(return_value=[])
        master._llm_review = AsyncMock()
        state = {
            "target": "https://bench.example", "round_no": 0,
            "dag": dag.to_dict(), "tasks_dispatched": [{"node_id": "finished"}],
            "round_views": [{"vuln_in_pipeline": 0,
                             "flags": {"extracted": 1, "accepted": 1}}],
            "continue_iteration": True, "stop_reason": "",
            "plan_source": "mode_assembly",
        }

        result = await master._review(state)

        master._llm_review.assert_not_awaited()
        self.assertTrue(result["continue_iteration"])
        review = [c.args[2] for c in audit.log.call_args_list
                  if c.args[2].get("action") == "review"][-1]
        self.assertTrue(review["auto_continue"])

        # 资源持续不可用而整轮无任务时，仍让模型判断平台错误和后续处置。
        master._llm_review.return_value = {
            "continue_iteration": False, "reason": "平台资源持续不可用"}
        state["tasks_dispatched"] = []
        stopped = await master._review(state)
        master._llm_review.assert_awaited_once()
        self.assertFalse(stopped["continue_iteration"])

    def test_failed_dependency_does_not_trigger_auto_continue(self) -> None:
        dag = TaskDAG()
        blocker = dag.add_node("CTF-WEB", "failed")
        dag.mark(blocker.node_id, TaskStatus.FAILED)
        dag.add_node("CTF-WEB", "blocked", depends_on=[blocker.node_id],
                     meta={"challenge": {"id": "c2"}})
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(extra={
            "mode_prompt": "竞赛策略", "platform": SimpleNamespace(
                progress=lambda: {"unsolved_ids": ["c2"]})})

        self.assertFalse(master._competition_auto_continue(dag))

    async def test_deferred_challenge_cancels_nodes_and_closes_instance(self) -> None:
        dag = TaskDAG()
        dag.add_node(
            "CTF-WEB", "[web] stuck", meta={"challenge": {
                "id": "c1", "title": "stuck"}})
        platform = SimpleNamespace(close_instance=AsyncMock(return_value=True))
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(
            audit=Mock(), extra={"mode_prompt": "竞赛策略", "platform": platform})

        await master._apply_competition_decisions(dag, {
            "defer_challenges": [{"id": "c1", "reason": "连续无新增信息"}],
        })

        updated = dag.all_nodes()[0]
        self.assertEqual(updated.status, TaskStatus.CANCELLED)
        self.assertTrue(updated.meta["deferred"])
        self.assertEqual(updated.meta["defer_reason"], "连续无新增信息")
        platform.close_instance.assert_awaited_once()

    async def test_retry_challenge_clears_deferred_marker(self) -> None:
        dag = TaskDAG()
        dag.add_node(
            "CTF-WEB", "[web] retry", meta={
                "challenge": {"id": "c1", "title": "retry"},
                "deferred": True, "defer_reason": "旧阻塞"})
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(
            audit=Mock(), extra={"mode_prompt": "竞赛策略"})

        await master._apply_competition_decisions(dag, {
            "retry_challenges": [{"id": "c1", "reason": "出现新线索"}],
        })

        self.assertFalse(dag.all_nodes()[0].meta["deferred"])
        self.assertNotIn("defer_reason", dag.all_nodes()[0].meta)

    async def test_deferred_mode_replan_continues_to_next_round(self) -> None:
        dag = TaskDAG()
        finished = dag.add_node("FIRST", "finished direction")
        dag.mark(finished.node_id, TaskStatus.DONE)

        audit = Mock()
        master = object.__new__(MasterAgent)
        master.agent_id = "master-test"
        master.ctx = SimpleNamespace(
            audit=audit,
            enable_llm_planning=True,
            extra={"user_inbox": []},
            plan_override=lambda state: {"dag_nodes": []},
            tactic_map={"NEXT": (AgentType.RECON, "recon")},
            pause_gate=None,
        )
        master._final_verdicts = Mock(return_value=[])
        master._llm_review = AsyncMock(return_value={
            "continue_iteration": True,
            "reason": "目标尚未达成，已有新的可验证方向",
            "dag_nodes": [{"tactic": "NEXT", "title": "next direction"}],
        })

        view = {
            "vuln_in_pipeline": 0,
            "flags": {"extracted": 1, "accepted": 0},
        }
        state = {
            "target": "example.test",
            "round_no": 0,
            "dag": dag.to_dict(),
            "tasks_dispatched": [],
            "round_views": [view],
            "continue_iteration": True,
            "stop_reason": "",
            "plan_source": "mode_assembly",
        }

        result = await master._review(state)

        self.assertTrue(result["continue_iteration"])
        self.assertEqual(result["stop_reason"], "")
        self.assertEqual(result["round_no"], 1)
        review_events = [
            call.args[2] for call in audit.log.call_args_list
            if call.args[2].get("action") == "review"
        ]
        self.assertEqual(review_events[-1]["deferred_replan"], True)


if __name__ == "__main__":
    unittest.main()
