"""CTF / 智能体模式全自动操作；SRC 仍按调用即时审批。"""

from __future__ import annotations

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, Mock

from spiritscout.core.exceptions import PolicyViolation
from spiritscout.core.models import RunMode, Scope
from spiritscout.policy.engine import DANGEROUS_ACTIONS, PolicyEngine
from spiritscout.policy.questions import QuestionBroker
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.invocation import InvocationContext, current_invocation
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec


class AutonomousCompetitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_actions_execute_without_approval_and_keep_invocation_audit(self) -> None:
        for mode in (RunMode.CTF, RunMode.COMPETITION):
            with self.subTest(mode=mode):
                gate = AsyncMock(return_value=False)
                audit = Mock()
                policy = PolicyEngine(mode, ScopeGuard(Scope(local_analysis=True)),
                                      approval_gate=gate, audit=audit)
                registry = ToolRegistry(Mock(), policy=policy)
                fn = AsyncMock(return_value=ToolResult(tool="sandbox", ok=True))
                registry.register_builtin("sandbox", fn, ToolSpec(
                    name="sandbox", kind=ToolKind.BUILTIN, action_required=True))
                for action in DANGEROUS_ACTIONS:
                    result = await asyncio.wait_for(registry.call("sandbox", {"action": action}), 1)
                    self.assertTrue(result.ok)
                gate.assert_not_awaited()
                self.assertEqual(fn.await_count, len(DANGEROUS_ACTIONS))
                records = [c.args[2] for c in audit.log.call_args_list
                           if c.args[0] == "policy.auto_approved"]
                self.assertEqual(len(records), len(DANGEROUS_ACTIONS))
                self.assertTrue(all(r["invocation_id"] and r["params_digest"] for r in records))

                # 即使宿主将工具定为破坏性，CTF / 智能体模式也按此次模式预授权执行并记账。
                recorded = Mock()
                token = current_invocation.set(InvocationContext("test-invocation", "digest", recorded))
                try:
                    await policy.ensure_tool_allowed(ToolSpec(
                        name="host-tool", kind=ToolKind.BUILTIN, risk=RiskLevel.DESTRUCTIVE))
                finally:
                    current_invocation.reset(token)
                recorded.assert_called_once_with("host-tool", True)

    async def test_src_still_requires_human_approval(self) -> None:
        gate = AsyncMock(return_value=False)
        policy = PolicyEngine(RunMode.SRC, ScopeGuard(Scope(local_analysis=True)), approval_gate=gate)
        with self.assertRaises(PolicyViolation):
            await policy.ensure_tool_allowed(ToolSpec("shell", ToolKind.BUILTIN),
                                             action="webshell_upload")
        gate.assert_awaited_once()

    async def test_ctf_real_cli_executes_without_entering_human_wait_state(self) -> None:
        gate = AsyncMock(return_value=False)
        audit, journal = Mock(), Mock()
        policy = PolicyEngine(RunMode.CTF, ScopeGuard(Scope(local_analysis=True)),
                              approval_gate=gate, audit=audit)
        registry = ToolRegistry(LocalExecutor(), policy=policy, journal=journal)
        registry.register(ToolSpec(
            name="ctf_fixture", kind=ToolKind.CLI, risk=RiskLevel.INTRUSIVE,
            command_template=[sys.executable, "-c", "print('ctf-auto-approved')"],
            action_required=True, allowed_params={"action"}))

        result = await asyncio.wait_for(registry.call("ctf_fixture", {"action": "delete_files"}), 5)

        self.assertTrue(result.ok, result.error)
        self.assertIn("ctf-auto-approved", result.stdout)
        gate.assert_not_awaited()
        journal.approval.assert_called_once_with(result.invocation_id, "delete_files", True)
        transitions = [c.args[1] for c in journal.transition.call_args_list]
        self.assertNotIn("awaiting_approval", transitions)
        self.assertIn("running", transitions)
        self.assertIn("succeeded", transitions)

    async def test_ctf_every_risk_level_and_tool_name_is_auto_approved(self) -> None:
        gate = AsyncMock(return_value=False)
        audit = Mock()
        policy = PolicyEngine(RunMode.CTF, ScopeGuard(Scope(local_analysis=True)),
                              approval_gate=gate, audit=audit)
        recorded = Mock()
        token = current_invocation.set(InvocationContext("ctf-invocation", "ctf-digest", recorded))
        try:
            for risk in RiskLevel:
                spec = ToolSpec(name="port_scan", kind=ToolKind.CLI, risk=risk)
                # 同步注册检查与异步执行检查必须遵循相同模式审批语义。
                policy.check_tool(spec, action="delete_files")
                await policy.ensure_tool_allowed(spec, action="delete_files")
            await policy.ensure_tool_allowed(ToolSpec(
                name="read_fixture", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
        finally:
            current_invocation.reset(token)
        gate.assert_not_awaited()
        self.assertEqual(recorded.call_count, len(RiskLevel) + 1)
        approved = [c.args[2] for c in audit.log.call_args_list if c.args[0] == "policy.auto_approved"]
        self.assertEqual(len(approved), len(RiskLevel) + 1)
        self.assertTrue(all(r["mode"] == "ctf" and r["params_digest"] == "ctf-digest" for r in approved))

    async def test_autonomous_question_never_creates_pending_or_fakes_user_answer(self) -> None:
        audit = Mock()
        broker = QuestionBroker(audit, interactive=False)
        result = await asyncio.wait_for(broker.ask(
            "master", "接下来怎么做？", ["继续", "停止"], ["https://example.invalid"]), 1)
        self.assertTrue(result["autonomous"])
        self.assertEqual(result["scope_added"], [])
        self.assertEqual(broker.view(), [])
        self.assertNotIn("user.answered", [c.args[0] for c in audit.log.call_args_list])

    async def test_interactive_question_still_waits(self) -> None:
        broker = QuestionBroker()
        waiting = asyncio.create_task(broker.ask("master", "确认吗？", ["是", "否"]))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.assertEqual(len(broker.view()), 1)
        broker.cancel_all()
        await waiting
