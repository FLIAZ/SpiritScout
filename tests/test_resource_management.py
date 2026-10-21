"""跨会话观测、资源对话和本地 npm/MCP 安装链路回归。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import shlex
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import McpServerConfig, SystemConfig, WorkspaceConfig
from spiritscout.core.exceptions import PolicyViolation, SpiritScoutError
from spiritscout.core.models import (
    AgentType,
    Asset,
    Authorization,
    BlackboardLayer,
    RunMode,
    Scope,
)
from spiritscout.gateway.gateway import ChatResponse
from spiritscout.resources.assistant import ResourceAssistant, public_proposal
from spiritscout.resources.store import ProjectNameConflict, ResourceStore, safe_asset
from spiritscout.runtime import _build_infra
from spiritscout.tools.mcp_client import McpStdioClient
from spiritscout.tools.registry import AgentToolView, ToolRegistry
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec
from spiritscout.web import modelcfg
from spiritscout.web.app import create_app


def cfg_at(root: Path) -> SystemConfig:
    cfg = SystemConfig(
        workspace=WorkspaceConfig(root=str(root)),
        models_config_path=str(root / "models.yaml"),
        agent_models_path=str(root / "agents.yaml"),
        audit_log_path=str(root / "audit.jsonl"),
    )
    modelcfg.write_config(
        cfg.models_config_path, [], [{"model": "local", "provider": "mock"}]
    )
    return cfg


class ResourceStoreTests(unittest.TestCase):
    def test_identity_changes_history_freshness_and_no_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResourceStore(directory)
            before = {
                "asset_type": "endpoint",
                "value": "https://user:password@EXAMPLE.test:443/a?token=hidden&page=1#secret",
                "fingerprint": {"server": "old"},
                "attrs": {
                    "method": "GET",
                    "Authorization": "private",
                    "body": "private",
                },
            }
            key = store.observe(before, "sess-one:asset@1", time.time() - 200000)
            after = {
                **before,
                "value": "https://example.test/a?page=2&token=different",
                "fingerprint": {"server": "new"},
            }
            self.assertEqual(store.observe(after, "sess-two:asset@2"), key)
            # 较晚导入的旧观测不能覆盖较新的现场。
            store.observe(before, "sess-three:asset@1", time.time() - 300000)
            row = store.assets(host="example.test")["items"][0]
            self.assertEqual(row["data"]["fingerprint"]["server"], "new")
            self.assertTrue(row["changed"])
            self.assertFalse(row["verified_current_session"])
            self.assertEqual(len(store.history(key)["items"]), 3)
            text = json.dumps(store.history(key))
            for secret in ("password", "hidden", "private", "different"):
                self.assertNotIn(secret, text)
            self.assertEqual(
                safe_asset(row["data"])[0], safe_asset(after)[0], "规范化应可重复应用"
            )
            self.assertEqual(
                safe_asset({**after, "port": 443})[0], safe_asset(after)[0]
            )
            post = {**after, "attrs": {"method": "POST"}}
            self.assertNotEqual(store.observe(post, "post"), key)

    def test_encryption_versions_and_missing_key_fail_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ResourceStore(directory)
            secret = {
                "name": "remote",
                "transport": "http",
                "url": "https://mcp.test",
                "headers": {"Authorization": "Bearer hidden-marker"},
            }
            store.save("mcp", "remote", secret, 0)
            self.assertNotIn(b"hidden-marker", store.path.read_bytes())
            with self.assertRaises(SpiritScoutError):
                store.save("mcp", "remote", secret, 0)
            self.assertEqual(
                store.effective_mcp([])[0].headers["Authorization"],
                "Bearer hidden-marker",
            )
            (Path(directory) / "catalog.key").unlink()
            with self.assertRaises(SpiritScoutError):
                store.settings("mcp")
            self.assertFalse((Path(directory) / "catalog.key").exists())

    def test_import_preserves_overwritten_observations_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "sess-old"
            workspace.mkdir()
            bb = Blackboard(workspace / "bb.db")
            for server in ("first", "second"):
                bb.write(
                    BlackboardLayer.ASSET,
                    "asset",
                    {
                        "asset_type": "app",
                        "value": "https://example.test",
                        "fingerprint": {"server": server},
                    },
                )
            bb.close()
            store = ResourceStore(root / "resources")
            store.import_session(workspace)
            store.import_session(workspace)
            row = store.assets()["items"][0]
            self.assertEqual(row["observations"], 2)
            self.assertEqual(row["data"]["fingerprint"]["server"], "second")

    def test_project_crud_membership_and_scoped_observations(self) -> None:
        """项目分区：CRUD、成员继承/回填、A/B/未分类/全部四视图互不串桶。"""
        with tempfile.TemporaryDirectory() as directory:
            store = ResourceStore(Path(directory) / "resources")
            alpha = store.create_project("  BiliBili   SRC ", "  主站与直播  ")
            self.assertEqual(alpha["name"], "BiliBili SRC")
            self.assertEqual(alpha["description"], "主站与直播")
            with self.assertRaises(ProjectNameConflict):
                store.create_project("bilibili src")
            with self.assertRaises(ValueError):
                store.create_project(" \x00 ")
            beta = store.create_project("某 CTF")
            with self.assertRaises(ProjectNameConflict):
                store.update_project(beta["project_id"], name="BILIBILI SRC")
            renamed = store.update_project(
                beta["project_id"], name="某 CTF 决赛", description="复赛环境")
            self.assertEqual(renamed["name"], "某 CTF 决赛")
            self.assertEqual(renamed["description"], "复赛环境")
            self.assertIsNone(store.update_project("prj-missing", name="空"))
            self.assertIsNone(store.project("prj-missing"))

            alpha_id, beta_id = alpha["project_id"], beta["project_id"]
            store.set_session_project("sess-alpha", alpha_id)
            self.assertEqual(store.project_for_session("sess-alpha"), alpha_id)
            self.assertIsNone(store.project_for_session("sess-never-seen"))
            self.assertEqual(store.set_session_project("sess-gamma", ""), 0)
            self.assertEqual(store.project_for_session("sess-gamma"), "")
            with self.assertRaises(ValueError):
                store.set_session_project("sess-gamma", "prj-missing")

            t0 = time.time() - 1000
            endpoint = {"asset_type": "endpoint",
                        "value": "https://shared.test/a?q=1",
                        "fingerprint": {"server": "alpha-1"}}
            # 先登记再观测：来源前缀继承项目
            store.observe(endpoint, "sess-alpha:asset:1@1", t0)
            store.observe({**endpoint, "value": "https://shared.test/a?q=2",
                           "fingerprint": {"server": "alpha-2"}},
                          "sess-alpha:asset:2@1", t0 + 10)
            # 先观测后登记：登记时回填既有观测
            store.observe({**endpoint, "fingerprint": {"server": "beta-1"}},
                          "sess-beta:asset:1@1", t0 + 20)
            finding = {"asset_type": "finding",
                       "value": "https://shared.test/a",
                       "fingerprint": {"title": "A 发现", "status": "accepted",
                                       "severity": "high", "vuln_type": "xss"}}
            store.observe(finding, "sess-alpha:vuln:1@1", t0 + 30)
            store.observe({**finding, "fingerprint": {
                "title": "B 待复核", "status": "pending", "severity": "low",
                "vuln_type": "xss"}}, "sess-beta:vuln:1@1", t0 + 40)
            self.assertEqual(store.set_session_project("sess-beta", beta_id), 2)
            # 无会话前缀的来源不继承（历史 sort 来源、测试夹具）
            store.observe({"asset_type": "app", "value": "https://orphan.test"},
                          "post", t0 + 50)

            alpha_assets = store.assets(project=alpha_id)
            beta_assets = store.assets(project=beta_id)
            unclassified = store.assets(unclassified=True)
            everything = store.assets()
            self.assertEqual(alpha_assets["total"], 2)      # 端点 + accepted 发现
            self.assertEqual(beta_assets["total"], 1)       # 发现最新为 pending
            self.assertEqual(store.assets(project=beta_id, kind="finding")["total"], 0)
            self.assertEqual(unclassified["total"], 1)      # 仅孤儿资产
            self.assertEqual(everything["total"], 2)        # 全局最新发现为 pending

            alpha_row = next(row for row in alpha_assets["items"]
                             if row["kind"] == "endpoint")
            beta_row = next(row for row in beta_assets["items"]
                            if row["kind"] == "endpoint")
            self.assertEqual(alpha_row["data"]["fingerprint"]["server"], "alpha-2")
            self.assertEqual(alpha_row["source"], "sess-alpha:asset:2@1")
            self.assertEqual(alpha_row["observations"], 2)
            self.assertTrue(alpha_row["changed"])
            self.assertEqual(beta_row["data"]["fingerprint"]["server"], "beta-1")
            self.assertEqual(beta_row["source"], "sess-beta:asset:1@1")
            self.assertEqual(beta_row["observations"], 1)
            self.assertFalse(beta_row["changed"])
            self.assertEqual(
                alpha_assets["items"][0]["data"]["fingerprint"]["title"], "A 发现")
            # 项目不是 ACL：历史仍返回全部观测，逐条标注归属
            history = store.history(alpha_row["id"])["items"]
            self.assertEqual([entry["project_id"] for entry in history],
                             [beta_id, alpha_id, alpha_id])
            self.assertEqual(len(store.history(alpha_row["id"])["items"]), 3)

            stats = {item["project_id"]: item for item in store.projects()["items"]}
            self.assertEqual(stats[alpha_id]["asset_count"], 1)
            self.assertEqual(stats[alpha_id]["finding_count"], 1)
            self.assertEqual(stats[beta_id]["asset_count"], 1)
            self.assertEqual(stats[beta_id]["finding_count"], 0)
            overview = store.projects()
            self.assertEqual(overview["unclassified"]["asset_count"], 1)
            self.assertEqual(overview["unclassified"]["finding_count"], 0)
            self.assertEqual(overview["all"]["asset_count"], 2)
            self.assertEqual(overview["all"]["finding_count"], 0)

            # 移入未分类：回填该会话观测，成员行仍显式保留
            self.assertEqual(store.set_session_project("sess-beta", ""), 2)
            self.assertEqual(store.project_for_session("sess-beta"), "")
            stats = {item["project_id"]: item for item in store.projects()["items"]}
            self.assertEqual(stats[beta_id]["asset_count"], 0)
            self.assertEqual(stats[beta_id]["finding_count"], 0)
            # 资产级未分类与项目桶互斥：共享资产仍被项目 A 观测
            self.assertEqual(store.assets(unclassified=True)["total"], 1)

    def test_delete_project_only_unbinds(self) -> None:
        """删除项目：观测/成员行转未分类，资产与发现绝不删除。"""
        with tempfile.TemporaryDirectory() as directory:
            store = ResourceStore(Path(directory) / "resources")
            alpha = store.create_project("A")
            beta = store.create_project("B")
            alpha_id, beta_id = alpha["project_id"], beta["project_id"]
            store.set_session_project("sess-a", alpha_id)
            store.set_session_project("sess-b", beta_id)
            shared = {"asset_type": "app", "value": "https://shared.test"}
            store.observe(shared, "sess-a:asset:1@1", time.time() - 30)
            store.observe({**shared, "fingerprint": {"server": "beta"}},
                          "sess-b:asset:1@1", time.time() - 20)
            store.observe({"asset_type": "app", "value": "https://alpha-only.test"},
                          "sess-a:asset:2@1", time.time() - 10)
            store.observe({"asset_type": "finding", "value": "https://beta.test",
                           "fingerprint": {"title": "B 发现", "status": "accepted"}},
                          "sess-b:vuln:1@1", time.time() - 5)

            detached = store.delete_project(alpha_id)
            self.assertEqual(detached["detached_observations"], 2)
            self.assertEqual(detached["detached_sessions"], 1)
            self.assertIsNone(store.project(alpha_id))
            self.assertEqual(store.project_for_session("sess-a"), "")
            self.assertEqual(store.assets(project=alpha_id)["total"], 0)
            beta_items = store.assets(project=beta_id)["items"]
            shared_row = next(row for row in beta_items
                              if row["data"]["value"] == "https://shared.test/")
            self.assertEqual(shared_row["data"]["fingerprint"]["server"], "beta")
            # 孤儿观测计入未分类；共享资产仍属于现存项目 B，不进未分类桶
            self.assertEqual(store.assets(unclassified=True)["total"], 1)
            self.assertEqual(store.assets()["total"], 3)
            history = store.history(shared_row["id"])["items"]
            self.assertEqual([entry["project_id"] for entry in history],
                             [beta_id, ""])
            self.assertEqual(store.projects()["all"]["finding_count"], 1)

    def test_legacy_campaign_migration_is_one_shot(self) -> None:
        """旧 campaign 一次性迁移；显式未分类与已删项目不会被重启复活。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "resources"
            root.mkdir()
            db = root / "catalog.db"
            legacy = sqlite3.connect(db)
            legacy.executescript(
                "CREATE TABLE assets(id TEXT PRIMARY KEY,kind TEXT,identity TEXT,"
                "host TEXT,first_seen REAL,last_seen REAL);"
                "CREATE TABLE observations(id INTEGER PRIMARY KEY,asset_id TEXT,"
                "source TEXT,observed_at REAL,digest TEXT,data TEXT,"
                "campaign TEXT NOT NULL DEFAULT '',UNIQUE(asset_id,source));"
                "CREATE TABLE session_campaigns(session_id TEXT PRIMARY KEY,"
                "campaign TEXT NOT NULL,created_at REAL);")
            data = json.dumps({"asset_type": "endpoint",
                               "value": "https://legacy.test/",
                               "fingerprint": {"server": "legacy"},
                               "attrs": {"method": "GET"}})
            legacy.execute(
                "INSERT INTO assets VALUES(?,?,?,?,?,?)",
                ("legacy-id", "endpoint", "endpoint|GET|https://legacy.test/||",
                 "legacy.test", 1.0, 1.0))
            legacy.execute(
                "INSERT INTO observations(asset_id,source,observed_at,digest,data,"
                "campaign) VALUES(?,?,?,?,?,?)",
                ("legacy-id", "sess-old:asset:1@1", 1.0, "digest", data,
                 "BiliBili SRC"))
            legacy.execute(
                "INSERT INTO session_campaigns VALUES(?,?,?)",
                ("sess-old", "BiliBili SRC", 1.0))
            legacy.execute(
                "INSERT INTO session_campaigns VALUES(?,?,?)",
                ("sess-none", "", 1.0))
            legacy.commit()
            legacy.close()

            expected_id = "prj-" + hashlib.sha256(b"bilibilisrc").hexdigest()[:10]
            store = ResourceStore(root)
            self.assertEqual(store.project(expected_id)["name"], "BiliBili SRC")
            self.assertEqual(store.project_for_session("sess-old"), expected_id)
            self.assertEqual(store.project_for_session("sess-none"), "")
            row = store.assets(project=expected_id)["items"][0]
            self.assertEqual(row["data"]["fingerprint"]["server"], "legacy")
            self.assertEqual(
                store.history(row["id"])["items"][0]["project_id"], expected_id)

            # 二次启动：不重复建项目
            again = ResourceStore(root)
            self.assertEqual(len(again.projects()["items"]), 1)
            again.set_session_project("sess-old", "")
            self.assertEqual(again.project_for_session("sess-old"), "")
            again.delete_project(expected_id)
            del again, store
            # 三次启动：显式未分类不复活，已删项目不按旧 campaign 重建
            reopened = ResourceStore(root)
            self.assertEqual(reopened.projects()["items"], [])
            self.assertEqual(reopened.project_for_session("sess-old"), "")
            self.assertEqual(reopened.assets(unclassified=True)["total"], 1)

    def test_project_memory_scope_bound_and_no_authorization(self) -> None:
        """共享记忆只含 accepted 发现，历史抽样限 4000 字符，说明不截断，不携带授权或凭据。"""
        with tempfile.TemporaryDirectory() as directory:
            store = ResourceStore(Path(directory) / "resources")
            project = store.create_project("BiliBili SRC", "用户提供的项目说明")
            project_id = project["project_id"]
            store.set_session_project("sess-mem", project_id)
            store.set_session_project("sess-mem-2", project_id)
            base = {"asset_type": "app", "value": "https://mem.test",
                    "attrs": {"Authorization": "Bearer memory-secret"}}
            store.observe(base, "sess-mem:asset:1@1", time.time() - 40)
            store.observe({"asset_type": "finding", "value": "https://mem.test/x",
                           "fingerprint": {"title": "已收录发现", "status": "accepted",
                                           "severity": "high", "vuln_type": "xss"}},
                          "sess-mem:vuln:1@1", time.time() - 30)
            store.observe({"asset_type": "finding", "value": "https://mem.test/y",
                           "fingerprint": {"title": "待复核发现", "status": "pending",
                                           "severity": "low", "vuln_type": "sqli"}},
                          "sess-mem:vuln:2@1", time.time() - 20)
            memory = store.project_memory(project_id)
            self.assertEqual(memory["name"], "BiliBili SRC")
            self.assertEqual(memory["session_count"], 2)
            self.assertEqual([item["title"] for item in memory["findings"]],
                             ["已收录发现"])
            self.assertIn("未在本次会话复核", memory["note"])
            text = json.dumps(memory, ensure_ascii=False)
            for forbidden in ("scope", "allowed_domains", "Authorization",
                              "memory-secret"):
                self.assertNotIn(forbidden, text)
            self.assertIsNone(store.project_memory("prj-missing"))

            # 超长资产列表从最旧条目开始丢弃，序列化硬上限 4000
            for index in range(60):
                store.observe(
                    {"asset_type": "app",
                     "value": "https://long.test/" + str(index) * 20 + "x" * 120},
                    f"sess-mem:asset:long{index}@1", time.time() - 10 + index / 100)
            bounded = store.project_memory(project_id, asset_limit=100,
                                           finding_limit=100)
            self.assertLessEqual(
                len(json.dumps(bounded, ensure_ascii=False)), 4000)
            self.assertIn("note", bounded)

            # 项目说明承载完整测试规则：超长也原样返回，历史抽样先让位
            long_description = "项目规则：只测主站，禁止爆破。" * 800
            long_project = store.create_project("规则项目", long_description)
            long_memory = store.project_memory(long_project["project_id"])
            self.assertEqual(long_memory["description"], long_description)
            self.assertGreater(
                len(json.dumps(long_memory, ensure_ascii=False)), 4000)


