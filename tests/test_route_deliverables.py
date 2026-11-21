from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.hostops.agent import HostVulnAgent, PostExploitAgent
from spiritscout.agents.recon.agent import ReconAgent
from spiritscout.agents.sub.base import RoutePlan
from spiritscout.agents.webvuln.agent import WebVulnAgent
from spiritscout.core.models import AgentType, Task
from spiritscout.gateway.backends import extract_json


def _task(**payload_extra) -> Task:
    payload = {"mission": "摸清目标后台的登录入口", "node_title": "侦察与资产测绘"}
    payload.update(payload_extra)
    return Task(task_id="task-1", task_type="recon", target="http://t",
                agent_type=AgentType.RECON, payload=payload)


class KbGuidanceTests(unittest.TestCase):
    """经验库注入只带核验参考（前置/核验方法/红线），不带步骤序列。"""

    def test_hunt_templates_carry_no_step_sequences(self) -> None:
        agent = object.__new__(ReconAgent)
        agent.agent_id = "agent-test"
        agent.agent_type = AgentType.RECON
        agent.ctx = SimpleNamespace(
            audit=Mock(),
            knowledge=SimpleNamespace(
                experiences=Mock(return_value=[]),
                hunt_templates=Mock(return_value=[{
                    "template_id": "T1", "scene": "登录口",
                    "precondition": "已观测到登录表单",
                    "step_sequence": [{"step": "1", "action": "爆破"}],
                    "common_pitfall": "误报锁定",
                    "compliance_redline": "禁止真实爆破",
                }])))
        plan = RoutePlan(name="r", strategy="s")

        agent._attach_kb_guidance(_task(vuln_classes=["sqli"]), [plan])

        tpl = plan.meta["kb_guidance"]["hunt_templates"][0]
        self.assertNotIn("steps", tpl)
        self.assertEqual(tpl["precondition"], "已观测到登录表单")
        self.assertEqual(tpl["compliance_redline"], "禁止真实爆破")

    def test_empty_kb_attaches_nothing(self) -> None:
        agent = object.__new__(ReconAgent)
        agent.agent_id = "agent-test"
        agent.agent_type = AgentType.RECON
        agent.ctx = SimpleNamespace(
            audit=Mock(),
            knowledge=SimpleNamespace(
                experiences=Mock(return_value=[]),
                hunt_templates=Mock(return_value=[])))
        plan = RoutePlan(name="r", strategy="s")

        agent._attach_kb_guidance(_task(), [plan])

        self.assertNotIn("kb_guidance", plan.meta)


class WildcardReconTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _task() -> Task:
        return Task(task_id="wildcard", task_type="recon", target="*.example.test",
                    agent_type=AgentType.RECON)

    async def test_wildcard_routes_only_to_asset_discovery_handoff(self) -> None:
        agent = object.__new__(ReconAgent)

        routes = agent.route_templates(self._task())

        self.assertEqual([step["action"] for step in routes[0].steps],
                         ["asset_discovery_required"])

    async def test_concrete_host_contract_does_not_classify_subdomain_keywords(self) -> None:
        agent = object.__new__(ReconAgent)
        task = Task(
            task_id="contract", task_type="recon", target="https://example.test",
            agent_type=AgentType.RECON,
            payload={"investigation_contract": {
                "expected_information": "证书透明度、通配判断与已核验子域清单",
            }},
        )

        routes = agent.route_templates(task)

        self.assertEqual([route.name for route in routes], ["contract_investigation"])
        self.assertEqual([step["action"] for step in routes[0].steps], ["investigate"])

    async def test_wildcard_defensive_steps_never_call_network_tools(self) -> None:
        agent = object.__new__(ReconAgent)
        agent.agent_id = "recon-test"
        agent.agent_type = AgentType.RECON
        agent.ctx = SimpleNamespace(tools=SimpleNamespace(call=AsyncMock()), builtin_ctx={})
        agent._write_clue = Mock(return_value="recon_clue:test")
        task = self._task()

        results = [
            await agent._step_homepage(task, {"url": "https://*.example.test"}),
            await agent._step_dns(task, {}),
            await agent._step_port_probe(task, {"port": 443}),
        ]

        self.assertTrue(all(result.ok and result.final_note for result in results))
        agent.ctx.tools.call.assert_not_awaited()


