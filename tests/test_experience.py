"""经验库跨会话闭环：共享路径、去重、只读快照、终态提炼与审计留痕。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import (
    ExperienceConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import Authorization, BlackboardLayer, RunMode, Scope
from spiritscout.gateway import ModelGateway, ModelRegistry
from spiritscout.gateway.backends import MockBackend
from spiritscout.knowledge.experience import (
    ExperienceStore,
    experience_db_path,
    format_insights,
    read_recent,
)
from spiritscout.runtime import _distill_best_effort, _distill_digest, _worth_distilling
from spiritscout.web.sessions import Session, SessionManager


def _authorization() -> Authorization:
    return Authorization(auth_id="auth-test", mode=RunMode.SRC, scope=Scope(),
                         target_description="example.test")


class ExperienceStoreTests(unittest.TestCase):
    def test_db_path_follows_root_or_workspace(self) -> None:
        self.assertEqual(experience_db_path("", ".spiritscout"),
                         Path(".spiritscout/experience.db"))
        self.assertEqual(experience_db_path("/tmp/exp", ".spiritscout"),
                         Path("/tmp/exp/experience.db"))

    def test_add_dedupes_and_recent_keeps_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = NullAuditLogger()
            store = ExperienceStore(Path(tmp) / "experience.db", audit=audit)
            try:
                first = store.add("登录接口错误回显泄露账号存在性",
                                  context="会员系统登录", tags=["web", "登录"],
                                  source_session="a.example")
                duplicate = store.add("登录接口错误回显泄露账号存在性")
                self.assertTrue(first)
                self.assertEqual(duplicate, "")
                self.assertEqual(store.count(), 1)
                items = store.recent(limit=5)
                self.assertEqual(items[0]["tags"], ["web", "登录"])
                types = [record["type"] for record in audit.records]
                self.assertIn("experience.added", types)
                self.assertIn("experience.duplicate", types)
                hits = store.search("账号存在性")
                self.assertEqual(len(hits), 1)
            finally:
                store.close()

    def test_read_recent_is_readonly_and_tolerates_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.db"
            self.assertEqual(read_recent(missing), {"total": 0, "items": []})
            self.assertFalse(missing.exists())      # 只读快照绝不建库
            store = ExperienceStore(Path(tmp) / "experience.db")
            store.add("一条洞察", tags=["ctf"])
            store.close()
            snapshot = read_recent(Path(tmp) / "experience.db", limit=3)
            self.assertEqual(snapshot["total"], 1)
            self.assertEqual(snapshot["items"][0]["tags"], ["ctf"])

    def test_format_insights_carries_scenario_labels(self) -> None:
        lines = format_insights([{
            "insight": "仅看响应码不足以判定越权", "context": "会员接口",
            "tags": ["web", "越权"], "source_session": "b.example"}])
        self.assertEqual(lines[0],
                         "[web、越权] 仅看响应码不足以判定越权（适用：会员接口；来源：b.example）")


class SharedExperienceRootTests(unittest.TestCase):
    """Web 会话的路径重定向把经验库排除在外——隔离工作区但不隔离经验。"""

    def _manager(self, root: Path, experience_root: str = "") -> SessionManager:
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
        if experience_root:
            cfg.experience = ExperienceConfig(root=experience_root)
        return SessionManager(cfg, [])

    def test_session_cfg_keeps_experience_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self._manager(root)
            workspace = root / "sessions" / "sess-a"
            workspace.mkdir(parents=True)
            session = Session(session_id="sess-a", mode="src", target="t",
                              workspace=workspace)
            session_cfg = manager._session_cfg(session, None)
            self.assertEqual(session_cfg.workspace.root, str(workspace))
            self.assertEqual(session_cfg.experience.root, str(root))
            self.assertEqual(
                experience_db_path(session_cfg.experience.root,
                                   session_cfg.workspace.root),
                root / "experience.db")

    def test_explicit_experience_root_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "expshare"
            manager = self._manager(root, experience_root=str(shared))
            session = Session(session_id="sess-b", mode="src", target="t",
                              workspace=root / "sessions" / "sess-b")
            session_cfg = manager._session_cfg(session, None)
            self.assertEqual(session_cfg.experience.root, str(shared))


class DistillTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_path_caps_distill_timeout(self) -> None:
        """停止按钮在等这次提炼：中止路径的单次上限收紧到 20 秒。"""
        with tempfile.TemporaryDirectory() as tmp:
            audit = NullAuditLogger()
            store = ExperienceStore(Path(tmp) / "experience.db", audit=audit)
            blackboard = Blackboard(Path(tmp) / "bb.db")
            infra = SimpleNamespace(
                audit=audit, gateway=None, blackboard=blackboard,
                agent_models={"master": ""}, authorization=_authorization(),
                cfg=SimpleNamespace(experience=SimpleNamespace(distill_timeout_seconds=45)))
            captured: dict[str, int] = {}

            async def fake_distill(gateway, experience, digest, *, timeout_seconds):
                captured["timeout"] = timeout_seconds
                return {"result": "empty", "insights": 0}

            try:
                with patch("spiritscout.runtime._distill_experience", new=fake_distill):
                    await _distill_best_effort(
                        infra, SimpleNamespace(), store, True,
                        {"target": "https://a.example", "rounds_executed": 1},
                        mission="", outcome="cancelled")
            finally:
                blackboard.close()
                store.close()
            self.assertEqual(captured["timeout"], 20)

    async def test_completed_run_distills_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = NullAuditLogger()
            store = ExperienceStore(Path(tmp) / "experience.db", audit=audit)
            blackboard = Blackboard(Path(tmp) / "bb.db")
            payload = json.dumps({"insights": [
                {"insight": "该平台对高频请求按秒级限流，批量测试需要间隔",
                 "context": "接口探测", "tags": ["限流"]},
                {"insight": "改包后要看业务字段是否真的变更，而不止响应码",
                 "context": "越权类测试", "tags": ["web", "越权"]},
            ]}, ensure_ascii=False)
            gateway = ModelGateway(
                ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                audit=audit, mock_backend=MockBackend(script=[payload]))
            assembly = SimpleNamespace(flag_manager=SimpleNamespace(stats=lambda: {
                "extracted": 1, "submitted": 0, "accepted": 0, "rejected": 0}))
            infra = SimpleNamespace(
                audit=audit, gateway=gateway, blackboard=blackboard,
                agent_models={"master": ""}, authorization=_authorization(),
                cfg=SimpleNamespace(experience=SimpleNamespace(distill_timeout_seconds=10)))
            try:
                await _distill_best_effort(
                    infra, assembly, store, True,
                    {"target": "https://a.example", "rounds_executed": 2,
                     "stop_reason": "核心目标已达成"},
                    mission="测试会员接口越权", outcome="completed")
                stored = store.count()
                items = store.recent(limit=2)
            finally:
                blackboard.close()
                store.close()
            self.assertEqual(stored, 2)
            self.assertEqual(items[0]["source_session"], "https://a.example")
            distill = [r for r in audit.records if r["type"] == "experience.distill"]
            self.assertEqual(len(distill), 1)
            self.assertEqual(distill[0]["detail"]["result"], "ok")
            self.assertEqual(distill[0]["detail"]["insights"], 2)
            self.assertEqual(distill[0]["detail"]["outcome"], "completed")

    async def test_cancelled_without_activity_skips_silently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = NullAuditLogger()
            store = ExperienceStore(Path(tmp) / "experience.db", audit=audit)
            blackboard = Blackboard(Path(tmp) / "bb.db")
            gateway = ModelGateway(
                ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                audit=audit, mock_backend=MockBackend(script=[]))
            infra = SimpleNamespace(
                audit=audit, gateway=gateway, blackboard=blackboard,
                agent_models={"master": ""}, authorization=_authorization(),
                cfg=SimpleNamespace(experience=SimpleNamespace(distill_timeout_seconds=10)))
            try:
                await _distill_best_effort(infra, SimpleNamespace(), store, True, None,
                                           mission="", outcome="cancelled")
                stored = store.count()
            finally:
                blackboard.close()
                store.close()
            self.assertEqual(stored, 0)
            distill = [r for r in audit.records if r["type"] == "experience.distill"]
            self.assertEqual(distill[0]["detail"]["result"], "skipped")

    async def test_digest_collects_trials_and_gates_cancelled_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blackboard = Blackboard(Path(tmp) / "bb.db")
            blackboard.write(BlackboardLayer.TASK_STATE, "task-1", {
                "task": {"agent_type": "web_vuln"},
                "routes": [{"name": "idor", "status": "pruned",
                            "notes": ["健康度 0.19 低于阈值"]}]})
            infra = SimpleNamespace(authorization=_authorization(), blackboard=blackboard)
            try:
                digest = _distill_digest(infra, SimpleNamespace(), None,
                                         "测试会员接口", "cancelled")
            finally:
                blackboard.close()
            self.assertEqual(digest["trials"][0]["route"], "idor")
            self.assertIn("健康度", digest["trials"][0]["note"])
            self.assertTrue(_worth_distilling(digest))
            self.assertFalse(_worth_distilling({"outcome": "cancelled"}))


if __name__ == "__main__":
    unittest.main()