class ResourceApiTests(unittest.TestCase):
    def test_management_pages_secret_fields_and_config_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = cfg_at(Path(directory))
            cfg.mcp_servers = [
                McpServerConfig(
                    name="remote",
                    transport="http",
                    url="https://mcp.test",
                    headers={"Authorization": "Bearer hidden-value"},
                )
            ]
            app = create_app(cfg, [])
            with TestClient(app, base_url="http://127.0.0.1") as client:
                page = client.get("/").text
                for key in ("view-mcp", "view-skill", "view-projects"):
                    self.assertIn(key, page)
                rows = client.get("/api/resources/mcp").json()["items"]
                self.assertNotIn("hidden-value", json.dumps(rows))
                self.assertIsInstance(rows[0]["data"]["command"], str)
                public = public_proposal(
                    {"type": "skill", "data": {"body": "可审阅的参考事实"}}
                )
                self.assertEqual(public["data"]["body"], "可审阅的参考事实")
                response = client.put(
                    "/api/resources/mcp/remote",
                    json={"data": {"enabled": False}, "expected_version": 0},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    app.state.resources.effective_mcp(cfg.mcp_servers)[0].headers[
                        "Authorization"
                    ],
                    "Bearer hidden-value",
                )
                self.assertEqual(
                    client.put(
                        "/api/resources/mcp/remote",
                        json={"data": {"enabled": True}, "expected_version": 0},
                    ).status_code,
                    409,
                )
                self.assertNotEqual(
                    client.get("/api/resources/mcp/chat").json()["thread"]["id"],
                    client.get("/api/resources/skill/chat").json()["thread"]["id"],
                )
                self.assertEqual(client.get("/api/sessions").json()["sessions"], [])

    def test_project_api_crud_filters_and_delete_unbinds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = cfg_at(Path(directory))
            app = create_app(cfg, [])
            with TestClient(app, base_url="http://127.0.0.1") as client:
                created = client.post(
                    "/api/projects",
                    json={"name": "BiliBili SRC", "description": "主站"},
                )
                self.assertEqual(created.status_code, 201)
                project_id = created.json()["project_id"]
                self.assertEqual(
                    client.post("/api/projects", json={"name": "bilibili src"})
                    .status_code, 409)
                self.assertEqual(
                    client.post("/api/projects", json={"name": " \x00 "})
                    .status_code, 422)
                catalog = client.get("/api/projects").json()
                self.assertEqual(catalog["projects"][0]["name"], "BiliBili SRC")
                self.assertEqual(catalog["unclassified"]["asset_count"], 0)
                self.assertEqual(catalog["all"]["asset_count"], 0)

                store = app.state.resources
                store.set_session_project("sess-api", project_id)
                key = store.observe(
                    {"asset_type": "app", "value": "https://api-project.test"},
                    "sess-api:asset:1@1")
                self.assertEqual(
                    client.get(f"/api/assets?project={project_id}").json()["total"],
                    1)
                self.assertEqual(
                    client.get("/api/assets?unclassified=1").json()["total"], 0)
                self.assertEqual(
                    client.get(
                        f"/api/assets?project={project_id}&unclassified=1"
                    ).status_code, 422)
                self.assertEqual(
                    client.get("/api/assets?kind=domain").status_code, 422)

                renamed = client.patch(
                    f"/api/projects/{project_id}", json={"description": "直播"})
                self.assertEqual(renamed.status_code, 200)
                self.assertEqual(renamed.json()["name"], "BiliBili SRC")
                self.assertEqual(renamed.json()["description"], "直播")
                self.assertEqual(
                    client.patch(f"/api/projects/{project_id}", json={})
                    .status_code, 422)
                self.assertEqual(
                    client.patch("/api/projects/prj-missing",
                                 json={"name": "x"}).status_code, 404)

                self.assertEqual(
                    client.delete(f"/api/projects/{project_id}").status_code,
                    409)
                deleted = client.delete(
                    f"/api/projects/{project_id}?confirm=true").json()
                self.assertEqual(deleted["detached_observations"], 1)
                self.assertEqual(deleted["detached_sessions"], 1)
                self.assertEqual(deleted["failed_sessions"], [])
                self.assertEqual(
                    client.get(f"/api/assets?project={project_id}").json()["total"],
                    0)
                self.assertEqual(
                    client.get("/api/assets?unclassified=1").json()["total"], 1)
                self.assertEqual(
                    client.get(f"/api/assets/{key}/history").json()["items"][0][
                        "project_id"], "")
                self.assertEqual(client.get("/api/projects").json()["projects"], [])

    def test_skill_edit_disable_and_retrieve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = cfg_at(Path(directory))
            app = create_app(cfg, [])
            with TestClient(app, base_url="http://127.0.0.1") as client:
                response = client.put(
                    "/api/resources/skills/custom-reference",
                    json={
                        "data": {
                            "body": "适用条件：存在公开目录。参考事实：接口需独立验证。",
                            "description": "接口参考",
                            "enabled": True,
                        },
                        "expected_version": 0,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    client.get("/api/resources/skills?q=接口参考").json()["items"][0][
                        "skill_id"
                    ],
                    "custom-reference",
                )
                response = client.put(
                    "/api/resources/skills/custom-reference",
                    json={"data": {"enabled": False}, "expected_version": 1},
                )
                self.assertEqual(response.status_code, 200)
                self.assertFalse(
                    client.get("/api/resources/skills/custom-reference").json()[
                        "enabled"
                    ]
                )


class ResourceConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_mcp_role_grant_keeps_policy_gate(self) -> None:
        spec = McpServerConfig(name="custom", command="node", allowed_agents=["ctf_web"])
        self.assertEqual(spec.allowed_agents, [AgentType.CTF_WEB])
        for forbidden in ("master", "secretary", "report", "recon", "missing"):
            with self.assertRaises(ValueError):
                McpServerConfig(name="custom", allowed_agents=[forbidden])
        policy = Mock()
        policy.ensure_tool_allowed = AsyncMock(side_effect=PolicyViolation("仍需审批"))
        registry = ToolRegistry(Mock(), policy=policy)
        handler = AsyncMock()
        tool = ToolSpec("mcp__custom__run", ToolKind.MCP, risk=RiskLevel.DESTRUCTIVE,
                        target_params=(), allowed_agents=("ctf_web",))
        registry.register_builtin(tool.name, handler, tool)
        allowed = AgentToolView(registry, "ctf_web")
        self.assertIn(tool.name, allowed.names())
        self.assertTrue(allowed.allows(tool.name))
        for role in ("business_context", "ctf_pwn", "master", "secretary", "report"):
            self.assertNotIn(tool.name, AgentToolView(registry, role).names())
        result = await allowed.call(tool.name, {})
        self.assertFalse(result.ok)
        self.assertIn("仍需审批", result.error)
        policy.ensure_tool_allowed.assert_awaited_once()
        handler.assert_not_awaited()

    async def test_natural_language_proposal_requires_application_and_survives_reopen(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = cfg_at(root)
            store = ResourceStore(root / "resources")
            assistant = ResourceAssistant(cfg, store)

            async def answer(gateway, messages, **kwargs):
                return ChatResponse(
                    content=json.dumps(
                        {
                            "message": "建议新增参考技能，请确认内容。",
                            "proposal": {
                                "type": "skill",
                                "data": {
                                    "skill_id": "facts",
                                    "body": "参考事实，不是步骤。",
                                    "description": "facts",
                                    "enabled": True,
                                },
                            },
                        }
                    ),
                    model="local",
                    prompt_tokens=100,
                    completion_tokens=20,
                    cost_usd=0,
                    latency_ms=1,
                )

            with patch("spiritscout.gateway.gateway.ModelGateway.chat", answer):
                response = await assistant.message("skill", "请添加一份 facts 参考技能")
            self.assertFalse(any(row["skill_id"] == "facts" for row in store.skills()))
            proposal = response["proposals"][0]
            self.assertEqual(proposal["state"], "pending")
            assistant.start()
            try:
                assistant.apply(proposal["id"])
                await asyncio.wait_for(assistant.queue.join(), 3)
                self.assertEqual(
                    assistant.view("skill")["proposals"][0]["state"], "completed"
                )
                self.assertTrue(
                    any(row["skill_id"] == "facts" for row in store.skills())
                )
                self.assertEqual(assistant.view("mcp")["messages"], [])
            finally:
                await assistant.close()
            reopened = ResourceAssistant(cfg, ResourceStore(root / "resources"))
            self.assertEqual(len(reopened.view("skill")["messages"]), 2)
            self.assertEqual(
                reopened.view("skill")["proposals"][0]["state"], "completed"
            )

    async def test_runtime_collects_changes_and_new_session_gets_historical_reference(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = cfg_at(root)
            cfg.resources.root = str(root / "resources")
            auth = Authorization(
                auth_id="local",
                mode=RunMode.SRC,
                human_confirmed=True,
                scope=Scope(
                    allowed_urls=["https://example.test"],
                    allowed_domains=["blocked.example.test"],
                    denied=["blocked.example.test"],
                ),
            )
            from spiritscout.config.settings import ModelEntry

            for index in (1, 2):
                session = root / f"sess-{index}"
                session.mkdir()
                cfg.workspace.root = str(session)
                cfg.blackboard.sqlite_path = str(session / "bb.db")
                cfg.knowledge.sqlite_path = str(session / "kb.db")
                cfg.audit_log_path = str(session / "audit.jsonl")
                infra = await _build_infra(
                    cfg, [ModelEntry(model="local", provider="mock")], auth
                )
                try:
                    if index == 1:
                        for server in ("old", "new"):
                            infra.convergence.merge_asset(
                                Asset(
                                    asset_id="app",
                                    asset_type="app",
                                    value="https://example.test",
                                    fingerprint={"server": server},
                                ),
                                AgentType.RECON,
                            )
                        self.assertEqual(
                            infra.blackboard.read(BlackboardLayer.ASSET, "app")[
                                "fingerprint"
                            ]["server"],
                            "new",
                        )
                    else:
                        previous = infra.blackboard.read(
                            BlackboardLayer.TOPOLOGY, "historical-assets"
                        )
                        self.assertEqual(
                            previous["items"][0]["data"]["fingerprint"]["server"], "new"
                        )
                        self.assertFalse(
                            previous["items"][0]["verified_current_session"]
                        )
                        result = await infra.tools.call(
                            "asset_memory_lookup",
                            {"host": "example.test"},
                            **infra.builtin_ctx,
                        )
                        self.assertTrue(result.ok)
                        denied = await infra.tools.call(
                            "asset_memory_lookup",
                            {"host": "blocked.example.test"},
                            **infra.builtin_ctx,
                        )
                        self.assertFalse(denied.ok)
                finally:
                    await infra.close()


@unittest.skipUnless(shutil.which("npm") and shutil.which("node"), "需要本机 Node/npm")
class ActualNpmInstallTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_registry_install_disables_scripts_and_starts_actual_mcp(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "unexpected-install-script"
            server_js = """const r=require('readline').createInterface({input:process.stdin});r.on('line',line=>{const m=JSON.parse(line);if(!('id'in m))return;let result={};if(m.method==='initialize')result={protocolVersion:m.params.protocolVersion,capabilities:{tools:{}},serverInfo:{name:'fixture',version:'1.0.0'}};if(m.method==='tools/list')result={tools:[{name:'fixture_read',description:'local read',inputSchema:{type:'object',properties:{}}}]};if(m.method==='tools/call')result={content:[{type:'text',text:'fixture-installed-and-running'}]};process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:m.id,result})+'\\n');});"""
            manifest = {
                "name": "fixture-local-mcp",
                "version": "1.0.0",
                "bin": {"fixture-local-mcp": "server.js"},
                "scripts": {
                    "postinstall": "node -e "
                    + shlex.quote(
                        "require('fs').writeFileSync("
                        + json.dumps(str(marker))
                        + ",'bad')"
                    )
                },
            }
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                for name, content in [
                    ("package/package.json", json.dumps(manifest)),
                    ("package/server.js", server_js),
                ]:
                    body = content.encode()
                    info = tarfile.TarInfo(name)
                    info.size = len(body)
                    info.mode = 0o755
                    tar.addfile(info, io.BytesIO(body))
            payload = archive.getvalue()
            base = ""

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    if self.path.endswith(".tgz"):
                        body = payload
                        kind = "application/octet-stream"
                    else:
                        metadata = {
                            **manifest,
                            "dist": {
                                "tarball": base + "/fixture.tgz",
                                "integrity": "sha512-"
                                + base64.b64encode(
                                    hashlib.sha512(payload).digest()
                                ).decode(),
                            },
                        }
                        body = json.dumps(
                            metadata
                            if self.path.endswith("/1.0.0")
                            or self.path.endswith("/latest")
                            else {
                                "name": manifest["name"],
                                "dist-tags": {"latest": "1.0.0"},
                                "versions": {"1.0.0": metadata},
                            }
                        ).encode()
                        kind = "application/json"
                    self.send_response(200)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                cfg = cfg_at(root)
                cfg.resources.npm_registry = base
                assistant = ResourceAssistant(cfg, ResourceStore(root / "resources"))
                config = await assistant._install_npm(
                    {
                        "package": "fixture-local-mcp",
                        "version": "1.0.0",
                        "config": {
                            "name": "fixture",
                            "args": [],
                            "default_risk": "readonly",
                        },
                    }
                )
                self.assertFalse(marker.exists(), "安装生命周期脚本不能执行")
                client = McpStdioClient(
                    "fixture", command=config["command"], args=config["args"], timeout=5
                )
                try:
                    tools = await client.start()
                    self.assertEqual(tools[0]["name"], "fixture_read")
                    result = await client.call("fixture_read", {})
                    self.assertIn("fixture-installed-and-running", result["text"])
                finally:
                    await client.close()
                # 实际安装的服务经运行时装配后，对分配角色可见且可调用。
                cfg.mcp_servers = [McpServerConfig.model_validate({
                    **config, "allowed_agents": ["ctf_web"], "scope_enforcement": "none"})]
                cfg.blackboard.sqlite_path = str(root / "bb.db")
                cfg.knowledge.sqlite_path = str(root / "kb.db")
                from spiritscout.config.settings import ModelEntry

                infra = await _build_infra(cfg, [ModelEntry(model="local", provider="mock")],
                                           Authorization(auth_id="fixture", mode=RunMode.CTF,
                                                         human_confirmed=True, scope=Scope(allowed_domains=["fixture.test"])))
                try:
                    name = "mcp__fixture__fixture_read"
                    granted = AgentToolView(infra.tools, "ctf_web")
                    self.assertIn(name, granted.names())
                    self.assertNotIn(name, AgentToolView(infra.tools, "ctf_pwn").names())
                    result = await granted.call(name, {}, **infra.builtin_ctx)
                    self.assertTrue(result.ok, result.error)
                    self.assertIn("fixture-installed-and-running", result.stdout)
                finally:
                    await infra.close()
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join()