class TaskIntentTests(unittest.IsolatedAsyncioTestCase):
    """灵枢任务包的任务意图随每次决策下发到执行者的 LLM 上下文。"""

    def _agent(self, agent_class=ReconAgent) -> ReconAgent:
        agent = object.__new__(agent_class)
        agent.agent_id = "agent-test"
        agent.agent_type = agent_class.agent_type
        agent.enable_llm_planning = True
        agent.ctx = SimpleNamespace(
            audit=Mock(), ring=None, builtin_ctx={},
            policy=SimpleNamespace(guard=SimpleNamespace(scope=SimpleNamespace(
                allowed_domains=[], allowed_cidrs=[], allowed_urls=[]))),
            gateway=SimpleNamespace(selected_model=lambda: "", chat=AsyncMock(return_value=SimpleNamespace(
                content=json.dumps({"paths": [], "rationale": "无"})))))
        return agent

    async def test_intent_from_task_package_reaches_decision_context(self) -> None:
        agent = self._agent()

        await agent._llm_chat_json(
            "path_planner", {"action": "suggest_paths"}, task=_task())

        # 事实块与当前观察分离；合并所有用户块检查任务意图仍完整传递。
        messages = agent.ctx.gateway.chat.await_args.args[0]
        payload = {}
        for message in messages:
            if message.get("role") == "user":
                payload.update(extract_json(message["content"]) or {})
        payload = payload.get("snapshot", payload)
        self.assertEqual(payload["task_intent"]["mission"], "摸清目标后台的登录入口")
        self.assertEqual(payload["task_intent"]["node"], "侦察与资产测绘")

    async def test_no_intent_without_task(self) -> None:
        agent = self._agent()

        await agent._llm_chat_json("path_planner", {"action": "suggest_paths"})

        messages = agent.ctx.gateway.chat.await_args.args[0]
        self.assertNotIn("task_intent", extract_json(messages[-1]["content"]))

    async def test_src_does_not_reinject_experience_from_a_legacy_task(self):
        from spiritscout.core.models import RunMode

        agent = self._agent()
        agent.ctx.policy.mode = RunMode.SRC
        await agent._llm_chat_json("path_planner", {"action": "suggest_paths"},
                                   task=_task(recent_experience=["legacy-insight-canary"]))
        messages = agent.ctx.gateway.chat.await_args.args[0]
        self.assertNotIn("legacy-insight-canary", str(messages))

    async def test_recon_source_followup_never_asks_model_to_invent_paths(self) -> None:
        from spiritscout.agents.sub.base import StepResult

        agent = self._agent()
        agent.cache = SimpleNamespace(get=lambda namespace, key: {
            "page_paths": [
                {"path": "/api/orders", "source": "inline_api_literal"},
                {"path": "/docs", "source": "html_navigation"},
            ]})
        probed = []
        async def probe(_task_value, step):
            probed.append(step["path"])
            return StepResult(ok=True, note=step["path"])
        agent._step_path_probe = probe

        result = await agent._step_source_path_followup(_task(), {})

        self.assertTrue(result.ok)
        self.assertEqual(probed, ["/api/orders", "/docs"])
        agent.ctx.gateway.chat.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class ContractExecutionTests(unittest.IsolatedAsyncioTestCase):
    _agent = TaskIntentTests._agent

    async def test_src_investigation_uses_scoped_mcp_catalog_and_keeps_request_prefix(self):
        from spiritscout.core.models import RunMode
        from spiritscout.tools.spec import ToolResult

        agent = self._agent(WebVulnAgent)
        agent.ctx.policy.mode = RunMode.SRC
        agent.ctx.convergence = Mock()
        agent.ctx.mcp_tools = {"mcp__playwright__browser_evaluate": {
            "schema": {"description": "browser execution", "inputSchema": {"type": "object"}}}}
        agent.ctx.tools = SimpleNamespace(call=AsyncMock(return_value=ToolResult(
            tool="mcp__playwright__browser_evaluate", ok=True, invocation_id="inv-browser",
            source_invocation_id="inv-original", reuse="cached",
            stdout="browser-proof: executed")))
        decisions = [
            {"local_tool": {"name": "mcp_search", "arguments": {"query": "browser"}}},
            {"local_tool": {"name": "mcp_call", "arguments": {
                "name": "mcp__playwright__browser_evaluate", "arguments": {"function": "fixture"}}}},
            {"status": "completed", "reason": "浏览器已执行", "hypothesis_outcome": "supporting_signal",
             "findings": [{"vuln_type": "xss", "title": "浏览器执行候选", "evidence": [
                 {"execution_id": "inv-browser", "quote": "browser-proof: executed"}]}]},
        ]
        agent.ctx.gateway.chat.side_effect = [SimpleNamespace(content=json.dumps(item)) for item in decisions]
        result = await agent._step_investigate(_task(), {})

        self.assertEqual(result.findings[0]["type"], "xss")
        self.assertEqual(result.findings[0]["evidence_ref"], "inv-original")
        candidate = agent.ctx.convergence.merge_vulnerability.call_args.args[0]
        self.assertEqual(candidate.evidence[0]["execution_id"], "inv-original")
        agent.ctx.tools.call.assert_awaited_once_with(
            "mcp__playwright__browser_evaluate", {"function": "fixture"}, **agent.ctx.builtin_ctx)
        requests = [call.args[0] for call in agent.ctx.gateway.chat.await_args_list]
        self.assertEqual(requests[1][:len(requests[0])], requests[0])
        self.assertEqual(requests[2][:len(requests[1])], requests[1])

    async def test_interface_contract_sends_real_post_and_reads_saved_body_without_redownload(self):
        for agent_class in (ReconAgent, WebVulnAgent, HostVulnAgent, PostExploitAgent):
            with self.subTest(agent=agent_class.agent_type):
                await self._check_http_contract(agent_class)

    async def test_src_dangerous_request_denied_before_network_execution(self):
        await self._check_http_contract(WebVulnAgent, denied=True)

    async def _check_http_contract(self, agent_class, denied=False):
        import asyncio
        import tempfile
        from pathlib import Path

        from spiritscout.agents.sub.cache import LocalSharedCache
        from spiritscout.agents.sub.health import BranchHealthManager
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.models import BlackboardLayer, RunMode, Scope, TaskStatus
        from spiritscout.policy.engine import PolicyEngine
        from spiritscout.policy.scope import ScopeGuard
        from spiritscout.tools.builtin import BUILTIN_SPECS, http_probe
        from spiritscout.tools.http_responses import SPEC as RESPONSE_SPEC
        from spiritscout.tools.http_responses import http_response_read
        from spiritscout.tools.registry import AgentToolView, ToolRegistry

        requests = []

        async def serve(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                headers = dict(line.split(b": ", 1) for line in head.split(b"\r\n")[1:] if b": " in line)
                body = await reader.readexactly(int(headers.get(b"Content-Length", b"0")))
                requests.append((head, body))
                content = b'{"result":{"readonly":true}}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(content)).encode() + b"\r\nConnection: close\r\n\r\n" + content)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory() as tmp:
            target = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/check"
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            agent = self._agent(agent_class)
            agent.cache = LocalSharedCache()
            agent.health_mgr = BranchHealthManager()
            agent.ctx.pause_gate = None
            agent.ctx.blackboard = bb
            from spiritscout.blackboard.convergence import ConvergenceEngine

            agent.ctx.convergence = ConvergenceEngine(bb)
            agent.ctx.policy.mode = RunMode.SRC
            agent.ctx.policy.guard = ScopeGuard(Scope(allowed_domains=["127.0.0.1"]))
            agent.ctx.builtin_ctx = {"guard": agent.ctx.policy.guard, "workspace_root": tmp}
            agent._report = AsyncMock()
            agent._live_report = AsyncMock()
            task = _task(investigation_contract={
                "expected_information": "接口 JSON 与实际请求头",
                "next_minimal_experiment": "对指定接口发一次只读 POST，并核对保存的正文",
            })
            task.target = target
            task.agent_type = agent_class.agent_type
            agent.plan_routes = AsyncMock(return_value=agent.route_templates(task))
            calls = []

            async def tool(name, arguments, **context):
                calls.append(name)
                result = await (http_probe(**arguments, **context) if name == "http_probe"
                                else http_response_read(**arguments, **context))
                if name == "http_probe":
                    bb.write(BlackboardLayer.ASSET, "observed", {"value": target, "status": result.parsed["status_code"]})
                return result

            approval = AsyncMock(return_value=not denied)
            registry = ToolRegistry(Mock(), policy=PolicyEngine(
                RunMode.SRC, agent.ctx.policy.guard, approval_gate=approval))
            registry.register_builtin("http_probe", lambda **kwargs: tool(
                "http_probe", {key: value for key, value in kwargs.items() if key not in {"guard", "workspace_root"}},
                guard=kwargs["guard"], workspace_root=kwargs["workspace_root"]), BUILTIN_SPECS["http_probe"])
            registry.register_builtin("http_response_read", lambda **kwargs: tool(
                "http_response_read", {key: value for key, value in kwargs.items() if key not in {"guard", "workspace_root"}},
                guard=kwargs["guard"], workspace_root=kwargs["workspace_root"]), RESPONSE_SPEC)
            view = AgentToolView(registry, AgentType.RECON)
            agent.ctx.tools = SimpleNamespace(call=AsyncMock(side_effect=view.call))

            async def decide(messages, **kwargs):
                index = agent.ctx.gateway.chat.await_count
                text = str(messages)
                if index == 1:
                    data = {"local_tool": {"name": "http_probe", "arguments": {
                        "url": target, "method": "POST", "action": "brute_force" if denied else "analysis", "data": '{"params":{}}',
                        "headers": {"Content-Type": "application/json", "X-Test": "single-variable"}}}}
                elif denied:
                    data = {"status": "blocked", "reason": "用户拒绝危险动作", "missing": ["本次操作许可"],
                            "evidence_refs": []}
                elif index == 2:
                    result = agent.ctx.tools.call.await_args
                    saved = list((Path(tmp) / "http-responses").iterdir())[0].name
                    self.assertIn(saved, text)
                    self.assertIn("body_path", text)
                    data = {"local_tool": {"name": "http_response_read", "arguments": {
                        "body_ref": saved, "mode": "search", "query": "readonly"}}}
                    self.assertEqual(result.args[0], "http_probe")
                else:
                    self.assertIn("readonly", text)
                    data = {"status": "completed", "reason": "实际 POST 与保存正文已核对",
                            "missing": [], "evidence_refs": ["asset:observed@1"]}
                    if agent_class != ReconAgent:
                        data["findings"] = [{"vuln_type": "business_logic", "title": "待复核业务差异",
                                             "endpoint": target, "evidence": [{"body_ref": list((Path(tmp) / "http-responses").iterdir())[0].name, "quote": "readonly"}]}]
                return SimpleNamespace(content=json.dumps(data), tool_calls=[])

            agent.ctx.gateway.chat = AsyncMock(side_effect=decide)
            summary = await agent.run(task)
            if denied:
                self.assertEqual(task.status, TaskStatus.FAILED)
                self.assertEqual(requests, [])
                approval.assert_awaited_once()
                return
            self.assertEqual(task.status, TaskStatus.DONE)
            self.assertTrue(summary["contract_satisfied"])
            self.assertEqual(calls, ["http_probe", "http_response_read"])
            self.assertEqual(agent.ctx.gateway.chat.await_count, 3, "最终交付复用同一推理，不另加一次核验调用")
            approval.assert_not_awaited()
            self.assertEqual(len(requests), 1)
            head, body = requests[0]
            self.assertTrue(head.startswith(b"POST /api/check "))
            self.assertIn(b"X-Test: single-variable", head)
            self.assertEqual(body, b'{"params":{}}')
            self.assertEqual(agent._task_observations, {})
            if agent_class != ReconAgent:
                candidates = bb.query(BlackboardLayer.VULN)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["data"]["vuln_type"], "business_logic")
                self.assertEqual(candidates[0]["data"]["status"], "candidate")
            observed = [row["data"] for row in bb.query(BlackboardLayer.ASSET) if row["data"].get("value") == target]
            if agent_class == ReconAgent:
                self.assertTrue(any(row.get("attrs", {}).get("method") == "POST" and row["attrs"].get("body_ref") for row in observed))

    async def test_failed_contract_decision_never_falls_back_to_page_baseline(self):
        agent = self._agent()
        agent._llm_chat_json = AsyncMock(return_value=None)
        agent.ctx.tools = SimpleNamespace(call=AsyncMock())
        result = await agent._step_investigate(_task(investigation_contract={
            "next_minimal_experiment": "核对接口签名"}), {})
        self.assertFalse(result.ok)
        agent.ctx.tools.call.assert_not_awaited()


class ContractOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_src_findings_accept_different_response_shapes_but_reject_fabricated_quotes(self):
        import tempfile
        from pathlib import Path

        from spiritscout.blackboard import Blackboard, ConvergenceEngine
        from spiritscout.core.models import BlackboardLayer, RunMode
        from spiritscout.tools.http_responses import save_response

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            agent = object.__new__(WebVulnAgent)
            agent.agent_id = "web-test"
            agent.ctx = SimpleNamespace(blackboard=bb, convergence=ConvergenceEngine(bb),
                policy=SimpleNamespace(mode=RunMode.SRC), builtin_ctx={"workspace_root": tmp})
            task = _task()
            for vuln_type, status, part, quote, valid in [
                ("sqli", 500, "body", "SQLSTATE: fixture", True),
                ("open_redirect", 302, "headers", "https://destination.test", True),
                ("business_logic", 200, "body", "state: changed", True),
                ("upload", 201, "body", "invented-content", False),
            ]:
                with self.subTest(vuln_type=vuln_type):
                    body = "SQLSTATE: fixture; state: changed"
                    saved = save_response(tmp, body.encode(), body, {
                        "status_code": status, "final_url": task.target, "download_complete": True,
                        "headers": {"location": "https://destination.test"}})
                    decision = {"status": "completed", "hypothesis_outcome": "supporting_signal",
                                "findings": [{"vuln_type": vuln_type, "title": "待复核实验差异",
                                              "evidence": [{"body_ref": saved["body_ref"], "part": part, "quote": quote}]}]}
                    agent._llm_chat_json = AsyncMock(return_value=decision)
                    plans = agent.route_templates(task)
                    result = await agent.execute_step(task, None, None, plans[0].steps[0])
                    self.assertEqual(bool(result.findings), valid)
                    if not valid:
                        self.assertEqual(decision["hypothesis_outcome"], "inconclusive")
            candidates = [row["data"] for row in bb.query(BlackboardLayer.VULN)]
            self.assertEqual({item["vuln_type"] for item in candidates}, {"sqli", "open_redirect", "business_logic"})
            self.assertTrue(all(item["status"] == "candidate" and not item["poc_verified"] for item in candidates))

    async def test_src_exploration_can_publish_partial_hypothesis_without_an_extra_model_call(self):
        import tempfile
        from pathlib import Path

        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import BlackboardLayer, RunMode

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "entry", {"value": "https://a.test/"})
            agent = object.__new__(ReconAgent)
            agent.agent_id, agent.enable_llm_planning = "recon-test", True
            agent.ctx = SimpleNamespace(blackboard=bb, policy=SimpleNamespace(mode=RunMode.SRC), builtin_ctx={})
            task = _task()
            agent._completion_assessments = {task.task_id: {
                "status": "completed", "evidence_refs": ["asset:entry@1"],
                "hypotheses": [{"claim": "该入口可能存在缓存隔离问题", "subject": "https://a.test/",
                                "evidence_refs": ["asset:entry@1"]}]}}
            agent._llm_chat_json = AsyncMock()
            self.assertEqual(await agent._assess_task_completion(task, {"handoff": {}}), "")
            agent._llm_chat_json.assert_not_awaited()
            self.assertEqual(len([row for row in bb.query(BlackboardLayer.TOPOLOGY)
                                  if row["data"].get("node_type") == "hypothesis"]), 1)

    async def test_completion_can_publish_model_hypothesis_without_another_observation_task(self):
        import tempfile
        from pathlib import Path

        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.models import BlackboardLayer, RunMode

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "interface", {"value": "https://a.test/api/check"})
            agent = object.__new__(ReconAgent)
            agent.agent_id = "recon-test"
            agent.enable_llm_planning = True
            agent.ctx = SimpleNamespace(blackboard=bb, policy=SimpleNamespace(mode=RunMode.SRC), builtin_ctx={})
            task = _task(investigation_contract={"expected_information": "接口身份与签名边界"})
            hypothesis = {"claim": "客户端签名可能未用于服务端身份校验", "subject": "https://a.test/api/check",
                          "test_type": "authorization", "expected_signal": "删签名后仍返回受保护内容",
                          "counter_signal": "服务端拒绝", "next_minimal_experiment": "复用真实基线，仅删除签名头",
                          "stop_condition": "无有效基线则阻塞", "evidence_refs": ["asset:interface@1"]}
            agent._llm_chat_json = AsyncMock(return_value={
                "status": "completed", "reason": "实际接口已定位", "missing": [],
                "evidence_refs": ["asset:interface@1", "asset:invented@1"],
                "hypotheses": [hypothesis, {**hypothesis, "claim": "无依据猜测", "evidence_refs": ["asset:invented@1"]}]})
            summary = {"handoff": {"evidence_refs": []}}
            self.assertEqual(await agent._assess_task_completion(task, summary), "")
            records = [row["data"] for row in bb.query(BlackboardLayer.TOPOLOGY)
                       if row["data"].get("node_type") == "hypothesis"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["text"], hypothesis["claim"])
            self.assertEqual(records[0]["next_minimal_experiment"], hypothesis["next_minimal_experiment"])
            self.assertEqual(summary["completion_assessment"]["evidence_refs"], ["asset:interface@1"])

    async def test_hypothesis_control_requires_valid_baseline_and_real_protected_content_quotes(self):
        import tempfile
        from pathlib import Path

        from spiritscout.agents.sub.base import StepResult
        from spiritscout.agents.webvuln.agent import WebVulnAgent
        from spiritscout.blackboard.hypotheses import upsert
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.models import VulnStatus
        from spiritscout.tools.http_responses import save_response

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            target = "https://a.test/api/check"
            ref = upsert(bb, claim="签名删除后可能仍能读取受保护对象", subject=target,
                         test_type="authorization", actor="recon", agent_type=AgentType.RECON)["hypothesis_ref"]
            content = '{"object":"protected-object-42"}'
            metadata = {"status_code": 200, "final_url": target, "download_complete": True,
                        "response_is_partial": False}
            first = save_response(tmp, content.encode(), content, metadata,
                                  request={"method": "POST", "url": target, "line": b"POST /api/check HTTP/1.1", "headers": [(b"X-Sign", b"fixture-sign")], "body": b"{}"})
            second = save_response(tmp, content.encode(), content, metadata,
                                   request={"method": "POST", "url": target, "line": b"POST /api/check HTTP/1.1", "headers": [], "body": b"{}"})
            task = Task(task_id="web-contract", task_type="web_vuln", target=target,
                        agent_type=AgentType.WEB_VULN, payload={"investigation_contract": {
                            "hypothesis_ref": ref, "next_minimal_experiment": "仅删除签名头，比较受保护对象响应"}})
            agent = object.__new__(WebVulnAgent)
            agent.agent_id = "web-test"
            agent.ctx = SimpleNamespace(blackboard=bb, builtin_ctx={"workspace_root": tmp}, convergence=Mock())
            route = agent.route_templates(task)[0]
            self.assertEqual(route.name, "contract_hypothesis_test")
            self.assertEqual(route.steps[0]["action"], "hypothesis_investigate")
            for baseline_valid, quote, expected in [(False, "protected-object-42", 0),
                                                    (True, "fabricated-sensitive-data", 0),
                                                    (True, "protected-object-42", 1)]:
                with self.subTest(baseline_valid=baseline_valid, quote=quote):
                    agent.ctx.convergence.reset_mock()
                    agent._step_investigate = AsyncMock(return_value=StepResult(ok=True))
                    decision = {"status": "completed", "hypothesis_outcome": "supporting_signal",
                                "baseline_valid": baseline_valid, "control_valid": True,
                                "signals": [{"baseline_ref": first["body_ref"], "variant_ref": second["body_ref"],
                                             "evidence_quote": quote, "rationale": "合法基线与匿名对照都含同一受保护对象"}]}
                    agent._completion_assessments = {task.task_id: decision}
                    result = await agent.execute_step(task, route, None, route.steps[0])
                    self.assertEqual(len(result.findings), expected)
                    self.assertEqual(agent.ctx.convergence.merge_vulnerability.call_count, expected)
                    if expected:
                        candidate = agent.ctx.convergence.merge_vulnerability.call_args.args[0]
                        self.assertEqual(candidate.status, VulnStatus.CANDIDATE)
                        self.assertEqual(candidate.hypothesis_ref, ref)
                        self.assertEqual({item["body_ref"] for item in candidate.evidence},
                                         {first["body_ref"], second["body_ref"]})
                    else:
                        self.assertEqual(decision["status"], "partial")
                        self.assertEqual(decision["hypothesis_outcome"], "inconclusive")

    def test_dns_observation_keeps_domain_relation_without_granting_ip_scope(self):
        import tempfile
        from pathlib import Path

        from spiritscout.blackboard.convergence import ConvergenceEngine
        from spiritscout.blackboard.store import Blackboard
        from spiritscout.core.models import BlackboardLayer, Scope
        from spiritscout.policy.scope import ScopeGuard
        from spiritscout.tools.spec import ToolResult

        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            guard = ScopeGuard(Scope(allowed_domains=["example.test"]))
            agent = object.__new__(ReconAgent)
            agent.agent_id = "recon-test"
            agent.ctx = SimpleNamespace(convergence=ConvergenceEngine(bb), policy=SimpleNamespace(guard=guard))
            task = _task()
            result = ToolResult(tool="dns_resolve", ok=True, parsed={"ips": ["192.0.2.10"]})
            refs = agent._observe_investigation_tool(task, "dns_resolve", {"host": "example.test"}, result)
            self.assertTrue(refs["evidence_refs"])
            asset = bb.query(BlackboardLayer.ASSET)[0]["data"]
            self.assertEqual(asset["attrs"]["resolved_from"], "example.test")
            self.assertFalse(asset["attrs"]["scope_inherited"])
            self.assertFalse(guard.is_allowed("192.0.2.10"))
            count = len(bb.query(BlackboardLayer.ASSET))
            result.parsed = {"ips": ["198.18.0.1"], "proxy_virtual": True}
            self.assertEqual(agent._observe_investigation_tool(task, "dns_resolve", {"host": "example.test"}, result), {})
            self.assertEqual(len(bb.query(BlackboardLayer.ASSET)), count)
