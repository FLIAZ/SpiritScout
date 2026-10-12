from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.recon.agent import ReconAgent
from spiritscout.agents.sub.base import RoutePlan
from spiritscout.core.models import AgentType, Task


def _task(**payload_extra) -> Task:
    payload = {"mission": "摸清目标后台的登录入口", "node_title": "侦察与资产测绘"}
    payload.update(payload_extra)
    return Task(task_id="task-1", task_type="recon", target="http://t",
                agent_type=AgentType.RECON, payload=payload)


class KbGuidanceTests(unittest.TestCase):
    """经验库注入只带核验参考（前置/核验方法/红线），不带步骤序列。"""

    def test_hunt_templates_carry_no_step_sequences(self) -> None:
        agent = object.__new__(ReconAgent)
        agent.agent_id = "agent-test"
        agent.agent_type = AgentType.RECON
        agent.ctx = SimpleNamespace(
            audit=Mock(),
            knowledge=SimpleNamespace(
                experiences=Mock(return_value=[]),
                hunt_templates=Mock(return_value=[{
                    "template_id": "T1", "scene": "登录口",
                    "precondition": "已观测到登录表单",
                    "step_sequence": [{"step": "1", "action": "爆破"}],
                    "common_pitfall": "误报锁定",
                    "compliance_redline": "禁止真实爆破",
                }])))
        plan = RoutePlan(name="r", strategy="s")

        agent._attach_kb_guidance(_task(vuln_classes=["sqli"]), [plan])

        tpl = plan.meta["kb_guidance"]["hunt_templates"][0]
        self.assertNotIn("steps", tpl)
        self.assertEqual(tpl["precondition"], "已观测到登录表单")
        self.assertEqual(tpl["compliance_redline"], "禁止真实爆破")

    def test_empty_kb_attaches_nothing(self) -> None:
        agent = object.__new__(ReconAgent)
        agent.agent_id = "agent-test"
        agent.agent_type = AgentType.RECON
        agent.ctx = SimpleNamespace(
            audit=Mock(),
            knowledge=SimpleNamespace(
                experiences=Mock(return_value=[]),
                hunt_templates=Mock(return_value=[])))
        plan = RoutePlan(name="r", strategy="s")

        agent._attach_kb_guidance(_task(), [plan])

        self.assertNotIn("kb_guidance", plan.meta)


class TaskIntentTests(unittest.IsolatedAsyncioTestCase):
    """灵枢任务包的任务意图随每次决策下发到执行者的 LLM 上下文。"""

    def _agent(self) -> ReconAgent:
        agent = object.__new__(ReconAgent)
        agent.agent_id = "agent-test"
        agent.agent_type = AgentType.RECON
        agent.enable_llm_planning = True
        agent.ctx = SimpleNamespace(
            audit=Mock(), ring=None,
            policy=SimpleNamespace(guard=SimpleNamespace(scope=SimpleNamespace(
                allowed_domains=[], allowed_cidrs=[], allowed_urls=[]))),
            gateway=SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(
                content=json.dumps({"paths": [], "rationale": "无"})))))
        return agent

    async def test_intent_from_task_package_reaches_decision_context(self) -> None:
        agent = self._agent()

        await agent._llm_chat_json(
            "path_planner", {"action": "suggest_paths"}, task=_task())

        messages = agent.ctx.gateway.chat.await_args.args[0]
        payload = json.loads(messages[-1]["content"])
        self.assertEqual(payload["task_intent"]["mission"], "摸清目标后台的登录入口")
        self.assertEqual(payload["task_intent"]["node"], "侦察与资产测绘")

    async def test_no_intent_without_task(self) -> None:
        agent = self._agent()

        await agent._llm_chat_json("path_planner", {"action": "suggest_paths"})

        messages = agent.ctx.gateway.chat.await_args.args[0]
        self.assertNotIn("task_intent", json.loads(messages[-1]["content"]))


if __name__ == "__main__":
    unittest.main()
