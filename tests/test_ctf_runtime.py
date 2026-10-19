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

    def test_later_calls_keep_constraints_and_evidence(self) -> None:
        sent = _model_messages(self.messages, initialized=True)

        self.assertEqual([message["role"] for message in sent],
                         ["system", "user", "assistant", "tool"])
        self.assertEqual(sent[-1]["content"], "现场证据")
        self.assertEqual(len(self.messages), 4, "过滤不能改写持久会话状态")

    def test_send_view_does_not_share_message_dicts_with_saved_state(self) -> None:
        sent = _model_messages(self.messages, initialized=True)

        sent[0]["content"] = "网关修改过的内容"

        self.assertEqual(self.messages[1]["content"], "题面与入口")

    def test_system_remains_protected_when_history_exceeds_budget(self) -> None:
        messages = [
            {"role": "system", "content": "S" * 100_000},
            {"role": "user", "content": "题面"},
            {"role": "assistant", "content": "判断"},
            {"role": "tool", "content": "E" * 1_000},
        ]

        CTFBaseAgent._compact_messages(messages, initialized=True)

        self.assertEqual(messages[0]["content"], "S" * 100_000)
        self.assertIn("已折叠", messages[-1]["content"])

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
            {"method": "POST", "path": "/api/exec", "data": "cmd=ls",
             "action": "intrusive_write"})
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


class SpawnIdentityTests(unittest.IsolatedAsyncioTestCase):
    """spawn 的子路线必须有真实身份：_solve_turn 构造 ExecutionScope 与
    journal checkpoint 都读 route.route_id——缺了就 100% 摔 AttributeError，
    整个 spawn 分支连一次模型调用都到不了。"""

    async def test_spawn_child_route_carries_route_id(self) -> None:
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = "a"
        agent.agent_type = AgentType.CTF_WEB
        agent.direction = "web"
        agent.route_namespace = "ctf_web"
        agent.cache = SimpleNamespace(get=Mock(return_value=None), put=Mock())
        agent.ctx = SimpleNamespace(audit=Mock())
        seen: list[str] = []

        class _Stop(Exception):
            pass

        async def fake_solve(task, route, plan):
            seen.append(str(getattr(route, "route_id", "")))
            raise _Stop()

        agent._solve_turn = fake_solve
        task = Task(task_id="t1", task_type="ctf_solve", target="http://x",
                    agent_type=AgentType.CTF_WEB, payload={"challenge": {"id": "c1"}})
        state = {"depth": 0, "child_seq": 0, "observations": []}

        with self.assertRaises(_Stop):
            await agent._tool_spawn(task, SimpleNamespace(name="solve"), state,
                                    {"brief": "试点分支"})

        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0])


class CrossReplayDisclosureTests(unittest.IsolatedAsyncioTestCase):
    """跨分支回放必须自我披露并留 refresh 逃生口；带参数 GET 不回放。

    回放若无标记，模型会把旧录像当成新鲜响应（登录后再访问仍见登录前
    快照 → 误判未登录 → 死循环）；缓存键不含参数，带参回放会答非所问。
    """

    @staticmethod
    def _agent(shared: str | None) -> CTFBaseAgent:
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = "agent-test"
        agent.agent_type = AgentType.CTF_WEB
        agent.direction = "web"
        agent.flag_manager = None

        def cache_get(ns: str, key: str):
            if shared and ns == "ctf_shared" and key.startswith("fetch|"):
                return shared
            return None

        agent.cache = SimpleNamespace(get=Mock(side_effect=cache_get), put=Mock())
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

    async def test_cross_branch_replay_discloses_itself(self) -> None:
        agent = self._agent("GET /kb → 200\n正文: 登录页快照")
        out = await agent._tool_http(
            self._task(), SimpleNamespace(name="solve"),
            {"observations": [], "fetches": {}, "flags": [], "turn": 2},
            {"path": "/kb"})
        self.assertTrue(out.ok)
        self.assertIn("记录回放", out.text)
        self.assertIn("refresh", out.text)
        agent.ctx.tools.call.assert_not_awaited()

    async def test_parameterized_get_is_never_cross_replayed(self) -> None:
        agent = self._agent("GET /kb → 200\n正文: 旧答案")
        out = await agent._tool_http(
            self._task(), SimpleNamespace(name="solve"),
            {"observations": [], "fetches": {}, "flags": [], "turn": 2},
            {"path": "/kb", "params": {"q": "flag"}})
        agent.ctx.tools.call.assert_awaited_once()
        self.assertIn("fresh", out.text)

    async def test_cross_branch_refresh_forces_live_request(self) -> None:
        agent = self._agent("GET /kb → 200\n正文: 登录前快照")
        out = await agent._tool_http(
            self._task(), SimpleNamespace(name="solve"),
            {"observations": [], "fetches": {}, "flags": [], "turn": 2},
            {"path": "/kb", "refresh": True})
        agent.ctx.tools.call.assert_awaited_once()
        self.assertIn("fresh", out.text)


