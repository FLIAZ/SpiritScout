"""轻量长历史、指令回执与真实管道错误回归。"""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.evidence import execution_digest
from spiritscout.agents.ring import RingLedger
from spiritscout.core.models import AgentType, RunMode
from spiritscout.ctf.agents import CTFBaseAgent, CTFMiscAgent, _complete_tool_history
from spiritscout.ctf.flags import FlagManager
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.gateway.light_context import ContextWindowError, project_context
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import ToolResult

TOOLS = [{"type": "function", "function": {"name": "shell", "parameters": {"type": "object"}}}]


def long_history():
    messages = [{"role": "system", "content": "preserve contracts"},
                {"role": "user", "content": "base plus obsolete world", "_light_base": "original challenge"}]
    for i in range(60):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"call-{i}", "function": {"name": "shell", "arguments": json.dumps({
                "command": "# " + "x" * 1800 + f"\nprintf {i}"})}}]})
        messages.append({"role": "tool", "tool_call_id": f"call-{i}", "content": f"execution-{i}: result " + "data " * 200})
        messages.append({"role": "user", "_light_kind": "steering", "content": "repeated generated directive " * 100})
        messages.append({"role": "user", "_light_kind": "world", "content": "obsolete world " * 100})
    return messages


class ProjectionTests(unittest.TestCase):
    def test_complete_turns_archived_without_mutating_original_or_user_updates(self):
        history = long_history()
        history.insert(5, {"role": "user", "content": "真实用户原话：不允许修改正常功能"})
        before = copy.deepcopy(history)
        world = {"authorization": {"testing_rules": "ORIGINAL_RULE"},
                 "user_context": {"mission": "USER_MISSION"}, "environment": {"workdir": "current"}}
        result = project_context(history, TOOLS, 6000, world=world)
        self.assertLessEqual(result.estimated_tokens, 6000)
        self.assertGreater(result.archived_turns, 0)
        self.assertEqual(history, before)
        self.assertTrue(_complete_tool_history(result.messages))
        rendered = json.dumps(result.messages, ensure_ascii=False)
        self.assertIn("ORIGINAL_RULE", rendered)
        self.assertIn("USER_MISSION", rendered)
        self.assertIn("真实用户原话", rendered)
        self.assertIn("message_index", rendered)
        self.assertNotIn("obsolete world", rendered)
        self.assertEqual(result.messages[-1]["tool_call_id"], "call-59")

    def test_archival_index_keeps_early_executed_commands_not_just_recent_paths(self):
        result = project_context(long_history(), TOOLS, 6000)
        archived = next(m for m in result.messages if "已归档" in str(m.get("content")))
        self.assertIn("arguments_preview", archived["content"])
        self.assertIn("printf 0", archived["content"])
        self.assertIn("history_read", archived["content"])

    def test_instruction_ids_and_feedback_remain_available_without_repeated_body(self):
        records = [{"id": f"steer-{i}", "agent": "ctf_misc", "task_id": "one", "hint": "read actual classes",
                    "rationale": "same", "feedback": {"one|r": {"status": "delivered"}}} for i in range(10)]
        result = project_context(long_history(), TOOLS, 6000, instructions=records)
        text = json.dumps(result.messages, ensure_ascii=False)
        self.assertEqual(text.count("read actual classes"), 1)
        self.assertIn("steer-0", text)
        self.assertIn("steer-9", text)
        self.assertIn("delivered", text)

    def test_oversized_user_contract_raises_instead_of_sending_or_truncating(self):
        history = [{"role": "system", "content": "rules"},
                   {"role": "user", "content": "USER_BOUNDARY" * 10000}]
        with self.assertRaises(ContextWindowError):
            project_context(history, TOOLS, 3000)
        self.assertEqual(history[1]["content"], "USER_BOUNDARY" * 10000)

    def test_legacy_generated_events_can_be_replaced_but_raw_user_context_is_kept(self):
        history = long_history()
        world = {"authorization": {"testing_rules": "RULE"},
                 "user_context": {"statements": [{"text": "灵枢任务指令事件：这是用户的原话"}]}}
        history[1] = {"role": "user", "content": "original task\n<world_state>\n"
                      + json.dumps(world, ensure_ascii=False, separators=(",", ":")) + "\n</world_state>\nGOAL"}
        history.insert(2, {"role": "user", "content": "灵枢任务指令事件：" + "old generated instruction" * 300})
        before = copy.deepcopy(history)
        result = project_context(history, TOOLS, 6000, world=world)
        self.assertEqual(history, before)
        text = json.dumps(result.messages, ensure_ascii=False)
        self.assertIn("这是用户的原话", text)
        self.assertIn("GOAL", text)
        self.assertNotIn("old generated instruction", text)
        self.assertLessEqual(result.estimated_tokens, 6000)

    def test_short_history_is_deeply_detached(self):
        history = [{"role": "user", "content": "short"}]
        result = project_context(history, TOOLS, 3000)
        result.messages[0]["content"] = "changed"
        self.assertEqual(history[0]["content"], "short")


