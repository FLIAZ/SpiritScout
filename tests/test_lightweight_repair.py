"""轻量题型提示、公开正文与重复观察复盘回归。"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_ctf_runtime import EmptyReplyRepairTests

from spiritscout.agents.sub.base import RoutePlan
from spiritscout.core.models import AgentType, RunMode, Task
from spiritscout.ctf.agents import _ToolOutcome
from spiritscout.ctf.evidence_review import observe, review_row
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.gateway.context import estimate_input_tokens


def single():
    ctx = SimpleNamespace(mode=RunMode.CTF, shared_cache=None, search_enabled=False,
        gateway=SimpleNamespace(config=SimpleNamespace(lightweight_local=True),
                                context_budget=lambda **kw: 10650, chat=AsyncMock()),
        audit=Mock(), builtin_ctx={}, blackboard=None, policy=None)
    ctx.for_agent = lambda role: ctx
    return SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))


class ObservationTests(unittest.TestCase):
    def test_repeated_empty_queries_are_observed_across_all_tools(self):
        state = {"messages": []}
        outcome = _ToolOutcome(ok=True, text=json.dumps({"ok": True, "items": [], "total": 0}))
        self.assertIsNone(observe(state, "blackboard_read", {"layer": "asset"}, outcome))
        review = observe(state, "blackboard_read", {"layer": "asset"}, outcome)
        self.assertTrue(review["empty"])
        self.assertEqual(review["same_result_count"], 2)
        self.assertEqual(review["reason"], "重复空查询")

    def test_results_not_paths_or_call_ids_determine_repetition(self):
        state = {"messages": []}
        first = _ToolOutcome(ok=True, text="wrapper with invocation-one", metadata={
            "invocation_id": "one", "observation": {"digest": "actual-output", "preview": "real bytes"}})
        second = _ToolOutcome(ok=True, text="different wrapper with invocation-two", metadata={
            "invocation_id": "two", "observation": {"digest": "actual-output", "preview": "real bytes"}})
        self.assertIsNone(observe(state, "shell", {"command": "read-a"}, first))
        self.assertIsNotNone(observe(state, "shell", {"command": "read-b"}, second))
        changed = _ToolOutcome(ok=True, text="", metadata={"observation": {"digest": "new-output", "preview": "new bytes"}})
        self.assertIsNone(observe(state, "shell", {"command": "read-a"}, changed))

    def test_structured_binary_summary_keeps_addresses_and_pagination(self):
        summary = json.dumps({"items": [{"name": "function-" + str(i), "address": hex(i)} for i in range(12)],
                              "next_offset": 12, "has_more": True})
        original = {"name": "binary_inspect", "preview": summary, "structured": True, "message_index": 3}
        projected = review_row(original)
        self.assertEqual(json.loads(projected["result_preview"])["next_offset"], 12)
        self.assertEqual(len(json.loads(projected["result_preview"])["items"]), 12)
        self.assertTrue(json.loads(projected["result_preview"])["has_more"])

    def test_review_projection_does_not_duplicate_full_parameters_and_results(self):
        original = {"name": "binary_inspect", "arguments_preview": "argument" * 1000,
                    "first_request": "duplicate" * 1000, "preview": "result" * 1000,
                    "message_index": 7, "invocation_id": "inv-one"}
        projected = review_row(original)
        self.assertNotIn("first_request", projected)
        self.assertLessEqual(len(projected["arguments_preview"]), 300)
        self.assertLessEqual(len(projected["result_preview"]), 400)
        self.assertEqual(projected["message_index"], 7)
        self.assertTrue(projected["partial"])
        self.assertEqual(len(original["preview"]), 6000)

    def test_earlier_observations_survive_a_long_run_of_repeated_failures(self):
        state = {"messages": []}
        observe(state, "binary_inspect", {"operation": "info"},
                _ToolOutcome(ok=True, text='{"ok":true,"info":{"class":"PE32"}}'))
        for _ in range(15):
            observe(state, "shell", {"command": "wrong tool"}, _ToolOutcome(ok=False, text="unsupported format"))
        remembered = [entry["first_observation"] for entry in state["evidence_seen"].values()]
        self.assertTrue(any("PE32" in entry["preview"] for entry in remembered))
        self.assertLessEqual(len(state["evidence_rows"]), 12)

    def test_successful_writes_without_output_are_not_mistaken_for_stagnation(self):
        state = {"messages": []}
        outcome = _ToolOutcome(ok=True, text="", metadata={"observation": {"digest": "no-output", "empty": True}})
        for _ in range(3):
            self.assertIsNone(observe(state, "shell", {"action": "intrusive_write", "command": "write"}, outcome))

    def test_repeated_failures_trigger_review_but_terminal_results_do_not(self):
        state = {"messages": []}
        failed = _ToolOutcome(ok=False, text="format not supported")
        observe(state, "shell", {"command": "bad"}, failed)
        self.assertTrue(observe(state, "shell", {"command": "bad"}, failed)["failed"])
        self.assertIsNone(observe(state, "submit_fix", {}, _ToolOutcome(ok=True, text="registered", stop=True)))


class SingleReviewTests(unittest.IsolatedAsyncioTestCase):
    def task(self):
        return Task(task_id="one", agent_type=AgentType.CTF_MISC, task_type="ctf_single", target="file.exe",
            payload={"challenge": {"id": "one", "description": "original challenge",
                "user_type_hints": {"types": ["Reverse"], "source": "user"}}, "mission": "USER_GOAL"})

    async def test_identity_is_separate_from_direction_and_binding(self):
        agent = single()
        task = self.task()
        with patch("spiritscout.ctf.agents.CTFBaseAgent.run", new=AsyncMock(return_value={})):
            await agent.run(task)
        self.assertEqual(agent.agent_type, AgentType.CTF_MISC)
        self.assertEqual(task.payload["executor_kind"], "single")
        self.assertEqual(task.payload["user_direction_hints"], ["Reverse"])
        identity = next(call for call in agent.ctx.audit.log.call_args_list if call.args[0] == "executor.identity")
        self.assertEqual(identity.args[2]["user_direction_hints"], ["Reverse"])
        state = {"seed": []}
        agent._seed_messages(task, None, None, state)
        self.assertIn("用户选择的调查方向：Reverse", state["messages"][1]["content"])
        self.assertIn("不是后端分类", state["messages"][1]["content"])

    async def test_review_replaces_next_view_without_calling_another_model_or_blocking_tools(self):
        agent = single()
        state = {"messages": [{"role": "system", "content": "s"},
                               {"role": "user", "content": "USER_GOAL", "_light_base": "USER_GOAL"}],
                 "world_state": {"authorization": {"testing_rules": "ORIGINAL_RULE"}}}
        empty = _ToolOutcome(ok=True, text='{"ok":true,"items":[],"total":0}')
        with patch("spiritscout.ctf.agents.CTFBaseAgent._run_tool_call_inner", new=AsyncMock(return_value=empty)) as perform:
            for _ in range(2):
                outcome = await agent._run_tool_call_inner(self.task(), None, state,
                    {"name": "blackboard_read", "arguments": {"layer": "asset"}})
                self.assertTrue(outcome.ok)
            self.assertEqual(perform.await_count, 2)
        self.assertTrue(state["review_due"]["empty"])
        sent = agent._light_model_messages(self.task(), SimpleNamespace(route_id="r"), state)
        self.assertIn("证据复盘", sent[-1]["content"])
        self.assertIn("ORIGINAL_RULE", str(sent))
        self.assertIn("USER_GOAL", str(sent))
        self.assertLessEqual(estimate_input_tokens(sent, agent.tool_schemas), 10650)
        agent.ctx.gateway.chat.assert_not_called()
        self.assertIsNone(state["review_due"])


class PublicBodyTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, public, light):
        fixture = EmptyReplyRepairTests()
        agent = fixture._agent([])
        agent.public_body = public
        agent.flag_manager = FlagManager(local_only=True)
        agent.ctx.mode = RunMode.CTF
        agent.ctx.gateway.config = SimpleNamespace(lightweight_local=light)
        agent.tool_schemas = [{"function": {"name": "shell"}}]
        agent._run_tool_call = AsyncMock(return_value=_ToolOutcome(ok=True, text="actual output"))
        async def reply(*args, **kwargs):
            kwargs["on_delta"]("text", "已确认文件格式。")
            kwargs["on_delta"]("text", "下一步需要核对输入逻辑。")
            return SimpleNamespace(content="已确认文件格式。下一步需要核对输入逻辑。", reasoning="",
                tool_calls=[{"id": "one", "name": "shell", "arguments": {"command": "fixture", "action": "analysis"}}])
        agent.ctx.gateway.chat = AsyncMock(side_effect=reply)
        await agent._solve_turn(fixture._task(), fixture._route(), RoutePlan(name="solve", strategy="", steps=[{"action": "investigate"}]))
        return [call.args for call in agent.ctx.audit.log.call_args_list]

    async def test_light_single_public_body_streams_once_without_progress_duplication(self):
        events = await self.exercise(public=True, light=True)
        text = "".join(event[2].get("text", "") for event in events if event[0] == "agent.delta" and event[2].get("kind") == "text")
        self.assertEqual(text, "已确认文件格式。下一步需要核对输入逻辑。")
        self.assertFalse(any(event[0] == "agent.progress" for event in events))

    async def test_normal_local_answer_stream_policy_remains_unchanged(self):
        events = await self.exercise(public=True, light=False)
        self.assertFalse(any(event[0] == "agent.delta" and event[2].get("kind") == "text" for event in events))
        self.assertTrue(any(event[0] == "agent.progress" for event in events))
