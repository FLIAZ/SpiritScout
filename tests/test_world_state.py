"""世界状态片段：系统提示保持稳定，授权扩围只追加自包含的变化片段。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from spiritscout.agents.profiles import agent_system_prompt
from spiritscout.core.models import AgentType, Scope
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

    def test_missing_snapshot_on_resume_replays_full_state(self) -> None:
        fragment, changed = self.agent._sync_world_state({})
        self.assertIn("<world_state>", fragment)
        self.assertIn("authorization", fragment)
        self.assertIn("environment", fragment)
        self.assertEqual(changed, ["authorization", "environment"])


if __name__ == "__main__":
    unittest.main()
