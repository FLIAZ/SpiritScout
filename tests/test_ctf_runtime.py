from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.ring import attempt_key
from spiritscout.agents.sub.base import RoutePlan
from spiritscout.core.models import AgentType, Task
from spiritscout.ctf.agents import (
    CTFBaseAgent,
    _challenge_instance_blocker,
    _model_messages,
)


class ChallengeAvailabilityTests(unittest.TestCase):
    def test_platform_instance_not_found_is_a_blocker(self) -> None:
        text = (
            "题目实例未找到 challenge-gateway / route-check "
            "题目实例不存在、已经销毁，或访问域名不正确。"
            "请从比赛题目页重新启动实例后再进入。"
        )

        result = _challenge_instance_blocker(404, text)

        self.assertIn("赛题实例不可用", result)
        self.assertIn("重新启动实例", result)

    def test_normal_application_404_is_not_a_blocker(self) -> None:
        self.assertEqual(
            _challenge_instance_blocker(404, "Not Found: /missing-page"), "")

    def test_marker_without_error_status_is_not_a_blocker(self) -> None:
        self.assertEqual(
            _challenge_instance_blocker(200, "题目实例未找到"), "")


class SolverContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = [
            {"role": "system", "content": "首轮角色与方法论"},
            {"role": "user", "content": "题面与入口"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "现场证据"},
        ]

    def test_first_model_call_keeps_initial_system_prompt(self) -> None:
        sent = _model_messages(self.messages, initialized=False)

        self.assertEqual(sent[0]["role"], "system")
        self.assertEqual(len(sent), 4)

    def test_later_model_calls_drop_initial_system_but_keep_evidence(self) -> None:
        sent = _model_messages(self.messages, initialized=True)

        self.assertEqual([message["role"] for message in sent],
                         ["user", "assistant", "tool"])
        self.assertEqual(sent[-1]["content"], "现场证据")
        self.assertEqual(len(self.messages), 4, "过滤不能改写持久会话状态")

    def test_send_view_does_not_share_message_dicts_with_saved_state(self) -> None:
        sent = _model_messages(self.messages, initialized=True)

        sent[0]["content"] = "网关修改过的内容"

        self.assertEqual(self.messages[1]["content"], "题面与入口")

    def test_removed_system_prompt_does_not_consume_later_turn_budget(self) -> None:
        messages = [
            {"role": "system", "content": "S" * 100_000},
            {"role": "user", "content": "题面"},
            {"role": "assistant", "content": "判断"},
            {"role": "tool", "content": "E" * 1_000},
        ]

        CTFBaseAgent._compact_messages(messages, initialized=True)

        self.assertEqual(messages[-1]["content"], "E" * 1_000)

    def test_fold_keeps_head_and_tail(self) -> None:
        body = "头" * 150 + "中" * 1_000 + "尾结论" + "！" * 100
        messages = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "题面"},
            {"role": "assistant", "content": "判断"},
            {"role": "tool", "content": body},
        ]

        # 制造超预算触发
        messages[1] = {"role": "user", "content": "题面" + "x" * 60_000}
        CTFBaseAgent._compact_messages(messages, initialized=True)

        folded = messages[3]["content"]
        self.assertIn("头", folded[:250])
        self.assertIn("尾结论", folded[-250:])
        self.assertIn("已折叠", folded)

    def test_injected_user_messages_fold_but_last_two_survive(self) -> None:
        msgs = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "题面"},
        ]
        for i in range(5):
            msgs.append({"role": "user",
                         "content": f"同伴动态{i}" + "y" * 600})
        msgs.append({"role": "assistant", "content": "判断"})
        msgs.append({"role": "tool", "content": "E" * 1_000})
        msgs[1] = {"role": "user", "content": "题面" + "x" * 60_000}

        CTFBaseAgent._compact_messages(msgs, initialized=True)

        folded = [m["content"] for m in msgs[2:7]]
        self.assertTrue(all("已折叠" in c for c in folded[:3]),
                        "最旧的注入消息应被折叠")
        self.assertNotIn("已折叠", msgs[5]["content"])
        self.assertNotIn("已折叠", msgs[6]["content"])
        self.assertTrue(msgs[5]["content"].startswith("同伴动态3"))
        self.assertTrue(msgs[6]["content"].startswith("同伴动态4"))


