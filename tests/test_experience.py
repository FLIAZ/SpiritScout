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
from spiritscout.gateway.gateway import ChatResponse
from spiritscout.knowledge.experience import (
    ExperienceStore,
    delete_insight,
    experience_db_path,
    format_insights,
    read_page,
    read_recent,
)
from spiritscout.runtime import (
    _distill_best_effort,
    _distill_digest,
    _distill_experience,
    _worth_distilling,
)
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

    def test_read_page_filters_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "experience.db"
            missing = read_page(Path(tmp) / "nope.db")
            self.assertEqual(missing["total"], 0)
            self.assertEqual(missing["tags"], [])
            self.assertIsNone(missing["last_ts"])
            store = ExperienceStore(db)
            store.add("首页注释常泄露内部入口", context="黑盒 Web", tags=["web", "侦察"],
                      source_session="sess-a")
            store.add("该平台按秒限流", context="接口探测", tags=["限流"],
                      source_session="sess-a")
            store.add("Java 反序列化看依赖版本", context="Java 站点", tags=["java", "rce"],
                      source_session="sess-b")
            store.add("越权先比对自己与目标账号", context="会员接口", tags=["web", "越权"],
                      source_session="sess-b")
            store.close()
            page = read_page(db)
            self.assertEqual(page["total"], 4)
            self.assertEqual(len(page["items"]), 4)
            self.assertEqual(page["sources"], 2)
            self.assertTrue(page["last_ts"])
            self.assertEqual(page["tags"][0], {"tag": "web", "count": 2})
            # 关键词命中 insight/context；标签精确过滤（sql 不误命中前缀）
            self.assertEqual(read_page(db, q="限流")["total"], 1)
            self.assertEqual(read_page(db, tag="web")["total"], 2)
            self.assertEqual(read_page(db, tag="we")["total"], 0)
            self.assertEqual(read_page(db, q="java", tag="web")["total"], 0)
            paged = read_page(db, limit=2)
            self.assertTrue(paged["has_more"])
            self.assertEqual(read_page(db, limit=2, offset=4)["has_more"], False)

    def test_delete_insight_removes_entry_and_audits(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.events: list[tuple[str, str, dict]] = []

            def log(self, event: str, actor: str, detail: dict) -> None:
                self.events.append((event, actor, detail))

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "experience.db"
            self.assertIsNone(delete_insight(db, "exp-none"))     # 库不存在
            store = ExperienceStore(db)
            keep = store.add("GET 状态变更是 CSRF 判读信号", tags=["csrf"])
            drop = store.add("本题入口位于 /log，应优先探测日志端点", tags=["web"])
            store.close()
            self.assertIsNone(delete_insight(db, ""))             # 空 ID 不动作
            recorder = Recorder()
            item = delete_insight(db, drop, audit=recorder)
            self.assertEqual(item["insight_id"], drop)
            self.assertIn("/log", item["insight"])
            self.assertEqual(recorder.events[0][0], "experience.deleted")
            self.assertEqual(recorder.events[0][1], "user")
            self.assertEqual(recorder.events[0][2]["insight_id"], drop)
            self.assertIsNone(delete_insight(db, drop))           # 重复删除
            self.assertIsNone(delete_insight(db, keep + "-missing"))
            page = read_page(db)
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["insight_id"], keep)


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

            async def fake_distill(gateway, experience, digest, *, timeout_seconds,
                                   audit=None, source_session=""):
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
                cfg=SimpleNamespace(
                    experience=SimpleNamespace(distill_timeout_seconds=10),
                    workspace=SimpleNamespace(
                        root=str(Path(tmp) / "sessions" / "sess-distill"))))
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
            # 来源记录真实会话 ID（经验页可回链），不是目标 URL
            self.assertEqual(items[0]["source_session"], "sess-distill")
            distill = [r for r in audit.records if r["type"] == "experience.distill"]
            self.assertEqual(len(distill), 1)
            self.assertEqual(distill[0]["detail"]["result"], "ok")
            self.assertEqual(distill[0]["detail"]["insights"], 2)
            self.assertEqual(distill[0]["detail"]["outcome"], "completed")

    async def test_distill_retries_when_thinking_eats_output(self) -> None:
        """思考型模型吃光输出额度（正文空 + finish=length）：加大余量重试一次，
        经验不再静默丢失——此前所有会话的提炼都败在这里，经验库长期为空。"""
        with tempfile.TemporaryDirectory() as tmp:
            audit = NullAuditLogger()
            store = ExperienceStore(Path(tmp) / "experience.db", audit=audit)
            payload = json.dumps({"insights": [
                {"insight": "正文为空要看是否被额度掐断，而不是当作无事发生",
                 "context": "思考型模型输出", "tags": ["模型"]}]}, ensure_ascii=False)
            calls: list[dict] = []

            class FlakyGateway:
                async def chat(self, messages, **kwargs):
                    calls.append(kwargs)
                    if len(calls) == 1:
                        return ChatResponse(
                            content="", model="fixture", prompt_tokens=10,
                            completion_tokens=1024, cost_usd=0, latency_ms=1,
                            finish_reason="length")
                    return ChatResponse(
                        content=payload, model="fixture", prompt_tokens=10,
                        completion_tokens=60, cost_usd=0, latency_ms=1,
                        finish_reason="stop")

            try:
                result = await _distill_experience(
                    FlakyGateway(), store, {"target": "https://b.example"},
                    timeout_seconds=30, audit=audit, source_session="sess-x")
                stored = store.count()
            finally:
                store.close()
            self.assertEqual(result, {"result": "ok", "insights": 1})
            self.assertEqual(stored, 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["max_tokens"], 8192)
            self.assertEqual(calls[1]["max_tokens"], 32768)
            self.assertTrue(any(r["type"] == "llm.output_retry"
                                for r in audit.records))

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


