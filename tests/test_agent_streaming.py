"""角色流式输出的审计节流、并发归属与既有回调兼容。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from spiritscout.agents.sub.base import StepResult, SubAgent
from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.hypotheses import resolve as resolve_hypothesis
from spiritscout.blackboard.hypotheses import upsert
from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, RouteState, Task, TaskStatus
from spiritscout.ctf.agents import _DeltaEmitter
from spiritscout.gateway import ModelGateway, ModelRegistry
from spiritscout.gateway.backends import MockBackend
from spiritscout.gateway.streaming import AuditStream
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope


class AgentStreamingTests(unittest.IsolatedAsyncioTestCase):
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

    def test_recon_subdomain_contract_rejects_root_only_completion(self) -> None:
        from spiritscout.agents.recon.agent import ReconAgent

        agent = object.__new__(ReconAgent)
        agent.ctx = SimpleNamespace(blackboard=SimpleNamespace(query=lambda *args, **kwargs: []))
        task = Task(
            task_id="t1", task_type="recon", target="https://example.test",
            agent_type=AgentType.RECON,
            payload={"investigation_contract": {
                "expected_information": "范围内的子域名清单与存活状态",
            }},
        )
        reason = agent._contract_incomplete_reason(task, {})
        self.assertIn("未发现或验证任何子域", reason)

    async def test_recon_run_rejects_done_when_subdomain_contract_is_unmet(self) -> None:
        from spiritscout.agents.recon.agent import ReconAgent
        from spiritscout.agents.sub.base import RoutePlan

        agent = object.__new__(ReconAgent)
        agent.agent_id = "recon-a"
        agent.agent_type = AgentType.RECON
        agent.cache = SimpleNamespace(stats=lambda: {})
        agent.ctx = SimpleNamespace(
            audit=NullAuditLogger(),
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
