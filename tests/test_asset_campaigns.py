"""AI 收集的可观察行为、批次归属、命令流与回收契约。"""

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from spiritscout.assets.agent import _configured_intelligence
from spiritscout.assets.campaigns import AssetCampaignManager
from spiritscout.assets.providers.base import ProviderCandidate, ProviderResult
from spiritscout.assets.providers.cyberspace import HunterProvider
from spiritscout.config.settings import ModelEntry, SystemConfig, WorkspaceConfig
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.gateway import ChatResponse
from spiritscout.gateway.session_model import bind_session_model
from spiritscout.resources.store import ResourceStore, project_context
from spiritscout.tools.sandbox import sandbox_available
from spiritscout.tools.spec import ToolResult
from spiritscout.web.asset_campaigns import mount_asset_campaigns


def config(root):
    cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)), mcp_servers=[])
    cfg.resources.root = str(root / "resources")
    cfg.knowledge.sqlite_path = str(root / "kb.db")
    cfg.blackboard.sqlite_path = str(root / "bb.db")
    cfg.audit_log_path = str(root / "audit.jsonl")
    cfg.search.enabled = False
    return cfg


def response(decision):
    return ChatResponse(json.dumps(decision, ensure_ascii=False), "fixture", 1, 1, 0, 0)


class AssetCampaignTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_selects_engine_then_registers_only_supported_assets(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(Path(root))
            store = ResourceStore(cfg.resources.root)
            project = store.create_project("资产收集", description="仅收集资产，不做漏洞测试")
            campaign = store.create_asset_campaign(project["project_id"], ["示例组织"],
                {"excluded_assets": ["skip.example.com"], "instruction": "考虑特殊业务入口"})
            decisions = [
                {"message": "根据已知资料选择域名引擎", "tool": {"name": "asset_collect",
                    "arguments": {"domain": "example.com", "brute": False, "action": "analysis"}}},
                {"assets": [{"value": "api.example.com", "evidence_ref": "execution-1", "source": "证书记录",
                             "ip": "203.0.113.7", "title": "业务 API", "verification_status": "passive"}],
                 "finish": "completed", "reason": "已取得候选，并按用户规则排除 skip.example.com"},
            ]
            result = ProviderResult("internal", "ok", candidates=[ProviderCandidate("api.example.com"),
                                                                   ProviderCandidate("skip.example.com")])
            with patch("spiritscout.assets.agent.InternalAssetProvider.collect", new=AsyncMock(return_value=result)) as engine, \
                 patch("spiritscout.gateway.gateway.ModelGateway.chat", new=AsyncMock(side_effect=[response(d) for d in decisions])) as chat:
                manager = AssetCampaignManager(store, cfg, [ModelEntry(model="fixture", provider="mock")])
                manager.start(campaign["campaign_id"], model="fixture")
                await asyncio.wait_for(manager._running[campaign["campaign_id"]].task, 10)
            self.assertEqual(engine.call_args.args[0], "example.com", "组织不由后端硬转域名，必须是模型选择")
            batch = ResourceStore(cfg.resources.root).asset_campaign_targets(campaign["campaign_id"])
            self.assertEqual([a["value"] for a in batch["targets"]], ["api.example.com"])
            self.assertEqual(batch["targets"][0]["ip"], "203.0.113.7")
            self.assertEqual(batch["targets"][0]["title"], "业务 API")
            self.assertEqual(batch["targets"][0]["source"], "证书记录")
            saved = store.asset_campaign(campaign["campaign_id"])
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["stats"]["candidates"], 1)
            self.assertEqual(saved["stats"]["commands"], 1)
            # 同阶段模型输入保持前缀，工具结果只在增量现场里发送，不重建整段历史。
            first, second = [call.args[0] for call in chat.call_args_list]
            self.assertEqual(second[:len(first)], first)
            self.assertIn("特殊业务入口", first[-1]["content"])
            self.assertEqual(json.loads(first[-1]["content"])["snapshot"]["configured_intelligence"], [])
            self.assertIn("skip.example.com", first[-1]["content"])
            self.assertIn("execution-1", second[-1]["content"])
            self.assertNotIn(campaign["campaign_id"], manager._running)
            baseline = {"project_id": project["project_id"], "name": project["name"], "description": project["description"]}
            self.assertEqual(project_context(store.project_memory(project["project_id"])), project_context(baseline))

    @unittest.skipUnless(sandbox_available(), "需要现有 Linux/bwrap")
    async def test_real_ai_command_output_arrives_before_process_finishes_and_stop_reaps(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(Path(root))
            store = ResourceStore(cfg.resources.root)
            project = store.create_project("实时命令")
            campaign = store.create_asset_campaign(project["project_id"], ["仅本地验证"], {})
            code = "printf 'command-progress\\n'; while [ ! -f finish ]; do sleep .05; done; printf 'final-output\\n'"
            decisions = [response({"message": "运行本地输出测试", "tool": {"name": "sandbox_exec",
                "arguments": {"code": code, "action": "analysis", "timeout": 30}}})]
            with patch("spiritscout.gateway.gateway.ModelGateway.chat", new=AsyncMock(side_effect=decisions)):
                manager = AssetCampaignManager(store, cfg, [ModelEntry(model="fixture", provider="mock")])
                manager.start(campaign["campaign_id"], model="fixture")
                task = manager._running[campaign["campaign_id"]].task
                try:
                    async with asyncio.timeout(10):
                        while not any(r["level"] == "stdout" and "command-progress" in r["message"]
                                      for r in store.asset_campaign_events(campaign["campaign_id"])):
                            if task.done():
                                self.fail(store.asset_campaign(campaign["campaign_id"])["error"])
                            await asyncio.sleep(.02)
                    self.assertFalse(task.done())
                    self.assertEqual(store.asset_campaign(campaign["campaign_id"])["status"], "running")
                finally:
                    await manager.stop(campaign["campaign_id"])
                self.assertTrue(task.done())
                self.assertEqual(store.asset_campaign(campaign["campaign_id"])["status"], "stopped")
                self.assertNotIn(campaign["campaign_id"], manager._running)

    async def test_pause_resume_and_stop_wait_for_runner_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(Path(root))
            project = store.create_project("暂停恢复")
            campaign = store.create_asset_campaign(project["project_id"], ["example.test"], {})
            entered, cleaned = asyncio.Event(), asyncio.Event()
            async def runner(manager, campaign, running):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    cleaned.set()
            with patch("spiritscout.assets.campaigns.run_collection", new=runner):
                manager = AssetCampaignManager(store)
                manager.start(campaign["campaign_id"])
                await entered.wait()
                handle = manager._running[campaign["campaign_id"]]
                manager.pause(campaign["campaign_id"])
                self.assertTrue(handle.pause_gate.paused)
                self.assertEqual(store.asset_campaign(campaign["campaign_id"])["status"], "paused")
                manager.resume(campaign["campaign_id"])
                self.assertFalse(handle.pause_gate.paused)
                await manager.stop(campaign["campaign_id"])
                self.assertTrue(cleaned.is_set())
                self.assertNotIn(campaign["campaign_id"], manager._running)

    async def test_missing_model_is_visible_without_fixed_engine_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(Path(root))
            project = store.create_project("模型缺失")
            campaign = store.create_asset_campaign(project["project_id"], ["example.test"], {})
            with patch("spiritscout.assets.agent.InternalAssetProvider.collect", new=AsyncMock()) as engine:
                manager = AssetCampaignManager(store)
                manager.start(campaign["campaign_id"])
                await manager._running[campaign["campaign_id"]].task
            engine.assert_not_called()
            self.assertEqual(store.asset_campaign(campaign["campaign_id"])["status"], "failed")
            self.assertIn("AI 模型", store.asset_campaign(campaign["campaign_id"])["error"])

    async def test_old_batch_results_and_delete_preserve_catalog(self):
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(Path(root))
            project = store.create_project("历史兼容")
            campaign = store.create_asset_campaign(project["project_id"], ["example.test"], {})
            store.observe({"asset_type": "domain", "value": "api.example.test"},
                          f"asset-campaign:{campaign['campaign_id']}:legacy", project_id=project["project_id"])
            store.observe({"asset_type": "domain", "value": "other.example.test"},
                          "asset-campaign:other:legacy", project_id=project["project_id"])
            batch = ResourceStore(Path(root)).asset_campaign_targets(campaign["campaign_id"])
            self.assertEqual([a["value"] for a in batch["targets"]], ["api.example.test"])
            store.append_asset_campaign_event(campaign["campaign_id"], "output")
            await AssetCampaignManager(store).delete(campaign["campaign_id"])
            self.assertEqual(store.asset_campaign_events(campaign["campaign_id"]), [])
            self.assertEqual(store.assets(project=project["project_id"], kind="asset")["total"], 2)

    async def test_event_http_stream_replays_and_drains_terminal_output(self):
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(Path(root))
            project = store.create_project("输出接口")
            campaign = store.create_asset_campaign(project["project_id"], ["example.test"], {})
            app = FastAPI()
            mount_asset_campaigns(app, None, store)
            for index in range(205):
                store.append_asset_campaign_event(campaign["campaign_id"], f"output-{index}")
            store.update_asset_campaign(campaign["campaign_id"], status="completed")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                url = f"/api/asset-campaigns/{campaign['campaign_id']}/events"
                result = await client.get(url)
                self.assertEqual(result.text.count("event: output"), 205)
                self.assertTrue(result.text.endswith("event: done\ndata: {}\n\n"))
                resumed = await client.get(url + "?after=2", headers={"Last-Event-ID": "204"})
                self.assertEqual(resumed.text.count("event: output"), 1)
                self.assertEqual((await client.get(url + "?after=-1")).status_code, 422)
                self.assertEqual((await client.get("/api/asset-campaigns/missing/events")).status_code, 404)

    async def test_dangerous_action_requires_live_approval_and_rejection_does_not_execute(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(Path(root))
            store = ResourceStore(cfg.resources.root)
            project = store.create_project("即时审批")
            campaign = store.create_asset_campaign(project["project_id"], ["本地审批验证"], {})
            app = FastAPI()
            manager = mount_asset_campaigns(app, cfg, store, [ModelEntry(model="fixture", provider="mock")])
            decisions = [response({"tool": {"name": "asset_collect", "purpose": "验证审批，不执行真实枚举",
                "arguments": {"domain": "fixture.test", "action": "brute_force", "brute": True}}}),
                response({"finish": "blocked", "reason": "用户拒绝本次操作，不执行枚举"})]
            with patch("spiritscout.gateway.gateway.ModelGateway.chat", new=AsyncMock(side_effect=decisions)), \
                 patch("spiritscout.assets.agent.InternalAssetProvider.collect", new=AsyncMock()) as engine:
                manager.start(campaign["campaign_id"], model="fixture")
                task = manager._running[campaign["campaign_id"]].task
                try:
                    async with asyncio.timeout(10):
                        while not manager.view(campaign["campaign_id"])["approvals"]:
                            if task.done():
                                self.fail(store.asset_campaign(campaign["campaign_id"])["error"])
                            await asyncio.sleep(.01)
                    pending = manager.view(campaign["campaign_id"])["approvals"][0]
                    engine.assert_not_called()
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                        url = f"/api/asset-campaigns/{campaign['campaign_id']}/approvals/{pending['approval_id']}"
                        self.assertEqual((await client.post(url, json={"allow": "yes"})).status_code, 422)
                        approved = await client.post(url, json={"allow": False})
                        self.assertEqual(approved.status_code, 200)
                        await asyncio.wait_for(task, 10)
                        self.assertEqual((await client.post(url, json={"allow": True})).status_code, 404)
                    engine.assert_not_called()
                    self.assertEqual(store.asset_campaign(campaign["campaign_id"])["status"], "failed")
                finally:
                    await manager.stop(campaign["campaign_id"])

    async def test_ai_registration_requires_current_execution_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(Path(root))
            store = ResourceStore(cfg.resources.root)
            project = store.create_project("证据边界")
            campaign = store.create_asset_campaign(project["project_id"], ["示例组织"], {})
            decisions = [response({"assets": [{"value": "invented.test", "evidence_ref": "old-session"}],
                                   "finish": "completed", "reason": "没有执行证据的声明不能通过"}),
                         response({"finish": "blocked", "reason": "缺少实际证据"})]
            with patch("spiritscout.gateway.gateway.ModelGateway.chat", new=AsyncMock(side_effect=decisions)):
                manager = AssetCampaignManager(store, cfg, [ModelEntry(model="fixture", provider="mock")])
                manager.start(campaign["campaign_id"], model="fixture")
                await asyncio.wait_for(manager._running[campaign["campaign_id"]].task, 10)
            self.assertEqual(store.asset_campaign_targets(campaign["campaign_id"])["targets"], [])
            self.assertEqual(store.asset_campaign(campaign["campaign_id"])["status"], "failed")

    async def test_stop_before_runner_starts_and_overlapping_stop_are_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(Path(root))
            project = store.create_project("即时停止")
            campaign = store.create_asset_campaign(project["project_id"], ["example.test"], {})
            manager = AssetCampaignManager(store)
            manager.start(campaign["campaign_id"])
            first = await manager.stop(campaign["campaign_id"])
            self.assertEqual(first["status"], "stopped", "立即停止必须覆盖尚未调度的协程")
            self.assertNotIn(campaign["campaign_id"], manager._running)
            async def runner(*args):
                await asyncio.Event().wait()
            with patch("spiritscout.assets.campaigns.run_collection", new=runner):
                manager.start(campaign["campaign_id"])
                first, second = await asyncio.gather(manager.stop(campaign["campaign_id"]), manager.stop(campaign["campaign_id"]))
            self.assertEqual(first["status"], "stopped")
            self.assertEqual(second["status"], "stopped")
            self.assertNotIn(campaign["campaign_id"], manager._running)

    async def test_collection_model_uses_latest_credentials_and_isolates_session_and_fallbacks(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(Path(root))
            store = ResourceStore(cfg.resources.root)
            project = store.create_project("独立收集选模")
            campaign = store.create_asset_campaign(project["project_id"], ["仅本地分析"], {})
            other = ModelEntry(model="same-name", provider_ref="anthropic", provider="anthropic_compatible")
            chosen = ModelEntry(model="same-name", provider_ref="openai", provider="openai_compatible", api_key="test-key-new")
            spare = ModelEntry(model="fallback", provider="mock", is_fallback_for=[chosen.key])
            loaded = []
            def source():
                loaded.append(True)
                return [other, chosen, spare]
            manager = AssetCampaignManager(store, cfg, [other], models_source=source)
            decision = response({"finish": "completed", "reason": "仅验证选模链路，没有请求真实目标"})
            async def invoke(entry, messages, **kwargs):
                self.assertEqual(entry.key, chosen.key)
                self.assertEqual(entry.api_key, "test-key-new")
                return decision
            with bind_session_model(other.key, lambda: [other]), patch(
                    "spiritscout.gateway.gateway.ModelGateway._invoke", new=AsyncMock(side_effect=invoke)) as request:
                manager.start(campaign["campaign_id"], model=chosen.key)
                await asyncio.wait_for(manager._running[campaign["campaign_id"]].task, 10)
            self.assertEqual(loaded, [True])
            self.assertEqual(request.await_count, 1)
            saved = ResourceStore(cfg.resources.root).asset_campaign(campaign["campaign_id"])
            self.assertEqual(saved["config"]["model"], chosen.key)
            self.assertEqual(saved["status"], "completed")
            self.assertNotIn("test-key-new", json.dumps(saved))
            self.assertNotIn("test-key-new", json.dumps(store.asset_campaign_events(campaign["campaign_id"])))
            with patch("spiritscout.gateway.gateway.ModelGateway._invoke", new=AsyncMock(side_effect=RuntimeError("selected failed"))) as request:
                manager.start(campaign["campaign_id"])
                await asyncio.wait_for(manager._running[campaign["campaign_id"]].task, 10)
            self.assertEqual(request.await_count, 1, "失败不能调用其他供应商的备用模型")
            self.assertEqual(store.asset_campaign(campaign["campaign_id"])["status"], "failed")
            with self.assertRaises(ConfigurationError):
                manager.start(campaign["campaign_id"], model="same-name")
            with self.assertRaises(ConfigurationError):
                manager.start(campaign["campaign_id"], model="deleted-model")

    async def test_collection_model_api_validates_identity_and_freezes_running_model(self):
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(root)
            project = store.create_project("选模接口")
            first = ModelEntry(model="same", provider_ref="one", provider="mock")
            second = ModelEntry(model="same", provider_ref="two", provider="mock")
            app = FastAPI()
            manager = mount_asset_campaigns(app, config(Path(root)), store, [first, second])
            async def runner(*args):
                await asyncio.Event().wait()
            with patch("spiritscout.assets.campaigns.run_collection", new=runner):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    url = f"/api/projects/{project['project_id']}/asset-campaigns"
                    invalid = await client.post(url, json={"targets": ["example.test"], "model": "same"})
                    self.assertEqual(invalid.status_code, 422)
                    created = await client.post(url, json={"targets": ["example.test"], "model": first.key})
                    self.assertEqual(created.status_code, 201)
                    campaign = created.json()
                    base = f"/api/asset-campaigns/{campaign['campaign_id']}"
                    self.assertEqual((await client.post(base+"/start")).status_code, 200)
                    self.assertEqual((await client.post(base+"/start", json={"model": second.key})).status_code, 422)
                    await client.post(base+"/pause")
                    self.assertEqual((await client.post(base+"/resume", json={"model": second.key})).status_code, 422)
                    self.assertEqual((await client.post(base+"/resume")).status_code, 200)
                    await client.post(base+"/stop")
                    started = await client.post(base+"/resume", json={"model": second.key})
                    self.assertEqual(started.status_code, 200)
                    self.assertEqual(started.json()["config"]["model"], second.key)
                    await manager.stop(campaign["campaign_id"])

    def test_intelligence_readiness_respects_enable_overrides_and_complete_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(Path(root))
            store = ResourceStore(cfg.resources.root)
            names = ("fofa", "hunter", "quake", "zoomeye", "aiqicha", "qichacha")
            secrets = {"asset-provider:fofa": json.dumps({"email": "fixture@example.test", "key": "fofa-secret"}),
                       **{f"asset-provider:{name}": f"{name}-secret" for name in names[1:]}}
            with patch.dict("os.environ", {"FOFA_EMAIL": "", "FOFA_KEY": "", "HUNTER_API_KEY": "",
                    "QUAKE_API_KEY": "", "ZOOMEYE_API_KEY": "", "AIQICHA_COOKIE": "", "QICHACHA_COOKIE": ""}), \
                    patch("spiritscout.web.vault.get_key", side_effect=lambda key: secrets.get(key, "")):
                self.assertEqual(_configured_intelligence(cfg.asset_intelligence, store), {}, "有密钥但未启用不能使用")
                for name in names:
                    store.save("asset_provider", name, {"enabled": True}, 0)
                self.assertEqual(set(_configured_intelligence(cfg.asset_intelligence, store)), set(names))
                secrets["asset-provider:fofa"] = json.dumps({"key": "key-without-email"})
                secrets["asset-provider:quake"] = ""
                row = store.setting("asset_provider", "zoomeye")
                store.save("asset_provider", "zoomeye", {"enabled": False}, row["version"])
                self.assertEqual(set(_configured_intelligence(cfg.asset_intelligence, store)),
                                 {"hunter", "aiqicha", "qichacha"})
                with patch.dict("os.environ", {"QUAKE_API_KEY": "environment-key"}):
                    self.assertIn("quake", _configured_intelligence(cfg.asset_intelligence, store))

    async def test_ai_prioritizes_configured_intelligence_and_can_fall_back_after_failure(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected), tempfile.TemporaryDirectory() as root:
                cfg = config(Path(root))
                store = ResourceStore(cfg.resources.root)
                store.save("asset_provider", "hunter", {"enabled": True}, 0)
                project = store.create_project("情报工具优先")
                campaign = store.create_asset_campaign(project["project_id"], ["example.test"], {})
                requests = []
                def transport(request):
                    requests.append(request)
                    self.assertEqual(request.url.params["api-key"], "fixture-hunter-key")
                    query = base64.urlsafe_b64decode(request.url.params["search"]).decode()
                    self.assertEqual(query, 'domain.suffix="example.test"')
                    return httpx.Response(403 if rejected else 200, request=request, json={"code": 200,
                        "data": {"total": 1, "arr": [{"domain": "api.example.test", "ip": "203.0.113.7"}]}})
                real_search = HunterProvider.search_domain
                async def search(provider, target, limiter=None):
                    return await real_search(provider, target, limiter, transport=httpx.MockTransport(transport))
                call = {"tool": {"name": "asset_intelligence_search", "arguments": {
                        "provider": "hunter", "target": "example.test", "target_type": "domain", "action": "analysis"}}}
                decisions = ([call, {"message": "情报工具查询失败，改用域名引擎补充", "tool": {
                    "name": "asset_collect", "arguments": {"domain": "example.test", "action": "analysis"}}}]
                    if rejected else [call, call])
                decisions.append({"assets": [{"value": "api.example.test", "evidence_ref": "execution-2",
                    "source": "内置引擎" if rejected else "Hunter 情报", "ip": "203.0.113.7"}],
                    "finish": "completed", "reason": "根据实际工具证据登记候选"})
                async def chat(self_gateway, messages, **kwargs):
                    self.assertNotIn("fixture-hunter-key", json.dumps(messages))
                    if len(decisions) == 3:
                        payload = json.loads(messages[-1]["content"])["snapshot"]
                        self.assertEqual(payload["configured_intelligence"],
                                         [{"provider": "hunter", "target_types": ["domain"]}])
                        self.assertIn("优先使用 configured_intelligence", messages[0]["content"])
                    return response(decisions.pop(0))
                engine_result = ProviderResult("internal", "ok", candidates=[ProviderCandidate("api.example.test")])
                with patch.dict("os.environ", {"HUNTER_API_KEY": ""}), \
                        patch("spiritscout.web.vault.get_key", side_effect=lambda key: "fixture-hunter-key" if key == "asset-provider:hunter" else ""), \
                        patch.object(HunterProvider, "search_domain", new=search), \
                        patch("spiritscout.assets.agent.InternalAssetProvider.collect", new=AsyncMock(return_value=engine_result)) as engine, \
                        patch("spiritscout.gateway.gateway.ModelGateway.chat", new=chat):
                    manager = AssetCampaignManager(store, cfg, [ModelEntry(model="fixture", provider="mock")])
                    manager.start(campaign["campaign_id"], model="fixture")
                    await asyncio.wait_for(manager._running[campaign["campaign_id"]].task, 10)
                self.assertEqual(len(requests), 1, "成功结果复用，不增加 API 请求；失败由模型改选其他方式")
                self.assertEqual(engine.await_count, int(rejected))
                batch = store.asset_campaign(campaign["campaign_id"])
                self.assertEqual(batch["status"], "completed")
                self.assertEqual(batch["stats"]["failed"], int(rejected))
                assets = store.asset_campaign_targets(campaign["campaign_id"])["targets"]
                self.assertEqual([asset["value"] for asset in assets], ["api.example.test"])
                self.assertEqual(assets[0]["source"], "内置引擎" if rejected else "Hunter 情报")
                events = json.dumps(store.asset_campaign_events(campaign["campaign_id"]), ensure_ascii=False)
                self.assertIn("优先使用已配置情报工具：hunter", events)
                self.assertIn("状态 failed" if rejected else "状态 ok", events)
                if not rejected:
                    self.assertIn("复用已有工具结果", events)
                self.assertNotIn("fixture-hunter-key", events)
                self.assertNotIn("fixture-hunter-key", json.dumps(batch))

    @unittest.skipUnless(sandbox_available(), "需要现有 Linux/bwrap")
    async def test_live_asset_reports_are_registered_before_long_command_finishes(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(Path(root))
            store = ResourceStore(cfg.resources.root)
            project = store.create_project("流式存活入库")
            campaign = store.create_asset_campaign(project["project_id"], ["本地夹具"], {})
            async def chat(gateway, messages, **kwargs):
                path = json.loads(messages[-1]["content"])["snapshot"]["asset_report_file"]
                items = [{"value": "api.fixture.test", "http_status": 403, "source": "本地报告夹具"},
                         {"value": "api.fixture.test", "http_status": 302, "title": "更新标题"},
                         {"value": "proxy.fixture.test", "http_status": 502, "response_source": "session_proxy", "http_error": "upstream_failed"},
                         {"value": "dns.fixture.test", "verification_status": "dns_resolved", "ip": "203.0.113.1"}]
                code = ("import json,time\n"+f"p={path!r}\n"+f"items={items!r}\n"+
                    "with open(p,'a') as f:\n for item in items:\n  f.write(json.dumps(item)+'\\n');f.flush()\n"+
                    "print('reports-written',flush=True)\nwhile True: time.sleep(.05)\n")
                return response({"tool": {"name": "sandbox_exec", "arguments": {"code": code, "language": "python", "action": "analysis", "timeout": 30}}})
            with patch("spiritscout.gateway.gateway.ModelGateway.chat", new=chat):
                manager = AssetCampaignManager(store, cfg, [ModelEntry(model="fixture", provider="mock")])
                manager.start(campaign["campaign_id"], model="fixture")
                task = manager._running[campaign["campaign_id"]].task
                try:
                    async with asyncio.timeout(10):
                        while store.asset_campaign(campaign["campaign_id"])["stats"].get("assets_revision", 0) < 4:
                            if task.done():
                                self.fail(store.asset_campaign(campaign["campaign_id"])["error"])
                            await asyncio.sleep(.025)
                    self.assertFalse(task.done(), "资产必须在命令结束前写入")
                    assets = {a["value"]: a for a in store.asset_campaign_targets(campaign["campaign_id"])["targets"]}
                    self.assertEqual(assets["api.fixture.test"]["http_status"], 302)
                    self.assertEqual(assets["api.fixture.test"]["title"], "更新标题")
                    self.assertEqual(assets["api.fixture.test"]["verification_status"], "http_responded")
                    self.assertEqual(assets["proxy.fixture.test"]["verification_status"], "verification_failed")
                    self.assertEqual(assets["dns.fixture.test"]["verification_status"], "dns_resolved")
                    self.assertEqual(store.assets(project=project["project_id"])["total"], 3)
                finally:
                    await manager.stop(campaign["campaign_id"])
                self.assertEqual(ResourceStore(cfg.resources.root).asset_campaign_targets(campaign["campaign_id"])["targets"], list(assets.values()))

    async def test_http_responses_are_live_without_a_200_allowlist_or_proxy_false_positive(self):
        for status, proxy in [(200,False), (302,False), (401,False), (403,False), (404,False), (429,False), (500,False), (502,True)]:
            with self.subTest(status=status, proxy=proxy), tempfile.TemporaryDirectory() as root:
                cfg = config(Path(root))
                store = ResourceStore(cfg.resources.root)
                project = store.create_project("存活判据")
                campaign = store.create_asset_campaign(project["project_id"], ["http://fixture.test"], {})
                decisions = [response({"tool":{"name":"http_probe", "arguments":{"url":"http://fixture.test", "action":"analysis"}}}),
                             response({"assets": [{"value": "unrelated.fixture.test", "evidence_ref": "execution-1", "verification_status": "http_responded"}],
                                       "finish":"completed","reason":"仅验证响应判据"})]
                parsed = {"status_code":status,"title":"夹具","response_source":"session_proxy" if proxy else "target"}
                if proxy:
                    parsed["http_failure"] = {"phase":"proxy_upstream"}
                with patch("spiritscout.gateway.gateway.ModelGateway.chat", new=AsyncMock(side_effect=decisions)), \
                        patch("spiritscout.infrastructure.http_probe", new=AsyncMock(return_value=ToolResult(tool="http_probe",ok=True,parsed=parsed))):
                    manager = AssetCampaignManager(store,cfg,[ModelEntry(model="fixture",provider="mock")])
                    manager.start(campaign["campaign_id"],model="fixture")
                    await asyncio.wait_for(manager._running[campaign["campaign_id"]].task,10)
                assets = store.asset_campaign_targets(campaign["campaign_id"])["targets"]
                related = [a for a in assets if a["value"] == "http://fixture.test/"]
                unrelated = [a for a in assets if a["value"] == "unrelated.fixture.test"]
                self.assertEqual(len(related), 1, "出口失败候选仍保留")
                if proxy:
                    self.assertEqual(related[0]["verification_status"], "verification_failed")
                    self.assertIsNone(related[0]["http_status"])
                if related and not proxy:
                    self.assertEqual(related[0]["http_status"],status)
                    self.assertEqual(related[0]["verification_status"],"http_responded")
                self.assertEqual(len(unrelated), 1)
                self.assertNotEqual(unrelated[0]["verification_status"], "http_responded", "另一个主机不能借本次HTTP证据标注存活")

    async def test_proxy_failure_is_retained_and_direct_confirmation_is_opt_in(self):
        import csv
        import io
        from contextlib import asynccontextmanager

        from spiritscout.assets.export import asset_csv

        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as root:
                cfg = config(Path(root))
                store = ResourceStore(cfg.resources.root)
                project = store.create_project('按出口观察')
                campaign = store.create_asset_campaign(project['project_id'], ['http://fixture.test'],
                    {'proxy_plan_id': 'proxy-0123456789ab', 'direct_fallback': fallback})
                closed = []
                @asynccontextmanager
                async def egress(store, session):
                    session.request['proxy_binding'].update(exit_ip='203.0.113.8')
                    try:
                        yield
                    finally:
                        closed.append(True)
                async def probe(url, **kwargs):
                    direct = kwargs['workspace_root'].endswith('direct-confirmation')
                    parsed = {'status_code': 200 if direct else None,
                              'response_source': 'target' if direct else 'session_proxy'}
                    if not direct:
                        parsed['http_failure'] = {'message': '代理连接失败'}
                        parsed['retryable'] = False
                    return ToolResult(tool='http_probe', ok=direct, parsed=parsed)
                decisions = [response({'tool': {'name': 'http_probe', 'arguments':
                    {'url': 'http://fixture.test', 'action': 'analysis'}}}),
                    response({'finish': 'completed', 'reason': '夹具结束'})]
                with patch('spiritscout.traffic.egress.bind', return_value={'id': 'proxy-0123456789ab', 'name': '测试出口'}), \
                     patch('spiritscout.traffic.egress.session_egress', egress), \
                     patch('spiritscout.infrastructure.http_probe', new=AsyncMock(side_effect=probe)) as calls, \
                     patch('spiritscout.gateway.gateway.ModelGateway.chat', new=AsyncMock(side_effect=decisions)):
                    manager = AssetCampaignManager(store, cfg, [ModelEntry(model='fixture', provider='mock')])
                    manager.start(campaign['campaign_id'], model='fixture')
                    await asyncio.wait_for(manager._running[campaign['campaign_id']].task, 10)
                self.assertEqual(closed, [True])
                self.assertEqual(calls.await_count, 2 if fallback else 1)
                target = store.asset_campaign_targets(campaign['campaign_id'])['targets'][0]
                self.assertEqual(target['verification_status'], 'http_responded' if fallback else 'verification_failed')
                rows = list(csv.DictReader(io.StringIO(asset_csv([target]))))
                proxy_row = next(r for r in rows if r['egress_id'] == 'proxy-0123456789ab')
                self.assertEqual(proxy_row['exit_ip'], '203.0.113.8')
                self.assertEqual(proxy_row['http_status'], '')
                self.assertIn('代理连接失败', proxy_row['http_error'])
                if fallback:
                    direct = next(r for r in rows if r['egress_id'] == 'direct')
                    self.assertEqual(direct['http_status'], '200')
                    self.assertEqual(direct['exit_ip'], '', '未测量直连 IP 不伪造出口地址')

    async def test_handoff_freezes_selected_asset_facts_and_keeps_other_assets_out(self):
        import csv
        import io

        from spiritscout.blackboard import Blackboard
        from spiritscout.core.exceptions import SpiritScoutError
        from spiritscout.core.models import BlackboardLayer
        from spiritscout.web.asset_handoff import materialize_handoff, resolve_handoff
        with tempfile.TemporaryDirectory() as root:
            store = ResourceStore(Path(root) / 'resources')
            project = store.create_project('冻结资产')
            other = store.create_project('其他项目')
            batch = store.create_asset_campaign(project['project_id'], ['fixture.test'], {})
            for host in ('chosen.fixture.test', 'other.fixture.test'):
                store.observe({'asset_type': 'domain', 'value': host, 'attrs': {'title': '=1+1',
                    'http_status': 403, 'verification_status': 'http_responded', 'egress_id': 'direct', 'egress_name': '直连'}},
                    f"asset-campaign:{batch['campaign_id']}:{host}", project_id=project['project_id'])
            store.update_asset_campaign(batch['campaign_id'], status='completed')
            payload = {'mode': 'src', 'project_id': project['project_id'], 'asset_campaign_id': batch['campaign_id'],
                       'asset_values': ['chosen.fixture.test']}
            for updates in ({'mode': 'ctf'}, {'project_id': other['project_id']}, {'asset_values': ['missing.test']}):
                with self.assertRaises(SpiritScoutError):
                    resolve_handoff(store, {**payload, **updates})
            snapshot = resolve_handoff(store, payload)
            workspace = Path(root) / 'session'
            workspace.mkdir()
            records = await materialize_handoff(snapshot, workspace, workspace / 'bb.db')
            self.assertEqual([r['name'] for r in records], ['collected-assets.csv'])
            self.assertEqual({p.name for p in (workspace / 'attachments').iterdir()}, {'collected-assets.csv'})
            rows = list(csv.DictReader(io.StringIO((workspace / 'attachments/collected-assets.csv').read_text(encoding='utf-8-sig'))))
            self.assertEqual([r['asset'] for r in rows], ['chosen.fixture.test'])
            self.assertEqual(rows[0]['http_status'], '403')
            self.assertEqual(rows[0]['title'], "'=1+1")
            bb = Blackboard(workspace / 'bb.db')
            try:
                assets = bb.query(BlackboardLayer.ASSET)
                self.assertEqual([a['data']['value'] for a in assets], ['chosen.fixture.test'])
                self.assertEqual(assets[0]['data']['scope_status'], 'allowed_test')
                self.assertEqual(assets[0]['data']['attrs']['http_status'], 403)
            finally:
                await bb.aclose()
            from spiritscout.core.models import RunMode
            from spiritscout.launch import LaunchRequest, prepare_launch
            from spiritscout.web.sessions import SessionManager

            manager = SessionManager(config(Path(root)), [], resources=store)
            mission = '根据选定资产开始 SRC 测试，优先预发环境'
            plan = prepare_launch(LaunchRequest(mode=RunMode.SRC, mission=mission))
            with patch('spiritscout.config.load_model_config', return_value=[ModelEntry(model='fixture', provider='mock')]), \
                 patch.object(manager, 'start'):
                created = await manager.create(
                    {**payload, 'mission': mission, 'no_llm': True, '_asset_snapshot': snapshot}, plan)
            self.assertEqual(created.user_inbox, [], '生成的 CSV 不重复触发执行中新消息处理')
            self.assertEqual(len(created.dialogue_history), 1)
            self.assertEqual(created.dialogue_history[0]['content'], mission)
            self.assertEqual([r['name'] for r in created.dialogue_history[0]['attachments']], ['collected-assets.csv'])
            self.assertTrue(Path(created.dialogue_history[0]['attachments'][0]['path']).is_file())
            self.assertFalse(created.workspace.joinpath('attachments/asset-summary.md').exists())
            self.assertEqual(manager._queue.qsize(), 1)
            frozen = (workspace / 'attachments/collected-assets.csv').read_bytes()
            store.delete_asset_campaign(batch['campaign_id'])
            self.assertTrue((workspace / 'asset-handoff.json').is_file())
            self.assertEqual((workspace / 'attachments/collected-assets.csv').read_bytes(), frozen)
