"""请求验证、持久化恢复、密钥主键与实时计量回归。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.web import vault
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session, SessionManager


class WebRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = SystemConfig(
            workspace=WorkspaceConfig(root=str(self.root)),
            models_config_path=str(self.root / "models.yaml"),
            audit_log_path=str(self.root / "audit.jsonl"),
        )
        self.app = create_app(self.cfg, [])
        self.client = self.enterContext(TestClient(self.app, raise_server_exceptions=False))

    def test_bad_request_shapes_return_422_without_creating_sessions(self) -> None:
        for payload in [
            {"scope": 9}, {"scope": "example.test"}, {"scope": [3]},
            {"upload_ids": 123}, {"upload_ids": [{}]}, {"mission": 42},
            {"target": {"value": "https://audit.test"}}, {"rules_yaml": []},
            {"no_llm": "false"}, {"budget_tokens": {}}, {"unknown_option": True},
            {"rps": "nan"}, {"rps": 0},
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/sessions", json=payload).status_code, 422)
        self.assertEqual(self.app.state.manager.list(), [])
        self.assertEqual(list((self.root / "sessions").iterdir()), [])

    def test_canonical_provider_names_are_used_for_all_key_operations(self) -> None:
        payload = {"name": " old ", "provider": "openai_compatible",
                   "base_url": "https://audit.test", "api_key_value": "old-marker"}
        self.assertEqual(self.client.post("/api/providers", json=payload).status_code, 201)
        self.assertEqual(vault.get_key("old"), "old-marker")
        self.assertFalse(vault.has_key(" old "))
        payload.update(name=" new ", api_key_value="new-marker")
        response = self.client.put("/api/providers/old", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(vault.get_key("new"), "new-marker")
        self.assertFalse(vault.has_key("old"))
        self.assertFalse(vault.has_key(" new "))
        payload.update(api_key_value="")
        self.assertEqual(self.client.put("/api/providers/new", json=payload).status_code, 200)
        self.assertFalse(vault.has_key("new"))

    def test_invalid_provider_change_does_not_change_existing_key(self) -> None:
        payload = {"name": "old", "provider": "openai_compatible",
                   "base_url": "https://audit.test", "api_key_value": "marker"}
        self.client.post("/api/providers", json=payload)
        response = self.client.put("/api/providers/old", json={
            **payload, "name": " ", "api_key_value": "replacement",
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(vault.get_key("old"), "marker")

    def test_dashboard_list_and_state_use_same_live_usage(self) -> None:
        workspace = self.root / "sessions" / "sess-usage"
        workspace.mkdir()
        sess = Session(session_id="sess-usage", mode="src", target="audit",
                       workspace=workspace, status="running", summary={"run": {
                           "model_usage": {"used_tokens": 10, "used_usd": 0.01}}})
        self.app.state.manager._sessions[sess.session_id] = sess
        usage = {"used_tokens": 123, "used_usd": 0.2,
                 "per_model_tokens": {"audit": 123},
                 "call_series": [{"ts": 1792022400, "tokens": 123}]}
        (workspace / "usage.json").write_text(json.dumps(usage), encoding="utf-8")
        self.assertEqual(self.client.get("/api/sessions").json()["sessions"][0]["tokens"], 123)
        dash = self.client.get("/api/dashboard").json()
        self.assertEqual(dash["total_tokens"], 123)
        self.assertEqual(sum(dash["daily"].values()), 123)
        state = self.client.get("/api/sessions/sess-usage/state").json()
        self.assertEqual(state["model_usage"]["used_tokens"], 123)
        for bad_content in ["[1]", "{broken", '{"used_tokens": null}',
                            '{"used_tokens": 2, "call_series": 9}',
                            '{"used_tokens": 2, "used_usd": "nan"}']:
            (workspace / "usage.json").write_text(bad_content, encoding="utf-8")
            self.assertEqual(self.client.get("/api/dashboard").json()["total_tokens"], 10)


class NamingChainApiTests(unittest.TestCase):
    """命名候选链 API：模型页排的降级顺序，落盘热读、越权 ID 拒绝。"""

    def test_chain_roundtrip_validation_and_hot_persistence(self) -> None:
        from spiritscout.web import agentcfg, modelcfg

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=str(root)),
                models_config_path=str(root / "models.yaml"),
                agent_models_path=str(root / "agents.yaml"),
                audit_log_path=str(root / "audit.jsonl"),
            )
            modelcfg.write_config(cfg.models_config_path, [],
                                  [{"model": "mini", "provider": "mock"},
                                   {"model": "backup", "provider": "mock"}])
            client = TestClient(create_app(cfg, []))
            self.assertEqual(client.get("/api/models/naming").json()["chain"], [])
            # 顺序即用户排的降级顺序；重复项去重保序
            r = client.put("/api/models/naming",
                           json={"models": ["mini", "backup", "mini"]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["chain"], ["mini", "backup"])
            # 落进分配表文件，命名调用每次热读（改完即生效）
            self.assertEqual(agentcfg.read_naming_chain(cfg.agent_models_path),
                             ["mini", "backup"])
            # 不在注册表的模型 / 非法形状 → 422，原链保持不变
            self.assertEqual(client.put("/api/models/naming",
                                        json={"models": ["ghost"]}).status_code, 422)
            self.assertEqual(client.put("/api/models/naming",
                                        json={"models": "mini"}).status_code, 422)
            self.assertEqual(agentcfg.read_naming_chain(cfg.agent_models_path),
                             ["mini", "backup"])
            # 清空链 = 回到默认（跟随编队页灵枢指派）
            self.assertEqual(client.put("/api/models/naming",
                                        json={"models": []}).json()["chain"], [])


class MetadataRepairTests(unittest.TestCase):
    def test_bad_field_types_do_not_prevent_other_sessions_loading(self) -> None:
        for field, value in [("opinions", 3), ("dialogue_history", 7),
                             ("excluded_seconds", "oops"), ("status", []),
                             ("created_at", "nan"), ("excluded_seconds", "inf"),
                             ("summary", {"run": 4})]:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for sid in ["sess-good", "sess-bad"]:
                    workspace = root / "sessions" / sid
                    workspace.mkdir(parents=True)
                    data = {"session_id": sid, "mode": "ctf", "target": "audit",
                            "status": "completed"}
                    if sid == "sess-bad":
                        data[field] = value
                    (workspace / "meta.json").write_text(json.dumps(data), encoding="utf-8")
                manager = SessionManager(SystemConfig(
                    workspace=WorkspaceConfig(root=tmp)), [])
                self.assertIsNotNone(manager.get("sess-good"))
                self.assertIsNotNone(manager.get("sess-bad"))
                self.assertEqual(manager.get("sess-bad").pause_gate.excluded_seconds, 0)