class SolverViewTests(unittest.IsolatedAsyncioTestCase):
    def agent(self, mode=RunMode.AWDP, light=True):
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = "fixture"
        agent.agent_type = AgentType.CTF_MISC
        agent.tool_schemas = TOOLS + [{"function": {"name": "history_read"}}]
        agent.ctx = SimpleNamespace(mode=mode, ring=None, audit=Mock(),
            gateway=SimpleNamespace(config=SimpleNamespace(lightweight_local=light),
                                    context_budget=lambda **kw: 6000))
        return agent

    def test_only_light_modes_project_and_metadata_does_not_reach_model(self):
        history = long_history()
        state = {"messages": history, "world_state": {"authorization": {"testing_rules": "RULE"}}}
        for mode, light in ((RunMode.SRC, True), (RunMode.COMPETITION, True), (RunMode.CTF, False)):
            result = self.agent(mode, light)._light_model_messages(SimpleNamespace(task_id="one"), SimpleNamespace(route_id="r"), state)
            self.assertEqual(len(result), len(history))
        result = self.agent()._light_model_messages(SimpleNamespace(task_id="one"), SimpleNamespace(route_id="r"), state)
        self.assertLessEqual(estimate_input_tokens(result, self.agent().tool_schemas), 6000)
        self.assertTrue(all(not any(k.startswith("_") for k in m) for m in result))

    async def test_archived_original_arguments_can_be_read_in_pages(self):
        agent = self.agent()
        state = {"messages": long_history()}
        result = await agent._run_tool_call_inner(None, None, state, {
            "name": "history_read", "arguments": {"message_index": 2, "length": 100}})
        first = json.loads(result.text)
        self.assertTrue(first["has_more"])
        result = await agent._run_tool_call_inner(None, None, state, {
            "name": "history_read", "arguments": {"message_index": 2, "offset": first["next_offset"], "length": 4000}})
        second = json.loads(result.text)
        self.assertEqual(json.loads(first["text"] + second["text"])["tool_calls"], state["messages"][2]["tool_calls"])

    def test_real_light_catalog_restores_feedback_and_skills_without_mutating_normal_schemas(self):
        def construct(light):
            ctx = SimpleNamespace(mode=RunMode.AWDP, shared_cache=None, search_enabled=False,
                gateway=SimpleNamespace(config=SimpleNamespace(lightweight_local=light)))
            ctx.for_agent = lambda role: ctx
            return CTFMiscAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
        normal = construct(False)
        before = copy.deepcopy(normal.tool_schemas)
        light = construct(True)
        names = {s["function"]["name"] for s in light.tool_schemas}
        self.assertTrue({"steering_feedback", "blackboard_read", "skill_search", "skill_open", "skill_read", "history_read"} <= names)
        self.assertEqual(normal.tool_schemas, before)
        self.assertEqual(construct(False).tool_schemas, before)
        self.assertNotIn("history_read", {s["function"]["name"] for s in normal.tool_schemas})

    async def test_instruction_lookup_is_task_scoped_and_returns_full_original(self):
        agent = self.agent()
        original = {"id": "own", "hint": "long instruction" * 100, "rationale": "actual reason"}
        state = {"messages": [], "instruction_state": {"own": original}}
        result = await agent._run_tool_call_inner(None, None, state, {
            "name": "history_read", "arguments": {"instruction_id": "own", "length": 4000}})
        self.assertEqual(json.loads(json.loads(result.text)["text"]), original)
        bad = await agent._run_tool_call_inner(None, None, state, {
            "name": "history_read", "arguments": {"instruction_id": "other"}})
        self.assertFalse(bad.ok)

    async def test_same_output_across_different_commands_is_reported_without_blocking(self):
        agent = self.agent()
        agent.ctx.tools = SimpleNamespace(call=AsyncMock(side_effect=[
            ToolResult(tool="sandbox_exec", ok=True, returncode=0, stdout="same", parsed={"exit_code": 0}),
            ToolResult(tool="sandbox_exec", ok=True, returncode=0, stdout="same", parsed={"exit_code": 0}),
            ToolResult(tool="sandbox_exec", ok=True, returncode=0, stdout="changed", parsed={"exit_code": 0})]))
        agent._tool_ctx = lambda task: {}
        state = {"messages": [], "observations": []}
        outputs = []
        for command in ("read-one", "read-two", "read-three"):
            outputs.append(await agent._tool_sandbox(None, SimpleNamespace(name="r"), state,
                "shell", {"command": command, "action": "analysis"}))
        self.assertIn("与此前 1 次调用完全一致", outputs[1].text)
        self.assertNotIn("与此前", outputs[2].text)
        self.assertEqual(agent.ctx.tools.call.await_count, 3)
        self.assertTrue(all(output.ok for output in outputs))

    async def test_recovery_uses_bounded_projection_too(self):
        agent = self.agent()
        agent.flag_manager = SimpleNamespace(fix_mode=True)
        agent.task_effort = lambda task: "low"
        agent.challenge_id = lambda task: "one"
        agent.ctx.gateway.chat = AsyncMock(return_value=SimpleNamespace(tool_calls=[], content=""))
        state = {"messages": long_history(), "world_state": {"authorization": {"testing_rules": "RULE"}}}
        await agent._forced_tool_recovery(SimpleNamespace(task_id="one"), SimpleNamespace(name="r", route_id="r"), state, 1)
        sent = agent.ctx.gateway.chat.call_args.args[0]
        self.assertLessEqual(estimate_input_tokens(sent, agent.tool_schemas), 6000)
        self.assertTrue(_complete_tool_history(sent))


