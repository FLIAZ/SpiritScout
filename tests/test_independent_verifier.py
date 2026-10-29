"""独立复测使用真实本地 HTTP；模型脚本只控制实验选择，不模拟探测结果。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

from spiritscout.agents.report.agent import ReportAgent
from spiritscout.agents.sub.base import SubAgentContext
from spiritscout.agents.verifier import VerificationQueue, VerifierAgent
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    RunMode,
    Scope,
    Severity,
    Vulnerability,
    VulnStatus,
)
from spiritscout.gateway import world_state
from spiritscout.gateway.gateway import ChatResponse
from spiritscout.policy import PolicyEngine, ScopeGuard
from spiritscout.resources.store import ResourceStore, record_asset
from spiritscout.tools.builtin import BUILTIN_SPECS, http_probe
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.registry import ToolRegistry

RULE = "只有未授权读取其他用户资料才算有效危害；普通错误页不收录。"


class Fixture(BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(("other-user-record:fixture-only" if self.path == "/record/2" else "public-home").encode())

    def log_message(self, *_args) -> None:
        pass


class VerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_reading_source_keeps_original_execution_and_is_not_independent_control(self) -> None:
        from spiritscout.tools.http_responses import SPEC, http_response_read

        self.tools.register_builtin("http_response_read", http_response_read, SPEC)
        self.ctx.builtin_ctx["workspace_root"] = self.tmp.name

        async def respond(messages, **kwargs):
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            calls = []
            if not results:
                calls = [{"id": "fetch", "name": "http_probe", "arguments": {"url": self.url + "/record/2"}}]
            elif len(results) == 1:
                calls = [{"id": "read", "name": "http_response_read",
                          "arguments": {"body_ref": results[0]["parsed"]["body_ref"]}}]
            if calls:
                return ChatResponse(cost_usd=0, latency_ms=0, content="取得实际证据", model="fixture",
                                    prompt_tokens=1, completion_tokens=1, tool_calls=calls)
            self.assertEqual(results[0]["execution_id"], results[1]["execution_id"])
            proof = {"execution_id": results[1]["execution_id"], "quote": "other-user-record:fixture-only"}
            return ChatResponse(cost_usd=0, latency_ms=0, model="fixture", prompt_tokens=1, completion_tokens=1,
                content=json.dumps({"verdict": "confirmed", "reason": "读取记录", "harm": "读取其他用户资料",
                                    "rule_quote": RULE, "evidence": [proof], "controls": [proof]}))

        self.gateway.chat.side_effect = respond
        result = await VerifierAgent(self.ctx).review(self.candidate(), dict(self.rules))
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertEqual(len(Fixture.hits), 1)

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bb = Blackboard(Path(self.tmp.name) / "bb.db")
        self.audit = NullAuditLogger()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        Fixture.hits = []
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        guard = ScopeGuard(Scope(allowed_urls=[self.url]))
        self.policy = PolicyEngine(RunMode.SRC, guard, audit=self.audit)
        self.tools = ToolRegistry(LocalExecutor(), policy=self.policy, audit=self.audit)
        self.tools.register_builtin("http_probe", http_probe, BUILTIN_SPECS["http_probe"])
        self.gateway = SimpleNamespace(chat=AsyncMock(side_effect=self.reply))
        self.ctx = SubAgentContext(bus=None, blackboard=self.bb, convergence=ConvergenceEngine(self.bb),
            gateway=self.gateway, policy=self.policy, tools=self.tools, audit=self.audit, builtin_ctx={"guard": guard})
        self.rules = {"mission": RULE, "standing_rules": "", "user_messages": [], "user_replies": []}
        self.verdict = "confirmed"

    async def asyncTearDown(self) -> None:
        self.bb.close()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def candidate(self, key: str = "candidate") -> Vulnerability:
        return Vulnerability(vuln_id=key, title="疑似越权读取", vuln_type="idor", target=self.url,
            endpoint=self.url + "/record/2", severity=Severity.HIGH, found_by="web-vuln",
            evidence=[{"note": "发现者声称存在危害，不能作为复测实证"}])

    async def reply(self, messages, **kwargs):
        tools = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if not tools:
            calls = [{"id": str(i), "name": "http_probe", "arguments": {"url": self.url + path}}
                     for i, path in enumerate(("/", "/record/2"))]
            return ChatResponse(cost_usd=0, latency_ms=0, content="先独立获取对照和疑似入口", model="fixture", prompt_tokens=1,
                                completion_tokens=1, tool_calls=calls)
        raw = {"verdict": self.verdict, "reason": "独立请求能够读取其他用户资料", "harm": "越权读取他人资料",
            "rule_quote": RULE, "severity": "high",
            "evidence": [{"execution_id": tools[1].get("execution_id", "none"), "quote": "other-user-record:fixture-only"}],
            "controls": [{"execution_id": tools[0].get("execution_id", "none"), "quote": "public-home"}]}
        return ChatResponse(cost_usd=0, latency_ms=0, content=json.dumps(raw), model="fixture", prompt_tokens=1, completion_tokens=1)

    async def test_verifier_receives_complete_boundary_and_live_user_update(self) -> None:
        rules = "规则原文\n" * 5000 + "尾部禁止生产环境"
        self.policy.guard.scope.testing_rules = rules
        project_rule = "该租户仅是演示，不满足收录标准。"
        self.bb.write(BlackboardLayer.TOPOLOGY, "project-memory", {"description": project_rule})
        first = {"kind": "message", "text": "仅在测试环境复测"}
        second = {"kind": "answer", "question": "能扩大测试吗？", "answer": "不能。\n" * 2000}
        world_state.record_user_statement(self.bb, first)
        original = self.reply
        seen = []

        async def review(messages, **kwargs):
            seen.append([dict(message) for message in messages])
            if len(seen) == 1:
                initial = json.loads(messages[1]["content"])["world_state"]
                ref = initial["authorization"]["testing_rules"]["blackboard_ref"]
                self.assertEqual(self.bb.read(BlackboardLayer(ref["layer"]), ref["record_id"])["content"], rules)
                ref = initial["project_rules"]["blackboard_ref"]
                self.assertEqual(self.bb.read(BlackboardLayer(ref["layer"]), ref["record_id"])["content"], project_rule)
                world_state.record_user_statement(self.bb, second)
                return await original(messages, **kwargs)
            fragment = messages[-1]["content"]
            update = json.loads(fragment.split("\n", 1)[1].rsplit("\n", 1)[0])
            ref = update["user_context"]["blackboard_ref"]
            self.assertEqual(self.bb.read(BlackboardLayer(ref["layer"]), ref["record_id"])["statements"], [first, second])
            return ChatResponse(cost_usd=0, latency_ms=0, content=json.dumps({
                "verdict": "out_of_rules", "reason": "用户项目规则不收录演示租户", "rule_quote": project_rule}),
                model="fixture", prompt_tokens=1, completion_tokens=1)

        self.gateway.chat.side_effect = review
        result = await VerifierAgent(self.ctx).review(self.candidate(), dict(self.rules))
        self.assertEqual(result["verdict"], "out_of_rules")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0], seen[1][0], "系统前缀不随边界变化")
        self.assertNotIn(rules, seen[0][0]["content"])

    def test_rule_quote_requires_one_trusted_original_not_candidate_or_tool_text(self) -> None:
        for source in ({"authorization": {"testing_rules": RULE}}, {"project_rules": RULE},
                       {"user_context": {"statements": [{"kind": "answer", "answer": RULE}]}}):
            result = VerifierAgent.validate({"verdict": "out_of_rules", "rule_quote": RULE}, {}, {"world_state": source})
            self.assertEqual(result["verdict"], "out_of_rules")
        for source in ({"user_context": {"statements": [{"question": RULE}]}},
                       {"candidate": {"reason": RULE}}, {"observations": {"text": RULE}}):
            result = VerifierAgent.validate({"verdict": "out_of_rules", "rule_quote": RULE}, {}, {"world_state": source})
            self.assertEqual(result["verdict"], "inconclusive")
        result = VerifierAgent.validate({"verdict": "out_of_rules", "rule_quote": "规则 A\n规则 B"}, {},
                                        {"mission": "规则 A", "standing_rules": "规则 B"})
        self.assertEqual(result["verdict"], "inconclusive")

    async def test_shared_user_context_change_rechecks_previous_verification(self) -> None:
        queue = VerificationQueue(self.ctx, lambda: dict(self.rules))
        queue.start()
        try:
            candidate = self.candidate()
            self.bb.write(BlackboardLayer.VULN, candidate.vuln_id, candidate.model_dump(mode="json"),
                          agent_type=AgentType.WEB_VULN)
            await queue.drain()
            self.assertEqual(len(Fixture.hits), 2)
            world_state.record_user_statement(self.bb, {"kind": "message", "text": "此租户是公开演示"})
            self.verdict = "out_of_rules"
            await queue.drain()
            self.assertEqual(len(Fixture.hits), 4)
            self.assertEqual(self.bb.read(BlackboardLayer.VULN, candidate.vuln_id)["status"], "rejected_compliance")
        finally:
            await queue.close()

    async def test_candidate_immediately_dispatched_with_independent_requests_and_report_gate(self) -> None:
        queue = VerificationQueue(self.ctx, lambda: dict(self.rules))
        queue.start()
        try:
            v = self.candidate()
            self.bb.write(BlackboardLayer.VULN, v.vuln_id, v.model_dump(mode="json"), agent_type=AgentType.WEB_VULN)
            await asyncio.wait_for(queue.drain(), 5)
            found = Vulnerability.model_validate(self.bb.read(BlackboardLayer.VULN, v.vuln_id))
            self.assertEqual(Fixture.hits, ["/", "/record/2"])
            self.assertEqual(found.status, VulnStatus.ACCEPTED)
            self.assertTrue(found.poc_verified)
            self.assertEqual(found.verification["rule_quote"], RULE)
            self.assertEqual(len(ReportAgent(self.bb).collect()["pool"]), 1)
            self.assertEqual(queue.stats()["accepted"], 1)
            from spiritscout.core.models import Authorization

            report = ReportAgent(self.bb).generate(Authorization(auth_id="fixture", mode=RunMode.SRC,
                human_confirmed=True, scope=Scope(allowed_urls=[self.url])), self.url, Path(self.tmp.name) / "report.md")
            text = report.read_text()
            self.assertIn("鉴真独立复测确认", text)
            self.assertIn(RULE, text)
            self.assertIn("other-user-record:fixture-only", text)
            self.assertTrue(any(row["type"] == "verification.queued" for row in self.audit.records))
        finally:
            await queue.close()

    async def test_false_positive_never_enters_report_or_shared_finding_pool(self) -> None:
        self.verdict = "false_positive"
        queue = VerificationQueue(self.ctx, lambda: dict(self.rules))
        queue.start()
        try:
            v = self.candidate()
            self.bb.write(BlackboardLayer.VULN, v.vuln_id, v.model_dump(mode="json"), agent_type=AgentType.WEB_VULN)
            await asyncio.wait_for(queue.drain(), 5)
            found = self.bb.read(BlackboardLayer.VULN, v.vuln_id)
            self.assertEqual(found["status"], "rejected_false_positive")
            self.assertFalse(found["poc_verified"])
            self.assertEqual(ReportAgent(self.bb).collect()["pool"], [])
            store = ResourceStore(Path(self.tmp.name) / "assets")
            store.observe(record_asset("vuln", {**found, "status": "accepted"}), "previous", 1)
            store.observe(record_asset("vuln", found), "retested", 2)
            self.assertEqual(store.assets()["total"], 0)
            # 重复探测候选不能覆盖已完成的独立误报裁决。
            self.assertEqual(ConvergenceEngine._pick_better_vuln(Vulnerability.model_validate(found), v).status,
                             VulnStatus.REJECTED_FP)
        finally:
            await queue.close()

    async def test_approval_rejection_is_inconclusive_and_does_not_execute_http(self) -> None:
        async def denied(messages, **kwargs):
            if not any(m["role"] == "tool" for m in messages):
                return ChatResponse(cost_usd=0, latency_ms=0, content="", model="fixture", prompt_tokens=1, completion_tokens=1, tool_calls=[
                    {"id": "danger", "name": "http_probe", "arguments": {
                        "url": self.url, "method": "DELETE", "action": "delete_files"}}])
            return ChatResponse(cost_usd=0, latency_ms=0, content=json.dumps({"verdict": "false_positive", "reason": "被拒绝",
                "rule_quote": RULE}), model="fixture", prompt_tokens=1, completion_tokens=1)
        self.gateway.chat.side_effect = denied
        result = await VerifierAgent(self.ctx).review(self.candidate(), dict(self.rules))
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertEqual(Fixture.hits, [])

    async def test_model_error_is_inconclusive_and_pipeline_does_not_accept(self) -> None:
        self.gateway.chat.side_effect = TimeoutError("fixture timeout")
        queue = VerificationQueue(self.ctx, lambda: dict(self.rules))
        queue.start()
        try:
            v = self.candidate()
            self.bb.write(BlackboardLayer.VULN, v.vuln_id, v.model_dump(mode="json"), agent_type=AgentType.WEB_VULN)
            await asyncio.wait_for(queue.drain(), 5)
            self.assertEqual(self.bb.read(BlackboardLayer.VULN, v.vuln_id)["status"], "inconclusive")
            self.assertEqual(ReportAgent(self.bb).collect()["pool"], [])
        finally:
            await queue.close()

    async def test_native_verdict_rejects_decorated_quote_then_accepts_exact_proof(self) -> None:
        original = self.reply
        attempts = 0
        async def native(messages, **kwargs):
            nonlocal attempts
            if not any(m["role"] == "tool" for m in messages):
                return await original(messages, **kwargs)
            # 原始脚本只读取真实 HTTP 工具结果，不把判定反馈误当 HTTP 响应。
            http_messages = [m for m in messages if m.get("role") != "tool" or
                             "execution_id" in json.loads(m["content"])]
            response = await original(http_messages, **kwargs)
            verdict = json.loads(response.content)
            if attempts == 0:
                verdict["evidence"][0]["quote"] += "（这里是解释，不能算原文）"
            attempts += 1
            return ChatResponse(cost_usd=0, latency_ms=0, content="", model="fixture",
                prompt_tokens=1, completion_tokens=1, tool_calls=[{"id": f"finish-{attempts}",
                    "name": "finish_verification", "arguments": verdict}])
        self.gateway.chat.side_effect = native
        result = await VerifierAgent(self.ctx).review(self.candidate(), dict(self.rules))
        self.assertEqual(result["verdict"], "confirmed")
        self.assertEqual(attempts, 2)
        self.assertEqual(Fixture.hits, ["/", "/record/2"])

    async def test_changed_user_rules_rechecks_previous_acceptance(self) -> None:
        queue = VerificationQueue(self.ctx, lambda: dict(self.rules))
        queue.start()
        try:
            v = self.candidate()
            self.bb.write(BlackboardLayer.VULN, v.vuln_id, v.model_dump(mode="json"), agent_type=AgentType.WEB_VULN)
            await queue.drain()
            self.rules["standing_rules"] = "新增规则：此测试账户属于公开演示，不作为越权危害。"
            self.verdict = "out_of_rules"
            await queue.drain()
            self.assertEqual(self.bb.read(BlackboardLayer.VULN, v.vuln_id)["status"], "rejected_compliance")
            self.assertEqual(len(Fixture.hits), 4)
            self.assertEqual(ReportAgent(self.bb).collect()["pool"], [])
        finally:
            await queue.close()

    async def test_cancellation_saves_inconclusive_instead_of_confirmed(self) -> None:
        entered = asyncio.Event()
        async def wait_model(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.gateway.chat.side_effect = wait_model
        queue = VerificationQueue(self.ctx, lambda: dict(self.rules))
        queue.start()
        v = self.candidate()
        self.bb.write(BlackboardLayer.VULN, v.vuln_id, v.model_dump(mode="json"), agent_type=AgentType.WEB_VULN)
        await asyncio.wait_for(entered.wait(), 2)
        await queue.close()
        self.assertEqual(self.bb.read(BlackboardLayer.VULN, v.vuln_id)["status"], "inconclusive")
        self.assertFalse(ReportAgent(self.bb).collect()["pool"])

    def test_first_page_and_paginated_api_preserve_verification_rule(self) -> None:
        from fastapi.testclient import TestClient

        from spiritscout.config.settings import SystemConfig, WorkspaceConfig
        from spiritscout.web.app import create_app
        from spiritscout.web.sessions import Session

        root = Path(self.tmp.name)
        app = create_app(SystemConfig(workspace=WorkspaceConfig(root=str(root)),
            models_config_path=str(root / "models.yaml"), agent_models_path=str(root / "agents.yaml")), [])
        v = self.candidate()
        v.verification = {"verdict": "inconclusive", "reason": "等待对照", "rule_quote": RULE}
        self.bb.write(BlackboardLayer.VULN, v.vuln_id, v.model_dump(mode="json"))
        app.state.manager._sessions["fixture"] = Session(session_id="fixture", mode="src",
            target=self.url, workspace=root, status="completed")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            state = client.get("/api/sessions/fixture/state").json()
            page = client.get("/api/sessions/fixture/vulnerabilities").json()
        self.assertEqual(state["vulns"]["items"][0]["verification"]["rule_quote"], RULE)
        self.assertEqual(page["items"][0]["verification"]["rule_quote"], RULE)

    def test_fabricated_evidence_or_rule_cannot_confirm(self) -> None:
        raw = {"verdict": "confirmed", "harm": "claimed", "rule_quote": RULE,
               "evidence": [{"execution_id": "fake", "quote": "claimed"}], "controls": []}
        self.assertEqual(VerifierAgent.validate(raw, {}, dict(self.rules))["verdict"], "inconclusive")
        accepted = self.candidate().model_copy(update={"status": VulnStatus.ACCEPTED, "poc_verified": True})
        self.bb.write(BlackboardLayer.TOPOLOGY, "verification-policy", {"independent_retest_required": True})
        self.bb.write(BlackboardLayer.VULN, "forged", accepted.model_dump(mode="json"))
        self.assertEqual(ReportAgent(self.bb).collect()["pool"], [])
