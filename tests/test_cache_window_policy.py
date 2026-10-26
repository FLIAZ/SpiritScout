"""模型窗口压力与原生历史回放；全部离线，不发真实 API 请求。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from spiritscout.agents.sub.cache import LocalSharedCache
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry
from spiritscout.ctf.agents import CTFBaseAgent, _complete_tool_history, _model_messages
from spiritscout.gateway.context import (
    DecisionMemory,
    compact_history_tokens,
    decision_messages,
    estimate_input_tokens,
    input_window_budget,
)
from spiritscout.gateway.gateway import (
    AgentBoundGateway,
    ChatResponse,
    ModelGateway,
    chat_decision,
)
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.tools.journal import ExecutionJournal
from tests import test_steering_delivery as steering_fixture


class CacheWindowTests(unittest.IsolatedAsyncioTestCase):
    def test_bound_capacity_follows_chat_model_and_output_reservation(self) -> None:
        gateway = ModelGateway(ModelRegistry([
            ModelEntry(model="small", provider="mock", context_window=32000),
            ModelEntry(model="large", provider="mock", context_window=1000000,
                       max_output_tokens=384000),
        ]))
        bound = AgentBoundGateway(gateway, "large")
        self.assertEqual(bound.context_budget(), 550464)
        self.assertEqual(bound.context_budget(model="small"), input_window_budget(32000))
        self.assertEqual(bound.context_budget(max_tokens=8192), 800000)
        self.assertIsNone(input_window_budget(0))

    def test_window_pressure_counts_tools_arguments_and_reasoning(self) -> None:
        messages = [{"role": "assistant", "content": "", "reasoning_content": "推理" * 8000,
                     "tool_calls": [{"id": "c1", "function": {
                         "name": "run", "arguments": "x" * 12000}}]}]
        tools = [{"type": "function", "function": {"name": "run", "description": "schema" * 8000}}]
        self.assertGreater(estimate_input_tokens(messages), 25000)
        self.assertGreater(estimate_input_tokens(messages, tools), estimate_input_tokens(messages) + 10000)

    def test_large_warm_prefix_is_not_rewritten_below_model_pressure(self) -> None:
        messages = [
            {"role": "system", "content": "角色"},
            {"role": "user", "content": "授权与题面"},
            {"role": "assistant", "content": "已有判断"},
            {"role": "tool", "content": "早期真实工具证据" * 15000},
            {"role": "assistant", "content": "进一步判断"},
            {"role": "tool", "content": "最新证据"},
        ]
        original = copy.deepcopy(messages)
        self.assertFalse(compact_history_tokens(messages, 800000))
        self.assertEqual(messages, original)
        self.assertTrue(compact_history_tokens(messages, 10000))
        self.assertEqual(messages[:2], original[:2])
        self.assertEqual(messages[-2:], original[-2:])
        self.assertIn("完整内容见执行记录", messages[3]["content"])

    def test_pressure_preserves_user_contracts_call_pairs_and_native_fields(self) -> None:
        messages = [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "原始授权"},
            {"role": "assistant", "content": "旧观察" * 2000, "reasoning_content": "原始思考",
             "tool_calls": [{"id": "c1", "function": {"name": "run", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "旧证据" * 4000},
            {"role": "user", "content": "最新用户范围声明" * 8000},
            {"role": "tool", "content": "必须完整的工具契约" * 2000, "_context_contract": True},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": "最新完整结果" * 2000},
        ]
        original = copy.deepcopy(messages)
        compact_history_tokens(messages, 1000)
        for index in (0, 1, 4, 5, 6, 7):
            self.assertEqual(messages[index], original[index])
        self.assertEqual(messages[2]["tool_calls"], original[2]["tool_calls"])
        self.assertEqual(messages[2]["reasoning_content"], "原始思考")
        self.assertEqual(messages[3]["tool_call_id"], "c1")

    def test_native_tool_reply_is_replayed_verbatim_and_deeply_detached(self) -> None:
        messages = [{"role": "assistant", "content": "", "reasoning_content": "原生思考",
                     "tool_calls": [{"id": "a", "function": {"name": "run", "arguments": "{}"}}]}]
        sent = _model_messages(messages, initialized=True)
        self.assertEqual(sent, messages)
        sent[0]["tool_calls"][0]["function"]["arguments"] = "changed"
        self.assertEqual(messages[0]["tool_calls"][0]["function"]["arguments"], "{}")

    async def test_master_long_session_uses_window_instead_of_legacy_character_rebase(self) -> None:
        response = ChatResponse(content='{"continue":true}', reasoning="原始推理", model="mock",
                                prompt_tokens=1, completion_tokens=1, cost_usd=0, latency_ms=0)
        gateway = SimpleNamespace(chat=AsyncMock(return_value=response),
                                  context_budget=lambda **kwargs: 800000)
        memory = DecisionMemory(soft_chars=1000)
        first = decision_messages("稳定契约", {"observations": "真实证据" * 4000}, 100000)
        await chat_decision(gateway, first, memory=memory)
        history = copy.deepcopy(memory.messages)
        await chat_decision(gateway, decision_messages("稳定契约", {
            "observations": "真实证据" * 4000, "progress": "新的进展"}, 100000), memory=memory)
        sent = gateway.chat.call_args.args[0]
        self.assertEqual(sent[:len(history)], history)
        self.assertFalse(memory.segment_changed)
        self.assertEqual(sent[-1]["role"], "user")

    def test_master_rebases_at_token_pressure_with_latest_authorization(self) -> None:
        memory = DecisionMemory()
        memory.prepare(decision_messages("契约", {"authorization": "仅 A", "state": 1}), max_input_tokens=1000)
        memory.commit("长决策" * 3000)
        prepared = memory.prepare(decision_messages("契约", {
            "authorization": "仅 B，撤销 A", "state": 2}), max_input_tokens=1000)
        self.assertTrue(memory.segment_changed)
        self.assertEqual(memory.segment_info["reason"], "context_window")
        self.assertIn("仅 B，撤销 A", prepared[-1]["content"])
        self.assertNotIn("仅 A", prepared[-1]["content"])


    async def test_solver_retains_large_prefix_and_native_reply_on_actual_turn_path(self) -> None:
        agent, state, task, route, plan = steering_fixture.SteeringExecutionTests()._solver()
        state["messages"].extend([
            {"role": "assistant", "content": "", "reasoning_content": "原生思考",
             "tool_calls": [{"id": "old", "function": {"name": "run", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "old", "content": "现场证据" * 20000},
        ])
        original = copy.deepcopy(state["messages"])
        agent.ctx.gateway.context_budget = lambda **kwargs: 800000
        agent.ctx.gateway.chat.return_value = SimpleNamespace(content="调查结束", tool_calls=[], reasoning="")
        await agent._solve_turn(task, route, plan)
        sent = agent.ctx.gateway.chat.call_args.args[0]
        self.assertEqual(sent[:len(original)], original)
        self.assertTrue(agent.ctx.gateway.chat.call_args.kwargs["preserve_context"])
        self.assertFalse(any(call.args[0] == "context.compacted" for call in agent.ctx.audit.log.call_args_list))

    def test_encrypted_checkpoint_restores_only_same_task_and_complete_tool_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bb = Blackboard(root / "session.db")
            try:
                journal = ExecutionJournal(bb, root, "first")
                agent, _, task, route, _ = steering_fixture.SteeringExecutionTests()._solver()
                agent.route_namespace = "ctf_web"
                agent.ctx.journal = journal
                agent.cache = LocalSharedCache()
                state = CTFBaseAgent._inv_state(agent, task, route)
                state["messages"] = [
                    {"role": "system", "content": "原始契约"},
                    {"role": "user", "content": "原始题面"},
                    {"role": "assistant", "content": "", "reasoning_content": "原始思考",
                     "tool_calls": [{"id": "call", "function": {"name": "run", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "call", "content": "完整结果"},
                ]
                original = copy.deepcopy(state)
                agent._save_inv_state(task, route, state)
                agent.cache.clear()
                agent.ctx.journal = ExecutionJournal(bb, root, "reopened")
                restored = CTFBaseAgent._inv_state(agent, task, route)
                self.assertEqual(restored["messages"], original["messages"])
                agent.ctx.gateway.chat.assert_not_awaited()
                other = task.model_copy(update={"task_id": "different-task"})
                self.assertEqual(CTFBaseAgent._inv_state(agent, other, route)["messages"], [])
                incomplete = copy.deepcopy(original)
                incomplete["messages"].pop()
                journal.checkpoint("ctf_conversation", f"{task.task_id}:{route.route_id}",
                                   "turn_saved", incomplete)
                agent.cache.clear()
                self.assertEqual(CTFBaseAgent._inv_state(agent, task, route)["messages"], [])
            finally:
                bb.close()

    def test_incomplete_tool_pairs_are_never_treated_as_replayable_history(self) -> None:
        self.assertFalse(_complete_tool_history([{"role": "tool", "tool_call_id": "missing"}]))
        self.assertFalse(_complete_tool_history([
            {"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "user", "content": "工具结果未知"}]))
        self.assertFalse(_complete_tool_history([
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "a"}]}]))


if __name__ == "__main__":
    unittest.main()
