"""轻量资格、并发工具隔离与真实沙箱目录回归。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, RunMode
from spiritscout.ctf.agents import CTFWebAgent
from spiritscout.gateway.attempts import AttemptTrace, current_attempt_trace
from spiritscout.gateway.lightweight import lightweight_enabled
from spiritscout.gateway.ollama import OllamaBackend
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.sandbox_paths import (
    open_directory,
    sandbox_directory,
    sandbox_scope,
    sandbox_tmp_directory,
)
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec


class EligibilityTests(unittest.TestCase):
    def test_opt_in_requires_deployment_mode_and_all_candidates(self):
        local = ModelEntry(model="local", provider="ollama", ollama_lightweight=True)
        unmarked = ModelEntry(model="other", provider="ollama")
        cloud = ModelEntry(model="cloud", ollama_lightweight=True)
        for mode in RunMode:
            self.assertEqual(lightweight_enabled(True, mode, [local]),
                             mode in (RunMode.CTF, RunMode.AWDP))
        self.assertFalse(lightweight_enabled(False, RunMode.CTF, [local]))
        for entries in ([], [unmarked], [cloud], [local, cloud], [local, unmarked]):
            self.assertFalse(lightweight_enabled(True, RunMode.CTF, entries))

    def test_tmp_scopes_do_not_alias_session_and_topic(self):
        self.assertNotEqual(sandbox_tmp_directory(None), sandbox_tmp_directory("session"))
        self.assertNotEqual(sandbox_directory("../../one"), sandbox_directory("one"))

    def test_transformed_scopes_preserve_distinct_topic_identity(self):
        scopes = [None, "", "one", "题目一", "题目二", "web/a", "web:a", "web_a",
                  "x" * 80 + "1", "x" * 80 + "2", "../../one", ".", ".."]
        # None 和空字符串都是未指定题目的会话级目录。
        identities = scopes[1:]
        for directory in (sandbox_directory, sandbox_tmp_directory):
            paths = [directory(scope) for scope in identities]
            self.assertEqual(len(paths), len(set(paths)))
            self.assertEqual(directory(None), directory(""))
            for scope in identities:
                self.assertNotIn("..", directory(scope).parts)
        self.assertEqual(sandbox_directory("one"), Path("sandbox/one"))
        generated = sandbox_scope("题目一")
        self.assertNotEqual(sandbox_scope(generated), generated)
        self.assertLessEqual(len(sandbox_scope("x" * 81)), 80)


class RegistryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_cannot_forge_runtime_gates_or_remove_false(self):
        async def capture(**kwargs):
            return ToolResult(tool="capture", ok=True, parsed=kwargs)
        registry = ToolRegistry(None)
        registry.register_builtin("capture", capture, ToolSpec(
            name="capture", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
        forged = {"_run_mode": RunMode.CTF, "_lightweight_local": True, "data": 3}
        absent = await registry.call("capture", forged)
        self.assertNotIn("_run_mode", absent.parsed)
        self.assertNotIn("_lightweight_local", absent.parsed)
        first, second = await asyncio.gather(
            registry.call("capture", forged, _run_mode=RunMode.SRC, _lightweight_local=False),
            registry.call("capture", {"_run_mode": RunMode.SRC, "_lightweight_local": False},
                          _run_mode=RunMode.AWDP, _lightweight_local=True))
        self.assertEqual((first.parsed["_run_mode"], first.parsed["_lightweight_local"]),
                         (RunMode.SRC, False))
        self.assertEqual((second.parsed["_run_mode"], second.parsed["_lightweight_local"]),
                         (RunMode.AWDP, True))
        self.assertEqual(first.parsed["data"], 3)


@unittest.skipUnless(sandbox_available(), "需要 Linux/bubblewrap")
class SandboxIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ss-isolation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    async def call(self, code, *, scope="one", light=True, mode=RunMode.CTF, language="shell"):
        result = await sandbox_exec(code, workspace_root=str(self.root), workspace_scope=scope,
                                    _lightweight_local=light, _run_mode=mode, language=language)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    async def test_persistent_tmp_is_per_topic_and_control_directory_is_hidden(self):
        await self.call("printf evidence > /tmp/result")
        same = await self.call("test ! -e ../../.sandbox-tmp; printf '%s' \"$(< /tmp/result)\"")
        self.assertEqual(same.stdout, "evidence")
        other = await self.call("test ! -e /tmp/result; printf separate", scope="two")
        self.assertEqual(other.stdout, "separate")
        self.assertTrue(same.parsed["tmp_persistent"])
        self.assertTrue(Path(same.parsed["output_file"]).is_file())

    async def test_chinese_and_punctuation_ids_have_separate_workdirs_and_tmp(self):
        for first, second in (("题目一", "题目二"), ("web/a", "web:a")):
            with self.subTest(first=first, second=second):
                await self.call("printf first > proof.txt; printf first > /tmp/proof.txt", scope=first)
                other = await self.call("test ! -e proof.txt && test ! -e /tmp/proof.txt", scope=second)
                same = await self.call("cat proof.txt /tmp/proof.txt", scope=first)
                self.assertEqual(same.stdout, "firstfirst")
                self.assertNotEqual(same.parsed["workdir"], other.parsed["workdir"])

    async def test_light_and_normal_calls_do_not_contaminate_each_other(self):
        light, normal = await asyncio.gather(
            self.call("python3 -c 'print(\"x\" * 6000)'", scope="light"),
            self.call("python3 -c 'print(\"x\" * 6000)'", scope="normal", light=False))
        self.assertIn("输出超限", light.stdout)
        self.assertNotIn("输出超限", normal.stdout)
        for mode, flag in ((RunMode.CTF, False), (RunMode.SRC, True), (RunMode.COMPETITION, True)):
            scope = mode.value + str(flag)
            await self.call("printf old > /tmp/not_persistent", scope=scope, mode=mode, light=flag)
            result = await self.call("test ! -e /tmp/not_persistent", scope=scope, mode=mode, light=flag)
            self.assertFalse(result.parsed["tmp_persistent"])

    async def test_scoped_and_private_parent_symlinks_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "sandbox").mkdir()
        (self.root / "sandbox/one").symlink_to(outside, target_is_directory=True)
        result = await sandbox_exec("print('no')", language="python", workspace_root=str(self.root),
                                    workspace_scope="one")
        self.assertFalse(result.ok)
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / "sandbox/one").unlink()
        (self.root / ".sandbox-tmp").symlink_to(outside, target_is_directory=True)
        result = await sandbox_exec("true", workspace_root=str(self.root), workspace_scope="one",
                                    _run_mode=RunMode.CTF, _lightweight_local=True)
        self.assertFalse(result.ok)
        self.assertEqual(list(outside.iterdir()), [])

    async def test_directory_descriptor_survives_path_replacement(self):
        directory = self.root / "sandbox/one"
        directory.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        with open_directory(self.root, Path("sandbox/one")) as fd:
            directory.rename(self.root / "sandbox/old")
            directory.symlink_to(outside, target_is_directory=True)
            self.assertNotEqual(Path(f"/proc/self/fd/{fd}").resolve(), outside)
        result = await sandbox_exec("true", workspace_root=str(self.root), workspace_scope="one")
        self.assertFalse(result.ok)

    async def test_host_directory_descriptors_are_closed_before_user_code(self):
        result = await self.call("import os, stat\n"
            "assert not any(stat.S_ISDIR(os.fstat(fd).st_mode) "
            "for fd in range(3, 64) if os.path.exists(f'/proc/self/fd/{fd}'))\n"
            "print('descriptors closed')", language="python")
        self.assertIn("descriptors closed", result.stdout)

    async def test_python_scripts_and_logs_remain_usable(self):
        result = await self.call("from pathlib import Path\nPath('created.txt').write_text('done')\nprint('done')",
                                 language="python")
        self.assertEqual(result.stdout.strip(), "done")
        self.assertEqual((Path(result.parsed["workdir"]) / "created.txt").read_text(), "done")


class SolverIsolationTests(unittest.IsolatedAsyncioTestCase):
    def agent(self, mode=RunMode.CTF, light=True):
        agent = object.__new__(CTFWebAgent)
        agent.agent_type = AgentType.CTF_WEB
        agent.agent_id = "fixture"
        agent.ctx = SimpleNamespace(mode=mode, audit=NullAuditLogger(),
            gateway=SimpleNamespace(config=SimpleNamespace(lightweight_local=light)),
            tools=SimpleNamespace(call=AsyncMock(return_value=ToolResult(
                tool="sandbox_exec", ok=True, stdout="fresh", parsed={"exit_code": 0}))))
        agent.flag_manager = SimpleNamespace(fix_mode=mode == RunMode.AWDP,
                                             local_only=True, multiple_answers=False)
        agent._tool_ctx = lambda task: {}
        return agent

    async def test_exact_repeats_execute_and_changed_suffix_is_not_conflated(self):
        agent = self.agent()
        state = {"observations": [], "messages": []}
        command = "printf " + "a" * 180
        for _ in range(4):
            result = await agent._tool_sandbox(None, SimpleNamespace(name="route"), state,
                "shell", {"command": command, "action": "analysis"})
            self.assertTrue(result.ok)
        self.assertIn("本次仍真实执行", result.text)
        different = await agent._tool_sandbox(None, SimpleNamespace(name="route"), state,
            "shell", {"command": command + "b", "action": "analysis"})
        self.assertNotIn("完整command已调用", different.text)
        self.assertEqual(agent.ctx.tools.call.await_count, 5)
        self.assertEqual(state["messages"], [])

    async def test_ordinary_solver_does_not_collect_repeat_counts(self):
        for mode, light in ((RunMode.CTF, False), (RunMode.SRC, True), (RunMode.COMPETITION, True)):
            agent = self.agent(mode, light)
            state = {"observations": [], "messages": []}
            await agent._tool_sandbox(None, SimpleNamespace(name="route"), state,
                                     "shell", {"command": "true", "action": "analysis"})
            self.assertNotIn("exec_signatures", state)

    def test_compact_prompt_and_fix_goal_are_mode_gated(self):
        compact = "用工具取得原始证据，按照本次目标自主选择方法"
        for mode, light in ((RunMode.CTF, False), (RunMode.SRC, True), (RunMode.COMPETITION, True)):
            self.assertNotIn(compact, self.agent(mode, light)._solver_system_prompt())
        self.assertIn(compact, self.agent()._solver_system_prompt())
        agent = self.agent(RunMode.AWDP)
        prompt = agent._solver_system_prompt()
        self.assertIn("正常功能与漏洞回归", prompt)
        self.assertNotIn("record_answer", prompt)
        self.assertNotIn("流程：", prompt)
        self.assertIn("submit_fix", agent._completion_instruction())

    async def test_recovery_preserves_tool_choice_and_awdp_goal(self):
        agent = self.agent(RunMode.AWDP)
        agent.tool_schemas = [{"function": {"name": "shell"}}, {"function": {"name": "submit_fix"}}]
        agent.ctx.gateway.chat = AsyncMock(return_value=SimpleNamespace(tool_calls=[], content=""))
        agent.task_effort = lambda task: "low"
        agent.challenge_id = lambda task: "one"
        state = {"messages": []}
        await agent._forced_tool_recovery(None, SimpleNamespace(name="r"), state, 1)
        self.assertIn("submit_fix", state["messages"][-1]["content"])
        self.assertNotIn("record_answer", state["messages"][-1]["content"])
        options = agent.ctx.gateway.chat.call_args.kwargs
        self.assertEqual(options["tools"], agent.tool_schemas)
        self.assertEqual(options["tool_choice"], "auto")
        agent.ctx.mode = RunMode.SRC
        await agent._forced_tool_recovery(None, SimpleNamespace(name="r"), state, 2)
        self.assertEqual(agent.ctx.gateway.chat.await_count, 1)


class RetryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, light=False, marked=True, stream=False, partial=False):
        requests = []
        async def handle(reader, writer):
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                size = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                            if line.lower().startswith(b"content-length:"))
                requests.append(json.loads(await reader.readexactly(size)))
                if len(requests) == 1:
                    body = (json.dumps({"message": {"content": "partial"}, "done": False}) + "\n").encode() if partial else b""
                else:
                    body = json.dumps({"done": True, "message": {"content": "ok"},
                                       "prompt_eval_count": 2, "eval_count": 1}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        entry = ModelEntry(model="fixture", provider="ollama", ollama_lightweight=marked,
                           base_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        trace = AttemptTrace(NullAuditLogger(), "fixture", {"call_id": "retry"})
        token = current_attempt_trace.set(trace)
        try:
            with self.assertRaises(ValueError) if not (light and marked and stream and not partial) else nullcontext():
                await OllamaBackend().complete(entry, [], _lightweight_local=light,
                    on_delta=(lambda kind, text: None) if stream else None)
        finally:
            current_attempt_trace.reset(token)
            server.close()
            await server.wait_closed()
        return requests, trace

    async def test_retry_is_opt_in_and_each_http_attempt_is_counted(self):
        for light, marked in ((False, True), (True, False)):
            requests, trace = await self.exercise(light=light, marked=marked, stream=True)
            self.assertEqual(len(requests), 1)
            self.assertEqual(trace.metrics()["sdk_attempts"], 1)
        requests, trace = await self.exercise(light=True, stream=True)
        self.assertEqual(len(requests), 2)
        self.assertEqual(trace.metrics()["sdk_attempts"], 2)
        self.assertEqual(trace.metrics()["unreported_attempts"], 1)

    async def test_partial_stream_is_not_replayed(self):
        requests, trace = await self.exercise(light=True, stream=True, partial=True)
        self.assertEqual(len(requests), 1)
        self.assertEqual(trace.metrics()["sdk_attempts"], 1)
