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
from spiritscout.agents.report.vulnerability import (
    cvss31_score,
    validate_assessment,
    validate_cvss,
    validate_report,
    verification_summary,
)
from spiritscout.agents.sub.base import SubAgentContext
from spiritscout.agents.verifier import VerificationQueue, VerifierAgent
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.blackboard.hypotheses import resolve as resolve_hypothesis
from spiritscout.blackboard.hypotheses import transition, upsert
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


def delivery(evidence, controls=()):
    """脚本模型撰写交付内容；引用仍由测试中的真实工具执行提供。"""
    refs = [row["execution_id"] for row in [*controls, *evidence]]
    return {
        "severity_assessment": {
            "rationale": "匿名读取了其他用户的非公开资料，建议高危",
            "attack_conditions": "可访问目标接口", "privileges_required": "无需认证",
            "user_interaction": "无需交互", "impact_scope": "本次仅验证单条资料，未验证批量影响",
            "platform_basis": "依据用户明确的资料泄露收录规则，平台最终评级待人工确认",
        },
        "report": {
            "summary": "独立请求确认读取他人资料", "prerequisites": "使用隔离测试站点",
            "steps": [{"action": "匿名发送 GET /record/2，并核对返回的测试资料",
                       "expected": "拒绝未授权读取", "observed": "读取到其他用户测试资料",
                       "execution_ids": refs}],
            "impact": "本次单条资料泄露", "limitations": "未验证写入、批量读取或其他租户",
            "remediation": "在该资料接口按当前身份验证对象归属，并增加匿名读取被拒绝的回归用例",
        },
        "cvss_note": "未确认全部技术影响指标，暂未评分",
    }


