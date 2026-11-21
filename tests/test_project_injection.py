"""项目共享记忆注入：运行时拓扑、共享上下文与 CTF 首条消息。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from spiritscout.agents.sub.base import RoutePlan
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry, SystemConfig, WorkspaceConfig
from spiritscout.core.models import (
    AgentType,
    Authorization,
    BlackboardLayer,
    RunMode,
    Scope,
    Task,
)
from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.gateway import world_state
from spiritscout.gateway.context import shared_context
from spiritscout.policy.scope import ScopeGuard
from spiritscout.resources.store import ResourceStore
from spiritscout.runtime import _build_infra


def _cfg(root: Path) -> SystemConfig:
    cfg = SystemConfig(
        workspace=WorkspaceConfig(root=str(root)),
        models_config_path=str(root / "models.yaml"),
        agent_models_path=str(root / "agents.yaml"),
        audit_log_path=str(root / "audit.jsonl"))
    cfg.resources.root = str(root / "resources")
    return cfg


AUTH = Authorization(
    auth_id="local",
    mode=RunMode.SRC,
    human_confirmed=True,
    scope=Scope(allowed_urls=["https://example.test"]),
)


class RuntimeProjectMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_src_keeps_project_context_but_only_reads_archives_on_demand(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = _cfg(root / "project-session")
            session_root = Path(cfg.workspace.root)
            session_root.mkdir(parents=True)
            cfg.blackboard.sqlite_path = str(session_root / "bb.db")
            cfg.knowledge.sqlite_path = str(session_root / "kb.db")
            cfg.audit_log_path = str(session_root / "audit.jsonl")
            store = ResourceStore(cfg.resources.root)
            project = store.create_project("BiliBili SRC", "主站与直播；仅限测试租户")
            store.set_session_project("project-session", project["project_id"])
            store.observe({"asset_type": "app", "value": "https://example.test"},
                          "project-session:asset:1@1")
            store.observe({"asset_type": "app", "value": "https://old-other.test"},
                          "project-session:asset:2@1")

            infra = await _build_infra(
                cfg, [ModelEntry(model="local", provider="mock")], AUTH)
            try:
                memory = infra.blackboard.read(
                    BlackboardLayer.TOPOLOGY, "project-memory")
                self.assertEqual(memory["name"], "BiliBili SRC")
                self.assertNotIn("assets", memory)
                self.assertNotIn("findings", memory)
                self.assertIn("仅限测试租户", memory["description"])
                text = json.dumps(memory, ensure_ascii=False)
                for forbidden in ("scope", "allowed_domains", "denied"):
                    self.assertNotIn(forbidden, text)
                shared = shared_context(infra.blackboard)
                self.assertNotIn("project_memory", shared)
                self.assertNotIn("historical_assets", shared)
                world = world_state.snapshot(blackboard=infra.blackboard)
                self.assertEqual(world["project_context"]["description"], memory["description"])
                self.assertNotIn("project_rules", world, "背景不再被整体升级成测试规则")
                self.assertNotIn("old-other.test", json.dumps(world))

                allowed = infra.policy.guard.scope.model_dump()
                history = await infra.tools.call("history_lookup", {"kind": "assets"}, **infra.builtin_ctx)
                self.assertTrue(history.ok, history.error)
                archive = json.loads(history.stdout)
                self.assertEqual({item["value"] for item in archive["items"]},
                                 {"https://example.test/", "https://old-other.test/"})
                self.assertTrue(all(item["source"] and item["last_seen"]
                                    and not item["verified_current_session"] for item in archive["items"]))
                self.assertFalse(infra.policy.guard.is_allowed("https://old-other.test/"))
                self.assertEqual(infra.policy.guard.scope.model_dump(), allowed)

                # 旧快照留在库里可查，恢复投影不应再次自动带入；当前调查记录仍保留。
                infra.blackboard.write(BlackboardLayer.TOPOLOGY, "historical-assets", {"items": ["legacy-history"]})
                infra.blackboard.write(BlackboardLayer.TOPOLOGY, "project-memory", {
                    **memory, "assets": ["legacy-history"], "findings": ["legacy-finding"]})
                infra.blackboard.write(BlackboardLayer.TASK_STATE, "current-task", {"status": "running"})
                self.assertNotIn("legacy-history", json.dumps(shared_context(infra.blackboard)))
                self.assertNotIn("legacy-history", json.dumps(world_state.snapshot(blackboard=infra.blackboard)))
                from spiritscout.agents.secretary import secretary_snapshot

                advisory = secretary_snapshot(infra.blackboard, {})
                self.assertNotIn("legacy-history", json.dumps(advisory))
                self.assertNotIn("legacy-finding", json.dumps(advisory))
                self.assertIn("current-task", json.dumps(advisory))
                self.assertEqual(infra.blackboard.read(BlackboardLayer.TASK_STATE, "current-task"), {"status": "running"})
                self.assertIsNotNone(infra.blackboard.read(BlackboardLayer.TOPOLOGY, "historical-assets"))
            finally:
                await infra.close()

            other = _cfg(root / "plain-session")
            other_root = Path(other.workspace.root)
            other_root.mkdir(parents=True)
            other.blackboard.sqlite_path = str(other_root / "bb.db")
            other.knowledge.sqlite_path = str(other_root / "kb.db")
            other.audit_log_path = str(other_root / "audit.jsonl")
            infra = await _build_infra(
                other, [ModelEntry(model="local", provider="mock")], AUTH)
            try:
                self.assertIsNone(infra.blackboard.read(
                    BlackboardLayer.TOPOLOGY, "project-memory"))
                self.assertNotIn("project_memory",
                                 shared_context(infra.blackboard))
            finally:
                await infra.close()


class HistoryLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_master_can_request_history_without_automatic_loading_and_keeps_prefix(self):
        from unittest.mock import AsyncMock

        from spiritscout.agents.master.agent import MasterAgent
        from spiritscout.agents.master.contracts import MasterContext
        from spiritscout.gateway.context import decision_memory, decision_messages
        from spiritscout.gateway.gateway import ChatResponse, chat_decision
        from spiritscout.policy import PolicyEngine
        from spiritscout.tools.history import history_lookup

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            store = ResourceStore(Path(tmp) / "resources")
            project = store.create_project("history")
            store.set_session_project("current", project["project_id"])
            store.observe({"asset_type": "domain", "value": "old-only.test"}, "current:asset:old@1")

            async def lookup(arguments):
                result = await history_lookup(**arguments, resources=store, workspace_root="/sessions/current")
                return {"ok": result.ok, "result": result.stdout}

            callback = AsyncMock(side_effect=lookup)
            gateway = SimpleNamespace(chat=AsyncMock())
            master = object.__new__(MasterAgent)
            master.ctx = MasterContext(bus=None, blackboard=bb, convergence=None, gateway=gateway,
                policy=PolicyEngine(RunMode.SRC, ScopeGuard(Scope(allowed_domains=["current.test"]))),
                agents={}, history_lookup=callback)

            async def reply(messages, **kwargs):
                if gateway.chat.await_count == 1:
                    callback.assert_not_awaited()
                    self.assertNotIn("old-only.test", str(messages))
                    content = {"skill_requests": [{"name": "history_lookup", "arguments": {"kind": "assets"}}]}
                else:
                    self.assertIn("old-only.test", str(messages))
                    content = {"dag_nodes": [], "analysis": "历史只作为后续核验线索"}
                return ChatResponse(content=json.dumps(content), model="fixture", prompt_tokens=1,
                                    completion_tokens=1, cost_usd=0, latency_ms=0)

            gateway.chat.side_effect = reply
            await chat_decision(gateway, decision_messages(master._system_prompt(), {"target": "current.test"}),
                                memory=decision_memory(master, "history-test"), actor="master", response_format="json")
            callback.assert_awaited_once_with({"kind": "assets"})
            first, second = [call.args[0] for call in gateway.chat.await_args_list]
            self.assertEqual(second[:len(first)], first)
            self.assertFalse(master.ctx.policy.guard.is_allowed("old-only.test"))

    async def test_asset_lookup_is_project_scoped_and_keeps_source_and_time(self):
        from spiritscout.tools.history import history_lookup

        with tempfile.TemporaryDirectory() as tmp:
            store = ResourceStore(Path(tmp) / "resources")
            first, second = store.create_project("first"), store.create_project("second")
            store.set_session_project("current", first["project_id"])
            store.set_session_project("old", second["project_id"])
            store.observe({"asset_type": "domain", "value": "first.test"}, "current:asset:one@1")
            store.observe({"asset_type": "domain", "value": "second.test"}, "old:asset:two@1")
            result = await history_lookup("assets", resources=store, workspace_root="/sessions/current")
            self.assertTrue(result.ok)
            items = json.loads(result.stdout)["items"]
            self.assertEqual([item["value"] for item in items], ["first.test"])
            self.assertEqual(items[0]["source"], "current:asset:one@1")
            self.assertTrue(items[0]["last_seen"])
            self.assertFalse(items[0]["verified_current_session"])
            self.assertEqual(store.assets()["total"], 2)

    async def test_experience_lookup_pages_candidates_without_promoting_them(self):
        from spiritscout.knowledge.experience import ExperienceStore
        from spiritscout.tools.history import history_lookup

        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / "experience.db")
            try:
                for index in range(10):
                    store.add(f"登录历史观察 {index}", source_session="old-session")
                store.add("不相关的二进制观察")
                first = await history_lookup("experience", query="登录", experience=store)
                second = await history_lookup("experience", query="登录", offset=8, experience=store)
                self.assertTrue(first.ok and second.ok)
                one, two = json.loads(first.stdout), json.loads(second.stdout)
                self.assertEqual((len(one["items"]), len(two["items"])), (8, 2))
                self.assertTrue(one["has_more"])
                self.assertFalse(two["has_more"])
                self.assertEqual(len({item["insight_id"] for item in one["items"] + two["items"]}), 10)
                self.assertTrue(all(item["quality_status"] == "candidate" and item["ts"]
                                    and item["source_session"] == "old-session" for item in one["items"]))
            finally:
                store.close()
            failed = await history_lookup("experience", query="登录", experience=store)
            self.assertFalse(failed.ok, "数据库错误不能伪装成空档案")


class CTFProjectMemorySeedTests(unittest.TestCase):
    def _agent(self, blackboard: Blackboard) -> CTFBaseAgent:
        agent = object.__new__(CTFBaseAgent)
        agent.ctx = SimpleNamespace(
            blackboard=blackboard,
            policy=SimpleNamespace(
                guard=ScopeGuard(Scope(allowed_urls=["http://challenge.test/"]))),
            builtin_ctx={})
        agent.direction = "web"
        agent.challenge_of = lambda task: {"url": "http://challenge.test/"}
        agent._shared_conclusions = lambda task: []
        agent._shared_notes = lambda task: []
        agent._shared_site = lambda task: {}
        agent._peer_flags = lambda task, state: []
        agent._solver_system_prompt = lambda: "system"
        return agent

    def test_long_task_rules_survive_seed_observation_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blackboard = Blackboard(Path(directory) / "bb.db")
            self.addCleanup(blackboard.close)
            agent = self._agent(blackboard)
            rules = "用户既定边界\n" * 5000 + "完整规则尾部：不测第三方"
            task = Task(task_id="t-long", task_type="ctf_solve", target="http://challenge.test/",
                        agent_type=AgentType.CTF_WEB, payload={"rules": rules})
            route = RoutePlan(name="web", strategy="先读现场")
            state = {"seed": ["预感知响应" * 20000]}
            agent._seed_messages(task, route, route, state)
            self.assertNotIn(rules, state["messages"][1]["content"])
            self.assertIn("blackboard_ref", state["messages"][1]["content"])
            records = blackboard.query(BlackboardLayer.TARGET_AUTH)
            self.assertTrue(any(row["data"].get("content") == rules for row in records))
            self.assertIn("## 任务", state["messages"][1]["content"])

    def test_memory_appended_last_and_capped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blackboard = Blackboard(Path(directory) / "bb.db")
            memory = {
                "project_id": "prj-test",
                "name": "BiliBili SRC",
                "description": "长" * 8000,
                "assets": [],
                "findings": [],
                "note": "同项目历史参考，未复核，不构成授权。",
            }
            blackboard.write(BlackboardLayer.TOPOLOGY, "project-memory", memory,
                             updated_by="project-memory")
            agent = self._agent(blackboard)
            task = Task(task_id="t1", task_type="ctf_solve",
                        target="http://challenge.test/",
                        agent_type=AgentType.CTF_WEB)
            route = RoutePlan(name="web", strategy="")
            state = {"seed": []}
            try:
                agent._seed_messages(task, route, route, state)
                content = state["messages"][1]["content"]
            finally:
                blackboard.close()
            self.assertIn("<world_state>", content)
            self.assertIn("challenge.test", content)
            self.assertIn("## 所属项目的历史记忆", content)
            # 说明作为规则无损落黑板，上下文仅带引用
            self.assertIn("project_rules", content)
            self.assertNotIn("长" * 8000, content)
            self.assertIn("blackboard_ref", content)
            self.assertLess(content.index("project_rules"), content.index("## 任务"))
            encoded = json.dumps(memory, ensure_ascii=False)
            self.assertNotIn(encoded, content)
            # 历史记忆块和上下文不复制规则正文
            self.assertEqual(content.count("长" * 8000), 0)


if __name__ == "__main__":
    unittest.main()
