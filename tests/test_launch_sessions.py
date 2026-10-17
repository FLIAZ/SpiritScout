from __future__ import annotations

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
        return SimpleNamespace(content=content)


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
        # 审计打码：抽取留痕可见，凭据值不见
        actions = [line["detail"].get("action") for line in audit_lines]
        self.assertIn("platform_profile_extracted", actions)
        self.assertNotIn("tsec-test-token", audit_text)
        extracted = next(line for line in audit_lines
                         if line["detail"].get("action") == "platform_profile_extracted")
        self.assertEqual(extracted["detail"]["profile"]["name"], "tsecbench")
        # 凭据在审计里被摘要化/打码，任何形态都不得出现原文
        self.assertNotIn("tsec-test-token",
                         json.dumps(extracted, ensure_ascii=False))

    async def test_mission_without_platform_description_runs_unchanged(self) -> None:
        session, _, run_mock, _, audit_lines, _ = await self._run(
            PLAIN_CTF_MISSION, json.dumps({"platform": None}))

        self.assertIsNone(run_mock.call_args.kwargs["platform_profile"])
        self.assertNotIn("platform_yaml", session.launch_request)
        actions = [line["detail"].get("action") for line in audit_lines]
        self.assertIn("platform_not_found", actions)


if __name__ == "__main__":
    unittest.main()