class ExperiencePageTests(unittest.TestCase):
    """经验提炼页接口：产出列表 + 来源回链 + 提炼过程记录（含失败）。"""

    def test_experience_endpoint_serves_items_and_history(self) -> None:
        from fastapi.testclient import TestClient

        from spiritscout.web.app import create_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp),
                               models_config_path=str(root / "models.yaml"))
            app = create_app(cfg, [])
            # 两条真实经验（来源会话一个在册、一个已归档失联）
            store = ExperienceStore(root / "experience.db")
            store.add("越权先比业务字段再下结论", context="会员接口", tags=["web", "越权"],
                      source_session="sess-known")
            store.add("老库经验无对应会话", context="历史", tags=["历史"],
                      source_session="sess-gone")
            store.close()
            # 在册会话：brief 提供 source_title；审计尾段有提炼事件
            session = Session(session_id="sess-known", mode="src", target="local",
                              workspace=root / "sessions" / "sess-known",
                              status="completed", title="会员接口越权测试")
            session.workspace.mkdir(parents=True)
            session.audit_path.write_text(
                json.dumps({"type": "agent.message", "detail": {}}) + "\n"
                + json.dumps({"type": "experience.distill", "ts": "2026-10-17T09:00:00",
                              "detail": {"result": "ok", "insights": 2,
                                         "outcome": "completed"}}) + "\n",
                encoding="utf-8")
            # 失联会话：提炼失败也要留痕可见
            gone = root / "sessions" / "sess-gone"
            gone.mkdir(parents=True)
            (gone / "audit.jsonl").write_text(
                json.dumps({"type": "experience.distill", "ts": "2026-10-17T10:00:00",
                            "detail": {"result": "error", "insights": 0,
                                       "outcome": "failed",
                                       "error": "ValueError: 模型输出为空"}}) + "\n",
                encoding="utf-8")
            app.state.manager._sessions["sess-known"] = session
            with TestClient(app) as client:
                resp = client.get("/api/experience")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data["total"], 2)
                self.assertEqual(data["sources"], 2)
                by_sid = {item["source_session"]: item for item in data["items"]}
                self.assertEqual(by_sid["sess-known"]["source_title"], "会员接口越权测试")
                self.assertEqual(by_sid["sess-gone"]["source_title"], "")
                # 提炼记录按时间倒序，失败原因原样可见
                self.assertEqual([h["session_id"] for h in data["history"]],
                                 ["sess-gone", "sess-known"])
                self.assertEqual(data["history"][0]["result"], "error")
                self.assertIn("模型输出为空", data["history"][0]["error"])
                # 标签过滤走查询参数
                filtered = client.get("/api/experience", params={"tag": "web"}).json()
                self.assertEqual(filtered["total"], 1)
                self.assertEqual(filtered["items"][0]["source_session"], "sess-known")

    def test_experience_delete_endpoint_removes_item(self) -> None:
        from fastapi.testclient import TestClient

        from spiritscout.web.app import create_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp),
                               models_config_path=str(root / "models.yaml"),
                               audit_log_path=str(root / "audit.jsonl"))
            app = create_app(cfg, [])
            store = ExperienceStore(root / "experience.db")
            keep = store.add("GET 状态变更是 CSRF 判读信号", tags=["csrf"])
            drop = store.add("本题入口位于 /log，应优先探测日志端点", tags=["web"])
            store.close()
            with TestClient(app) as client:
                resp = client.delete(f"/api/experience/{drop}")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"deleted": drop})
                data = client.get("/api/experience").json()
                self.assertEqual(data["total"], 1)
                self.assertEqual(data["items"][0]["insight_id"], keep)
                # 删除后条目不再参与召回源；重复删除 404
                self.assertEqual(
                    client.delete(f"/api/experience/{drop}").status_code, 404)
            # 人工删除动作留痕于系统审计（actor=user），可追溯
            audit_text = (root / "audit.jsonl").read_text(encoding="utf-8")
            self.assertIn("experience.deleted", audit_text)


if __name__ == "__main__":
    unittest.main()
