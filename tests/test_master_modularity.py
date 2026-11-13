"""主 Agent 模型请求契约：稳定前缀、原有参数与旧导入入口。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.master.agent import MasterAgent, MasterContext, MasterState
from spiritscout.agents.master.contracts import MasterContext as SharedContext
from spiritscout.agents.master.contracts import MasterState as SharedState
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, RunMode
from spiritscout.gateway.gateway import ChatResponse


async def capture(cls: type[MasterAgent]) -> list[dict]:
    master = object.__new__(cls)
    master.agent_id = "master-prefix-fixture"
    response = ChatResponse(
        content='{"steering":[]}',
        reasoning="核对过的路线进度",
        model="fixture",
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=0,
        latency_ms=0,
    )
    master.ctx = SimpleNamespace(
        audit=NullAuditLogger(),
        blackboard=None,
        policy=None,
        agents={AgentType.CTF_WEB: object(), AgentType.CTF_CRYPTO: object()},
        extra={
            "mission": "核对给定赛题，保留已确认的事实。",
            "standing_rules": "仅使用本次用户给出的测试规则。",
            "mode_prompt": "开放赛制，模型动态决策。",
            "competition_context": {
                "challenges": [{"id": "c1", "title": "缓存契约夹具", "score": 100}]
            },
        },
        gateway=SimpleNamespace(chat=AsyncMock(return_value=response)),
    )
    digest = {
        "tasks": [
            {
                "task_id": "t1",
                "agent": "ctf_web",
                "challenge": {"id": "c1"},
                "routes": [{"notes": ["旧观察"]}],
            }
        ]
    }
    requests = []
    for note in ["旧观察", "新观察"]:
        digest["tasks"][0]["routes"][0]["notes"] = [note]
        await master._llm_steer(digest)
        call = master.ctx.gateway.chat.call_args
        requests.append(
            {
                "messages": copy.deepcopy(call.args[0]),
                "options": {
                    k: v
                    for k, v in call.kwargs.items()
                    if k
                    in [
                        "reasoning_effort",
                        "preserve_context",
                        "max_tokens",
                        "temperature",
                        "tools",
                        "response_format",
                    ]
                },
            }
        )
    return requests


class MasterModuleContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_ctf_opening_dispatch_is_a_single_low_effort_plan_with_inline_rules(self):
        from spiritscout.agents.master.dag import TaskDAG
        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import BlackboardLayer

        with tempfile.TemporaryDirectory() as tmp:
            blackboard = Blackboard(Path(tmp) / 'bb.db')
            self.addCleanup(blackboard.close)
            rules = '只测试本题目标，保留全部原始记录。'
            blackboard.write(BlackboardLayer.TARGET_AUTH, 'user-context',
                             {'mission': '核实入口', 'standing_rules': rules}, updated_by='user')
            for mode, round_no, expected in ((RunMode.CTF, 0, 'low'), (RunMode.AWDP, 0, 'low'),
                                             (RunMode.CTF, 1, 'max'), (RunMode.SRC, 0, 'max')):
                with self.subTest(mode=mode, round_no=round_no):
                    master = object.__new__(MasterAgent)
                    master.agent_id = 'master-opening'
                    master.ctx = SimpleNamespace(
                        audit=NullAuditLogger(), blackboard=blackboard, policy=SimpleNamespace(mode=mode),
                        agents={AgentType.CTF_WEB: object()},
                        tactic_map={'CTF-WEB': (AgentType.CTF_WEB, 'ctf_solve')},
                        extra={'competition_context': {'challenges': [{'id': 'one'}]}},
                        gateway=SimpleNamespace(chat=AsyncMock(return_value=ChatResponse(
                            content='{"dag_nodes":[{"tactic":"CTF-WEB","title":"核实入口并取得答案","challenge_id":"one"}]}',
                            model='fixture', prompt_tokens=1, completion_tokens=1, cost_usd=0, latency_ms=0))))
                    master._system_prompt = lambda: '规划夹具'
                    master._situation_digest = lambda: {}
                    result = await master._llm_plan({'target': 'https://fixture.test', 'round_no': round_no,
                                                     'dag': TaskDAG().to_dict(), 'round_views': []})
                    self.assertIsNotNone(result)
                    self.assertEqual(result['dag_nodes'][0]['title'], '核实入口并取得答案')
                    master.ctx.gateway.chat.assert_awaited_once()
                    self.assertEqual(master.ctx.gateway.chat.call_args.kwargs['reasoning_effort'], expected)
                    sent = json.dumps(master.ctx.gateway.chat.call_args.args[0], ensure_ascii=False)
                    self.assertIn(rules, sent)
                    self.assertIn('blackboard_ref', sent)

    def test_original_imports_keep_same_context_and_state(self) -> None:
        self.assertIs(MasterContext, SharedContext)
        self.assertIs(MasterState, SharedState)

    async def test_requests_match_lifecycle_contract_and_keep_prefix(self) -> None:
        requests = await capture(MasterAgent)
        # 检查实际调用契约与稳定前缀，不锁死整份提示词的字面哈希。
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["options"]["response_format"], "json")
        first, second = [item["messages"] for item in requests]
        self.assertEqual(second[: len(first)], first)
        self.assertEqual(requests[0]["options"], requests[1]["options"])


class PlanPriorityTests(unittest.IsolatedAsyncioTestCase):
    """方向选择归模型：LLM 规划优先，规则派单只在模型不可用时兜底。"""

    async def test_llm_plan_wins_over_mode_assembly(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(
            enable_llm_planning=True,
            plan_override=Mock(return_value={"dag_nodes": [], "analysis": "规则派单"}),
            extra={"mode_prompt": ""})
        master._llm_plan = AsyncMock(return_value={"dag_nodes": [{"tactic": "X"}],
                                                   "analysis": "模型规划"})

        plan, source = await master._acquire_plan({"round_no": 0})

        self.assertEqual((source, plan["analysis"]), ("llm", "模型规划"))
        master.ctx.plan_override.assert_not_called()

    async def test_mode_assembly_is_fallback_not_shortcut(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(
            enable_llm_planning=True,
            plan_override=Mock(return_value={"dag_nodes": [], "analysis": "规则派单"}),
            extra={"mode_prompt": ""})
        master._llm_plan = AsyncMock(return_value=None)  # 规划失败/不可用

        plan, source = await master._acquire_plan({"round_no": 0})

        self.assertEqual((source, plan["analysis"]), ("mode_assembly", "规则派单"))
        master._llm_plan.assert_awaited_once()

    async def test_planning_disabled_uses_mode_assembly_directly(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(
            enable_llm_planning=False,
            plan_override=Mock(return_value={"dag_nodes": [], "analysis": "规则派单"}),
            extra={})
        master._llm_plan = AsyncMock()

        plan, source = await master._acquire_plan({"round_no": 0})

        self.assertEqual((source, plan["analysis"]), ("mode_assembly", "规则派单"))
        master._llm_plan.assert_not_awaited()

    def test_non_llm_plan_orders_mode_prompt_and_rule(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(enable_llm_planning=False, plan_override=None,
                                     extra={"mode_prompt": "竞赛策略"})

        plan, source = master._non_llm_plan({})

        self.assertEqual(source, "unavailable")
        self.assertEqual(plan["dag_nodes"], [])

        master.ctx = SimpleNamespace(enable_llm_planning=False, plan_override=None,
                                     extra={})
        master._rule_plan = Mock(return_value={"dag_nodes": [], "analysis": "规则"})
        plan, source = master._non_llm_plan({})
        self.assertEqual((source, plan["analysis"]), ("rule", "规则"))


class ChallengeResolutionTests(unittest.TestCase):
    def test_ctf_plan_node_stamps_direction(self) -> None:
        """模型赛题节点入图时带上方向事实（战术即方向），原始清单不被就地改写。"""
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(
            extra={"competition_context": {"challenges": [
                {"id": "c1", "title": "无扩展名样本", "attachments": ["/a/x"]}]}})

        nodes = master._resolve_plan_challenges([
            {"tactic": "CTF-REVERSE@1", "title": "[reverse] 无扩展名样本",
             "challenge_id": "c1"}])

        self.assertEqual(nodes[0]["meta"]["challenge"]["direction"], "reverse")
        self.assertNotIn("direction",
                         master.ctx.extra["competition_context"]["challenges"][0])


class SegmentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_segment_sequence_merges_like_the_graph(self) -> None:
        """分段循环替代状态图后锁住等价语义：顺序、检查点、列表累加。"""
        master = object.__new__(MasterAgent)
        seen: list[tuple[str, dict]] = []

        def node(name: str, update: dict):
            async def handler(state):
                seen.append((name, dict(state)))
                return update
            return handler

        master._meta_plan = node("meta_plan", {"round_no": 1, "tasks_dispatched": [{"n": 1}]})
        master._dispatch = node("dispatch", {"tasks_dispatched": [{"n": 2}], "plan_source": "llm"})
        master._aggregate = node("aggregate", {"round_views": [{"r": 1}]})
        master._review = node("review", {"continue_iteration": False, "stop_reason": "收敛"})
        checkpoints: list[str] = []
        master.ctx = SimpleNamespace(journal=SimpleNamespace(
            checkpoint=lambda *args, **kwargs: checkpoints.append(args[2])))

        state = {"target": "t", "round_no": 0, "dag": {}, "tasks_dispatched": [],
                 "round_views": [], "continue_iteration": True, "stop_reason": "", "plan_source": "",
                 "last_decision_event": "", "replan_requested": False}
        final = await master._run_segment(state)

        self.assertEqual([name for name, _ in seen],
                         ["meta_plan", "dispatch", "aggregate", "review"])
        # 列表键跨节点累加，标量键覆盖，后续节点看到前序更新
        self.assertEqual(final["tasks_dispatched"], [{"n": 1}, {"n": 2}])
        self.assertEqual(final["round_views"], [{"r": 1}])
        self.assertEqual(final["round_no"], 1)
        self.assertFalse(final["continue_iteration"])
        self.assertEqual(seen[1][1]["round_no"], 1)
        self.assertEqual(seen[1][1]["tasks_dispatched"], [{"n": 1}])
        self.assertEqual(checkpoints, [
            "before_meta_plan", "after_meta_plan", "before_dispatch", "after_dispatch",
            "before_aggregate", "after_aggregate", "before_review", "after_review"])
