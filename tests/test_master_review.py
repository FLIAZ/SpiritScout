from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.core.models import AgentType, TaskStatus


class MasterReviewTests(unittest.IsolatedAsyncioTestCase):
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
