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
    async def test_project_session_gets_memory_unclassified_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = _cfg(root / "project-session")
            session_root = Path(cfg.workspace.root)
            session_root.mkdir(parents=True)
            cfg.blackboard.sqlite_path = str(session_root / "bb.db")
            cfg.knowledge.sqlite_path = str(session_root / "kb.db")
            cfg.audit_log_path = str(session_root / "audit.jsonl")
            store = ResourceStore(cfg.resources.root)
            project = store.create_project("BiliBili SRC", "主站与直播")
            store.set_session_project("project-session", project["project_id"])
            store.observe({"asset_type": "app", "value": "https://example.test"},
                          "project-session:asset:1@1")

            infra = await _build_infra(
                cfg, [ModelEntry(model="local", provider="mock")], AUTH)
            try:
                memory = infra.blackboard.read(
                    BlackboardLayer.TOPOLOGY, "project-memory")
                self.assertEqual(memory["name"], "BiliBili SRC")
                self.assertEqual(memory["assets"][0]["value"],
                                 "https://example.test/")
                text = json.dumps(memory, ensure_ascii=False)
                for forbidden in ("scope", "allowed_domains", "denied"):
                    self.assertNotIn(forbidden, text)
                shared = shared_context(infra.blackboard)
                self.assertEqual(shared["project_memory"]["name"], "BiliBili SRC")
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
            self.assertIn(rules, state["messages"][1]["content"])
            self.assertEqual(state["messages"][1]["content"].count(rules), 1)
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
            # 说明就是测试规则：全文注入且不参与正文截断
            self.assertIn("project_rules", content)
            self.assertIn("长" * 8000, content)
            self.assertLess(content.index("project_rules"), content.index("## 任务"))
            encoded = json.dumps(memory, ensure_ascii=False)
            self.assertNotIn(encoded, content)
            # 历史记忆块不含说明正文，规则只出现一次
            self.assertEqual(content.count("长" * 8000), 1)


if __name__ == "__main__":
    unittest.main()
