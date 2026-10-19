from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry, SystemConfig, WorkspaceConfig
from spiritscout.core.models import AgentType, BlackboardLayer, RunMode
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.web.app import _read_state
from spiritscout.web.sessions import Session, SessionManager

CTF_MISSION = (
    "一道ctf题目，拿flag："
    "http://8000-d310a0ca-737b-403d-82a7-000eb4beb8d3.challenge.ctfplus.cn/"
)


class LaunchRoutingTests(unittest.TestCase):
    def test_explicit_ctf_mission_is_deterministic(self) -> None:
        plan = prepare_launch(LaunchRequest(mission=CTF_MISSION))

        self.assertEqual(plan.authorization.mode, RunMode.CTF)
        self.assertTrue(plan.mode_auto)
        self.assertTrue(plan.needs_mode_routing)
        self.assertEqual(len(plan.challenges), 1)

    def test_ambiguous_web_mission_still_uses_semantic_router(self) -> None:
        plan = prepare_launch(LaunchRequest(
            mission="检查 https://example.test 的登录流程"))

        self.assertEqual(plan.authorization.mode, RunMode.SRC)
        self.assertTrue(plan.needs_mode_routing)


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_runtime_error_cannot_leave_session_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
            manager = SessionManager(cfg, [])
            workspace = root / "sessions" / "sess-test"
            workspace.mkdir(parents=True)
            session = Session(
                session_id="sess-test", mode="invalid", target="test",
                workspace=workspace, status="running", phase="starting")
            plan = prepare_launch(LaunchRequest(
                mode=RunMode.SRC, target="https://example.test"))
            # 故障注入：runtime 入口抛普通异常，验证会话故障隔离落终态
            # （不依赖真实装配链的失败点——那个会随实现漂移）
            with patch("spiritscout.runtime.run_engagement",
                       new=AsyncMock(side_effect=ValueError("注入的启动前故障"))):
                await manager._run(session, cfg, plan, True, [])

            self.assertEqual(session.status, "failed")
            self.assertEqual(session.phase, "failed")
            self.assertIn("ValueError", session.error)
            self.assertTrue(session.meta_path.exists())

    async def test_unstarted_auto_session_rebuilds_from_original_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
            manager = SessionManager(cfg, [])
            workspace = root / "sessions" / "sess-recover"
            workspace.mkdir(parents=True)
            session = Session(
                session_id="sess-recover", mode="src", target="old",
                workspace=workspace,
                launch_request={
                    "mission": CTF_MISSION, "mode": "src", "mode_auto": True,
                },
            )

            plan = manager._rebuild_plan(session)

            self.assertIsNotNone(plan)
            self.assertEqual(plan.authorization.mode, RunMode.CTF)
            self.assertTrue(plan.needs_mode_routing)

    async def test_state_exposes_startup_phase_without_blackboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "sess-phase"
            workspace.mkdir()
            session = Session(
                session_id="sess-phase", mode="ctf", target="challenge",
                workspace=workspace, status="running", phase="model_preflight")

            state = _read_state(session)

            self.assertEqual(state["session"]["phase"], "model_preflight")
            self.assertIsNone(state["business_context"])

    async def test_resume_cancels_stale_running_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
            manager = SessionManager(cfg, [])
            workspace = root / "sessions" / "sess-stale"
            workspace.mkdir(parents=True)
            session = Session(
                session_id="sess-stale", mode="ctf", target="challenge",
                workspace=workspace, status="stopped")
            bb = Blackboard(session.bb_path, bus=None)
            bb.write(
                BlackboardLayer.TASK_STATE, "task-old",
                {"task": {"task_id": "task-old", "status": "running"},
                 "summary": {"live": True},
                 "routes": [{"name": "solve", "status": "running"}]},
                agent_type=AgentType.MASTER)
            bb.write(
                BlackboardLayer.TASK_STATE, "dag-snapshot",
                {"dag_snapshot": [{"node_id": "old", "status": "running"}]},
                agent_type=AgentType.MASTER)
            bb.close()

            changed = manager._cancel_stale_tasks(session)

            bb = Blackboard(session.bb_path, bus=None)
            self.assertEqual(changed, 1)
            self.assertEqual(
                bb.read(BlackboardLayer.TASK_STATE, "task-old")["task"]["status"],
                "cancelled")
            self.assertEqual(
                bb.read(BlackboardLayer.TASK_STATE, "dag-snapshot")
                ["dag_snapshot"][0]["status"], "cancelled")
            bb.close()

    async def test_state_hides_tasks_outside_current_dag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "sess-current"
            workspace.mkdir()
            session = Session(
                session_id="sess-current", mode="ctf", target="challenge",
                workspace=workspace, status="completed", phase="completed",
                summary={"run": {"dag_nodes": [{
                    "node_id": "new", "task_ids": ["task-new"]}]}})
            bb = Blackboard(session.bb_path, bus=None)
            for task_id, status in (("task-old", "running"), ("task-new", "done")):
                bb.write(
                    BlackboardLayer.TASK_STATE, task_id,
                    {"task": {"task_id": task_id, "status": status},
                     "summary": {}, "routes": []},
                    agent_type=AgentType.MASTER)
            bb.close()

            state = _read_state(session)

            self.assertEqual(
                [task["task_id"] for task in state["tasks"]], ["task-new"])