class EmptyReplyRepairTests(unittest.IsolatedAsyncioTestCase):
    """思考型模型空正文回合不是结论：注入提示再给一次机会，连续两次才收尾。"""

    def _agent(self, responses: list) -> CTFBaseAgent:
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = "agent-test"
        agent.agent_type = AgentType.CTF_WEB
        agent.direction = "web"
        agent.flag_manager = None
        agent.tool_schemas = []
        state = {"messages": [{"role": "system", "content": "s"},
                              {"role": "user", "content": "题面"}],
                 "seed": [], "observations": [], "flags": [],
                 "concluded": False, "stall": "", "turn": 0,
                 "llm_failures": 0, "empty_streak": 0,
                 "system_initialized": True, "fetches": {},
                 "empty_replies": 0, "peer_seen":
                     {"site": [], "notes": 0, "concl": 0, "flags": []}}
        self.state = state

        def cache_get(ns: str, key: str):
            return state if key.startswith("inv|") else None

        agent.cache = SimpleNamespace(get=Mock(side_effect=cache_get), put=Mock())
        agent.ctx = SimpleNamespace(
            audit=Mock(), ring=None, journal=None, builtin_ctx={},
            blackboard=SimpleNamespace(write=Mock()),
            gateway=SimpleNamespace(chat=AsyncMock(side_effect=responses)))
        return agent

    @staticmethod
    def _task() -> Task:
        return Task(task_id="task-1", task_type="ctf_solve", target="http://t",
                    agent_type=AgentType.CTF_WEB,
                    payload={"challenge": {"url": "http://t"}})

    @staticmethod
    def _route() -> SimpleNamespace:
        return SimpleNamespace(name="solve", route_id="route-1")

    @staticmethod
    def _reply(content: str) -> SimpleNamespace:
        return SimpleNamespace(content=content, tool_calls=[],
                               finish_reason="stop")

    async def test_first_empty_reply_gets_nudge_not_conclusion(self) -> None:
        agent = self._agent([self._reply("")])
        plan = RoutePlan(name="solve", strategy="", steps=[{"action": "investigate"}])
        result = await agent._solve_turn(self._task(), self._route(), plan)
        self.assertFalse(self.state["concluded"])
        self.assertEqual(self.state["empty_replies"], 1)
        nudges = [m for m in self.state["messages"] if m.get("role") == "user"
                  and "没有收到你的输出" in str(m.get("content"))]
        self.assertEqual(len(nudges), 1)
        self.assertTrue(result.ok)

    async def test_second_empty_reply_concludes_with_fallback(self) -> None:
        agent = self._agent([self._reply(""), self._reply("")])
        plan = RoutePlan(name="solve", strategy="", steps=[{"action": "investigate"}])
        await agent._solve_turn(self._task(), self._route(), plan)
        await agent._solve_turn(self._task(), self._route(), plan)
        self.assertTrue(self.state["concluded"])
        self.assertIn("未输出结论文本", self.state["conclusion_text"])

    async def test_nonempty_reply_concludes_immediately(self) -> None:
        agent = self._agent([self._reply("结论：Flag 在 /kb 的提示里")])
        plan = RoutePlan(name="solve", strategy="", steps=[{"action": "investigate"}])
        await agent._solve_turn(self._task(), self._route(), plan)
        self.assertTrue(self.state["concluded"])
        self.assertIn("/kb", self.state["conclusion_text"])
        self.assertEqual(self.state["empty_replies"], 0)