class SupervisionTests(unittest.IsolatedAsyncioTestCase):
    def master(self, mode=RunMode.AWDP, light=True):
        master = object.__new__(MasterAgent)
        master.agent_id = "fixture"
        master.ctx = SimpleNamespace(extra={"lightweight_local": light}, policy=SimpleNamespace(mode=mode),
                                      ring=RingLedger(), audit=Mock())
        return master

    def test_exact_pending_instruction_reuses_existing_target_but_other_modes_do_not(self):
        tasks = [{"agent": "ctf_misc", "task_id": "one"}]
        proposal = [{"agent": "ctf_misc", "task_id": "one", "hint": "inspect classes", "rationale": "new wording"}]
        for mode, light in ((RunMode.AWDP, True), (RunMode.SRC, True), (RunMode.CTF, False)):
            master = self.master(mode, light)
            master.ctx.ring.steer("ctf_misc", "inspect classes", task_id="one")
            result = master._target_steering(proposal, tasks)
            self.assertEqual(len(result), 0 if mode == RunMode.AWDP and light else 1)
        master = self.master()
        master.ctx.ring.steer("ctf_misc", "old instruction", task_id="one")
        self.assertEqual(len(master._target_steering(proposal, tasks)), 1)

    async def test_identical_execution_with_new_ids_and_round_notes_does_not_retrigger(self):
        master = self.master()
        checked = asyncio.Event()
        reads = 0
        def digest(ids):
            nonlocal reads
            reads += 1
            if reads > 5:
                checked.set()
            return {"tasks": [{"task_id": "one", "agent": "ctf_misc", "routes": [{"notes": [f"round {reads}"]}],
                "executions": [{"invocation_id": f"inv-{reads}", "tool_name": "sandbox_exec",
                    "params_digest": "same", "execution_status": "succeeded", "exit_code": 0,
                    "result_digest": "same-result"}]}], "platform_progress": {}}
        master._STEER_INTERVAL = .001
        master._progress_digest = digest
        master._llm_steer = AsyncMock(return_value=[])
        watcher = asyncio.create_task(master._steering_watch(["one"]))
        try:
            await asyncio.wait_for(checked.wait(), 1)
            self.assertEqual(master._llm_steer.await_count, 1)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def test_execution_digest_exposes_real_failure_and_is_task_scoped(self):
        journal = SimpleNamespace(
            list_recent=lambda n: [{"task_id": "other", "invocation_id": "other"},
                {"task_id": "one", "invocation_id": "own", "tool_name": "sandbox_exec", "params_digest": "p"}],
            result=lambda identifier: ToolResult(tool="sandbox_exec", ok=True, returncode=1,
                stderr="cannot create directory: Read-only file system", parsed={"workdir": "/sandbox/one"}),
            parameters=lambda identifier: {"code": "mkdir attachments/unpacked"})
        result = execution_digest(journal, "one")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["invocation_id"], "own")
        self.assertEqual(result[0]["exit_code"], 1)
        self.assertIn("Read-only", result[0]["stderr_preview"])
        self.assertEqual(result[0]["workdir"], "/sandbox/one")


@unittest.skipUnless(sandbox_available(), "需要 Linux/bubblewrap")
class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_light_shell_preserves_middle_command_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            normal = await sandbox_exec("false | true", workspace_root=folder)
            light = await sandbox_exec("false | true", workspace_root=folder,
                                       _run_mode=RunMode.AWDP, _lightweight_local=True)
            self.assertEqual(normal.returncode, 0)
            self.assertEqual(light.returncode, 1)
            self.assertTrue(light.ok)
