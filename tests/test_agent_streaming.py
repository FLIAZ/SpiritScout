"""角色流式输出的审计节流、并发归属与既有回调兼容。"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from spiritscout.agents.sub.base import StepResult, SubAgent
from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.hypotheses import resolve as resolve_hypothesis
from spiritscout.blackboard.hypotheses import upsert
from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    RouteState,
    RunMode,
    Task,
    TaskStatus,
)
from spiritscout.ctf.agents import _DeltaEmitter
from spiritscout.gateway import ModelGateway, ModelRegistry, chat_decision
from spiritscout.gateway.backends import MockBackend
from spiritscout.gateway.streaming import AuditStream
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope
from spiritscout.tools.spec import ToolResult


class AgentStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_dependencies_gate_model_entry_in_all_modes(self) -> None:
        from spiritscout.policy.questions import QuestionBroker

        for mode in ("src", "ctf", "competition"):
            gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]))
            gateway.set_mode(mode)
            broker = QuestionBroker(interactive=mode != "competition")
            gateway.questions = broker
            ask = asyncio.create_task(broker.ask("master", "fixture", [], blocking_task_ids=["blocked"]))
            await asyncio.sleep(0)
            token = current_execution_scope.set(ExecutionScope(agent_id="worker", task_id="blocked"))
            try:
                with patch.object(gateway._mock, "complete", wraps=gateway._mock.complete) as complete:
                    request = asyncio.create_task(gateway.chat([{"role": "user", "content": "{}"}]))
                    try:
                        await asyncio.sleep(0)
                        if mode != "competition":
                            complete.assert_not_awaited()
                            self.assertFalse(request.done())
                            broker._resolve(broker.view()[0]["question_id"], {"answer": "fixture"})
                        await asyncio.wait_for(request, 1)
                        complete.assert_awaited_once()
                    finally:
                        if not request.done():
                            request.cancel()
                            await asyncio.gather(request, return_exceptions=True)
                        broker.cancel_all()
                        await ask
            finally:
                current_execution_scope.reset(token)

    async def test_stream_content_refreshes_idle_timeout_without_changing_request(self) -> None:
        from spiritscout.config.settings import GatewayConfig
        from spiritscout.core.exceptions import SpiritScoutError
        from spiritscout.gateway.backends import emit_delta

        for kind in ("reasoning", "text", "tool_call"):
            for silent_end in (False, True):
                with self.subTest(kind=kind, silent_end=silent_end):
                    audit = NullAuditLogger()
                    gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                        audit=audit, config=GatewayConfig(max_retries=0))
                    received = []
                    cancelled = asyncio.Event()
                    request = []

                    async def complete(entry, messages, **options):
                        request.append((messages, options["timeout"]))
                        try:
                            for index in range(8):
                                await asyncio.sleep(.02)
                                await emit_delta(options["on_delta"], kind, str(index))
                            if silent_end:
                                # 空增量和本地重置均不能把静默请求无限延长。
                                while True:
                                    await emit_delta(options["on_delta"], "reset", "")
                                    await emit_delta(options["on_delta"], kind, "")
                                    await asyncio.sleep(.02)
                            return {"content": "done", "prompt_tokens": 10, "completion_tokens": 5}
                        finally:
                            cancelled.set()

                    with patch.object(gateway._mock, "complete", side_effect=complete):
                        call = gateway.chat([{"role": "user", "content": "fixture"}], timeout_seconds=.1,
                            on_delta=lambda kind, text: received.append((kind, text)))
                        if silent_end:
                            with self.assertRaisesRegex(SpiritScoutError, "没有新输出"):
                                await asyncio.wait_for(call, 2)
                        else:
                            result = await asyncio.wait_for(call, 2)
                            self.assertEqual(result.content, "done")
                            self.assertEqual((result.prompt_tokens, result.completion_tokens), (10, 5))
                    self.assertTrue(cancelled.is_set())
                    self.assertEqual(request, [([{"role": "user", "content": "fixture"}], .1)])
                    self.assertEqual([text for emitted, text in received if emitted == kind and text],
                                     [str(index) for index in range(8)])

    async def test_gateway_deadline_cancels_backend_and_stops_feedback(self) -> None:
        from spiritscout.config.settings import GatewayConfig
        from spiritscout.core.exceptions import SpiritScoutError

        for actor in ("", "master-test"):
            with self.subTest(actor=actor):
                audit = NullAuditLogger()
                gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                                       audit=audit, config=GatewayConfig(max_retries=0))
                cancelled = asyncio.Event()

                async def complete(*args, **kwargs):
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                token = current_execution_scope.set(ExecutionScope(agent_id=actor))
                try:
                    with patch.object(gateway._mock, "complete", side_effect=complete), \
                            patch("spiritscout.gateway.streaming.ModelWait.INTERVAL", .005):
                        with self.assertRaisesRegex(SpiritScoutError, "超过 0.02 秒没有新输出"):
                            await asyncio.wait_for(gateway.chat([], timeout_seconds=.02), 1)
                    self.assertTrue(cancelled.is_set())
                    self.assertEqual(audit.records[-1]["type"], "model.failed")
                    count = len(audit.records)
                    await asyncio.sleep(.02)
                    self.assertEqual(len(audit.records), count)
                finally:
                    current_execution_scope.reset(token)

    async def test_decision_actor_streams_before_completion_without_changing_request(self) -> None:
        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                               audit=audit)
        started, finish = asyncio.Event(), asyncio.Event()
        requests = []

        async def complete(entry, messages, **options):
            callback = options.pop("on_delta")
            requests.append((messages, options))
            if callback is not None:
                callback("reasoning", "核对授权范围" * 50)
                started.set()
                await finish.wait()
            return {"content": '{"dag_nodes":[]}', "prompt_tokens": 10,
                    "completion_tokens": 5}

        scope = ExecutionScope(task_id="task", route_id="route", identity_id="identity")
        token = current_execution_scope.set(scope)
        try:
            with patch.object(gateway._mock, "complete", side_effect=complete):
                options = dict(response_format="json", task_type="meta_planning")
                baseline = await chat_decision(gateway, [], **options)
                task = asyncio.create_task(chat_decision(
                    gateway, [], audit=audit, actor="master-test", **options))
                try:
                    await asyncio.wait_for(started.wait(), 1)
                    self.assertFalse(task.done())
                    delta = next(r for r in audit.records if r["type"] == "agent.delta")
                    self.assertEqual(delta["actor"], "master-test")
                    self.assertEqual(delta["detail"]["task_id"], "task")
                    self.assertEqual(delta["detail"]["route"], "route")
                finally:
                    finish.set()
                    result = await task
                self.assertEqual(result.content, baseline.content)
                self.assertEqual(requests[0], requests[1])
                self.assertIs(current_execution_scope.get(), scope)
        finally:
            current_execution_scope.reset(token)

    async def test_cancelled_decision_restores_execution_scope(self) -> None:
        gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]))
        scope = ExecutionScope(agent_id="parent", identity_id="identity")

        async def complete(*args, **kwargs):
            self.assertEqual(current_execution_scope.get().agent_id, "master-test")
            self.assertEqual(current_execution_scope.get().identity_id, "identity")
            raise asyncio.CancelledError

        token = current_execution_scope.set(scope)
        try:
            with patch.object(gateway._mock, "complete", side_effect=complete):
                with self.assertRaises(asyncio.CancelledError):
                    await chat_decision(gateway, [], actor="master-test", response_format="json")
            self.assertIs(current_execution_scope.get(), scope)
        finally:
            current_execution_scope.reset(token)

    async def test_cancelled_nonstream_call_stops_wait_feedback(self) -> None:
        from spiritscout.config.settings import GatewayConfig

        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([ModelEntry(model='fixture', provider='mock')]),
                               GatewayConfig(max_retries=0), audit=audit)
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch('spiritscout.gateway.streaming.ModelWait.INTERVAL', .005), \
                patch.object(gateway._mock, 'complete', side_effect=blocked):
            task = asyncio.create_task(gateway.chat([{'role': 'user', 'content': 'fixture'}]))
            await started.wait()
            await asyncio.sleep(.02)
            self.assertTrue(any(r['type'] == 'model.waiting' for r in audit.records))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            count = len(audit.records)
            await asyncio.sleep(.02)
            self.assertEqual(len(audit.records), count)
            self.assertEqual(audit.records[-1]['type'], 'model.cancelled')

    async def test_small_chunk_flushes_while_model_is_silent(self) -> None:
        audit = NullAuditLogger()
        delivered = asyncio.Event()
        original = audit.log

        def log(*args):
            result = original(*args)
            delivered.set()
            return result

        audit.log = log
        sink = AuditStream(audit, "agent", {"call_id": "call"})
        sink("reasoning", "小片段")
        try:
            await asyncio.wait_for(delivered.wait(), timeout=1)
            self.assertEqual(audit.records[0]["detail"]["text"], "小片段")
            sink.flush()
            self.assertEqual(len(audit.records), 1)
        finally:
            sink.flush()

    async def test_reset_and_final_flush_cancel_pending_timer(self) -> None:
        audit = NullAuditLogger()
        sink = AuditStream(audit, "agent", {"call_id": "call"})
        sink("reasoning", "旧片段")
        timer = sink._timer
        sink("reset", "")
        self.assertTrue(timer.cancelled())
        sink("reasoning", "新片段")
        timer = sink._timer
        sink.flush()
        self.assertTrue(timer.cancelled())
        self.assertEqual([r["detail"]["text"] for r in audit.records], ["", "新片段"])

    def test_step_findings_survive_route_and_task_summary(self) -> None:
        agent = SimpleNamespace(
            agent_id="web-a", agent_type=AgentType.WEB_VULN,
            cache=SimpleNamespace(stats=lambda: {}),
            health_mgr=SimpleNamespace(evaluate=lambda route: None),
        )
        route = RouteState(route_id="r1", task_id="t1", name="权限边界")
        finding = {"type": "idor_signal", "evidence_ref": "invocation-1"}

        SubAgent._update_route(agent, route, StepResult(
            ok=True, value=0.5, findings=[finding], note="观察到跨对象响应差异",
        ), total_steps=1)
        task = Task(
            task_id="t1", task_type="web_vuln", target="https://a.test/api/user",
            agent_type=AgentType.WEB_VULN, status=TaskStatus.DONE,
        )
        summary = SubAgent._summarize(agent, task, [route])

        self.assertEqual(route.findings, [finding])
        self.assertEqual(summary["findings"], [finding])

    def test_task_summary_exposes_adaptive_pacing_from_detection_signals(self) -> None:
        agent = SimpleNamespace(
            agent_id="web-a", agent_type=AgentType.WEB_VULN,
            cache=SimpleNamespace(stats=lambda: {}),
        )
        task = Task(
            task_id="t-paced", task_type="web_vuln", target="https://a.test",
            agent_type=AgentType.WEB_VULN, status=TaskStatus.DONE,
        )
        route = RouteState(
            route_id="r-paced", task_id=task.task_id, name="边界测试",
            status=TaskStatus.DONE, tool_calls=3, detection_signals=2,
        )

        summary = SubAgent._summarize(agent, task, [route])

        self.assertEqual(summary["execution_feedback"]["tool_calls"], 3)
        self.assertEqual(summary["execution_feedback"]["detection_signals"], 2)
        self.assertEqual(summary["execution_feedback"]["adaptive_pacing"], "slow_down")

    def test_adaptive_delay_only_activates_after_detection_signal(self) -> None:
        quiet = RouteState(route_id="quiet", task_id="t", name="quiet")
        blocked = RouteState(route_id="blocked", task_id="t", name="blocked",
                             detection_signals=2, step_times=[1.0] * 6)

        self.assertEqual(SubAgent._adaptive_delay([quiet]), 0.0)
        self.assertGreater(SubAgent._adaptive_delay([blocked]), 0.0)
        self.assertLessEqual(SubAgent._adaptive_delay([blocked]), 1.5)

    def test_task_outcome_settles_linked_hypothesis_without_claiming_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            audit = NullAuditLogger()
            agent = SimpleNamespace(
                agent_id="web-a", agent_type=AgentType.WEB_VULN,
                ctx=SimpleNamespace(blackboard=bb, audit=audit),
            )

            def hypothesis(claim: str) -> str:
                return upsert(
                    bb, claim=claim, subject="https://a.test/api/user", test_type="idor",
                    actor="js-reverse", agent_type=AgentType.JS_REVERSE,
                )["hypothesis_ref"]

            supported = hypothesis("对象标识变化可能越权读取")
            supported_task = Task(
                task_id="web-supported", task_type="web_vuln", target="https://a.test/api/user",
                agent_type=AgentType.WEB_VULN, status=TaskStatus.DONE,
                payload={"investigation_contract": {"hypothesis_ref": supported}},
            )
            supported_summary = {
                "findings": [{"type": "response_difference", "evidence_ref": "probe-1"}],
                "handoff": {"evidence_refs": ["request-1"]},
            }
            SubAgent._settle_hypothesis(agent, supported_task, supported_summary)
            supported_data = resolve_hypothesis(bb, supported)[1]
            self.assertEqual(supported_data["state"], "supported")
            self.assertEqual(set(supported_data["evidence_refs"]), {"request-1", "probe-1"})
            self.assertNotIn("confirmed", supported_data)
            self.assertEqual(supported_summary["hypothesis"]["outcome"], "supporting_signal")

            exhausted = hypothesis("缺少身份校验可能允许匿名读取")
            exhausted_task = Task(
                task_id="web-exhausted", task_type="web_vuln", target="https://a.test/api/user",
                agent_type=AgentType.WEB_VULN, status=TaskStatus.DONE,
                payload={"investigation_contract": {"hypothesis_ref": exhausted}},
            )
            exhausted_summary = {"findings": [], "handoff": {"evidence_refs": []}}
            SubAgent._settle_hypothesis(agent, exhausted_task, exhausted_summary)
            self.assertEqual(resolve_hypothesis(bb, exhausted)[1]["state"], "exhausted")

            blocked = hypothesis("第二账号可能读取第一账号对象")
            blocked_task = Task(
                task_id="web-blocked", task_type="web_vuln", target="https://a.test/api/user",
                agent_type=AgentType.WEB_VULN, status=TaskStatus.FAILED,
                payload={"investigation_contract": {"hypothesis_ref": blocked}},
            )
            blocked_summary = {"findings": [], "incomplete_reason": "缺少第二账号"}
            SubAgent._settle_hypothesis(agent, blocked_task, blocked_summary)
            self.assertEqual(resolve_hypothesis(bb, blocked)[1]["state"], "blocked")

    def test_task_summary_carries_investigation_handoff_contract(self) -> None:
        agent = SimpleNamespace(agent_id="recon-a", agent_type=AgentType.RECON,
                                cache=SimpleNamespace(stats=lambda: {}))
        task = Task(
            task_id="t1", task_type="recon", target="test", agent_type=AgentType.RECON,
            status=TaskStatus.DONE,
            payload={"investigation_contract": {
                "expected_information": "区分真实接口与统一软 200 页面",
                "decision_impact": "决定是否交给业务测试继续验证",
                "next_minimal_experiment": "比较响应摘要和 OpenAPI 标记",
                "stop_condition": "响应与首页一致且无接口标记时关闭线索",
                "remaining_unknowns": ["该路径是否返回真实文档"],
                "evidence_refs": ["asset:api@1"],
            }},
        )
        routes = [RouteState(route_id="r1", task_id="t1", name="真实性核实",
                             status=TaskStatus.DONE, notes=["软 200 尚未排除"])]

        summary = SubAgent._summarize(agent, task, routes)

        self.assertEqual(summary["handoff"]["established_facts"], [
            {"route": "真实性核实", "text": "软 200 尚未排除"}])
        self.assertEqual(summary["handoff"]["remaining_unknowns"], ["该路径是否返回真实文档"])
        self.assertEqual(summary["handoff"]["evidence_refs"], ["asset:api@1"])
        self.assertIn("关闭线索", summary["handoff"]["stop_condition"])

    def test_step_blockers_flow_to_handoff_without_becoming_findings(self) -> None:
        agent = SimpleNamespace(
            agent_id="business-a", agent_type=AgentType.BUSINESS_CONTEXT,
            cache=SimpleNamespace(stats=lambda: {}), health_mgr=SimpleNamespace(evaluate=lambda route: None),
        )
        route = RouteState(route_id="r1", task_id="t1", name="登录态走访")
        blocker = {
            "type": "authentication_required",
            "description": "公开入口止于登录页",
            "prerequisite": "测试账号或登录后请求记录",
            "user_resolvable": True,
            "alternative_capabilities": ["script_and_api_extraction", "evidence_enrichment"],
            "evidence_refs": ["asset:login@1"],
        }

        SubAgent._update_route(agent, route, StepResult(
            ok=True, value=0.1, blockers=[blocker, dict(blocker)]), 1)
        task = Task(task_id="t1", task_type="business_context", target="https://a.test",
                    agent_type=AgentType.BUSINESS_CONTEXT, status=TaskStatus.DONE)
        summary = SubAgent._summarize(agent, task, [route])

        self.assertEqual(summary["findings"], [])
        self.assertEqual(summary["handoff"]["blockers"], [blocker])
        self.assertEqual(len(route.blockers), 1)

    def test_task_summary_drops_cross_session_refs_from_current_evidence(self) -> None:
        agent = SimpleNamespace(
            agent_id="recon-a", agent_type=AgentType.RECON,
            cache=SimpleNamespace(stats=lambda: {}),
            ctx=SimpleNamespace(builtin_ctx={"workspace_root": "/tmp/sess-current"}),
        )
        task = Task(
            task_id="t1", task_type="recon", target="test", agent_type=AgentType.RECON,
            status=TaskStatus.DONE,
            payload={"investigation_contract": {"evidence_refs": [
                "sess-old:asset:legacy@1", "sess-current:asset:new@1", "asset:local@1",
            ]}},
        )
        summary = SubAgent._summarize(agent, task, [])
        self.assertEqual(summary["handoff"]["evidence_refs"], [
            "sess-current:asset:new@1", "asset:local@1",
        ])

    async def test_llm_step_receives_investigation_contract(self) -> None:
        class FixtureAgent(SubAgent):
            def route_templates(self, task):
                return []

            async def execute_step(self, task, route, step):
                raise NotImplementedError

        agent = object.__new__(FixtureAgent)
        agent.enable_llm_planning = True
        agent.agent_id = "recon-a"
        agent.agent_type = AgentType.RECON
        agent.ctx = SimpleNamespace(
            gateway=Mock(), audit=NullAuditLogger(), blackboard=None, ring=None,
            extra={}, policy=SimpleNamespace(mode=None), knowledge=None, pause_gate=None,
            builtin_ctx={}, tools=Mock(), questions=None,
        )
        agent._world_state = lambda: {}
        task = Task(
            task_id="t1", task_type="recon", target="test", agent_type=AgentType.RECON,
            payload={"investigation_contract": {
                "expected_information": "确认入口真实性",
                "stop_condition": "与首页一致则关闭",
            }},
        )
        with patch("spiritscout.agents.sub.base.chat_decision", AsyncMock(
                return_value=SimpleNamespace(content='{"ok": true}'))) as decision:
            await agent._llm_chat_json("fixture", {"action": "inspect"}, task=task)

        request = decision.await_args.args[1]
        self.assertIn("investigation_contract", str(request))
        self.assertIn("确认入口真实性", str(request))

    async def test_task_completion_contract_survives_actual_large_request_projection(self) -> None:
        import json

        from spiritscout.agents.recon.agent import ReconAgent

        agent = object.__new__(ReconAgent)
        agent.enable_llm_planning = True
        agent.agent_id = "recon-contract"
        agent.ctx = SimpleNamespace(
            gateway=Mock(), audit=NullAuditLogger(), blackboard=None, ring=None,
            extra={}, policy=SimpleNamespace(mode=RunMode.SRC), knowledge=None, pause_gate=None,
            builtin_ctx={}, tools=Mock(), questions=None,
        )
        agent._world_state = lambda: {"assets": [{"value": f"api-{i}.fixture.test", "details": "资产事实" * 50}
                                               for i in range(220)]}
        task = Task(task_id="large-contract", task_type="recon", target="fixture.test",
                    agent_type=AgentType.RECON, payload={"investigation_contract": {
                        "expected_information": "读取已有证据后核对完成判据"}})
        summary = {"findings": [], "handoff": {"evidence_refs": []}, "execution_feedback": {}}
        with patch("spiritscout.agents.sub.base.chat_decision", AsyncMock(
                return_value=SimpleNamespace(content='{"status":"blocked","reason":"缺少必要实验","missing":["对照证据"]}'))) as decision:
            reason = await agent._assess_task_completion(task, summary)
        self.assertTrue(reason)
        decision.assert_awaited_once()
        user_payload = json.loads(decision.call_args.args[1][-1]["content"])
        instruction = user_payload["instruction"]
        self.assertTrue(instruction.startswith("按任务契约逐项核对实际交付"))
        self.assertIn("返回 status（completed/partial/blocked）", instruction)
        self.assertTrue(instruction.endswith("仅有资产/脚本或参数错误不是支持信号。没有漏洞也可 completed，不能为了交付编造假设。"))
        self.assertNotEqual(summary.get("contract_satisfied"), True)

    async def test_completed_routes_do_not_satisfy_missing_interface_experiments(self) -> None:
        from spiritscout.agents.recon.agent import ReconAgent

        agent = object.__new__(ReconAgent)
        agent.enable_llm_planning = True
        agent.agent_id = "recon-test"
        agent.ctx = SimpleNamespace(policy=SimpleNamespace(mode=RunMode.SRC), blackboard=None)
        task = Task(task_id="t1", task_type="recon", target="https://example.test",
                    agent_type=AgentType.RECON, payload={"investigation_contract": {
                        "expected_information": "三个接口的 POST 响应和签名对照",
                    }})
        for assessment in [None, {}, {"status": "completed", "missing": ["未发送接口请求"]},
                           {"status": "completed", "missing": [], "evidence_refs": []},
                           {"status": "blocked", "reason": "基线停在参数错误分支", "missing": ["有效基线"]}]:
            with self.subTest(assessment=assessment):
                agent._llm_chat_json = AsyncMock(return_value=assessment)
                summary = {"findings": [{"type": "ip", "value": "192.0.2.1"}],
                           "handoff": {"evidence_refs": []}, "execution_feedback": {"tool_calls": 5}}
                reason = await agent._assess_task_completion(task, summary)
                self.assertTrue(reason)
                self.assertNotEqual(summary.get("contract_satisfied"), True)
                self.assertEqual(agent._llm_chat_json.call_args.args[0], "task_completion")

    def test_web_vuln_minimal_experiment_rejects_zero_execution_completion(self) -> None:
        from spiritscout.agents.webvuln.agent import WebVulnAgent

        agent = object.__new__(WebVulnAgent)
        task = Task(
            task_id="web-1", task_type="web_vuln", target="https://example.test",
            agent_type=AgentType.WEB_VULN,
            payload={"investigation_contract": {
                "next_minimal_experiment": "对已观测参数执行一次控制组差异请求",
            }},
        )

        reason = agent._contract_incomplete_reason(task, {
            "execution_feedback": {"tool_calls": 0},
            "handoff": {"evidence_refs": []},
        })

        self.assertIn("没有真实工具调用", reason)

    def test_blocked_hypothesis_is_not_misclassified_as_exhausted(self) -> None:
        from spiritscout.agents.webvuln.agent import WebVulnAgent

        agent = object.__new__(WebVulnAgent)
        agent.agent_id = "web-test"
        agent.ctx = SimpleNamespace(
            blackboard=Mock(), audit=Mock())
        task = Task(
            task_id="web-blocked", task_type="web_vuln",
            target="https://example.test/api/orders",
            agent_type=AgentType.WEB_VULN, status=TaskStatus.DONE,
            payload={"investigation_contract": {
                "hypothesis_ref": "topology:hypothesis-orders@1",
            }},
        )
        summary = {"findings": [], "contract_satisfied": True, "handoff": {
            "evidence_refs": [], "blockers": [{
                "type": "missing_boundary_fixture",
                "description": "缺少两个合法身份或对象样本",
                "prerequisite": "提供已授权对照样本",
            }],
        }}

        with patch("spiritscout.blackboard.hypotheses.transition",
                   return_value={"hypothesis_ref": "topology:hypothesis-orders@2"}) as transition:
            agent._settle_hypothesis(task, summary)

        self.assertEqual(transition.call_args.kwargs["state"], "blocked")
        self.assertEqual(transition.call_args.kwargs["outcome"], "prerequisite_missing")

    def test_recon_html_publishes_exact_cross_origin_scripts_as_readonly_assets(self) -> None:
        from spiritscout.agents.recon.agent import ReconAgent
        from spiritscout.core.models import Scope
        from spiritscout.policy.scope import ScopeGuard

        agent = object.__new__(ReconAgent)
        agent.agent_id = "recon-a"
        convergence = Mock()
        agent.ctx = SimpleNamespace(
            convergence=convergence,
            policy=SimpleNamespace(guard=ScopeGuard(
                Scope(allowed_domains=["example.test"]))),
        )
        agent._write_clue = Mock(return_value="recon_clue:external@1")
        task = Task(
            task_id="t1", task_type="recon", target="https://example.test",
            agent_type=AgentType.RECON,
        )
        result = ToolResult(
            tool="http_probe", ok=True, invocation_id="inv-home",
            stdout=("<script src='/app.js'></script>"
                    "<script src='https://cdn.other.test/vendor.js'></script>"),
            parsed={"body_ref": "body-home"},
        )

        findings = agent._publish_script_assets(
            task, "https://example.test/", result)

        self.assertEqual(convergence.merge_asset.call_count, 2)
        assets = [call.args[0] for call in convergence.merge_asset.call_args_list]
        self.assertEqual({asset.value for asset in assets}, {
            "https://example.test/app.js", "https://cdn.other.test/vendor.js"})
        derived = next(asset for asset in assets if "cdn.other.test" in asset.value)
        self.assertEqual(derived.attrs["scope_decision"], "derived_readonly")
        self.assertTrue(derived.attrs["readonly"])
        self.assertEqual(derived.attrs["source_page"], "https://example.test/")
        self.assertEqual(derived.attrs["evidence_ref"], "inv-home")
        self.assertEqual(derived.attrs["body_ref"], "body-home")
        self.assertEqual(
            {item["scope_decision"] for item in findings},
            {"in_scope", "derived_readonly"})
        self.assertTrue(agent.ctx.policy.guard.is_derived_resource(
            "https://cdn.other.test/vendor.js"))
        agent._write_clue.assert_not_called()

    async def test_recon_run_rejects_done_when_subdomain_contract_is_unmet(self) -> None:
        from spiritscout.agents.recon.agent import ReconAgent
        from spiritscout.agents.sub.base import RoutePlan

        agent = object.__new__(ReconAgent)
        agent.agent_id = "recon-a"
        agent.agent_type = AgentType.RECON
        agent.enable_llm_planning = True
        agent._llm_chat_json = AsyncMock(return_value={
            "status": "partial", "reason": "未发现或验证任何子域，约定调查尚未执行",
            "missing": ["子域清单"], "evidence_refs": []})
        agent.cache = SimpleNamespace(stats=lambda: {})
        agent.ctx = SimpleNamespace(
            audit=NullAuditLogger(), policy=SimpleNamespace(mode=RunMode.SRC),
            blackboard=SimpleNamespace(
                write=Mock(), query=lambda *args, **kwargs: []),
            builtin_ctx={"workspace_root": "/tmp/sess-current"},
        )
        agent.plan_routes = AsyncMock(return_value=[RoutePlan(
            name="root-baseline", strategy="根域基线", steps=[])])

        async def complete_route(task, plan, route, routes):
            route.status = TaskStatus.DONE
            route.notes.append("根域可达，但没有发现子域")

        agent._run_route = complete_route
        agent._report = AsyncMock()
        task = Task(
            task_id="t1", task_type="recon", target="https://example.test",
            agent_type=AgentType.RECON,
            payload={"investigation_contract": {
                "expected_information": "范围内的子域名清单与存活状态",
            }},
        )

        summary = await agent._run_task(task)

        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertFalse(summary["contract_satisfied"])
        self.assertIn("未发现或验证任何子域", summary["incomplete_reason"])
        agent._report.assert_awaited_once()

    async def test_completed_task_event_contains_text_summary_for_partial_history(self) -> None:
        audit = NullAuditLogger()
        agent = SimpleNamespace(
            agent_id="recon-a", agent_type=AgentType.RECON,
            ctx=SimpleNamespace(audit=audit, blackboard=Mock(), bus=SimpleNamespace(publish=AsyncMock())),
        )
        task = Task(task_id="t1", task_type="recon", target="test", agent_type=AgentType.RECON,
                    status=TaskStatus.DONE)
        routes = [RouteState(route_id="r1", task_id="t1", name="入口核实", status=TaskStatus.DONE,
                             notes=["中途进度", "确认登录入口，尚未认证。"])]

        await SubAgent._report(agent, task, routes, {"routes_total": 1, "routes_done": 1})

        event = audit.records[-1]
        self.assertEqual(event["type"], "task.done")
        self.assertEqual(event["detail"]["task_id"], "t1")
        self.assertEqual(event["detail"]["conclusions"],
                         [{"route": "入口核实", "text": "确认登录入口，尚未认证。"}])

    async def test_src_report_keeps_full_markdown_and_same_model_handoff(self) -> None:
        from spiritscout.agents.report.task_reports import list_task_reports
        from spiritscout.blackboard.exchange import SCHEMAS
        from spiritscout.gateway.world_state import snapshot
        from spiritscout.web.process_graph import project_blackboard

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bb = Blackboard(workspace / "bb.db")
            self.addCleanup(bb.close)
            audit = NullAuditLogger()
            publish = AsyncMock()
            agent = SimpleNamespace(
                agent_id="recon-a", agent_type=AgentType.RECON,
                ctx=SimpleNamespace(audit=audit, blackboard=bb,
                    policy=SimpleNamespace(mode=RunMode.SRC),
                    builtin_ctx={"workspace_root": tmp}, bus=SimpleNamespace(publish=publish)),
                health_mgr=SimpleNamespace(evaluate=lambda route: None),
            )
            task = Task(task_id="report-task", task_type="recon", target="https://test.invalid",
                        agent_type=AgentType.RECON, status=TaskStatus.DONE, parent_dag_node="dag-report")
            route = RouteState(route_id="report-route", task_id=task.task_id, name="核对入口", status=TaskStatus.DONE)
            full = "| 条件 | 结果 |\n| --- | --- |\n| 基线 | 未认证 |\n\n```python\n" + "print('evidence')\n" * 200 + "```\n结尾完整证据"
            SubAgent._update_route(agent, route, StepResult(ok=True, note=full, final_note=True), total_steps=1)
            self.assertNotIn("report_notes", route.model_dump())
            self.assertEqual(len(route.notes[-1]), 1600, "模型上下文保持原摘要长度")
            summary = {"routes_total": 1, "routes_done": 1, "handoff": {"remaining_unknowns": ["认证仍未确认"]}}
            task.result_summary = summary
            baseline = Blackboard(workspace / "baseline.db")
            self.addCleanup(baseline.close)
            for board in (bb, baseline):
                board.write(BlackboardLayer.TASK_STATE, task.task_id, {"task": task.model_dump(mode="json"),
                    "summary": summary, "routes": [route.model_dump(mode="json")]}, updated_by=agent.agent_id)
            baseline_agent = SimpleNamespace(agent_id=agent.agent_id, agent_type=agent.agent_type,
                ctx=SimpleNamespace(audit=NullAuditLogger(), blackboard=baseline,
                    policy=SimpleNamespace(mode=RunMode.CTF), bus=SimpleNamespace(publish=AsyncMock())))
            await SubAgent._report(baseline_agent, task, [route], summary)
            before = snapshot(blackboard=baseline)
            await SubAgent._report(agent, task, [route], summary)
            report = list_task_reports(workspace)[0]
            self.assertIn(full, (workspace / report["path"]).read_text(encoding="utf-8"))
            self.assertEqual(snapshot(blackboard=bb), before, "文件索引不挤占事实/机会，不增加规划上下文")
            self.assertNotIn("report", summary)
            self.assertNotIn("task_report", next(schema["function"]["parameters"]["properties"]["layer"]["enum"]
                                  for schema in SCHEMAS if schema["function"]["name"] == "blackboard_read"),
                             "内部文件索引不改变模型工具目录与缓存前缀")
            self.assertEqual(publish.call_args.args[0].payload["summary"], summary)
            self.assertEqual(audit.records[-1]["detail"]["conclusions"], [])
            self.assertEqual(audit.records[-2]["type"], "file.shared")
            node = next(n for n in project_blackboard(bb)["nodes"] if n["kind"] == "report")
            self.assertEqual(node["report"]["path"], report["path"])
            self.assertEqual(node["task_id"], task.task_id)
            self.assertNotIn("结尾完整证据", node["body"], "图只保存引用，不复制正文")
            await SubAgent._report(agent, task, [route], summary)
            self.assertEqual(len(list_task_reports(workspace)), 1)

    async def test_src_report_write_failure_does_not_publish_success_or_follow_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "session"
            outside = Path(tmp) / "outside"
            workspace.mkdir()
            outside.mkdir()
            (workspace / "reports").symlink_to(outside, target_is_directory=True)
            audit = NullAuditLogger()
            publish = AsyncMock()
            bb = Blackboard(workspace / "bb.db")
            self.addCleanup(bb.close)
            agent = SimpleNamespace(agent_id="recon-a", agent_type=AgentType.RECON,
                ctx=SimpleNamespace(audit=audit, blackboard=bb, policy=SimpleNamespace(mode=RunMode.SRC),
                    builtin_ctx={"workspace_root": str(workspace)}, bus=SimpleNamespace(publish=publish)))
            task = Task(task_id="write-failure", task_type="recon", target="test", agent_type=AgentType.RECON,
                        status=TaskStatus.DONE)
            with self.assertRaisesRegex(OSError, "符号链接"):
                await SubAgent._report(agent, task, [], {})
            publish.assert_not_awaited()
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(bb.count(BlackboardLayer.TASK_REPORT), 0)
            self.assertFalse(any(row["type"] in {"task.done", "file.shared"} for row in audit.records))

    async def test_default_stream_is_scoped_and_flushed_even_for_short_output(self) -> None:
        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                               audit=audit, mock_backend=MockBackend(script=["短输出"]))
        token = current_execution_scope.set(ExecutionScope(agent_id="recon-a", task_id="task-a", route_id="route-a"))
        try:
            await gateway.chat([{"role": "user", "content": "fixture"}])
        finally:
            current_execution_scope.reset(token)
        events = [r for r in audit.records if r["type"] == "agent.delta"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "recon-a")
        self.assertEqual(events[0]["detail"]["route"], "route-a")
        self.assertEqual(events[0]["detail"]["text"], "短输出")
        started = next(r for r in audit.records if r["type"] == "model.started")
        self.assertEqual(events[0]["detail"]["call_id"], started["detail"]["call_id"])

    async def test_custom_stream_callback_is_not_double_broadcast(self) -> None:
        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                               audit=audit, mock_backend=MockBackend(script=["CTF 原有回调"]))
        chunks = []
        token = current_execution_scope.set(ExecutionScope(agent_id="ctf-web-a"))
        try:
            await gateway.chat([], on_delta=lambda kind, text: chunks.append((kind, text)))
        finally:
            current_execution_scope.reset(token)
        self.assertTrue(chunks)
        self.assertFalse(any(r["type"] == "agent.delta" for r in audit.records))

    async def test_parallel_contexts_do_not_mix_actors(self) -> None:
        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                               audit=audit, mock_backend=MockBackend(script=["one", "two"]))
        async def run(actor: str) -> None:
            token = current_execution_scope.set(ExecutionScope(agent_id=actor))
            try:
                await gateway.chat([])
            finally:
                current_execution_scope.reset(token)
        await asyncio.gather(run("agent-one"), run("agent-two"))
        self.assertEqual({r["actor"] for r in audit.records if r["type"] == "agent.delta"},
                         {"agent-one", "agent-two"})

    def test_chunks_keep_complete_text_and_ignore_tool_argument_fragments(self) -> None:
        audit = NullAuditLogger()
        sink = AuditStream(audit, "agent", {"call_id": "call"})
        sink("tool_call", "private argument")
        sink("reasoning", "分析" * 3000)
        sink("text", "结果")
        sink.flush()
        deltas = [r["detail"] for r in audit.records]
        self.assertEqual("".join(r["text"] for r in deltas if r["kind"] == "reasoning"), "分析" * 3000)
        self.assertEqual(deltas[-1]["text"], "结果")
        self.assertTrue(all(len(r["text"]) <= 4000 for r in deltas))

    def test_json_decision_stream_hides_protocol_text_but_keeps_workbench_reasoning(self) -> None:
        audit = NullAuditLogger()
        sink = AuditStream(audit, "agent", {
            "call_id": "call", "response_format": "json",
            "task_id": "task", "route_id": "route",
        })
        sink("reasoning", "内部推理")
        sink("text", '{"payloads":[{"payload":"GET /private"}],')
        sink("text", '"progress":"公开进展"}')
        sink.flush()
        deltas = [r["detail"] for r in audit.records if r["type"] == "agent.delta"]
        self.assertEqual(deltas, [{
            "call_id": "call", "route": "route", "task_id": "task",
            "kind": "reasoning", "text": "内部推理",
        }])

    def test_stream_emits_only_natural_language_when_exploration_json_is_mixed(self) -> None:
        audit = NullAuditLogger()
        sink = AuditStream(audit, "agent", {"call_id": "call"})
        sink("text", '已观察到登录表单。\n\n{"exploration":{"intent":"核实登录边界",')
        sink("text", '"attempt":"对照两个响应","state":"running"}}')
        sink.flush()
        text = "".join(r["detail"]["text"] for r in audit.records
                       if r["type"] == "agent.delta" and r["detail"]["kind"] == "text")
        self.assertEqual(text, "已观察到登录表单。")

    def test_local_display_stream_preserves_large_chunks_and_retry_reset(self) -> None:
        audit = NullAuditLogger()
        scope = ExecutionScope(agent_id="local-display", task_id="display-task", route_id="display-route")
        token = current_execution_scope.set(scope)
        try:
            sink = _DeltaEmitter(audit, "local-display", route="display", challenge="fixture",
                                 turn=2, interval=3600)
            original = "配置说明" * 2500
            sink("text", original)
            sink("text", "尚未刷出的旧尾部")
            sink("reset", "")
            self.assertFalse(sink.has_public_text)
            sink("text", "重新生成的说明")
            sink.flush()
        finally:
            current_execution_scope.reset(token)
        deltas = [record["detail"] for record in audit.records]
        reset = next(i for i, detail in enumerate(deltas) if detail["kind"] == "reset")
        self.assertEqual("".join(detail["text"] for detail in deltas[:reset]), original)
        self.assertEqual(deltas[reset]["text"], "")
        self.assertEqual("".join(detail["text"] for detail in deltas[reset + 1:]), "重新生成的说明")
        self.assertTrue(all(len(detail["text"]) <= 4000 for detail in deltas))
        self.assertTrue(all(detail["task_id"] == scope.task_id and detail["route_id"] == scope.route_id
                            and detail["turn"] == 2 for detail in deltas))

    async def test_web_vuln_authorization_uses_recorded_dual_identity_fixture(self) -> None:
        from spiritscout.agents.webvuln.agent import WebVulnAgent
        from spiritscout.traffic import recorder

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / "bb.db")
            self.addCleanup(bb.close)
            ref = upsert(
                bb, claim="第二账号可能读取第一账号订单",
                subject="https://a.test/api/orders/42", test_type="idor",
                actor="business", agent_type=AgentType.BUSINESS_CONTEXT,
            )["hypothesis_ref"]

            async def capture(identity: str) -> None:
                token = current_execution_scope.set(ExecutionScope(identity_id=identity))
                try:
                    request = httpx.Request("GET", "https://a.test/api/orders/42")
                    response = httpx.Response(
                        200, headers={"content-type": "application/json"})
                    await recorder.record_http(
                        str(root), request, response, started=time.perf_counter(),
                        content=b'{"id":42,"owner":"account-a"}', complete=True)
                finally:
                    current_execution_scope.reset(token)

            await capture("account-a")
            await capture("account-b")
            convergence = Mock()
            convergence.merge_vulnerability.return_value = ("vuln-1", True, False)
            agent = object.__new__(WebVulnAgent)
            agent.agent_id = "web-a"
            agent.ctx = SimpleNamespace(
                blackboard=bb, builtin_ctx={"workspace_root": str(root)},
                convergence=convergence,
            )
            task = Task(
                task_id="web-auth", task_type="web_vuln",
                target="https://a.test/api/orders/42", agent_type=AgentType.WEB_VULN,
                payload={"investigation_contract": {"hypothesis_ref": ref}},
            )

            route = agent.route_templates(task)[0]
            result = await agent.execute_step(task, route, None, route.steps[0])

            self.assertEqual(route.name, "business_boundary_hypothesis")
            self.assertEqual(route.steps[0]["action"], "boundary_compare")
            self.assertEqual(result.findings[0]["type"], "access_control_signal")
            candidate = convergence.merge_vulnerability.call_args.args[0]
            self.assertEqual(candidate.hypothesis_ref, ref)
            self.assertEqual(candidate.evidence[0]["record_ids"], [2, 1])

    def test_web_vuln_authorization_without_dual_identity_stays_blocked(self) -> None:
        from spiritscout.agents.webvuln.agent import WebVulnAgent

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / "bb.db")
            self.addCleanup(bb.close)
            ref = upsert(
                bb, claim="订单接口可能缺少对象归属校验",
                subject="https://a.test/api/orders/42", test_type="authorization",
                actor="business", agent_type=AgentType.BUSINESS_CONTEXT,
            )["hypothesis_ref"]
            agent = object.__new__(WebVulnAgent)
            agent.ctx = SimpleNamespace(
                blackboard=bb, builtin_ctx={"workspace_root": str(root)})
            task = Task(
                task_id="web-auth-gap", task_type="web_vuln",
                target="https://a.test/api/orders/42", agent_type=AgentType.WEB_VULN,
                payload={"investigation_contract": {"hypothesis_ref": ref}},
            )

            route = agent.route_templates(task)[0]

            self.assertEqual(route.steps, [{
                "action": "context_gap", "reason": "missing_boundary_fixture",
                "test_type": "authorization", "subject": "https://a.test/api/orders/42",
            }])


class GatewayRetryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def response():
        return {"content": "完成", "prompt_tokens": 10, "completion_tokens": 1,
                "cached_input_tokens": 8, "tool_calls": [], "usage_status": "reported"}

    @staticmethod
    def unavailable(status=503, headers=None, body=None):
        error = httpx.HTTPStatusError("service unavailable", request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(status, headers=headers))
        if body is not None:
            error.body = body
        return error

    def gateway(self, **options):
        from spiritscout.config.settings import GatewayConfig
        return ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
            config=GatewayConfig(retry_initial_seconds=.001, retry_max_seconds=.002, **options),
            audit=NullAuditLogger())

    async def test_recoverable_failures_keep_request_and_tool_contract_identical(self):
        import copy

        gateway = self.gateway(max_retries=None)
        captured = []
        messages = [{"role": "system", "content": "stable prefix"}, {"role": "user", "content": "继续"}]
        tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
        original = copy.deepcopy((messages, tools))

        async def complete(entry, sent, **kwargs):
            captured.append(copy.deepcopy((sent, kwargs["tools"], kwargs["reasoning_effort"])))
            if len(captured) <= 3:
                raise self.unavailable()
            return self.response()

        with patch.object(gateway._mock, "complete", side_effect=complete):
            result = await gateway.chat(messages, tools=tools)
        self.assertEqual(result.content, "完成")
        self.assertEqual(len(captured), 4)
        self.assertTrue(all(request == captured[0] for request in captured))
        self.assertEqual((messages, tools), original)
        self.assertEqual(gateway.budget.used_tokens, 11)
        self.assertEqual(result.cached_input_tokens, 8)
        self.assertEqual(sum(e["type"] == "model.retry_scheduled" for e in gateway.audit.records), 3)
        self.assertFalse(any(e["type"] == "model.failed" for e in gateway.audit.records))

    async def test_permanent_errors_and_exhausted_retry_policy_are_not_silenced(self):
        from spiritscout.core.exceptions import SpiritScoutError

        for status, limit, expected in [(401, None, 1), (400, None, 1), (503, 2, 3), (503, 0, 1)]:
            with self.subTest(status=status, limit=limit):
                gateway = self.gateway(max_retries=limit)
                with patch.object(gateway._mock, "complete", side_effect=self.unavailable(status)) as complete:
                    with self.assertRaises(SpiritScoutError):
                        await gateway.chat([])
                self.assertEqual(complete.await_count, expected)
                self.assertEqual(gateway.audit.records[-1]["type"], "model.failed")

    async def test_cancellation_interrupts_retry_sleep_without_another_request(self):
        gateway = self.gateway(max_retries=None)
        failed = asyncio.Event()

        async def complete(*args, **kwargs):
            failed.set()
            raise self.unavailable(headers={"Retry-After": "3600"})

        with patch.object(gateway._mock, "complete", side_effect=complete) as backend:
            task = asyncio.create_task(gateway.chat([]))
            await asyncio.wait_for(failed.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(backend.await_count, 1)
        self.assertEqual(gateway.audit.records[-1]["type"], "model.cancelled")

    async def test_retry_after_wait_is_bounded_by_candidate_deadline(self):
        from spiritscout.core.exceptions import SpiritScoutError

        gateway = self.gateway(max_retries=None, request_timeout_seconds=1)
        unavailable = self.unavailable(headers={"Retry-After": "3600"})
        with patch.object(gateway._mock, "complete", side_effect=unavailable) as backend:
            with self.assertRaisesRegex(SpiritScoutError, "超过 1 秒没有新输出"):
                await asyncio.wait_for(gateway.chat([]), 3)
        backend.assert_awaited_once()
        self.assertEqual(gateway.audit.records[-1]["type"], "model.failed")

    async def test_paused_retry_waits_for_resume_and_discards_failed_stream(self):
        from spiritscout.core.pause import PauseGate

        gateway = self.gateway(max_retries=2)
        gate = PauseGate()
        gateway.pause_gate = gate
        gate.pause()
        waiting = asyncio.Event()
        original_wait = gate.wait

        async def wait():
            waiting.set()
            await original_wait()

        calls = 0
        chunks = []

        async def complete(*args, **kwargs):
            nonlocal calls
            calls += 1
            await kwargs["on_delta"]("text", "失败片段" if calls == 1 else "成功正文")
            if calls == 1:
                raise self.unavailable()
            return {**self.response(), "content": "成功正文"}

        with patch.object(gateway._mock, "complete", side_effect=complete), patch.object(gate, "wait", side_effect=wait):
            task = asyncio.create_task(gateway.chat([], on_delta=lambda kind, text: chunks.append((kind, text))))
            await asyncio.wait_for(waiting.wait(), 1)
            self.assertEqual(calls, 1)
            gate.resume()
            result = await asyncio.wait_for(task, 1)
        self.assertEqual(result.content, "成功正文")
        self.assertEqual(chunks, [("text", "失败片段"), ("reset", ""), ("text", "成功正文")])

    async def test_retry_after_and_backoff_do_not_retry_billing_failures(self):
        from datetime import datetime, timezone
        from email.utils import format_datetime

        from spiritscout.gateway.retry import recoverable, retry_delay

        gateway = self.gateway()
        self.assertAlmostEqual(retry_delay(self.unavailable(), 0, gateway.config), .001)
        self.assertAlmostEqual(retry_delay(self.unavailable(), 30, gateway.config), .002)
        self.assertEqual(retry_delay(self.unavailable(headers={"Retry-After": "90"}), 0, gateway.config), 90)
        self.assertEqual(retry_delay(self.unavailable(headers={"retry-after-ms": "250"}), 0, gateway.config), .25)
        date = format_datetime(datetime.fromtimestamp(1_700_000_030, timezone.utc), usegmt=True)
        with patch("spiritscout.gateway.retry.time.time", return_value=1_700_000_000):
            self.assertEqual(retry_delay(self.unavailable(headers={"Retry-After": date}), 0, gateway.config), 30)
        error = self.unavailable(429, body={"error": {"code": "insufficient_quota"}})
        self.assertFalse(recoverable(error))

    async def test_failed_stream_usage_is_accounted_without_mixing_recovered_text(self):
        from spiritscout.gateway.attempts import current_attempt_trace

        gateway = self.gateway(max_retries=1)
        calls = 0

        async def complete(*args, **kwargs):
            nonlocal calls
            calls += 1
            trace = current_attempt_trace.get()
            trace.start(streaming=True)
            trace.usage({"prompt_tokens": 10, "completion_tokens": 2 if calls == 1 else 1,
                         "prompt_tokens_details": {"cached_tokens": 8}})
            if calls == 1:
                kwargs["on_delta"]("text", "失败草稿" * 100)
                raise self.unavailable()
            trace.finish("completed")
            return self.response()

        token = current_execution_scope.set(ExecutionScope(agent_id="master-retry"))
        try:
            with patch.object(gateway._mock, "complete", side_effect=complete):
                response = await gateway.chat([])
        finally:
            current_execution_scope.reset(token)
        self.assertEqual(response.content, "完成")
        self.assertEqual(gateway.budget.used_tokens, 23)
        events = [row["detail"] for row in gateway.audit.records if row["type"] == "agent.delta"]
        reset = next(index for index, event in enumerate(events) if event["kind"] == "reset")
        self.assertEqual("".join(event["text"] for event in events[reset + 1:]), "完成")
        usage = next(row["detail"] for row in gateway.audit.records if row["type"] == "model.call")
        self.assertEqual(usage["input_tokens"], 20)
        self.assertEqual(usage["output_tokens"], 3)
        self.assertEqual(usage["cached_input_tokens"], 16)