class RecordReplayTests(unittest.IsolatedAsyncioTestCase):
    """已读取过的地址回放记录；refresh / POST / 分段请求不受影响。"""

    @staticmethod
    def _agent() -> CTFBaseAgent:
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = "agent-test"
        agent.agent_type = AgentType.CTF_WEB
        agent.direction = "web"
        agent.flag_manager = None
        agent.cache = SimpleNamespace(get=Mock(return_value=None), put=Mock())
        agent.ctx = SimpleNamespace(
            audit=Mock(), ring=None, builtin_ctx={},
            blackboard=SimpleNamespace(write=Mock()),
            tools=SimpleNamespace(call=AsyncMock(return_value=SimpleNamespace(
                ok=True, parsed={"status_code": 200, "headers": {}},
                stdout="<html><title>fresh</title></html>", error=None))))
        return agent

    @staticmethod
    def _task() -> Task:
        return Task(task_id="task-1", task_type="ctf_solve", target="http://t",
                    agent_type=AgentType.CTF_WEB,
                    payload={"challenge": {"url": "http://t"}})

    async def test_already_read_get_is_served_from_record(self) -> None:
        agent = self._agent()
        state = {"observations": [], "fetches": {}, "flags": [], "turn": 7}
        state["fetches"][attempt_key("GET", "http://t/static/x.js", None)] = {
            "round": 3, "obs": {"action": "GET /static/x.js", "status": 200,
                                "excerpt": "[标题] demo"}}

        out = await agent._tool_http(
            self._task(), SimpleNamespace(name="solve"), state,
            {"path": "/static/x.js"})

        self.assertTrue(out.ok)
        self.assertIn("记录回放", out.text)
        self.assertIn("第 3 回合", out.text)
        agent.ctx.tools.call.assert_not_awaited()

    async def test_refresh_requests_again_and_updates_record(self) -> None:
        agent = self._agent()
        state = {"observations": [], "fetches": {}, "flags": [], "turn": 7}
        key = attempt_key("GET", "http://t/static/x.js", None)
        state["fetches"][key] = {"round": 3,
                                 "obs": {"action": "a", "status": 200}}

        out = await agent._tool_http(
            self._task(), SimpleNamespace(name="solve"), state,
            {"path": "/static/x.js", "refresh": True})

        agent.ctx.tools.call.assert_awaited_once()
        self.assertTrue(out.ok)
        self.assertIn("fresh", out.text)
        self.assertEqual(state["fetches"][key]["round"], 7)

    async def test_post_and_ranged_requests_are_never_replayed(self) -> None:
        agent = self._agent()
        state = {"observations": [], "fetches": {}, "flags": [], "turn": 2}
        state["fetches"][attempt_key("POST", "http://t/api/exec", None)] = {
            "round": 1, "obs": {"action": "a", "status": 200}}
        state["fetches"][attempt_key("GET", "http://t/big.js", None)] = {
            "round": 1, "obs": {"action": "a", "status": 200}}

        await agent._tool_http(
            self._task(), SimpleNamespace(name="solve"), state,
            {"method": "POST", "path": "/api/exec", "data": "cmd=ls"})
        await agent._tool_http(
            self._task(), SimpleNamespace(name="solve"), state,
            {"path": "/big.js", "headers": {"Range": "bytes=0-100"}})

        self.assertEqual(agent.ctx.tools.call.await_count, 2)


class StepExtensionTests(unittest.TestCase):
    """解题环不设固定回合顶：占位步持续追加，收尾只由模型/停摆/停止决定。"""

    def test_no_fixed_turn_cap(self) -> None:
        plan = RoutePlan(name="solve", strategy="s",
                         steps=[{"action": "investigate"}])
        state = {"concluded": False, "stall": ""}

        for _ in range(50):
            CTFBaseAgent._extend_or_finish(plan, state, "第n回合")

        self.assertEqual(len(plan.steps), 51)
        self.assertFalse(state["concluded"])

    def test_concluded_or_stalled_stops_extension(self) -> None:
        plan = RoutePlan(name="solve", strategy="s", steps=[])
        state = {"concluded": True, "stall": ""}

        self.assertEqual(
            CTFBaseAgent._extend_or_finish(plan, state, "note"), "note")
        self.assertEqual(plan.steps, [])


class DirectionRotationTests(unittest.TestCase):
    """重攻方向按题目形态轮换：纯 URL 的 Web 题不交给杂项接手。"""

    def test_pure_url_web_challenge_stays_web(self) -> None:
        from spiritscout.modes.assembler import _candidate_directions

        challenge = {"id": "c1", "title": "Linux 之旅", "url": "http://x"}

        self.assertEqual(_candidate_directions(challenge), ["web"])

    def test_binary_attachments_rotate_through_binary_directions(self) -> None:
        from spiritscout.modes.assembler import _candidate_directions

        challenge = {"id": "c2", "title": "pwn 题", "attachments": ["/a/chall.elf"]}

        dirs = _candidate_directions(challenge)

        self.assertEqual(dirs[0], "pwn")
        self.assertIn("reverse", dirs)
        self.assertIn("misc", dirs)

    def test_attachment_misc_challenge_rotates_crypto(self) -> None:
        from spiritscout.modes.assembler import _candidate_directions

        challenge = {"id": "c3", "title": "神秘文件",
                     "attachments": ["/a/blob.zip"]}

        dirs = _candidate_directions(challenge)

        self.assertEqual(dirs[0], "misc")
        self.assertIn("crypto", dirs)


class SpawnGuardTests(unittest.IsolatedAsyncioTestCase):
    """spawn：层级护栏（子→孙）与参数校验；深度不越界不派工。"""

    def _agent(self):
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = "a"
        agent.agent_type = AgentType.CTF_WEB
        agent.direction = "web"

        async def _no_solve(*a, **k):
            raise AssertionError("超深的 spawn 不应再起对话")
        agent._solve_turn = _no_solve
        return agent

    async def test_depth_cap_refuses_grandchild_of_grandchild(self) -> None:
        agent = self._agent()
        state = {"depth": 2, "child_seq": 0, "observations": []}
        out = await agent._tool_spawn(
            None, SimpleNamespace(name="solve"), state, {"brief": "再派一层"})

        self.assertFalse(out.ok)
        self.assertIn("上限", out.text)

    async def test_spawn_requires_brief(self) -> None:
        agent = self._agent()
        state = {"depth": 0, "child_seq": 0, "observations": []}

        out = await agent._tool_spawn(
            None, SimpleNamespace(name="solve"), state, {})

        self.assertFalse(out.ok)
        self.assertIn("brief", out.text)


if __name__ == "__main__":
    unittest.main()
