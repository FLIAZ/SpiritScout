from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.core.models import AgentType, TaskStatus


class MasterReviewTests(unittest.IsolatedAsyncioTestCase):
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
