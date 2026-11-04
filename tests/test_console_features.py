"""控制台新增功能的离线回归，不调用真实模型。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry, SystemConfig, WorkspaceConfig
from spiritscout.core.models import BlackboardLayer
from spiritscout.gateway.gateway import AgentBoundGateway, ModelGateway
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.gateway.session_model import bind_session_model, current_model
from spiritscout.gateway.usage import UsageLedger
from spiritscout.knowledge import merge
from spiritscout.knowledge.experience import ExperienceStore, read_page
from spiritscout.runtime import _distill_experience
from spiritscout.web.app import create_app
from spiritscout.web.notify import NotificationReader
from spiritscout.web.session_experience import _TASKS, request_distill, session_page
from spiritscout.web.sessions import Session, SessionManager
from spiritscout.web.usage_stats import usage_stats


def models():
    return [ModelEntry(model="same", provider="mock", provider_ref=provider,
                       api_key="fixture-key", context_window=32768)
            for provider in ("alpha", "beta")]


def fixture(root):
    cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
    cfg.models_config_path = str(root / "models.yaml")
    cfg.agent_models_path = str(root / "agent_models.yaml")
    cfg.audit_log_path = str(root / "audit.jsonl")
    cfg.blackboard.sqlite_path = str(root / "base.db")
    cfg.knowledge.sqlite_path = str(root / "kb.db")
    cfg.resources.root = str(root / "resources")
    cfg.mcp_servers = []
    cfg.web.auto_name_sessions = False
    entries = models()
    Path(cfg.models_config_path).write_text(json.dumps({
        "providers": [{"name": name, "provider": "mock", "api_key": "fixture-key"} for name in ("alpha", "beta")],
        "models": [entry.model_dump() for entry in entries]}))
    return cfg, entries


def session(root, sid="sess-fixture"):
    workspace = root / "sessions" / sid
    workspace.mkdir(parents=True, exist_ok=True)
    return Session(session_id=sid, mode="ctf", target="fixture", workspace=workspace,
                   status="completed", request={"mission": "fixture observation"})


class ModelSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_model_preserves_provider_and_role_precedence(self):
        entries = models()
        gateway = ModelGateway(ModelRegistry(entries))
        selected = entries[1].key
        gateway.set_session_model(lambda: selected)
        master = AgentBoundGateway(gateway, entries[0].key, prefer_session=True)
        child = AgentBoundGateway(gateway, entries[0].key)
        self.assertEqual(master.resolve_model().key, entries[1].key)
        self.assertEqual(child.resolve_model().key, entries[0].key)
        self.assertEqual(master.resolve_model(model=entries[0].key).key, entries[0].key)
        self.assertEqual(master.context_identity()["provider_ref"], "beta")
        self.assertIsNotNone(master.context_budget())
        selected = ""
        self.assertEqual(master.resolve_model().key, entries[0].key)
        selected = "missing"
        self.assertEqual(master.resolve_model().key, entries[0].key)

    async def test_newly_selected_registry_entry_reaches_existing_gateway(self):
        initial = models()[:1]
        selected = initial[0].key
        available = initial
        with bind_session_model(lambda: selected, lambda: available):
            gateway = ModelGateway(ModelRegistry(initial))
            self.assertEqual(gateway.resolve_model().key, initial[0].key)
            available = models()
            selected = available[1].key
            self.assertEqual(gateway.resolve_model().key, selected)
            self.assertEqual(gateway.context_identity()["provider_ref"], "beta")

    async def test_context_isolated_and_running_selection_is_read_each_call(self):
        entries = models()
        barrier = asyncio.Event()
        selected = {"one": entries[0].key, "two": entries[1].key}
        async def call(name):
            with bind_session_model(lambda: selected[name]):
                gateway = ModelGateway(ModelRegistry(entries))
                await barrier.wait()
                return gateway.resolve_model().key
        one, two = asyncio.create_task(call("one")), asyncio.create_task(call("two"))
        await asyncio.sleep(0)
        selected["one"] = entries[1].key
        barrier.set()
        self.assertEqual(await asyncio.gather(one, two), [entries[1].key, entries[1].key])
        self.assertEqual(current_model(), "")

    async def test_actual_mock_call_uses_selected_model(self):
        entries = models()
        gateway = ModelGateway(ModelRegistry(entries))
        gateway.set_session_model(entries[1].key)
        with patch.object(gateway._mock, "complete", AsyncMock(return_value={
                "content": "fixture", "prompt_tokens": 1, "completion_tokens": 1})) as complete:
            await gateway.chat([{"role": "user", "content": "fixture"}])
            self.assertEqual(complete.call_args.args[0].key, entries[1].key)


class SessionModelApiTests(unittest.TestCase):
    def test_model_persists_and_invalid_change_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, entries = fixture(root)
            app = create_app(cfg, entries)
            manager = app.state.manager
            item = session(root)
            manager._sessions[item.session_id] = item
            with TestClient(app, base_url="http://127.0.0.1") as client:
                result = client.post(f"/api/sessions/{item.session_id}/model", json={"model": entries[1].key})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["model"], entries[1].key)
                self.assertEqual(client.post(f"/api/sessions/{item.session_id}/model", json={"model": "same"}).status_code, 422)
                self.assertEqual(item.model, entries[1].key)
                self.assertEqual(client.post(f"/api/sessions/{item.session_id}/model", json={"model": "unknown"}).status_code, 422)
                self.assertEqual(client.post("/api/sessions/missing/model", json={"model": ""}).status_code, 404)
                self.assertEqual(client.post(f"/api/sessions/{item.session_id}/model", json={"other": True}).status_code, 422)
            restored = SessionManager(cfg, entries).get(item.session_id)
            self.assertEqual(restored.model, entries[1].key)
            self.assertEqual(json.loads(item.meta_path.read_text())["model"], entries[1].key)

    def test_routes_exist_and_keep_existing_auth_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, entries = fixture(Path(tmp))
            app = create_app(cfg, entries)
            with TestClient(app, base_url="http://127.0.0.1") as client:
                for url in ("/api/notifications", "/api/usage/stats", "/api/experience/merge/candidates"):
                    result = client.get(url)
                    self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(client.get("/api/usage/stats?period=bad").status_code, 422)
                self.assertEqual(client.post("/api/sessions/missing/experience/distill").status_code, 404)
            app.state.auth.upsert("fixture-user", "fixture-password")
            protected = create_app(cfg, entries)
            with TestClient(protected, base_url="http://127.0.0.1") as client:
                self.assertEqual(client.get("/api/notifications").status_code, 401)
                self.assertEqual(client.get("/api/usage/stats").status_code, 401)


class NotificationTests(unittest.TestCase):
    def test_only_accepted_and_valid_persisted_completion_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = session(Path(tmp))
            flags = [{"flag_id": "a", "challenge_id": "one", "accepted": True, "value": "private-answer"},
                     {"flag_id": "b", "challenge_id": "two", "accepted": None}]
            item.flags_path.write_text(json.dumps(flags))
            complete = {"two": {"completed": True, "answer_ids": ["b"], "reason": "fixture evidence"}}
            item.flags_path.with_suffix(".completion.json").write_text(json.dumps(complete))
            reader = NotificationReader()
            first = reader.read([item])
            self.assertEqual(first["total"], 2)
            self.assertNotIn("private-answer", json.dumps(first))
            with patch("spiritscout.web.notify._json", side_effect=AssertionError("cache miss")):
                self.assertEqual(reader.read([item]), first)
            complete["two"]["completed"] = False
            item.flags_path.with_suffix(".completion.json").write_text(json.dumps(complete))
            self.assertEqual(reader.read([item])["total"], 1)
            item.flags_path.write_text("broken")
            self.assertEqual(reader.read([item])["total"], 0)

    def test_stable_question_ids_and_bounded_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = session(Path(tmp))
            item.questions = SimpleNamespace(view=lambda: [{"question_id": str(i), "question": "fixture", "requested_at": time.time()} for i in range(70)])
            reader = NotificationReader()
            result = reader.read([item, item])
            self.assertEqual(result["total"], 70)
            self.assertEqual(len(result["items"]), 60)
            self.assertTrue(result["truncated"])
            self.assertEqual({i["id"] for i in result["items"]}, {i["id"] for i in reader.read([item])["items"]})


class UsagePageTests(unittest.TestCase):
    def test_sqlite_wal_and_provider_identity_are_not_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, entries = fixture(root)
            item = session(root)
            manager = SimpleNamespace(list=lambda: [item, item])
            now = time.time()
            base = UsageLedger(root / "usage.db")
            own = UsageLedger(item.workspace / "usage.db")
            for ledger, entry, cid in ((base, entries[0], "one"), (own, entries[1], "two")):
                ledger.append({"call_id": cid, "model": entry.key, "ts": now, "tokens": 120,
                               "input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 60,
                               "cost_usd": .01, "sdk_attempts": 2, "usage_status": "reported"})
            before = (root / "usage.db").stat().st_mtime_ns
            result = usage_stats(cfg, manager, "today", now=now + 1)
            self.assertEqual(result["summary"]["tokens"], 240)
            self.assertEqual(result["summary"]["calls"], 2)
            self.assertEqual({row["provider"] for row in result["models"]}, {"alpha", "beta"})
            self.assertEqual(result["summary"]["cache"]["cached_input_tokens"], 120)
            self.assertAlmostEqual(result["summary"]["metered_usd"], .02)
            self.assertEqual((root / "usage.db").stat().st_mtime_ns, before)

    def test_unknown_cache_usage_is_not_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, _ = fixture(root)
            now = time.time()
            (root / "usage.json").write_text(json.dumps({"call_series": [
                {"model": "fixture", "tokens": 100, "input_tokens": 100, "ts": now,
                 "cached_input_tokens": None, "context_parts": "invalid"}]}))
            result = usage_stats(cfg, SimpleNamespace(list=lambda: []), "today", now=now + 1)
            self.assertEqual(result["summary"]["cache"]["unmeasured_calls"], 1)
            self.assertEqual(result["summary"]["cache"]["measured_calls"], 0)
            self.assertIsNone(result["summary"]["cache"]["hit_rate"])

    def test_unknown_historical_and_bad_ledgers_remain_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, _ = fixture(root)
            manager = SimpleNamespace(list=lambda: [])
            (root / "usage.json").write_text(json.dumps({"used_tokens": 50, "calls": 2, "call_series": []}))
            today = usage_stats(cfg, manager, "today")
            self.assertEqual(today["summary"]["tokens"], 0)
            self.assertEqual(today["undated_calls"], 2)
            all_time = usage_stats(cfg, manager, "all")
            self.assertEqual(all_time["summary"]["tokens"], 50)
            self.assertEqual(all_time["summary"]["cost"]["unpriced_calls"], 2)
            (root / "usage.json").write_text("bad")
            self.assertEqual(usage_stats(cfg, manager)["source_errors"], 1)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "experience.db"
        self.store = ExperienceStore(self.path)
        self.addCleanup(self.store.close)

    def originals(self):
        return [self.store.add(text, context="仅限测试夹具", tags=["登录"], source_session=sid,
                               sources=[{"ref": "topology:fixture@1", "source_session": sid,
                                         "evidence_state": "observed"}])
                for text, sid in (("登录差异需要核验", "sess-one"), ("登录差异仍需核验", "sess-two"))]

    def test_preview_merge_restore_preserves_all_originals_and_sources(self):
        ids = self.originals()
        draft = merge.preview(self.path, ids)
        self.assertEqual(self.store.count(), 2)
        with self.assertRaises(ValueError):
            merge.merge(self.path, draft["token"])
        result = merge.merge(self.path, draft["token"], confirmed=True)
        self.assertEqual(self.store.count(), 3)
        visible = read_page(self.path)["items"]
        self.assertEqual(len(visible), 1)
        self.assertEqual({s["source_session"] for s in visible[0]["sources"]}, {"sess-one", "sess-two"})
        self.assertEqual(visible[0]["quality_status"], "referenced")
        merge.restore(self.path, result["merged_id"])
        self.assertEqual({row["insight_id"] for row in read_page(self.path)["items"]}, set(ids))
        self.assertEqual(self.store.count(), 3)

    def test_changed_preview_and_overlong_draft_do_not_modify_originals(self):
        ids = self.originals()
        draft = merge.preview(self.path, ids)
        with self.store._conn:
            self.store._conn.execute("UPDATE insight_quality SET status='expired' WHERE insight_id=?", (ids[0],))
        with self.assertRaises(ValueError):
            merge.merge(self.path, draft["token"], confirmed=True)
        self.assertEqual(self.store.count(), 2)
        more = [self.store.add("a" * 400), self.store.add("b" * 400)]
        with self.assertRaises(ValueError):
            merge.preview(self.path, more)
        self.assertEqual(self.store.count(), 4)

    def test_missing_or_different_context_is_not_treated_as_duplicate(self):
        self.store.add("登录响应差异需核验", context="会员登录")
        self.store.add("登录响应差异需核验", context="管理后台登录")
        self.store.add("登录响应差异需核验")
        self.assertEqual(self.store.count(), 3)
        self.assertEqual(self.store.add("登录响应差异需核验", context="会员登录"), "")

    def test_similar_text_is_not_silently_discarded(self):
        self.store.add("只读接口允许访问", context="有授权")
        self.store.add("只读接口不允许访问", context="无授权")
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(merge.candidates(self.path, threshold=.5)["comparison"], "normalized_characters")


class DistillFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_empty_and_unparsed_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(Path(tmp) / "experience.db")
            self.addCleanup(store.close)
            store.add("fixture observation")
            for output, expected in (({"insights": [{"insight": "fixture observation"}]}, "duplicate"),
                                     ({"insights": []}, "empty"), ({"other": True}, "unparsed")):
                response = SimpleNamespace(content=json.dumps(output))
                with patch("spiritscout.gateway.gateway.chat_decision", AsyncMock(return_value=response)):
                    result = await _distill_experience(object(), store, {"outcome": "completed"}, timeout_seconds=1)
                self.assertEqual(result["result"], expected)

    async def test_manual_queue_reuses_evidence_and_shared_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, entries = fixture(root)
            manager = SessionManager(cfg, entries)
            item = session(root)
            manager._sessions[item.session_id] = item
            bb = Blackboard(item.bb_path)
            bb.write(BlackboardLayer.TOPOLOGY, "fixture", {"kind": "finding", "type": "fact", "text": "fixture observation", "evidence_state": "observed"})
            bb.close()
            response = SimpleNamespace(content=json.dumps({"insights": [{"insight": "fixture observation", "source_refs": ["topology:fixture@1"]}]}))
            with patch("spiritscout.gateway.gateway.chat_decision", AsyncMock(return_value=response)) as call:
                queued = await request_distill(cfg, manager, item)
                self.assertEqual(queued["result"], "queued")
                await _TASKS[str(item.workspace)]
                call.assert_awaited_once()
            page = session_page(cfg, item)
            self.assertEqual(page["total"], 1)
            self.assertFalse((item.workspace / "experience.db").exists())
            self.assertEqual(page["items"][0]["quality_status"], "referenced")
            item.status = "running"
            with self.assertRaises(Exception) as raised:
                await request_distill(cfg, manager, item)
            self.assertEqual(raised.exception.status_code, 409)