PLATFORM_DOC_MISSION = """平台托管的 CTF 跑分赛，请尽快拿分。平台 API 根地址：http://10.0.100.58
接口文档：
- GET http://10.0.100.58/api/v1/challenges 列出题目；
  应答 {"data": {"challenges": [{...}]}}，每题字段：
  unique_code / description / difficulty / container_addr / is_completed。
- POST /api/v1/challenge/start，body {"unique_code": "<题号>"}，应答含 container_addr。
- POST /api/v1/challenge/close，body {"unique_code": "<题号>"}。
- POST /api/v1/challenge/submit，body {"unique_code": "<题号>", "flag": "<flag>"}，
  应答 {"correct": true|false}；重复提交返回 409 {"code": "duplicate"}。
固定请求头：X-API-Key: tsec-test-token。同一时间最多开 2 个容器。
"""

PLAIN_CTF_MISSION = "一道ctf题目，拿flag：http://challenge.example.cn/"

_EXTRACTED_PROFILE = json.dumps({
    "platform": {
        "name": "tsecbench", "base_url": "http://10.0.100.58",
        "headers": {"X-API-Key": "tsec-test-token"},
        "ops": {
            "list": {"method": "GET", "path": "/api/v1/challenges",
                     "items_field": "data.challenges"},
            "start": {"method": "POST", "path": "/api/v1/challenge/start",
                      "body": {"unique_code": "{challenge}"},
                      "addr_field": "container_addr"},
            "close": {"method": "POST", "path": "/api/v1/challenge/close",
                      "body": {"unique_code": "{challenge}"}},
            "submit": {"method": "POST", "path": "/api/v1/challenge/submit",
                       "body": {"unique_code": "{challenge}", "flag": "{flag}"},
                       "accepted_field": "correct",
                       "duplicate_code": "duplicate"},
        },
        "limits": {"max_active": 2},
        "challenge_fields": {
            "id": "unique_code", "title": "description",
            "container_addr": "container_addr", "is_completed": "is_completed"},
    }}, ensure_ascii=False)


class _FakeChatGateway:
    """对话链路替身：按调用顺序吐预设应答，记录调用参数供断言。"""

    def __init__(self, *contents: str) -> None:
        self._contents = list(contents)
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        content = self._contents.pop(0) if self._contents else "{}"
        return SimpleNamespace(content=content, finish_reason="stop")


class PlatformExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, mission: str, reply: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
            models = [ModelEntry(model="test-model")]
            manager = SessionManager(cfg, models)
            workspace = root / "sessions" / "sess-platform"
            workspace.mkdir(parents=True)
            session = Session(session_id="sess-platform", mode="ctf",
                              target="ctf://challenges", workspace=workspace)
            session.launch_request = {"mission": mission, "mode": "ctf"}
            cfg.audit_log_path = str(session.audit_path)
            plan = prepare_launch(LaunchRequest(
                mode=RunMode.CTF, mission=mission))
            gateway = _FakeChatGateway(reply)
            with patch("spiritscout.web.sessions._conversation_gateway",
                       return_value=gateway), patch(
                    "spiritscout.runtime.run_ctf_engagement",
                    new=AsyncMock(return_value=SimpleNamespace(
                        run_summary={}, verify_stats={}, flag_stats={},
                        report_path=str(workspace / "report.md")))) as run_mock:
                await manager._run(session, cfg, plan, False, models)
            audit_text = Path(cfg.audit_log_path).read_text(encoding="utf-8")
            audit_lines = [json.loads(line) for line in audit_text.splitlines()
                           if line.strip()]
            self.assertEqual(session.status, "completed")
            return session, plan, run_mock, gateway, audit_lines, audit_text

    async def test_platform_doc_mission_extracts_profile_for_runtime(self) -> None:
        session, plan, run_mock, gateway, audit_lines, audit_text = await self._run(
            PLATFORM_DOC_MISSION, _EXTRACTED_PROFILE)

        profile = run_mock.call_args.kwargs["platform_profile"]
        self.assertEqual(profile["base_url"], "http://10.0.100.58")
        self.assertEqual(profile["headers"]["X-API-Key"], "tsec-test-token")
        self.assertEqual(profile["challenge_fields"]["id"], "unique_code")
        # 平台宿主并入授权域（纯托管题任务也要通过启动门）
        self.assertIn("10.0.100.58", plan.authorization.scope.allowed_domains)
        # 抽取结果回写启动参数：续传/重建计划不再重抽
        self.assertIn("tsecbench", session.launch_request["platform_yaml"])
        # 独立抽取调用：任务原文进了提示词（截断后）
        self.assertEqual(len(gateway.calls), 1)
        self.assertIn("10.0.100.58",
                      gateway.calls[0]["messages"][1]["content"])
        # 抽取留痕可见；平台凭据在 profile 里仍打码（用户自己的 key 不回显）
        actions = [line["detail"].get("action") for line in audit_lines]
        self.assertIn("platform_profile_extracted", actions)
        extracted = next(line for line in audit_lines
                         if line["detail"].get("action") == "platform_profile_extracted")
        self.assertEqual(extracted["detail"]["profile"]["name"], "tsecbench")
        self.assertEqual(extracted["detail"]["profile"]["headers"]["X-API-Key"], "***")

    async def test_mission_without_platform_description_runs_unchanged(self) -> None:
        session, _, run_mock, _, audit_lines, _ = await self._run(
            PLAIN_CTF_MISSION, json.dumps({"platform": None}))

        self.assertIsNone(run_mock.call_args.kwargs["platform_profile"])
        self.assertNotIn("platform_yaml", session.launch_request)
        actions = [line["detail"].get("action") for line in audit_lines]
        self.assertIn("platform_not_found", actions)


