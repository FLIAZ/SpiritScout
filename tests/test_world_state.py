"""世界状态片段：系统提示保持稳定，授权扩围只追加自包含的变化片段。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.profiles import agent_system_prompt
from spiritscout.blackboard import Blackboard
from spiritscout.core.models import AgentType, BlackboardLayer, Scope
from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.gateway import world_state
from spiritscout.gateway.context import decision_messages
from spiritscout.policy.scope import ScopeGuard


class WorldStateSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = ScopeGuard(Scope(
            allowed_domains=["a.test"], allowed_urls=["https://b.test/app"],
            denied=["deny.test"]))

    def test_authorization_carries_scope_approved_and_denied(self) -> None:
        self.guard.approve_target("https://c.test/login")
        snap = world_state.snapshot(guard=self.guard)
        auth = snap["authorization"]
        self.assertEqual(auth["allowed_domains"], ["a.test"])
        self.assertEqual(auth["allowed_urls"], ["https://b.test/app"])
        self.assertEqual(auth["denied"], ["deny.test"])
        self.assertEqual(auth["approved_by_user"], ["https://c.test/"])

    def test_long_boundary_declarations_are_not_truncated_or_compiled(self) -> None:
        rules = "仅测授权环境，第三方不要访问。\n" * 2500 + "最终例外需再次询问用户"
        notes = "用户补充原话\n" * 1000
        self.guard.scope.testing_rules = rules
        self.guard.scope.notes = notes
        auth = world_state.snapshot(guard=self.guard)["authorization"]
        self.assertEqual(auth["testing_rules"], rules)
        self.assertEqual(auth["notes"], notes)
        self.assertEqual(auth["denied"], ["deny.test"])
        self.assertEqual(auth["boundary_enforcement"], "model_context")

    def test_first_fragment_then_only_changed_fields(self) -> None:
        first = world_state.snapshot(guard=self.guard)
        self.assertEqual(world_state.update(None, first),
                         world_state.render(first))
        self.assertIsNone(world_state.update(first, first))

        self.guard.approve_target("https://c.test/")
        second = world_state.snapshot(guard=self.guard)
        fragment = world_state.update(first, second)
        assert fragment is not None
        self.assertIn('update="true"', fragment)
        self.assertIn("https://c.test/", fragment)
        self.assertIn('"authorization"', fragment)
        self.assertNotIn('"environment"', fragment)
        self.assertIsNone(world_state.update(second, second))

    def test_removed_field_is_marked_null(self) -> None:
        previous = {"authorization": {"denied": []}, "environment": {"date": "x"}}
        current = {"authorization": {"denied": []}}
        fragment = world_state.update(previous, current)
        assert fragment is not None
        self.assertIn('"environment":null', fragment.replace(" ", ""))

    def test_environment_is_runtime_fact_not_capability_list(self) -> None:
        env = world_state.environment(workspace="/ws", artifacts="/ws/artifacts")
        self.assertEqual(env["workspace"], "/ws")
        self.assertEqual(env["artifacts"], "/ws/artifacts")
        self.assertNotIn("tools", env)
        self.assertNotIn("commands", env)


class UserBoundaryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "bb.db"
        self.bb = Blackboard(self.path)
        self.addCleanup(self.bb.close)

    def test_user_originals_and_project_rules_survive_database_reopen(self) -> None:
        statements = [{"kind": "message", "text": "  先看测试环境\n" * 3000 + "尾部不要访问生产"},
                      {"kind": "answer", "question": "能扩围吗？", "answer": "不能，保持原范围。", "scope_added": []}]
        project_rules = "项目规则\n" * 4000 + "尾部仅限测试租户"
        self.bb.write(BlackboardLayer.TOPOLOGY, "project-memory", {"description": project_rules})
        for statement in statements:
            world_state.record_user_statement(self.bb, statement)
        restored = Blackboard(self.path)
        self.addCleanup(restored.close)
        snapshot = world_state.snapshot(blackboard=restored)
        self.assertEqual(snapshot["user_context"]["statements"], statements)
        self.assertEqual(snapshot["project_rules"], project_rules)
        self.assertEqual(set(snapshot["user_context"]), {"statements"})

    def test_concurrent_user_answers_do_not_overwrite_each_other(self) -> None:
        read = self.bb.read_versioned
        barrier = threading.Barrier(2)
        worker = threading.local()

        def simultaneous_read(*args, **kwargs):
            row = read(*args, **kwargs)
            if not getattr(worker, "read_once", False):
                worker.read_once = True
                barrier.wait(timeout=5)
            return row

        statements = [{"kind": "answer", "answer": answer} for answer in ("仅测 A", "不测 B")]
        with patch.object(self.bb, "read_versioned", side_effect=simultaneous_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda statement: world_state.record_user_statement(self.bb, statement), statements))
        saved = world_state.snapshot(blackboard=self.bb)["user_context"]["statements"]
        self.assertCountEqual(saved, statements)

    def test_concurrent_connections_keep_every_user_statement(self) -> None:
        barrier = threading.Barrier(4)
        statements = [{"kind": "message", "text": f"用户原话 {index}",
                       "statement_id": f"message-{index}"} for index in range(4)]

        def append(statement):
            blackboard = Blackboard(self.path)
            read = blackboard.read_versioned
            first = True

            def simultaneous_read(*args, **kwargs):
                nonlocal first
                row = read(*args, **kwargs)
                if first:
                    first = False
                    barrier.wait(timeout=5)
                return row

            try:
                with patch.object(blackboard, "read_versioned", side_effect=simultaneous_read):
                    world_state.record_user_statement(blackboard, statement)
            finally:
                blackboard.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, statements))
        saved = world_state.snapshot(blackboard=self.bb)["user_context"]["statements"]
        self.assertCountEqual(saved, statements)

    def test_statement_id_is_idempotent_but_identical_text_is_not(self) -> None:
        statement = {"kind": "message", "text": "只测测试环境", "statement_id": "message-1"}
        world_state.record_user_statement(self.bb, statement)
        world_state.record_user_statement(self.bb, statement)
        repeated = {**statement, "statement_id": "message-2"}
        world_state.record_user_statement(self.bb, repeated)
        saved = world_state.snapshot(blackboard=self.bb)["user_context"]["statements"]
        self.assertEqual(saved, [statement, repeated])

    def test_resume_restores_identified_messages_even_with_existing_context(self) -> None:
        original = {"kind": "answer", "answer": "不能扩围", "statement_id": "answer-1"}
        world_state.record_user_statement(self.bb, original)
        history = [{"role": "user", "content": "稍后只测 A", "statement_id": "message-1"},
                   {"role": "user", "content": "稍后只测 A", "statement_id": "message-2"}]
        for _ in range(2):
            world_state.sync_user_context(self.bb, dialogue_history=history,
                                          mission="继续", standing_rules="不要访问 B")
        context = world_state.snapshot(blackboard=self.bb)["user_context"]
        self.assertEqual(context["statements"], [original, *[
            {"kind": "message", "text": message["content"], "statement_id": message["statement_id"]}
            for message in history]])
        self.assertEqual(context["standing_rules"], "不要访问 B")

    def test_mission_update_cannot_overwrite_a_concurrent_user_answer(self) -> None:
        original = {"kind": "message", "text": "仅测 A", "statement_id": "message-1"}
        answer = {"kind": "answer", "answer": "不测 B", "statement_id": "answer-1"}
        world_state.record_user_statement(self.bb, original)
        write = self.bb.write
        injected = False

        def concurrent_write(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                world_state.record_user_statement(self.bb, answer)
            return write(*args, **kwargs)

        with patch.object(self.bb, "write", side_effect=concurrent_write):
            world_state.sync_user_context(self.bb, mission="继续测试", standing_rules="只读")
        context = world_state.snapshot(blackboard=self.bb)["user_context"]
        self.assertEqual(context["statements"], [original, answer])
        self.assertEqual(context["mission"], "继续测试")
        self.assertEqual(context["standing_rules"], "只读")

    def test_master_does_not_duplicate_a_persisted_inbox_message(self) -> None:
        statement = {"kind": "message", "text": "保持原范围", "statement_id": "message-1"}
        world_state.record_user_statement(self.bb, statement)
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(blackboard=self.bb, extra={"user_inbox": [statement]})
        self.assertEqual(master._drain_user_feed(), [statement])
        self.assertEqual(world_state.snapshot(blackboard=self.bb)["user_context"]["statements"],
                         [statement])

    def test_master_keeps_every_user_message_without_boundary_classification(self) -> None:
        message = "普通观察\n" * 3000 + "暂时别碰该租户"
        inbox = [{"text": message}, {"text": "谢谢"}, {"attachments": ["capture.har"]}]
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(blackboard=self.bb, extra={"user_inbox": inbox})
        received = master._drain_user_feed()
        self.assertEqual(len(received), 3)
        self.assertEqual(inbox, [])
        self.assertEqual(world_state.snapshot(blackboard=self.bb)["user_context"]["statements"],
                         [{"kind": "message", "text": message}, {"kind": "message", "text": "谢谢"}])


class StablePromptTests(unittest.TestCase):
    def test_role_prompt_does_not_interpolate_scope(self) -> None:
        guard = ScopeGuard(Scope(allowed_domains=["a.test"]))
        before = agent_system_prompt(AgentType.CTF_WEB)
        guard.approve_target("b.test")
        after = agent_system_prompt(AgentType.CTF_WEB)

        self.assertEqual(before, after)
        self.assertIn("<world_state>", before)
        self.assertNotIn("a.test", before)

    def test_world_state_leads_the_stable_user_block(self) -> None:
        messages = decision_messages("sys", {
            "world_state": {"authorization": {"allowed_urls": ["https://a.test"]}},
            "round": 3,
            "situation": {"round": 3},
        })
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn('"world_state"', messages[1]["content"])
        self.assertNotIn('"world_state"', messages[2]["content"])

    def test_world_state_is_protected_from_projection_truncation(self) -> None:
        world = {"authorization": {"allowed_urls": ["https://" + "a" * 160]}}
        messages = decision_messages("sys", {
            "world_state": world,
            "situation": {"noise": "x" * 8000},
        }, max_chars=200)
        self.assertIn("allowed_urls", messages[1]["content"])
        self.assertIn("a" * 160, messages[1]["content"])


class CTFWorldStateUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = ScopeGuard(Scope(allowed_domains=["a.test"]))
        self.agent = object.__new__(CTFBaseAgent)
        self.agent.agent_id = "ctf"
        self.agent.ctx = SimpleNamespace(
            policy=SimpleNamespace(guard=self.guard),
            builtin_ctx={"workspace_root": "/ws"})

    def test_scope_expansion_appends_one_self_contained_fragment(self) -> None:
        state = {"world_state": self.agent._world_snapshot()}
        self.assertEqual(self.agent._sync_world_state(state), ("", []))

        self.guard.approve_target("b.test")
        fragment, changed = self.agent._sync_world_state(state)
        self.assertIn('update="true"', fragment)
        self.assertIn("b.test", fragment)
        self.assertEqual(changed, ["authorization"])
        self.assertEqual(self.agent._sync_world_state(state), ("", []))

    def test_user_boundary_update_is_complete_for_the_next_ctf_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bb = Blackboard(Path(directory) / "bb.db")
            self.addCleanup(bb.close)
            self.agent.ctx.blackboard = bb
            first = {"kind": "message", "text": "仅测 A"}
            second = {"kind": "answer", "question": "能测 B 吗？", "answer": "不可以。" * 2000}
            world_state.record_user_statement(bb, first)
            state = {"world_state": self.agent._world_snapshot()}
            world_state.record_user_statement(bb, second)
            fragment, changed = self.agent._sync_world_state(state)
            self.assertEqual(changed, ["user_context"])
            body = json.loads(fragment.split("\n", 1)[1].rsplit("\n", 1)[0])
            self.assertEqual(body["user_context"]["statements"], [first, second])
            self.assertEqual(self.agent._sync_world_state(state), ("", []))

    def test_missing_snapshot_on_resume_replays_full_state(self) -> None:
        fragment, changed = self.agent._sync_world_state({})
        self.assertIn("<world_state>", fragment)
        self.assertIn("authorization", fragment)
        self.assertIn("environment", fragment)
        self.assertEqual(changed, ["authorization", "environment"])


if __name__ == "__main__":
    unittest.main()