class BlackboardDedupTests(unittest.TestCase):
    """同层同 dedup_key 只存一条：后续写入升级既有记录而非追加新条。"""

    def test_same_dedup_key_updates_single_record(self) -> None:
        import tempfile
        from pathlib import Path

        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.models import BlackboardLayer

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            bb.write(BlackboardLayer.TOPOLOGY, "finding-a",
                     {"kind": "finding", "text": "首页 200"},
                     agent_type=AgentType.CTF_WEB, dedup_key="k1")
            bb.write(BlackboardLayer.TOPOLOGY, "finding-b",
                     {"kind": "finding", "text": "首页 200 再核"},
                     agent_type=AgentType.CTF_WEB, dedup_key="k1")
            self.assertEqual(bb.count(BlackboardLayer.TOPOLOGY), 1)
            row = bb.find_by_dedup_key(BlackboardLayer.TOPOLOGY, "k1")
            assert row is not None
            self.assertEqual(row["data"]["text"], "首页 200 再核")
            self.assertEqual(row["version"], 2)

    def test_distinct_dedup_keys_stay_separate(self) -> None:
        import tempfile
        from pathlib import Path

        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.models import BlackboardLayer

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            bb.write(BlackboardLayer.TOPOLOGY, "finding-a", {"text": "a"},
                     agent_type=AgentType.CTF_WEB, dedup_key="k1")
            bb.write(BlackboardLayer.TOPOLOGY, "finding-b", {"text": "b"},
                     agent_type=AgentType.CTF_WEB, dedup_key="k2")
            self.assertEqual(bb.count(BlackboardLayer.TOPOLOGY), 2)

    def test_legacy_duplicates_redirect_to_most_recent_record(self) -> None:
        """历史重复（去重上线前的旧库）下，写入重定向须与读取同口径。

        读取按 updated_at 取最新一条；写入若重定向到旧条，收敛写回带着
        读取侧版本号就会撞 VersionConflict。
        """
        import json
        import tempfile
        from pathlib import Path

        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.models import BlackboardLayer

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            bb.write(BlackboardLayer.TOPOLOGY, "finding-a", {"text": "旧"},
                     agent_type=AgentType.CTF_WEB, dedup_key="k1")
            # 模拟旧库残留：同键第二条记录（版本更高、更新时间更新）
            bb._conn.execute(
                "INSERT INTO bb_records (layer, record_id, version, dedup_key,"
                " data, updated_by, updated_at) VALUES (?,?,?,?,?,?,?)",
                (BlackboardLayer.TOPOLOGY.value, "legacy-b", 3, "k1",
                 json.dumps({"text": "历史重复"}), "", "9999-12-31T00:00:00+00:00"))
            bb._conn.commit()

            target = bb.find_by_dedup_key(BlackboardLayer.TOPOLOGY, "k1")
            assert target is not None
            self.assertEqual(target["record_id"], "legacy-b")

            bb.write(BlackboardLayer.TOPOLOGY, "finding-c", {"text": "收敛"},
                     agent_type=AgentType.CTF_WEB, dedup_key="k1",
                     expected_version=target["version"])
            row = bb.find_by_dedup_key(BlackboardLayer.TOPOLOGY, "k1")
            assert row is not None
            self.assertEqual(row["record_id"], "legacy-b")
            self.assertEqual(row["data"]["text"], "收敛")
            self.assertEqual(row["version"], 4)


class PeerAcceptedFlagValveTests(unittest.TestCase):
    """并行收敛阀只看同伴战果：本路线自己提交的 Flag 不算「同伴夺旗」。"""

    @staticmethod
    def _agent(records: list[dict]) -> CTFBaseAgent:
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = "a"
        agent.agent_type = AgentType.CTF_WEB
        agent.direction = "web"
        agent.cache = SimpleNamespace(get=Mock(return_value=list(records)), put=Mock())
        return agent

    @staticmethod
    def _record(value: str, *, route: str, rnd: int = 2,
                verdict: str = "accepted") -> dict:
        return {"value": value, "by": "web", "route": route,
                "round": rnd, "verdict": verdict}

    @staticmethod
    def _task() -> Task:
        return Task(task_id="t1", task_type="ctf_solve", target="http://x",
                    agent_type=AgentType.CTF_WEB, round_no=2,
                    payload={"challenge": {"id": "c1"}})

    def _fresh(self, records: list[dict], *, route: str = "solve") -> list[str]:
        agent = self._agent(records)
        return agent._peer_accepted_flags(
            self._task(), SimpleNamespace(name=route), {"flags": []})

    def test_own_route_flag_is_not_a_convergence_signal(self) -> None:
        # 修复前：记录无 route 字段，本路线刚提交成功的 Flag 下一回合
        # 被当「同伴夺旗」收尾（结论文本谎报出处、多 Flag 题提前收工）
        self.assertEqual(self._fresh([self._record("flag{x}", route="solve")]), [])

    def test_peer_accepted_same_round_is_a_convergence_signal(self) -> None:
        self.assertEqual(self._fresh([self._record("flag{x}", route="peer")]),
                         ["flag{x}"])

    def test_peer_pending_or_old_round_is_not(self) -> None:
        self.assertEqual(
            self._fresh([self._record("flag{x}", route="peer", verdict="pending")]), [])
        self.assertEqual(
            self._fresh([self._record("flag{x}", route="peer", rnd=1)]), [])

    def test_flag_already_held_by_this_route_is_not(self) -> None:
        agent = self._agent([self._record("flag{x}", route="peer")])
        fresh = agent._peer_accepted_flags(
            self._task(), SimpleNamespace(name="solve"), {"flags": ["flag{x}"]})
        self.assertEqual(fresh, [])


class FindingDedupKeyTests(unittest.TestCase):
    """发现去重键跨进程稳定：内建 hash() 带随机盐，重启后同一发现重复落板。"""

    def test_dedup_key_uses_stable_digest(self) -> None:
        import zlib

        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = "a"
        agent.agent_type = AgentType.CTF_WEB
        agent.direction = "web"
        calls: list[dict] = []
        agent.ctx = SimpleNamespace(blackboard=SimpleNamespace(
            write=Mock(side_effect=lambda layer, rid, data, **kw: calls.append(kw))))
        task = Task(task_id="t1", task_type="ctf_solve", target="http://x",
                    agent_type=AgentType.CTF_WEB,
                    payload={"challenge": {"id": "c1"}})

        agent._record_finding(task, "flag", "flag{deadbeef}", url="http://x/f")

        expected = f"finding|c1|flag|{zlib.crc32(b'http://x/f'):08x}"
        self.assertEqual(calls[0]["dedup_key"], expected)


if __name__ == "__main__":
    unittest.main()