class SessionUsageTests(unittest.IsolatedAsyncioTestCase):
    """会话用量口径：旁路调用不进会话台账，终场汇总以台账为准。"""

    def _mgr(self, root: Path):
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
        models = [ModelEntry(model="test-model")]
        return cfg, SessionManager(cfg, models)

    async def test_naming_bypass_bills_global_ledger_not_session(self) -> None:
        """命名旁路走全局台账：会话的模型消耗不背命名开销。"""
        from spiritscout.web import modelcfg, vault

        seen: dict[str, str] = {}

        def fake_gateway(cfg, models, audit):
            seen["usage_root"] = str(Path(cfg.workspace.root))
            return _FakeChatGateway(json.dumps({"name": "问候", "campaign": ""}))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(
                workspace=WorkspaceConfig(root=str(root)),
                models_config_path=str(root / "models.yaml"),
                agent_models_path=str(root / "agents.yaml"),
                audit_log_path=str(root / "audit.jsonl"))
            modelcfg.write_config(cfg.models_config_path, [],
                                  [{"model": "mini", "provider": "mock"}])
            vault.configure(str(root))
            manager = SessionManager(cfg, [])
            manager._run = AsyncMock()
            plan = prepare_launch(LaunchRequest(
                mode=RunMode.SRC, mission="你好",
                target="https://example.com"))
            with patch("spiritscout.web.sessions._conversation_gateway",
                       fake_gateway):
                session = await manager.create(
                    {"mode": "src", "mission": "你好",
                     "target": "https://example.com"}, plan)
                for _ in range(200):
                    if session.auto_title:
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(session.auto_title, "问候")
            # 绑定的是全局工作区，不是会话目录——命名的 token 不进会话账
            self.assertEqual(seen.get("usage_root"), str(root))
            self.assertNotIn(session.session_id, seen.get("usage_root", ""))

    async def test_final_summary_refreshes_usage_from_ledger(self) -> None:
        """终场汇总以台账为准：收尾的提炼/报告调用不再漏记。"""
        from spiritscout.gateway.usage import UsageLedger

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg, manager = self._mgr(Path(tmp))
            workspace = root / "sessions" / "sess-usage"
            workspace.mkdir(parents=True)
            session = Session(session_id="sess-usage", mode="ctf",
                              target="ctf://challenges", workspace=workspace)
            session.launch_request = {"mission": "一道ctf题目：http://challenge.example.cn/", "mode": "ctf"}
            cfg.audit_log_path = str(session.audit_path)
            plan = prepare_launch(LaunchRequest(
                mode=RunMode.CTF, mission="一道ctf题目：http://challenge.example.cn/"))
            # 台账 = 运行期 1 笔 + 收尾（提炼/报告）2 笔；运行快照只记到第 1 笔
            ledger = UsageLedger(workspace / "usage.db")
            for i, tokens in enumerate((100, 40, 60), start=1):
                ledger.append({"ts": float(i), "tokens": tokens, "model": "m",
                               "call_id": f"c{i}", "task_type": "meta_planning"})
            stale = {"used_tokens": 100, "used_usd": 0.0,
                     "per_model_tokens": {"m": 100}, "per_mode_tokens": {},
                     "call_series": [{"ts": 1.0, "tokens": 100, "model": "m",
                                      "call_id": "c1"}]}
            with patch("spiritscout.runtime.run_ctf_engagement",
                       new=AsyncMock(return_value=SimpleNamespace(
                           run_summary={"model_usage": stale}, verify_stats={},
                           flag_stats={}, report_path="r.md"))):
                await manager._run(session, cfg, plan, False, [ModelEntry(model="m")])
            self.assertEqual(session.status, "completed")
            refreshed = session.summary["run"]["model_usage"]
            self.assertEqual(refreshed["used_tokens"], 200)
            self.assertEqual(len(refreshed["call_series"]), 3)


