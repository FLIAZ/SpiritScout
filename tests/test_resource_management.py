"""跨会话观测、资源对话和本地 npm/MCP 安装链路回归。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import shlex
import shutil
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
from spiritscout.resources.store import ResourceStore, safe_asset
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

    def test_campaign_inheritance_backfill_filters_and_import(self) -> None:
        """分类：来源自动继承 + 补名回填 + 聚合/过滤 + 导入携带。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ResourceStore(root / "resources")
            # 先登记分类再观测：来源前缀自动继承
            store.set_session_campaign("sess-alpha", "BiliBili SRC")
            store.observe({"asset_type": "app", "value": "https://a.example.com"},
                          "sess-alpha:asset:one@1")
            # 先观测后登记：登记时回填既有观测
            store.observe({"asset_type": "app", "value": "https://b.example.com"},
                          "sess-beta:asset:one@1")
            store.set_session_campaign("sess-beta", "某 CTF")
            # 空分类 = 已尝试判断不了：不再列入补名候选
            store.set_session_campaign("sess-gamma", "")
            store.observe({"asset_type": "app", "value": "https://c.example.com"},
                          "sess-gamma:asset:one@1")
            # 无会话前缀的来源不继承（历史 sort 来源、测试夹具）
            store.observe({"asset_type": "app", "value": "https://d.example.com"},
                          "post")

            self.assertEqual(store.session_campaign("sess-alpha"), "BiliBili SRC")
            campaigns = store.campaigns()
            self.assertEqual([row["name"] for row in campaigns["items"]],
                             ["BiliBili SRC", "某 CTF"])
            self.assertEqual(campaigns["unnamed"], 2)
            self.assertEqual(store.assets(campaign="某 CTF")["total"], 1)
            self.assertEqual(store.assets(unnamed=True)["total"], 2)
            self.assertEqual(
                store.assets(campaign="某 CTF")["items"][0]["campaigns"], ["某 CTF"])
            self.assertEqual(store.sessions_without_campaign(), [])
            # 会话导入携带已登记的分类（observe 按来源前缀自动继承）
            workspace = root / "sess-import"
            workspace.mkdir()
            bb = Blackboard(workspace / "bb.db")
            bb.write(BlackboardLayer.ASSET, "asset",
                     {"asset_type": "app", "value": "https://example.test"})
            bb.close()
            store.set_session_campaign("sess-import", "BiliBili SRC")
            store.import_session(workspace)
            imported = next(item for item in store.assets(campaign="BiliBili SRC")["items"]
                            if item["data"]["value"].startswith("https://example.test"))
            self.assertEqual(imported["campaigns"], ["BiliBili SRC"])


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
            with TestClient(app) as client:
                page = client.get("/").text
                for key in ("view-mcp", "view-skill", "view-assets"):
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

    def test_skill_edit_disable_and_retrieve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = cfg_at(Path(directory))
            app = create_app(cfg, [])
            with TestClient(app) as client:
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
