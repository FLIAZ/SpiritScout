"""长会话低水位整理、加密增量恢复、模型接力和请求尝试的离线验证。"""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from spiritscout.agents.master.dag import TaskDAG
from spiritscout.blackboard import Blackboard
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.models import AgentType, Task, TaskStatus
from spiritscout.ctf.platform import PlatformAdapter, parse_platform_profile
from spiritscout.gateway.attempts import (
    AttemptTrace,
    current_attempt_trace,
    tracked_request,
)
from spiritscout.gateway.context import (
    compact_history_tokens,
    decision_memory,
    decision_messages,
)
from spiritscout.tools.journal import ExecutionJournal
from tests import test_competition_continuous as continuous_fixture
from tests import test_steering_delivery as steering_fixture
from tests.test_platform_adapter import PROFILE, _challenge


class PersistenceEfficiencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bb = Blackboard(root / "session.db")
        self.root = root
        self.journal = ExecutionJournal(self.bb, root, "first")

    def tearDown(self) -> None:
        self.bb.close()
        self.tmp.cleanup()

    def test_incremental_chain_reopens_exactly_and_rebases_on_old_message_edit(self) -> None:
        state = {"messages": [], "turn": 0}
        for i in range(40):
            state["messages"].append({"role": "user", "content": f"record {i}:" + "x" * 3000})
            state["turn"] = i
            self.journal.checkpoint("ctf_conversation", "t:r", "turn_saved", state)
        with self.bb.transaction() as conn:
            rows = conn.execute("SELECT schema_version,length(state_cipher) FROM runtime_checkpoints").fetchall()
        # 新完整基线淘汰旧链，保留的增量仍必须可原样恢复。
        self.assertLessEqual(len(rows), 32)
        self.assertEqual(sum(row[0] == 1 for row in rows), 1)
        self.assertTrue(any(row[0] == 2 for row in rows))
        naive_bytes = sum(len(json.dumps({"messages": state["messages"][:i + 1]})) for i in range(40))
        self.assertLess(sum(row[1] for row in rows), naive_bytes / 3)
        reopened = ExecutionJournal(self.bb, self.root, "reopened")
        self.assertEqual(reopened.latest_checkpoint("ctf_conversation", "t:r")["state"], state)
        state["messages"][0]["content"] = "压缩后显式新基线"
        self.journal.checkpoint("ctf_conversation", "t:r", "turn_saved", state)
        restored = reopened.latest_checkpoint("ctf_conversation", "t:r")
        self.assertEqual(restored["schema_version"], 1)
        self.assertEqual(restored["state"], state)

    def test_missing_incremental_parent_does_not_silently_restore_wrong_history(self) -> None:
        base = self.journal.checkpoint("ctf_conversation", "t:r", "saved", {"messages": [{"role": "user", "content": "first"}]})
        self.journal.checkpoint("ctf_conversation", "t:r", "saved", {"messages": [
            {"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}]})
        with self.bb.transaction() as conn:
            conn.execute("DELETE FROM runtime_checkpoints WHERE checkpoint_id=?", (base,))
        with self.assertRaises(ConfigurationError):
            self.journal.latest_checkpoint("ctf_conversation", "t:r")

    def test_decision_memory_survives_owner_recreation_and_keeps_current_authorization(self) -> None:
        owner = SimpleNamespace(ctx=SimpleNamespace(journal=self.journal))
        memory = decision_memory(owner, "plan")
        memory.prepare(decision_messages("契约", {"authorization": "仅测试 A"}))
        memory.commit('{"plan":[]}', reasoning="原始思考")
        original = copy.deepcopy(memory.messages)
        reopened = SimpleNamespace(ctx=SimpleNamespace(journal=ExecutionJournal(self.bb, self.root, "next")))
        restored = decision_memory(reopened, "plan")
        prepared = restored.prepare(decision_messages("契约", {"authorization": "仅测试 B，撤销 A"}))
        self.assertEqual(prepared[:len(original)], original)
        self.assertEqual(json.loads(prepared[-1]["content"])["patch"][0]["value"], "仅测试 B，撤销 A")
        restored.commit('{"plan":[]}')
        self.assertEqual(restored.snapshot["authorization"], "仅测试 B，撤销 A")

    def test_evicted_decision_is_restored_instead_of_cold_started(self) -> None:
        owner = SimpleNamespace(ctx=SimpleNamespace(journal=self.journal))
        for i in range(129):
            memory = decision_memory(owner, str(i))
            memory.prepare(decision_messages("契约", {"state": i}))
            memory.commit('{"plan":[]}')
        self.assertNotIn("0", owner._decision_memories)
        restored = decision_memory(owner, "0")
        self.assertEqual(restored.snapshot, {"state": 0})
        self.assertEqual(len(owner._decision_memories), 128)

    def test_handoff_is_explicit_and_rejects_other_challenge_instance_model_or_incomplete_pair(self) -> None:
        agent, _, task, route, _ = steering_fixture.SteeringExecutionTests()._solver()
        agent.ctx.journal = self.journal
        agent.ctx.gateway._model = "model-a"
        source = {"messages": [{"role": "system", "content": "当前系统"},
                               {"role": "user", "content": "历史现场"},
                               {"role": "assistant", "content": "历史结论"}],
                  "concluded": True, "flags": ["old"]}
        agent._save_inv_state(task, route, source)
        continued = task.model_copy(deep=True, update={"task_id": "new-task"})
        continued.payload["continue_from"] = {"task_id": task.task_id, "route_id": route.route_id}
        def fresh() -> dict:
            return {"messages": [{"role": "system", "content": "当前系统"},
                                 {"role": "user", "content": "当前任务与授权"}],
                    "concluded": False, "flags": []}
        state = fresh()
        agent._inherit_conversation(continued, route, state)
        self.assertEqual(state["messages"][:3], source["messages"])
        self.assertEqual(state["messages"][-1]["content"], "当前任务与授权")
        self.assertFalse(state["concluded"])
        self.assertEqual(state["flags"], [])
        agent.ctx.gateway.chat.assert_not_awaited()
        for mutation in ("challenge", "instance", "model", "missing_selection"):
            changed = continued.model_copy(deep=True)
            if mutation == "challenge":
                changed.payload["challenge"]["id"] = "other"
            elif mutation == "instance":
                changed.payload["platform_handoff"] = {"instance_provisioning": {"start": "new"}}
            elif mutation == "model":
                agent.ctx.gateway._model = "model-b"
            else:
                changed.payload.pop("continue_from")
            rejected = fresh()
            agent._inherit_conversation(changed, route, rejected)
            self.assertEqual(len(rejected["messages"]), 2, mutation)
            agent.ctx.gateway._model = "model-a"
        source["response_models"] = ["unexpected-fallback"]
        agent._save_inv_state(task, route, source)
        rejected = fresh()
        agent._inherit_conversation(continued, route, rejected)
        self.assertEqual(len(rejected["messages"]), 2)
        source.pop("response_models")
        source["messages"].append({"role": "assistant", "tool_calls": [{"id": "unfinished"}]})
        agent._save_inv_state(task, route, source)
        rejected = fresh()
        agent._inherit_conversation(continued, route, rejected)
        self.assertEqual(len(rejected["messages"]), 2)

    def test_model_handoff_reference_survives_plan_insertion(self) -> None:
        dag = TaskDAG()
        result = dag.upsert_from_plan([{"tactic": "CTF-WEB", "title": "继续调查",
                                       "continue_from": {"task_id": "t", "route_id": "r"}}], 0)
        self.assertEqual(result.created[0].meta["continue_from"], {"task_id": "t", "route_id": "r"})

    def test_low_watermark_prevents_every_turn_compaction(self) -> None:
        messages = [{"role": "system", "content": "角色"}, {"role": "user", "content": "授权"}]
        for i in range(50):
            messages.extend([{"role": "assistant", "content": "判断"},
                             {"role": "tool", "content": "x" * 3000}])
        compact_history_tokens(messages, 30000)
        rewritten = 0
        for i in range(20):
            messages.extend([{"role": "assistant", "content": "新判断"},
                             {"role": "tool", "content": "y" * 3000}])
            rewritten += compact_history_tokens(messages, 30000)
        self.assertLessEqual(rewritten, 2)


class RequestAndSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_retries_keep_separate_unknown_and_reported_usage(self) -> None:
        audit = Mock()
        trace = AttemptTrace(audit, "agent", {"call_id": "call", "model": "fixture"})
        token = current_attempt_trace.set(trace)
        try:
            with self.assertRaises(ValueError):
                await tracked_request(AsyncMock(side_effect=ValueError("unsupported")))
            await tracked_request(AsyncMock(return_value=SimpleNamespace(usage={
                "prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 90})))
        finally:
            current_attempt_trace.reset(token)
        rows = [call.args[2] for call in audit.log.call_args_list if call.args[0] == "model.attempt_finished"]
        self.assertEqual([row["status"] for row in rows], ["failed", "completed"])
        self.assertEqual(rows[0]["usage_status"], "unknown")
        self.assertNotIn("input_tokens", rows[0])
        self.assertEqual(rows[1]["cached_input_tokens"], 90)
        self.assertNotEqual(rows[0]["attempt_id"], rows[1]["attempt_id"])

    async def test_cancelled_stream_keeps_usage_already_reported(self) -> None:
        audit = Mock()
        trace = AttemptTrace(audit, "agent", {"call_id": "call"})
        trace.start(streaming=True)
        trace.usage({"prompt_tokens": 200, "completion_tokens": 8})
        trace.finish("cancelled")
        row = audit.log.call_args.args[2]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["usage_status"], "reported")
        self.assertEqual(row["input_tokens"], 200)
        trace.finish("failed")
        self.assertEqual(audit.log.call_count, 2)

    async def test_unchanged_valid_refill_is_not_repeated_but_new_facts_wake_planner(self) -> None:
        adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"data": {"challenges": [_challenge("c1"), _challenge("c2")]}}
            if request.url.path.endswith("/challenges") else {"container_addr": "http://fixture.box"})))
        self.addAsyncCleanup(adapter.aclose)
        await adapter.list_challenges()
        await adapter.ensure_instance({"id": "c1"})
        hold = asyncio.Event()
        async def run(task: Task) -> None:
            await hold.wait()
        master = continuous_fixture.ContinuousCompetitionTests()._master(adapter, run, planning=True)
        master.ctx.extra["competition_context"] = {"challenges": [{"id": "c1"}, {"id": "c2"}]}
        master._llm_plan = AsyncMock(return_value={"dag_nodes": [], "analysis": "等待新证据"})
        world = {"revision": 1}
        master._world_state = lambda: dict(world)
        dag = TaskDAG()
        node = dag.add_node("CTF-WEB", "c1", meta={"challenge": {"id": "c1"}})
        dag.mark(node.node_id, TaskStatus.RUNNING)
        task = Task(task_id="c1", task_type="ctf_solve", target="http://fixture.box",
                    agent_type=AgentType.CTF_WEB, payload={"challenge": {"id": "c1"}})
        job = asyncio.create_task(master._dispatch({"dag": dag.to_dict(), "round_no": 0,
            "target": "http://fixture.box", "tasks_dispatched": [{"node_id": node.node_id,
                                                                     "task": task.model_dump(mode="json")}]}))
        try:
            await asyncio.sleep(0.08)
            self.assertEqual(master._llm_plan.await_count, 1)
            world["revision"] = 2
            await asyncio.sleep(0.08)
            self.assertEqual(master._llm_plan.await_count, 2)
            master._llm_plan.return_value = {"dag_nodes": [], "recheck_after_seconds": 0.02}
            world["revision"] = 3
            await asyncio.sleep(0.08)
            self.assertGreaterEqual(master._llm_plan.await_count, 4)
        finally:
            hold.set()
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
