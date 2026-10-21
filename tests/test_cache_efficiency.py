"""稳定前缀、动态探测标记、显式断点和真实计量口径回归。"""

from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.agents.webvuln.agent import WebVulnAgent
from spiritscout.config.settings import ModelEntry
from spiritscout.gateway.backends import LiteLLMBackend
from spiritscout.gateway.cache import PrefixTracker, cache_summary
from spiritscout.gateway.context import (
    DecisionMemory,
    decision_memory,
    decision_messages,
)


class CacheEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    def test_changed_observation_keeps_full_factual_prefix(self) -> None:
        base = {"constraints": "输出结构固定", "mission": "原任务不能丢",
                "shared_context": {"version": 2, "facts": ["真实事实" * 1000]},
                "task_intent": {"rules": "不得删除"}, "round": 1, "observations": "old"}
        first = decision_messages("系统契约", base, 20000)
        # 构造顺序及动态轮次改变，不改变事实块。
        second = decision_messages("系统契约", {**dict(reversed(list(base.items()))),
                                               "round": 2, "observations": "new"}, 20000)
        self.assertEqual(first[:2], second[:2])
        self.assertNotEqual(first[-1], second[-1])
        merged = {**json.loads(first[1]["content"]), **json.loads(first[-1]["content"])}
        self.assertEqual(merged, {k: v for k, v in base.items() if k != "constraints"})
        self.assertEqual(base["observations"], "old")
        changed = decision_messages("系统契约", {**base, "shared_context": {"version": 3}}, 20000)
        self.assertNotEqual(changed[1], first[1], "新事实必须真实发送，不能为命中率复用旧内容")
        tracker = PrefixTracker()
        tracker.observe(("model",), first, None)
        metric = tracker.observe(("model",), second, None)
        self.assertGreater(metric["reused_prefix_bytes"] / metric["request_bytes"], .95)
        self.assertLess(metric["new_suffix_bytes"], 512)
        self.assertEqual(tracker.observe(("other-session",), second, None)["reused_prefix_bytes"], 0)
        self.assertFalse(any(isinstance(part, str) for value in tracker.previous.values() for part in value))

    def test_decision_memory_appends_only_changes_and_preserves_prefix(self) -> None:
        memory = DecisionMemory()
        first_payload = {"mission": "完整保留任务", "context": "真实现场" * 3000,
                         "observations": {"status": 200, "obsolete": True, "nullable": "old"}}
        first = memory.prepare(decision_messages("契约和授权", first_payload, 50000))
        memory.commit('{"signals":[]}')
        second_payload = {**first_payload, "observations": {"status": 403, "nullable": None}}
        second = memory.prepare(decision_messages("契约和授权", second_payload, 50000))
        self.assertEqual(second[:len(first)], first)
        self.assertEqual(second[-2], {"role": "assistant", "content": '{"signals":[]}'})
        patch = json.loads(second[-1]["content"])["patch"]
        self.assertEqual(patch, [
            {"op": "replace", "path": "/observations/nullable", "value": None},
            {"op": "remove", "path": "/observations/obsolete"},
            {"op": "replace", "path": "/observations/status", "value": 403},
        ])
        self.assertLess(len(second[-1]["content"]), 300)
        self.assertNotIn("真实现场", second[-1]["content"])
        self.assertEqual(DecisionMemory.patch({"a/b": {"~key": 1}}, {"a/b": {"~key": 2}}),
                         [{"op": "replace", "path": "/a~1b/~0key", "value": 2}])
        # 失败没有 commit：下一次依旧针对最后成功现场生成补丁。
        third = memory.prepare(decision_messages("契约和授权", first_payload, 50000))
        self.assertEqual(json.loads(third[-1]["content"])["patch"], [])
        changed_scope = memory.prepare(decision_messages("授权已变更", second_payload, 50000))
        self.assertEqual(len(changed_scope), 2)
        self.assertEqual(json.loads(changed_scope[-1]["content"])["snapshot"]["observations"]["status"], 403)
        owner = SimpleNamespace()
        self.assertIsNot(decision_memory(owner, "task:route-a:phase"), decision_memory(owner, "task:route-b:phase"))

    def test_decision_memory_rebases_on_latest_snapshot_without_round_limit(self) -> None:
        memory = DecisionMemory(soft_chars=2000)
        rebased = False
        for index in range(50):
            payload = {"mission": "约束不丢", "observations": {"index": index, "body": str(index) * 500}}
            messages = memory.prepare(decision_messages("system", payload, 10000))
            if index and len(messages) == 2:
                rebased = True
                self.assertEqual(json.loads(messages[-1]["content"])["snapshot"]["observations"]["index"], index)
            memory.commit('{"signals":[]}')
        self.assertTrue(rebased)
        self.assertEqual(memory.snapshot["observations"]["index"], 49)

    def test_boundary_update_and_rebase_keep_latest_complete_originals(self) -> None:
        memory = DecisionMemory(soft_chars=1000)
        rules = "只测测试环境\n" * 3000 + "尾部不测生产"
        first = {"world_state": {"authorization": {"testing_rules": rules},
                                  "user_context": {"statements": [{"text": "仅测 A"}]}},
                 "observations": "旧现场"}
        memory.prepare(decision_messages("稳定契约", first, max_chars=100))
        memory.commit("继续")
        latest = copy.deepcopy(first)
        latest["world_state"]["user_context"]["statements"].append({"answer": "B 不允许\n" * 1500})
        update = memory.prepare(decision_messages("稳定契约", latest, max_chars=100))
        patch_rows = [row for row in json.loads(update[-1]["content"])["patch"]
                      if row["path"].startswith("/world_state/")]
        self.assertEqual(patch_rows, [{"op": "replace", "path": "/world_state/user_context/statements",
                                       "value": latest["world_state"]["user_context"]["statements"]}])
        memory.commit("很长的助手观察" * 40000)
        rebased = memory.prepare(decision_messages("稳定契约", latest, max_chars=100))
        self.assertEqual(len(rebased), 2)
        self.assertEqual(json.loads(rebased[-1]["content"])["snapshot"]["world_state"], latest["world_state"])
        memory.commit("完成本轮")
        self.assertEqual(memory.snapshot["world_state"], latest["world_state"])

    async def test_probe_token_is_data_not_system_contract(self) -> None:
        agent = WebVulnAgent.__new__(WebVulnAgent)
        agent.ctx = SimpleNamespace(blackboard=None)
        agent._llm_chat_json = AsyncMock(return_value=None)
        task = SimpleNamespace(reasoning_effort="")
        payloads = []
        for token in ("canary-one", "canary-two"):
            await agent._llm_decide_payloads(task, vuln_class="xss", endpoint="https://fixture.test",
                                            param="q", baseline_body="body", candidates=[],
                                            require_token=token)
            payloads.append(agent._llm_chat_json.call_args.args[1])
        first, second = [decision_messages("role", value) for value in payloads]
        self.assertEqual(first[0], second[0])
        self.assertNotIn("canary-one", first[0]["content"])
        self.assertEqual(json.loads(first[-1]["content"])["required_probe_token"], "canary-one")

    def test_grouped_actual_usage_does_not_count_local_prefix_as_hit(self) -> None:
        result = cache_summary([
            {"model": "m", "actor": "a", "task_type": "execution", "input_tokens": 100,
             "cached_input_tokens": 80, "output_tokens": 50, "reused_prefix_bytes": 999999},
            {"model": "m", "actor": "a", "task_type": "execution", "input_tokens": 900,
             "cached_input_tokens": 90, "output_tokens": 200},
            {"model": "m", "actor": "b", "input_tokens": 1000, "output_tokens": 300},
        ])
        self.assertEqual(result["hit_rate"], .17)
        self.assertEqual(result["uncached_input_tokens"], 830)
        self.assertEqual(result["total_input_tokens"], 2000)
        self.assertEqual(result["output_tokens"], 550)
        self.assertEqual(result["unmeasured_calls"], 1)
        self.assertIsNone(result["groups"][1]["hit_rate"])

    async def test_anthropic_checkpoints_include_tools_facts_and_latest_result(self) -> None:
        messages = [{"role": "system", "content": "system"},
                    {"role": "user", "content": "stable facts"},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function",
                     "function": {"name": "read", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "a", "content": "latest result"}]
        tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
        untouched = copy.deepcopy((messages, tools))
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
                                   usage=SimpleNamespace(prompt_tokens=100, completion_tokens=1))
        complete = AsyncMock(return_value=response)
        with patch("litellm.acompletion", complete):
            await LiteLLMBackend().complete(ModelEntry(model="fixture", provider="anthropic_compatible",
                prompt_caching="anthropic_ephemeral"), messages, temperature=.2,
                max_tokens=64, timeout=30, response_format=None, tools=tools)
        kwargs = complete.call_args.kwargs
        self.assertIn("cache_control", kwargs["tools"][-1])
        self.assertIn("cache_control", kwargs["messages"][1]["content"][0])
        self.assertIn("cache_control", kwargs["messages"][-1])
        self.assertEqual(kwargs["messages"][-1]["tool_call_id"], "a")
        self.assertEqual((messages, tools), untouched)