class SessionNamingTests(unittest.IsolatedAsyncioTestCase):
    """小模型自动命名与资产分类补名：解析收敛、失败静默、按任务去重。"""

    def _cfg(self, root: Path) -> SystemConfig:
        # 配置路径显式指向临时目录：模型注册表只能写进 tmp，
        # 默认值 "configs/models.yaml" 是仓库真实文件（相对 CWD）。
        cfg = SystemConfig(
            workspace=WorkspaceConfig(root=str(root)),
            models_config_path=str(root / "models.yaml"),
            agent_models_path=str(root / "agents.yaml"),
            audit_log_path=str(root / "audit.jsonl"),
        )
        return cfg

    async def test_llm_name_converges_to_existing_campaign(self) -> None:
        from spiritscout.core.audit import AuditLogger
        from spiritscout.web import agentcfg
        from spiritscout.web import sessions as web_sessions

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(Path(tmp))
            # 用户在模型页排的候选链：顺序即降级顺序
            agentcfg.set_naming_chain(cfg.agent_models_path, ["mini"], {"mini"})
            models = [ModelEntry(model="mini", api_key="k")]
            gateway = _FakeChatGateway(json.dumps({
                "name": "BiliBili 越权测试", "campaign": "bilibili src"}))
            with patch("spiritscout.web.sessions._conversation_gateway",
                       lambda *args: gateway):
                result = await web_sessions._llm_name(
                    cfg, models, AuditLogger(cfg.audit_log_path),
                    mission="对 bilibili 做登录越权测试",
                    target="https://bilibili.com", mode="src",
                    campaigns=["BiliBili SRC"])
            # 大小写变体收敛到既有分类名（分类不因拼写漂移裂开）
            self.assertEqual(result, {"name": "BiliBili 越权测试",
                                      "campaign": "BiliBili SRC"})
            call = gateway.calls[0]
            self.assertEqual(call["task_type"], "classification")
            self.assertEqual(call["reasoning_effort"], "low")
            self.assertEqual(call["model"], "mini")

    async def test_llm_name_walks_user_chain_on_failure(self) -> None:
        """链上首选调不动时按用户排的顺序换下一个，不发明额外兜底。"""
        from spiritscout.core.audit import AuditLogger
        from spiritscout.web import agentcfg
        from spiritscout.web import sessions as web_sessions

        class _Picky:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            async def chat(self, messages, **kwargs):
                self.calls.append({"messages": messages, **kwargs})
                if kwargs.get("model") == "mini":
                    raise TimeoutError("首选模型不可用")
                return SimpleNamespace(
                    content=json.dumps(
                        {"name": "备份命名", "campaign": "BiliBili SRC"}),
                    finish_reason="stop")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(Path(tmp))
            agentcfg.set_naming_chain(
                cfg.agent_models_path, ["mini", "backup"], {"mini", "backup"})
            gateway = _Picky()
            with patch("spiritscout.web.sessions._conversation_gateway",
                       lambda *args: gateway):
                result = await web_sessions._llm_name(
                    cfg, [ModelEntry(model="mini", api_key="k"),
                          ModelEntry(model="backup", api_key="k")],
                    AuditLogger(cfg.audit_log_path),
                    mission="对 bilibili 做登录越权测试",
                    target="https://bilibili.com", mode="src")
            # 严格按用户排的顺序尝试：mini 失败后是 backup，而不是注册表首项
            self.assertEqual([c["model"] for c in gateway.calls],
                             ["mini", "backup"])
            self.assertEqual(result, {"name": "备份命名",
                                      "campaign": "BiliBili SRC"})

    async def test_llm_name_retries_when_thinking_eats_output_budget(self) -> None:
        """思考型模型把小额输出额度耗在思考上：加大余量重试，而非弃案。"""
        from spiritscout.core.audit import AuditLogger
        from spiritscout.web import agentcfg
        from spiritscout.web import sessions as web_sessions

        class _Thinking:
            """首调思考吃光额度正文为空（sess-2bef27ae 实测故障形态）。"""

            def __init__(self) -> None:
                self.calls: list[dict] = []

            async def chat(self, messages, **kwargs):
                self.calls.append({"messages": messages, **kwargs})
                if len(self.calls) == 1:
                    return SimpleNamespace(content="", finish_reason="length")
                return SimpleNamespace(
                    content=json.dumps({"name": "PKW 学院教务 CTF",
                                        "campaign": "PKW CTF"}),
                    finish_reason="stop")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(Path(tmp))
            agentcfg.set_naming_chain(cfg.agent_models_path, ["mini"], {"mini"})
            gateway = _Thinking()
            with patch("spiritscout.web.sessions._conversation_gateway",
                       lambda *args: gateway):
                result = await web_sessions._llm_name(
                    cfg, [ModelEntry(model="mini", api_key="k")],
                    AuditLogger(cfg.audit_log_path),
                    mission="ctf题目，http://8080-x.challenge.ctfplus.cn/",
                    target="http://8080-x.challenge.ctfplus.cn/", mode="ctf")
            self.assertEqual(result, {"name": "PKW 学院教务 CTF",
                                      "campaign": "PKW CTF"})
            # 同一候选加大余量重试，而不是把空正文当成应答失败换链
            self.assertEqual([c["model"] for c in gateway.calls],
                             ["mini", "mini"])
            self.assertGreater(gateway.calls[1]["max_tokens"],
                               gateway.calls[0]["max_tokens"])

    async def test_empty_chain_follows_master_assignment(self) -> None:
        """未配置候选链时跟随编队页灵枢指派（用户的另一处选择）。"""
        from spiritscout.core.audit import AuditLogger
        from spiritscout.web import agentcfg
        from spiritscout.web import sessions as web_sessions

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(Path(tmp))
            agentcfg.set_assignment(cfg.agent_models_path, "master", "big", {"big"})
            gateway = _FakeChatGateway(json.dumps(
                {"name": "跟随指派", "campaign": "某 SRC"}))
            with patch("spiritscout.web.sessions._conversation_gateway",
                       lambda *args: gateway):
                await web_sessions._llm_name(
                    cfg, [ModelEntry(model="big", api_key="k"),
                          ModelEntry(model="mini", api_key="k")],
                    AuditLogger(cfg.audit_log_path),
                    mission="任务", target="t", mode="src")
            self.assertEqual(gateway.calls[0]["model"], "big")

    async def test_llm_name_failure_is_silent(self) -> None:
        from spiritscout.core.audit import AuditLogger
        from spiritscout.web import sessions as web_sessions

        class _Down:
            async def chat(self, messages, **kwargs):
                raise TimeoutError("模型不可用")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(Path(tmp))
            with patch("spiritscout.web.sessions._conversation_gateway",
                       lambda *args: _Down()):
                result = await web_sessions._llm_name(
                    cfg, [ModelEntry(model="mini", api_key="k")],
                    AuditLogger(cfg.audit_log_path),
                    mission="任务", target="t", mode="src")
            self.assertIsNone(result)
            lines = [json.loads(line) for line in
                     Path(cfg.audit_log_path).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[-1]["detail"]["action"], "failed")

    async def test_backfill_groups_identical_missions(self) -> None:
        from spiritscout.resources.store import ResourceStore
        from spiritscout.web import sessions as web_sessions

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg(root)
            store = ResourceStore(root / "resources")
            sessions_dir = root / "sessions"
            for sid, mission in (("sess-a", "对 bilibili 做越权测试"),
                                 ("sess-b", "对 bilibili 做越权测试"),
                                 ("sess-c", "打一道 CTF 反序列化题")):
                workspace = sessions_dir / sid
                workspace.mkdir(parents=True)
                (workspace / "meta.json").write_text(json.dumps({
                    "session_id": sid, "mode": "src",
                    "target": "https://bilibili.com",
                    "request": {"mission": mission},
                    "launch_request": {"mission": mission}}), encoding="utf-8")
                store.observe({"asset_type": "app",
                               "value": f"https://{sid}.example.com"},
                              f"{sid}:asset:one@1")
            self.assertEqual(len(store.sessions_without_campaign()), 3)
            gateway = _FakeChatGateway(
                json.dumps({"name": "越权测试", "campaign": "BiliBili SRC"}),
                json.dumps({"name": "反序列化", "campaign": "某 CTF 平台"}))
            models = [ModelEntry(model="mini", api_key="k")]
            with patch("spiritscout.web.sessions._conversation_gateway",
                       lambda *args: gateway):
                await web_sessions._campaign_backfill(
                    cfg, models, store, ["sess-a", "sess-b", "sess-c"])
            self.assertEqual(len(gateway.calls), 2)   # 同任务原文只调一次模型
            self.assertEqual(store.session_campaign("sess-b"), "BiliBili SRC")
            self.assertEqual(store.session_campaign("sess-c"), "某 CTF 平台")
            self.assertEqual(store.assets(campaign="BiliBili SRC")["total"], 2)
            self.assertEqual(store.sessions_without_campaign(), [])

    async def test_schedule_skips_when_nothing_to_do(self) -> None:
        from spiritscout.resources.store import ResourceStore
        from spiritscout.web import sessions as web_sessions

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(Path(tmp))
            ResourceStore(Path(tmp) / "resources")     # 空目录：无待补名会话
            self.assertEqual(web_sessions.schedule_campaign_backfill(
                cfg, [ModelEntry(model="mini", api_key="k")]), 0)
            self.assertEqual(web_sessions.schedule_campaign_backfill(
                cfg, []), 0)                            # 无模型：不可执行

    def test_display_title_priority(self) -> None:
        session = Session(
            session_id="sess-naming", mode="src", target="https://t.example.com",
            workspace=Path("/tmp/unused"),
            request={"mission": "原始任务第一行\n第二行"})
        self.assertEqual(session.display_title(), "原始任务第一行")
        session.auto_title = "自动短名"
        self.assertEqual(session.display_title(), "自动短名")
        session.title = "用户命名"                 # 用户重命名优先级最高
        self.assertEqual(session.display_title(), "用户命名")

    async def test_create_schedules_naming_for_new_session(self) -> None:
        from spiritscout.resources.store import ResourceStore, resource_root
        from spiritscout.web import modelcfg, vault

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg(root)
            modelcfg.write_config(cfg.models_config_path, [],
                                  [{"model": "mini", "provider": "mock"}])
            vault.configure(str(root))
            manager = SessionManager(cfg, [])
            manager._run = AsyncMock()   # 只验证创建/命名链路，不跑 engagement
            plan = prepare_launch(LaunchRequest(
                mode=RunMode.SRC, mission="对 bilibili 做登录越权测试",
                target="https://bilibili.com"))
            gateway = _FakeChatGateway(json.dumps({
                "name": "BiliBili 越权测试", "campaign": "BiliBili SRC"}))
            with patch("spiritscout.web.sessions._conversation_gateway",
                       lambda *args: gateway):
                session = await manager.create({
                    "mode": "src", "mission": "对 bilibili 做登录越权测试",
                    "target": "https://bilibili.com"}, plan)
                for _ in range(200):
                    if session.auto_title:
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(session.auto_title, "BiliBili 越权测试")
            self.assertEqual(session.display_title(), "BiliBili 越权测试")
            # 分类随会话落地：同一会话的资产观测将自动继承
            store = ResourceStore(resource_root(manager._cfg))
            self.assertEqual(store.session_campaign(session.session_id),
                             "BiliBili SRC")

    async def test_naming_does_not_gate_execution_start(self) -> None:
        """命名与执行并行：命名模型悬着不应答，任务照样即刻开工。"""
        from spiritscout.web import modelcfg, vault

        class _Gated:
            def __init__(self) -> None:
                self.gate = asyncio.Event()
                self.calls: list[dict] = []

            async def chat(self, messages, **kwargs):
                self.calls.append({"messages": messages, **kwargs})
                await self.gate.wait()      # 模拟命名模型迟迟不应答
                return SimpleNamespace(
                    content=json.dumps(
                        {"name": "迟到命名", "campaign": "BiliBili SRC"}),
                    finish_reason="stop")

        run_started = asyncio.Event()

        async def fake_run(session, cfg, plan, no_llm, models, resume=False):
            run_started.set()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg(root)
            modelcfg.write_config(cfg.models_config_path, [],
                                  [{"model": "mini", "provider": "mock"}])
            vault.configure(str(root))
            manager = SessionManager(cfg, [])
            manager._run = fake_run
            plan = prepare_launch(LaunchRequest(
                mode=RunMode.SRC, mission="对 bilibili 做登录越权测试",
                target="https://bilibili.com"))
            gateway = _Gated()
            with patch("spiritscout.web.sessions._conversation_gateway",
                       lambda *args: gateway):
                session = await manager.create({
                    "mode": "src", "mission": "对 bilibili 做登录越权测试",
                    "target": "https://bilibili.com"}, plan)
                # 命名模型不应答期间：执行任务照常开工（若命名阻塞开工，
                # 这里 wait_for 会超时失败——这正是要守住的不变量）
                await asyncio.wait_for(run_started.wait(), timeout=2)
                self.assertFalse(session.auto_title)
                gateway.gate.set()          # 放行命名应答
                for _ in range(200):
                    if session.auto_title:
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(session.auto_title, "迟到命名")

    async def test_auto_title_persists_across_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg(root)
            manager = SessionManager(cfg, [])
            workspace = root / "sessions" / "sess-meta"
            workspace.mkdir(parents=True)
            session = Session(session_id="sess-meta", mode="src",
                              target="https://t.example.com", workspace=workspace)
            manager._sessions["sess-meta"] = session
            session.auto_title = "自动短名"
            manager._persist(session)
            reloaded = SessionManager(cfg, []).get("sess-meta")
            self.assertEqual(reloaded.auto_title, "自动短名")
            self.assertEqual(reloaded.display_title(), "自动短名")


if __name__ == "__main__":
    unittest.main()