class VulnerabilityDeliveryTests(unittest.TestCase):
    def test_cvss31_base_scores_and_invalid_vectors(self):
        for suffix, score in (
            ("AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
            ("AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
            ("AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H", 9.9),
            ("AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", 8.8),
            ("AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:H", 8.1),
            ("AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N", 6.5),
            ("AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),
            ("AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:N/A:N", 4.3),
            ("AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
        ):
            with self.subTest(vector=suffix):
                self.assertEqual(cvss31_score("CVSS:3.1/" + suffix), score)
        base = "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N"
        for vector in (base + "/AV:N", base + "/E:X", base.replace("AC:L/", ""),
                       base.replace("AV:N", "AV:Z"), base.replace("3.1", "4.0"), ""):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                cvss31_score(vector)

    def test_cvss_requires_metric_reasons_and_ignores_model_supplied_score(self):
        raw = {"version": "3.1", "vector": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N",
               "score": 10, "metric_rationale": {key: "本次已验证的指标依据" for key in
                    ("AV", "AC", "PR", "UI", "S", "C", "I", "A")}}
        self.assertEqual(validate_cvss(raw)["score"], 6.5)
        missing = {**raw, "metric_rationale": {"AV": "远程接口"}}
        self.assertEqual(validate_cvss(missing)["status"], "invalid")
        self.assertNotIn("score", validate_cvss(missing))
        self.assertEqual(validate_cvss({**raw, "version": "4.0"})["status"], "invalid")
        self.assertEqual(validate_cvss(None, "业务损失不能直接推出技术影响")["note"], "业务损失不能直接推出技术影响")

    def test_missing_grading_basis_stays_pending_not_low_or_critical(self):
        for level in (None, "critical", "invalid", ["high"]):
            with self.subTest(level=level):
                self.assertEqual(validate_assessment(None, level)["status"], "pending")
        fields = delivery([])["severity_assessment"]
        self.assertEqual(validate_assessment(fields, "high")["status"], "assessed")
        self.assertEqual(validate_assessment({**fields, "impact_scope": ""}, "high")["status"], "pending")

    def test_report_requires_real_references_and_retains_model_wording(self):
        raw = delivery([{"execution_id": "observed"}], [{"execution_id": "control"}])["report"]
        raw["remediation"] = "模型针对当前入口写的建议" * 200
        valid = validate_report(raw, {"control", "observed"}, {"observed"}, model="actual-model")
        self.assertEqual(valid["source"], "model")
        self.assertEqual(valid["model"], "actual-model")
        self.assertEqual(valid["remediation"], raw["remediation"])
        for refs in (["forged"], ["control"], []):
            forged = {**raw, "source": "model", "model": "spoofed-model",
                      "steps": [{**raw["steps"][0], "execution_ids": refs}]}
            with self.subTest(refs=refs):
                self.assertEqual(validate_report(forged, {"control", "observed"}, {"observed"})["status"], "invalid")
        self.assertEqual(validate_report({**raw, "remediation": ""}, {"control", "observed"}, {"observed"})["status"], "invalid")
        self.assertEqual(validate_report(None, set(), set())["status"], "unavailable")

    def test_rating_and_cvss_do_not_change_evidence_verdict_or_each_other(self):
        evidence = [{"execution_id": "real", "quote": "verified-output"}]
        raw = {"verdict": "confirmed", "reason": "独立证据", "harm": "资料泄露", "severity": "high",
               "rule_quote": RULE, "proof_rationale": "独立匿名响应足够验证资料泄露", "evidence": evidence,
               "impact_proof": {"evidence_ref": "real", "impact": "单条资料泄露"}, **delivery(evidence)}
        executions = {"real": {"ok": True, "tool": "browser", "text": "verified-output"}}
        candidate = Vulnerability(vuln_id="one", title="泄露", target="test", vuln_type="info_leak")
        valid = VerifierAgent.validate(raw, executions, {"mission": RULE}, candidate, model="actual-model", src=True)
        self.assertEqual(valid["verdict"], "confirmed")
        self.assertEqual(valid["severity"], "high")
        self.assertTrue(valid["submission_ready"], "CVSS 可选，不阻止已有完整证据与报告的提交准备")
        missing = VerifierAgent.validate({**raw, "severity_assessment": {}}, executions,
                                       {"mission": RULE}, candidate, model="actual-model", src=True)
        self.assertEqual(missing["verdict"], "confirmed")
        self.assertEqual(missing["severity"], "info")
        self.assertEqual(missing["severity_assessment"]["status"], "pending")
        self.assertFalse(missing["submission_ready"])
        bad_cvss = VerifierAgent.validate({**raw, "cvss": {"version": "4.0"}}, executions,
                                        {"mission": RULE}, candidate, model="actual-model", src=True)
        self.assertEqual(bad_cvss["verdict"], "confirmed")
        self.assertTrue(bad_cvss["submission_ready"])
        self.assertEqual(bad_cvss["cvss"]["status"], "invalid")
        forged = VerifierAgent.validate(raw, {}, {"mission": RULE}, candidate, model="actual-model", src=True)
        self.assertEqual(forged["verdict"], "inconclusive", "模型报告不能取代独立证据")
        self.assertFalse(forged["submission_ready"])

    def test_polling_summary_excludes_report_and_long_scoring_rationale(self):
        raw = {"verdict": "confirmed", "reason": "结论", **delivery([{"execution_id": "one"}]),
               "cvss": {"status": "assessed", "version": "3.1", "score": 6.5,
                        "vector": "CVSS:3.1", "metric_rationale": {"AV": "长指标理由" * 100}}}
        summary = verification_summary(raw)
        self.assertNotIn("report", summary)
        self.assertEqual(summary["cvss"], {"status": "assessed", "version": "3.1", "score": 6.5})
        self.assertEqual(summary["severity_assessment"], {"status": None})
        self.assertEqual(summary["reason"], raw["reason"])



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
    def test_history_lookup_cannot_supply_independent_verification_or_control(self):
        result = VerifierAgent.validate({
            "verdict": "confirmed", "reason": "旧档案声称有漏洞", "harm": "资料泄露",
            "rule_quote": RULE, "proof_rationale": "历史返回了敏感内容",
            "evidence": [{"execution_id": "history", "quote": "archived-content"}],
            "impact_proof": {"evidence_ref": "history", "impact": "归档内容"},
        }, {"history": {"ok": True, "tool": "history_lookup", "text": "archived-content"}}, dict(self.rules))
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertEqual(result["evidence"], [])
        self.assertFalse(result["submission_ready"])
        self.assertEqual(result["impact_proof"], {})

    async def test_independent_disclosure_proof_can_finish_without_forced_control_or_extra_user_rule(self):
        async def respond(messages, **kwargs):
            results = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
            if not results:
                calls = [{"id": "independent", "name": "http_probe", "arguments": {"url": self.url + "/record/2"}}]
            else:
                calls = [{"id": "finish", "name": "finish_verification", "arguments": {
                    "verdict": "confirmed", "reason": "独立取得未授权内容", "harm": "资料泄露",
                    "rule_assessment": "目标属于用户要求检查的站点，未授权内容泄露符合任务目标",
                    "proof_rationale": "匿名独立请求已取得非公开内容，本次内容本身可核对，无需额外错误页对照",
                    "evidence": [{"execution_id": results[0]["execution_id"], "quote": "other-user-record:fixture-only"}],
                    **delivery([{"execution_id": results[0]["execution_id"]}])}}]
            return ChatResponse(cost_usd=0, latency_ms=0, content="", model="fixture",
                                prompt_tokens=1, completion_tokens=1, tool_calls=calls)

        self.gateway.chat.side_effect = respond
        result = await VerifierAgent(self.ctx).review(self.candidate(), {"mission": "检查该站点的信息泄露风险"})
        self.assertEqual(result["verdict"], "confirmed")
        self.assertEqual(result["controls"], [])
        self.assertEqual(Fixture.hits, ["/record/2"])
        self.assertEqual(self.gateway.chat.await_count, 2)

    async def test_verification_preserves_public_scene_and_exploration_without_extra_decision(self):
        from spiritscout.gateway.progress import (
            EXPLORATION_PROGRESS_GUIDANCE,
            FORMAT_GUIDANCE,
        )

        async def stream(messages, **kwargs):
            self.assertEqual(messages[-1]['content'], FORMAT_GUIDANCE)
            self.assertIn(EXPLORATION_PROGRESS_GUIDANCE, [m.get('content') for m in messages])
            response = await self.reply(messages, **kwargs)
            if response.tool_calls:
                response.content = json.dumps({
                    'progress': '当前是待核实的越权候选，先独立读取对照与疑似入口。',
                    'exploration': {'intent': '核实候选危害', 'attempt': '独立请求对照', 'state': 'running'}},
                    ensure_ascii=False)
                kwargs['on_delta']('text', response.content)
            return response

        self.gateway.chat.side_effect = stream
        result = await VerifierAgent(self.ctx).review(self.candidate(), dict(self.rules))
        self.assertEqual(result['verdict'], 'confirmed')
        self.assertEqual(self.gateway.chat.await_count, 2)
        first, second = [call.args[0] for call in self.gateway.chat.call_args_list]
        self.assertEqual(second[:len(first)], first)
        self.assertEqual(Fixture.hits, ['/', '/record/2'])
        progress = [e['detail'] for e in self.audit.records if e['type'] == 'agent.progress']
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]['exploration']['intent'], '核实候选危害')
        self.assertEqual((progress[0]['task_id'], progress[0]['turn']), ('verify-candidate', 1))
        self.assertTrue(progress[0]['streamed'])
        self.assertTrue(any(e['type'] == 'agent.delta' and e['detail']['task_id'] == 'verify-candidate'
                            and e['detail']['turn'] == 1 for e in self.audit.records))

    async def test_scanner_candidate_enters_existing_queue_and_retains_source_evidence(self):
        from spiritscout.tools.scan_evidence import submit_candidate
        self.ctx.builtin_ctx['workspace_root'] = self.tmp.name

        hypothesis_ref = upsert(
            self.bb, claim='接口可能允许跨对象读取', subject=self.url + '/record/2',
            test_type='idor', technical_basis={'template_ids': ['fixture-idor']},
            actor='scanner', agent_type=AgentType.SCANNER,
        )['hypothesis_ref']
        transition(
            self.bb, hypothesis_ref, state='supported', reason='扫描模板命中待复核',
            task_id='scan-fixture', outcome='supporting_signal',
            actor='scanner', agent_type=AgentType.SCANNER,
        )
        self.bb.write(BlackboardLayer.RECON_CLUE, 'scan-fixture', {
            'clue_type': 'scanner_finding', 'scanner': 'nuclei', 'value': self.url + '/record/2',
            'template_id': 'fixture-idor', 'severity': 'high', 'hypothesis_ref': hypothesis_ref,
            'scan_evidence': {
                'vulnerability_ids': ['CVE-fixture-only'], 'files': {}, 'missing': ['poc']}},
            agent_type=AgentType.SCANNER, updated_by='scanner')
        queue = VerificationQueue(self.ctx, lambda: dict(self.rules))
        queue.start()
        try:
            out = submit_candidate(self.bb, Path(self.tmp.name), {'finding_id': 'scan-fixture',
                'title': '扫描疑似越权', 'vuln_type': 'idor', 'reason': '命中需要独立对照',
                'status': 'accepted', 'poc_verified': True})
            pending = self.bb.read(BlackboardLayer.VULN, out['vuln_id'])
            self.assertEqual(pending['status'], 'candidate')
            self.assertFalse(pending['poc_verified'])
            self.assertEqual(pending['hypothesis_ref'].split('@')[0], hypothesis_ref.split('@')[0])
            await queue.drain()
            confirmed = self.bb.read(BlackboardLayer.VULN, out['vuln_id'])
            self.assertEqual(confirmed['status'], 'accepted')
            self.assertTrue(confirmed['poc_verified'])
            execution_files = confirmed['verification']['executions'][0]['files']
            self.assertTrue((Path(self.tmp.name)/execution_files['request']).is_file())
            self.assertIn(b'GET / HTTP/1.1', (Path(self.tmp.name)/execution_files['request']).read_bytes())
            self.assertEqual((Path(self.tmp.name)/execution_files['response_body']).read_bytes(),b'public-home')
            self.assertEqual(confirmed['vulnerability_ids'], ['CVE-fixture-only'])
            hypothesis = resolve_hypothesis(self.bb, hypothesis_ref)[1]
            self.assertEqual(hypothesis['state'], 'supported')
            self.assertEqual(hypothesis['attempts'][-1]['outcome'], 'independently_confirmed')
            self.assertEqual(len(Fixture.hits), 2, '复测使用两个真实独立 HTTP 请求')
            duplicate = submit_candidate(self.bb, Path(self.tmp.name), {'finding_id': 'scan-fixture',
                'title': '扫描疑似越权', 'vuln_type': 'idor', 'reason': '同一命中再次交接'})
            self.assertFalse(duplicate['created'])
            self.assertEqual(self.bb.read(BlackboardLayer.VULN, out['vuln_id'])['status'], 'accepted')
        finally:
            await queue.close()

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

    async def test_invalid_report_is_repaired_without_repeating_http_for_both_finish_paths(self):
        original = self.reply
        for native in (False, True):
            with self.subTest(native=native):
                Fixture.hits = []
                finishes = 0
                async def respond(messages, **kwargs):
                    nonlocal finishes
                    http_messages = [m for m in messages if m.get("role") != "tool" or
                                     "execution_id" in json.loads(m["content"])]
                    response = await original(http_messages, **kwargs)
                    if response.tool_calls:
                        return response
                    raw = json.loads(response.content)
                    finishes += 1
                    if finishes == 1:
                        raw["report"]["steps"][0]["execution_ids"] = ["forged"]
                    if native:
                        response.content = ""
                        response.tool_calls = [{"id": f"finish-{finishes}", "name": "finish_verification", "arguments": raw}]
                    else:
                        response.content = json.dumps(raw)
                    return response
                self.gateway.chat = AsyncMock(side_effect=respond)
                final = await asyncio.wait_for(VerifierAgent(self.ctx).review(self.candidate(), dict(self.rules)), 5)
                self.assertEqual(final["report"]["status"], "available")
                self.assertEqual(final["report"]["model"], "fixture")
                self.assertEqual(final["verdict"], "confirmed")
                self.assertEqual(self.gateway.chat.await_count, 3)
                self.assertEqual(Fixture.hits, ["/", "/record/2"])

    async def test_cvss_persists_calculated_score_and_model_report_in_existing_queue(self):
        original = self.reply
        async def respond(messages, **kwargs):
            response = await original(messages, **kwargs)
            if not response.tool_calls:
                raw = json.loads(response.content)
                raw["cvss"] = {"version": "3.1", "vector": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N",
                    "score": 10, "metric_rationale": {key: "脚本模型的测试指标依据" for key in ("AV","AC","PR","UI","S","C","I","A")}}
                raw["impact_proof"] = {"evidence_ref": raw["evidence"][0]["execution_id"], "impact": "单条测试资料泄露"}
                response.content = json.dumps(raw)
            return response
        self.gateway.chat.side_effect = respond
        candidate = self.candidate().model_copy(update={"vuln_type": "info_leak"})
        queue = VerificationQueue(self.ctx, lambda: dict(self.rules))
        queue.start()
        try:
            self.bb.write(BlackboardLayer.VULN, candidate.vuln_id, candidate.model_dump(mode="json"), agent_type=AgentType.WEB_VULN)
            await asyncio.wait_for(queue.drain(), 5)
            stored = Vulnerability.model_validate(self.bb.read(BlackboardLayer.VULN, candidate.vuln_id))
            self.assertEqual(stored.cvss_score, 6.5, "不采用模型自报的 10 分，也不由 CVSS 强制改危害等级")
            self.assertEqual(stored.severity, Severity.HIGH)
            self.assertEqual(stored.cvss_version, "3.1")
            self.assertEqual(stored.cvss_vector, stored.verification["cvss"]["vector"])
            self.assertTrue(stored.submission_ready)
            from spiritscout.web.deliverables import read_vulnerability_detail
            reopened = read_vulnerability_detail(Path(self.tmp.name), candidate.vuln_id, report=True)["report"]
            self.assertEqual(reopened["model"], "fixture")
            self.assertEqual(reopened["severity"]["level"], "high")
            self.assertEqual(reopened["cvss"]["score"], 6.5)
            self.assertEqual(self.gateway.chat.await_count, 2)
            self.assertEqual(Fixture.hits, ["/", "/record/2"])
        finally:
            await queue.close()

    async def test_non_src_verification_keeps_existing_tool_contract_and_no_report_requirement(self):
        self.ctx.mode = RunMode.COMPETITION
        original = self.reply
        async def respond(messages, **kwargs):
            finish = next(row for row in kwargs["tools"] if row["function"]["name"] == "finish_verification")
            self.assertNotIn("report", finish["function"]["parameters"]["properties"])
            self.assertNotIn("cvss", finish["function"]["parameters"]["properties"])
            response = await original(messages, **kwargs)
            if not response.tool_calls:
                raw = json.loads(response.content)
                raw.pop("report")
                raw.pop("severity_assessment")
                response.content = json.dumps(raw)
            return response
        self.gateway.chat.side_effect = respond
        result = await VerifierAgent(self.ctx).review(self.candidate(), dict(self.rules))
        self.assertEqual(result["verdict"], "confirmed")
        self.assertEqual(result["severity"], "high")
        self.assertNotIn("report", result)
        self.assertEqual(self.gateway.chat.await_count, 2)
        self.assertEqual(Fixture.hits, ["/", "/record/2"])

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
        if self.verdict == "confirmed":
            raw.update(delivery(raw["evidence"], raw["controls"]))
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
            fragment = next(m["content"] for m in reversed(messages)
                            if str(m.get("content", "")).startswith("<world_state"))
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
            self.assertEqual(found.verification["report"]["source"], "model")
            self.assertEqual(found.verification["report"]["model"], "fixture")
            self.assertEqual(found.verification["report"]["remediation"], delivery([])["report"]["remediation"])
            self.assertEqual(found.severity, Severity.HIGH)
            self.assertEqual(found.verification["severity_assessment"]["status"], "assessed")
            self.assertEqual(self.gateway.chat.await_count, 2, "报告与裁决同次输出，不额外调用模型")
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
            hypothesis_ref = upsert(
                self.bb, claim='接口可能允许跨对象读取', subject=self.url + '/record/2',
                test_type='idor', actor='web-vuln', agent_type=AgentType.WEB_VULN,
            )['hypothesis_ref']
            transition(
                self.bb, hypothesis_ref, state='supported', reason='专项角色观察到支持信号',
                task_id='web-candidate', outcome='supporting_signal',
                actor='web-vuln', agent_type=AgentType.WEB_VULN,
            )
            v = self.candidate().model_copy(update={'hypothesis_ref': hypothesis_ref})
            self.bb.write(BlackboardLayer.VULN, v.vuln_id, v.model_dump(mode="json"), agent_type=AgentType.WEB_VULN)
            await asyncio.wait_for(queue.drain(), 5)
            found = self.bb.read(BlackboardLayer.VULN, v.vuln_id)
            self.assertEqual(found["status"], "rejected_false_positive")
            self.assertFalse(found["poc_verified"])
            hypothesis = resolve_hypothesis(self.bb, hypothesis_ref)[1]
            self.assertEqual(hypothesis['state'], 'refuted')
            self.assertEqual(hypothesis['attempts'][-1]['outcome'], 'independently_refuted')
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
        v = self.candidate().model_copy(update={"cvss_score": 9.8, "cvss_version": "3.1",
            "cvss_vector": "old-vector", "submission_ready": True, "impact_proof": {"impact": "旧证明"}})
        self.bb.write(BlackboardLayer.VULN, v.vuln_id, v.model_dump(mode="json"), agent_type=AgentType.WEB_VULN)
        await asyncio.wait_for(entered.wait(), 2)
        await queue.close()
        cancelled = self.bb.read(BlackboardLayer.VULN, v.vuln_id)
        self.assertEqual(cancelled["status"], "inconclusive")
        self.assertIsNone(cancelled["cvss_score"])
        self.assertFalse(cancelled["submission_ready"])
        self.assertEqual(cancelled["impact_proof"], {})
        self.assertFalse(ReportAgent(self.bb).collect()["pool"])

    def test_first_page_and_paginated_api_preserve_verification_rule_and_hypotheses(self) -> None:
        from fastapi.testclient import TestClient

        from spiritscout.config.settings import SystemConfig, WorkspaceConfig
        from spiritscout.web.app import create_app
        from spiritscout.web.sessions import Session

        root = Path(self.tmp.name)
        app = create_app(SystemConfig(workspace=WorkspaceConfig(root=str(root)),
            models_config_path=str(root / "models.yaml"), agent_models_path=str(root / "agents.yaml")), [])
        primary = "topology:hypothesis-primary@3"
        related = "topology:hypothesis-related@2"
        v = self.candidate().model_copy(update={
            "hypothesis_ref": primary, "hypothesis_refs": [primary, related]})
        v.verification = {"verdict": "inconclusive", "reason": "等待对照", "rule_quote": RULE,
            "report": {"summary": "不要在轮询或分页中传递长报告" * 100},
            "severity_assessment": {"status": "pending", "rationale": "不要在列表中传递长定级理由" * 100},
            "cvss": {"status": "invalid", "note": "缺少指标", "metric_rationale": {"AV": "长理由" * 100}}}
        self.bb.write(BlackboardLayer.VULN, v.vuln_id, v.model_dump(mode="json"))
        app.state.manager._sessions["fixture"] = Session(session_id="fixture", mode="src",
            target=self.url, workspace=root, status="completed")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            state = client.get("/api/sessions/fixture/state").json()
            page = client.get("/api/sessions/fixture/vulnerabilities").json()
        self.assertEqual(state["vulns"]["items"][0]["verification"]["rule_quote"], RULE)
        self.assertEqual(page["items"][0]["verification"]["rule_quote"], RULE)
        self.assertEqual(state["vulns"]["items"][0]["hypothesis_ref"], primary)
        self.assertEqual(state["vulns"]["items"][0]["hypothesis_refs"], [primary, related])
        self.assertEqual(page["items"][0]["hypothesis_ref"], primary)
        self.assertEqual(page["items"][0]["hypothesis_refs"], [primary, related])
        for item in (state["vulns"]["items"][0], page["items"][0]):
            self.assertNotIn("report", item["verification"])
            self.assertEqual(item["verification"]["severity_assessment"], {"status": "pending"})
            self.assertNotIn("metric_rationale", item["verification"]["cvss"])
        self.assertEqual(state["vulns"]["items"][0]["verification"], page["items"][0]["verification"])

    def test_fabricated_evidence_or_rule_cannot_confirm(self) -> None:
        raw = {"verdict": "confirmed", "harm": "claimed", "rule_quote": RULE,
               "evidence": [{"execution_id": "fake", "quote": "claimed"}], "controls": []}
        self.assertEqual(VerifierAgent.validate(raw, {}, dict(self.rules))["verdict"], "inconclusive")
        accepted = self.candidate().model_copy(update={"status": VulnStatus.ACCEPTED, "poc_verified": True})
        self.bb.write(BlackboardLayer.TOPOLOGY, "verification-policy", {"independent_retest_required": True})
        self.bb.write(BlackboardLayer.VULN, "forged", accepted.model_dump(mode="json"))
        self.assertEqual(ReportAgent(self.bb).collect()["pool"], [])
