"""单执行者装配、无主控运行与真实 SIGPIPE 区分回归。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from spiritscout.agents.sub.base import RoutePlan
from spiritscout.config.settings import GatewayConfig, ModelEntry
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, RunMode, Task, TaskStatus
from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent, run_single
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.gateway.gateway import AgentBoundGateway, ModelGateway
from spiritscout.gateway.light_context import project_context
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.modes.assembler import ModeAssembler
from spiritscout.runtime import _run_master
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec


def context(mode=RunMode.AWDP, light=True):
    ctx = SimpleNamespace(mode=mode, shared_cache=None, search_enabled=False,
        gateway=SimpleNamespace(config=SimpleNamespace(lightweight_local=light)), audit=NullAuditLogger(),
        builtin_ctx={}, blackboard=None, policy=None)
    ctx.for_agent = lambda role: ctx
    return ctx


class AssemblyTests(unittest.IsolatedAsyncioTestCase):
    def test_recovery_uses_selected_or_bound_model_and_its_actual_fallbacks(self):
        ollama = ModelEntry(model='ollama', provider='ollama', ollama_lightweight=True)
        llama = ModelEntry(model='llama', local_runtime='llama_cpp', local_lightweight=True,
                           base_url='http://127.0.0.1:8081/v1')
        gateway = ModelGateway(ModelRegistry([ollama, llama]), GatewayConfig(lightweight_local=True),
                               audit=NullAuditLogger())
        ctx = context()
        ctx.gateway = AgentBoundGateway(gateway, llama.key)
        agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
        self.assertEqual(agent._recovery_tool_choice(), 'required')
        gateway.set_session_model(ollama.key)
        self.assertEqual(agent._recovery_tool_choice(), 'auto')
        gateway.set_session_model(llama.key)
        self.assertEqual(agent._recovery_tool_choice(), 'required')
        gateway.registry = ModelRegistry([ollama.model_copy(update={'is_fallback_for': [llama.key]}), llama])
        self.assertEqual(agent._recovery_tool_choice(), 'auto')

    @unittest.skipUnless(sandbox_available(), '需要现有 Linux/bwrap')
    async def test_initial_workdir_fact_matches_actual_sandbox_for_non_ascii_challenge_id(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(RunMode.AWDP)
            ctx.builtin_ctx = {'workspace_root': folder, '_run_mode': RunMode.AWDP, '_lightweight_local': True}
            agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
            task = Task(task_id='workdir-fixture', agent_type=agent.agent_type, task_type='ctf_single', target='',
                        payload={'challenge': {'id': '题目 / 路径'}})
            state = {'seed': []}
            agent._seed_messages(task, None, None, state)
            advertised = state['messages'][1]['_light_task_frame']['execution_environment']['default_workdir']
            observed = await sandbox_exec('pwd', **agent._tool_ctx(task))
            self.assertTrue(observed.ok, observed.error)
            self.assertEqual(observed.returncode, 0, observed.stderr)
            self.assertEqual(observed.parsed['workdir'], advertised)
            self.assertEqual(observed.stdout.strip(), advertised)

    def test_first_request_fits_small_window_with_runtime_state_and_original_contract(self):
        for mode in (RunMode.CTF, RunMode.AWDP):
            with self.subTest(mode=mode):
                ctx = context(mode)
                ctx.builtin_ctx = {"workspace_root": "/workspace/sessions/session-one"}
                ctx.ring = None
                agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True,
                    fix_mode=mode == RunMode.AWDP))
                mission = "请分析附件中的程序，寻找输入校验逻辑并根据实际运行结果说明结论。" * 3
                rules = "仅分析本题附件和隔离环境，保留原始文件与完整验证证据。"
                world = {"environment": agent._world_environment(),
                    "authorization": {"testing_rules": rules, "local_analysis": True},
                    "user_context": {"mission": mission, "standing_rules": rules,
                        "statements": [{"statement_id": "first", "text": mission}]}}
                agent._world_snapshot = lambda: world
                task = Task(task_id="one", agent_type=agent.agent_type, task_type="ctf_solve", target="",
                    payload={"challenge": {"id": "one", "description": mission,
                        "attachments": ["/workspace/sessions/session-one/attachments/challenge.zip"]},
                        "mission": mission, "rules": rules})
                state = {"seed": ["本次沙箱命令可用性：\n" + "/usr/bin/python3\n" * 12]}
                agent._seed_messages(task, None, None, state)
                original = copy.deepcopy(state["messages"])
                ctx.gateway.context_budget = lambda **kwargs: 10650
                sent = agent._light_model_messages(task, SimpleNamespace(route_id="r"), state)
                self.assertLessEqual(estimate_input_tokens(sent, agent._model_tool_schemas()), 10650)
                self.assertIn(mission, str(sent))
                self.assertIn(rules, str(sent))
                self.assertEqual(state["messages"], original)

    def test_compact_descriptions_preserve_parameters_and_other_executors(self):
        ctx = context()
        baseline = CTFBaseAgent(ctx, flag_manager=FlagManager(local_only=True))
        original = copy.deepcopy(baseline.tool_schemas)
        agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))
        compact = {s["function"]["name"]: s["function"] for s in agent.tool_schemas}

        def without_descriptions(value):
            if isinstance(value, dict):
                return {key: without_descriptions(item) for key, item in value.items() if key != "description"}
            if isinstance(value, list):
                return [without_descriptions(item) for item in value]
            return value

        # HTTP 和问询入口只在单执行者保留，也要核对其嵌套参数约束。
        for schema in [*CTFBaseAgent.tool_schemas, *original]:
            function = schema["function"]
            if function["name"] in compact:
                actual = without_descriptions(compact[function["name"]])
                if function["name"] == "record_answer":
                    actual["parameters"]["properties"].pop("review")
                elif function['name'] == 'history_read':
                    properties = actual['parameters']['properties']
                    self.assertEqual(properties['view']['enum'], ['index', 'message', 'content', 'output'])
                    self.assertEqual(properties['length']['maximum'], 4000)
                    for key in ('view', 'find', 'limit', 'expected_sha256', 'output_part'):
                        properties.pop(key)
                    properties['length'] = {'type': 'integer'}
                self.assertEqual(without_descriptions(function), actual)
        self.assertEqual(baseline.tool_schemas, original)
        self.assertEqual(CTFBaseAgent(ctx, flag_manager=FlagManager(local_only=True)).tool_schemas, original)

    def test_only_qualified_ctf_and_awdp_assemble_one_executor(self):
        for mode in (RunMode.CTF, RunMode.AWDP):
            assembly = ModeAssembler(context(mode)).assemble(mode,
                challenges=[{"id": "one"}], flag_manager=FlagManager(local_only=True, fix_mode=mode == RunMode.AWDP))
            self.assertTrue(assembly.single_executor)
            self.assertEqual(len(assembly.agents), 1)
            self.assertIsInstance(assembly.agents[AgentType.CTF_MISC], SingleCTFAgent)
            names = {s["function"]["name"] for s in assembly.agents[AgentType.CTF_MISC].tool_schemas}
            self.assertTrue({"shell", "python", "http_request", "skill_search", "history_read"} <= names)
            self.assertNotIn("spawn", names)
            self.assertIsNone(assembly.plan_provider)
        normal = ModeAssembler(context(RunMode.CTF, False)).assemble(RunMode.CTF)
        self.assertFalse(normal.single_executor)
        self.assertEqual(len(normal.agents), 5)

    async def test_one_route_does_not_call_route_planning_model(self):
        agent = SingleCTFAgent(context(), flag_manager=FlagManager(local_only=True, fix_mode=True))
        agent.ctx.gateway.chat = AsyncMock(side_effect=AssertionError("不应调用路线规划模型"))
        task = Task(task_id="one", agent_type=agent.agent_type, task_type="ctf_solve", target="", max_routes=8)
        routes = await agent.plan_routes(task)
        self.assertEqual(len(routes), 1)
        agent.ctx.gateway.chat.assert_not_awaited()

    def test_single_frame_keeps_user_goal_and_rules_but_not_generated_task_graph(self):
        ctx = context()
        agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
        world = {"authorization": {"testing_rules": "ORIGINAL_RULE"}, "user_context": {"mission": "USER_GOAL"}}
        agent._world_snapshot = lambda: world
        task = Task(task_id="one", agent_type=agent.agent_type, task_type="ctf_solve", target="",
            payload={"challenge": {"id": "one", "description": "original challenge"},
                     "mission": "USER_GOAL", "rules": "ORIGINAL_RULE", "source_context": "graph" * 10000})
        state = {"seed": ["bootstrap " * 500]}
        agent._seed_messages(task, None, RoutePlan(name="solve", strategy="", steps=[]), state)
        tools = agent._model_tool_schemas()
        # 此处验证用户契约与派生图的边界；工具说明长度变化不应改变测试条件。
        budget = estimate_input_tokens(state["messages"], tools) + 512
        view = project_context(state["messages"], tools, budget, world=world)
        self.assertLessEqual(view.estimated_tokens, budget)
        self.assertIn("USER_GOAL", str(view.messages))
        self.assertIn("ORIGINAL_RULE", str(view.messages))
        self.assertNotIn("graphgraph", str(view.messages))

    async def test_context_block_is_failure_not_done_after_prior_success(self):
        agent = SingleCTFAgent(context(), flag_manager=FlagManager(local_only=True, fix_mode=True))
        agent._inv_state = lambda task, route: {"context_blocked": True}
        route = SimpleNamespace(status=TaskStatus.DONE)
        with patch("spiritscout.ctf.agents.CTFBaseAgent._run_route_inner", new=AsyncMock()):
            await agent._run_route_inner(None, None, route)
        self.assertEqual(route.status, TaskStatus.FAILED)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def infrastructure(self, agent):
        return SimpleNamespace(cfg=SimpleNamespace(gateway=SimpleNamespace(lightweight_local=True)),
            authorization=SimpleNamespace(mode=RunMode.AWDP), blackboard=Mock(),
            audit=Mock(), tools=SimpleNamespace(journal=None), workspace=Path("test-workspace"),
            gateway=SimpleNamespace(usage_report=lambda: {"calls": 0}))

    async def test_runtime_bypasses_master_and_verifier_and_reuses_one_executor(self):
        agent = SimpleNamespace(agent_id="only-worker", agent_type=AgentType.CTF_MISC)
        seen = []
        async def run(task):
            seen.append((agent.agent_id, task.payload["challenge"]["id"]))
            task.status = TaskStatus.DONE
            return {"status": "done", "agent_id": agent.agent_id}
        agent.run = AsyncMock(side_effect=run)
        flags = FlagManager(local_only=True, fix_mode=True)
        assembly = SimpleNamespace(single_executor=True, agents={AgentType.CTF_MISC: agent}, flag_manager=flags,
            competition_context={"challenges": [{"id": "one"}, {"id": "two"}]}, notes=["single"])
        infra = self.infrastructure(agent)
        with patch("spiritscout.runtime.MasterAgent", side_effect=AssertionError("不应装配主控")), \
             patch("spiritscout.agents.verifier.VerificationQueue", side_effect=AssertionError("不应装配验证队列")), \
             patch("spiritscout.ctf.single.sync_user_context"):
            result = await _run_master(infra, assembly, "ctf://challenges", enable_llm_planning=True, mission="inspect")
        self.assertEqual(result["executor_count"], 1)
        self.assertFalse(result["supervision"])
        self.assertEqual(seen, [("only-worker", "one"), ("only-worker", "two")])
        self.assertEqual(agent.run.await_count, 2)
        self.assertEqual(result["model_usage"]["calls"], 0)

    async def test_failed_investigation_is_not_replaced_by_another_agent(self):
        agent = SimpleNamespace(agent_id="only-worker", agent_type=AgentType.CTF_MISC)
        async def run(task):
            task.status = TaskStatus.FAILED
            return {"status": "failed"}
        agent.run = AsyncMock(side_effect=run)
        assembly = SimpleNamespace(agents={AgentType.CTF_MISC: agent}, flag_manager=FlagManager(local_only=True),
            competition_context={"challenges": [{"id": "one"}]}, notes=[])
        with patch("spiritscout.ctf.single.sync_user_context"):
            result = await run_single(self.infrastructure(agent), assembly)
        agent.run.assert_awaited_once()
        self.assertEqual(result["dag_nodes"][0]["status"], "failed")

    async def test_user_feed_is_staged_without_claiming_delivery_before_model_response(self):
        for text in ("继续看这个文件", ""):
            with self.subTest(text=text):
                ctx = context()
                ctx.blackboard = Mock()
                agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
                feed = {"statement_id": "message-one", "text": text, "attachments": [{"path": "/attachments/new.jar"}]}
                agent.user_inbox = [feed]
                agent.on_user_feed = Mock()
                state = {}
                agent._inv_state = lambda *args: state
                task = Task(task_id="one", agent_type=agent.agent_type, task_type="ctf_solve", target="",
                            payload={"challenge": {"id": "one", "attachments": []}})
                with patch("spiritscout.ctf.single.record_user_statement") as record, \
                     patch("spiritscout.ctf.agents.CTFBaseAgent.execute_step", new=AsyncMock()) as execute:
                    await agent.execute_step(task, None, None, {})
                record.assert_called_once_with(ctx.blackboard, feed)
                agent.on_user_feed.assert_not_called()
                self.assertEqual(state['user_feed'], [feed])
                self.assertEqual(agent.user_inbox, [feed])
                execute.assert_awaited_once()
                self.assertEqual(task.payload["challenge"]["attachments"], ["/attachments/new.jar"])


class RecentTurnTests(unittest.TestCase):
    def test_latest_large_complete_turn_can_be_archived_without_losing_original(self):
        history = [{"role": "system", "content": "rules"}, {"role": "user", "content": "USER_GOAL"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "one", "function": {
                "name": "shell", "arguments": "x" * 30000}}]},
            {"role": "tool", "tool_call_id": "one", "content": "large result" * 1000}]
        original = copy.deepcopy(history)
        view = project_context(history, [], 2000)
        self.assertLessEqual(estimate_input_tokens(view.messages), 2000)
        self.assertEqual(view.archived_turns, 1)
        self.assertEqual(history, original)
        self.assertIn("USER_GOAL", str(view.messages))


@unittest.skipUnless(sandbox_available(), "需要 Linux/bubblewrap")
class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigpipe_with_successful_consumer_is_partial_not_a_blanket_141_success(self):
        with tempfile.TemporaryDirectory() as folder:
            kwargs = {"workspace_root": folder, "_run_mode": RunMode.AWDP, "_lightweight_local": True}
            partial = await sandbox_exec("seq 1 100000 | head -5", **kwargs)
            self.assertEqual(partial.returncode, 141)
            self.assertEqual(partial.parsed["pipeline_statuses"][-1], 0)
            self.assertTrue(partial.parsed["pipeline_partial"])
            failure = await sandbox_exec("false | true", **kwargs)
            self.assertEqual(failure.returncode, 1)
            self.assertFalse(failure.parsed["pipeline_partial"])
            direct = await sandbox_exec("printf output; exit 141", **kwargs)
            self.assertFalse(direct.parsed["pipeline_partial"])
            self.assertEqual(direct.returncode, 141)

    async def test_solver_keeps_partial_output_and_still_rejects_real_pipeline_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context()
            registry = ToolRegistry(None)
            registry.register_builtin("sandbox_exec", sandbox_exec, ToolSpec(
                name="sandbox_exec", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
            ctx.tools = registry
            ctx.builtin_ctx = {"workspace_root": folder, "_run_mode": RunMode.AWDP, "_lightweight_local": True}
            agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
            task = Task(task_id="one", agent_type=agent.agent_type, task_type="ctf_solve", target="",
                        payload={"challenge": {"id": "one"}})
            state = {"messages": [], "observations": []}
            route = SimpleNamespace(name="solve")
            partial = await agent._tool_sandbox(task, route, state, "shell", {
                "command": "seq 1 100000 | head -5", "action": "analysis"})
            self.assertTrue(partial.ok)
            self.assertIn("部分输出", partial.text)
            self.assertEqual(partial.value, 0)
            failure = await agent._tool_sandbox(task, route, state, "shell", {
                "command": "false | true", "action": "analysis"})
            self.assertFalse(failure.ok)
