"""并行扫描的生命周期、分模式审批、黑板证据与原文保留。"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.agents.scanner import close_scanner, start_scanner
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ScannerConfig, SystemConfig
from spiritscout.core.exceptions import PolicyViolation
from spiritscout.core.models import (
    AgentType,
    Authorization,
    BlackboardLayer,
    RunMode,
    Scope,
)
from spiritscout.policy import PolicyEngine, ScopeGuard
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.registry import AgentToolView, ToolRegistry
from spiritscout.tools.scanner import SPEC, NucleiScanner
from spiritscout.tools.spec import ToolResult


class ScannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bb = Blackboard(self.root / "bb.db")
        self.addAsyncCleanup(self.bb.aclose)
        self.guard = ScopeGuard(Scope(allowed_urls=["http://fixture.test"], max_requests_per_second=3))
        self.config = ScannerConfig()
        self.service = NucleiScanner(self.config, self.bb)
        self.ctx = {"workspace_root": str(self.root), "guard": self.guard}
        self.rows = [
            {"template-id": "fixture", "matched-at": "http://fixture.test/login",
             "info": {"name": "Fixture evidence", "severity": "info"},
             "request": "GET /login HTTP/1.1", "response": "HTTP/1.1 200 OK"},
            {"template-id": "fixture-2", "host": "http://fixture.test", "info": {"severity": "low"}},
        ]

    async def fake_scan(self, command, **kwargs):
        args = shlex.split(command)
        output = Path(args[args.index("-o") + 1])
        output.write_text('\n'.join(json.dumps(r) for r in self.rows) + '\n')
        self.assertIn("-duc", args)
        self.assertIn("-ni", args)
        self.assertEqual(args[args.index("-rl") + 1], "3")
        return ToolResult(tool="sandbox_exec", ok=True, returncode=0)

    def registry(self, mode, approval=None):
        registry = ToolRegistry(LocalExecutor(), policy=PolicyEngine(mode, self.guard, approval_gate=approval))
        registry.register_builtin(SPEC.name, self.service.run, SPEC)
        return registry

    async def test_raw_output_and_findings_are_saved_without_promoting_vulnerabilities(self):
        with patch("spiritscout.tools.scanner.sandbox_exec", side_effect=self.fake_scan):
            result = await self.registry(RunMode.CTF).call("nuclei_scan",
                {"targets": ["http://fixture.test"], "tags": ["tech"], "action": "analysis"}, **self.ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.parsed["findings"], 2)
        raw = Path(result.parsed["raw_output"])
        self.assertIn('GET /login HTTP/1.1', raw.read_text())
        clues = self.bb.query(BlackboardLayer.RECON_CLUE, limit=100)
        self.assertEqual(len(clues), 3)
        self.assertTrue(all(r["data"]["verification_status"] == "unverified" for r in clues))
        self.assertEqual(self.bb.query(BlackboardLayer.VULN), [])

    async def test_src_approval_wait_does_not_execute_or_block_other_coroutines(self):
        asked, allow = asyncio.Event(), asyncio.Event()
        async def approval(action, detail):
            asked.set()
            await allow.wait()
            return True
        backend = AsyncMock(side_effect=self.fake_scan)
        with patch("spiritscout.tools.scanner.sandbox_exec", backend):
            task = asyncio.create_task(self.registry(RunMode.SRC, approval).call("nuclei_scan",
                {"targets": ["http://fixture.test"], "tags": ["default-login"], "action": "brute_force"},
                **self.ctx, _purpose="核实该目标的默认登录线索，可能触发登录失败计数"))
            await asyncio.wait_for(asked.wait(), 1)
            self.assertFalse(task.done())
            backend.assert_not_awaited()
            allow.set()
            self.assertTrue((await task).ok)

    async def test_ctf_sensitive_action_never_asks_user(self):
        approval = AsyncMock(return_value=False)
        with patch("spiritscout.tools.scanner.sandbox_exec", side_effect=self.fake_scan):
            result = await self.registry(RunMode.CTF, approval).call("nuclei_scan",
                {"targets": ["http://fixture.test"], "template_ids": ["fixture"], "action": "brute_force"},
                **self.ctx)
        self.assertTrue(result.ok)
        approval.assert_not_awaited()

    async def test_failure_keeps_partial_findings_and_failure_status(self):
        async def partial(command, **ctx):
            await self.fake_scan(command, **ctx)
            return ToolResult(tool="sandbox_exec", ok=False, returncode=-9, timed_out=True)
        with patch("spiritscout.tools.scanner.sandbox_exec", side_effect=partial):
            result = await self.service.run(["http://fixture.test"], tags=["tech"], **self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.parsed["status"], "failed")
        self.assertEqual(result.parsed["findings"], 2)

    async def test_cancel_keeps_partial_output_and_scan_record(self):
        started = asyncio.Event()
        async def slow(command, **ctx):
            await self.fake_scan(command, **ctx)
            started.set()
            await asyncio.Event().wait()
        with patch("spiritscout.tools.scanner.sandbox_exec", side_effect=slow):
            task = asyncio.create_task(self.service.run(["http://fixture.test"], tags=["tech"], **self.ctx))
            await started.wait()
            await close_scanner(task)
        scans = self.bb.query(BlackboardLayer.RECON_CLUE, data_filter=("clue_type", "scanner_run"))
        self.assertEqual(scans[0]["data"]["status"], "cancelled")
        self.assertEqual(scans[0]["data"]["findings"], 2)

    async def test_invalid_and_missing_selection_never_execute(self):
        with patch("spiritscout.tools.scanner.sandbox_exec", new_callable=AsyncMock) as backend:
            for targets, tags in ((["http://fixture.test"], []), (["-config"], ["tech"]),
                                  (["http://fixture.test\nother"], ["tech"])):
                result = await self.service.run(targets, tags=tags, **self.ctx)
                self.assertFalse(result.ok)
            backend.assert_not_awaited()

    async def test_malformed_and_symlink_output_cannot_be_success(self):
        secret = self.root / "private"
        secret.write_text(json.dumps(self.rows[0]))
        async def bad(command, **ctx):
            args = shlex.split(command)
            Path(args[args.index("-o") + 1]).symlink_to(secret)
            return ToolResult(tool="sandbox_exec", ok=True)
        with patch("spiritscout.tools.scanner.sandbox_exec", side_effect=bad):
            result = await self.service.run(["http://fixture.test"], tags=["tech"], **self.ctx)
        self.assertFalse(result.ok)
        self.assertEqual(result.parsed["findings"], 0)

    def infra(self, mode=RunMode.CTF):
        config=SystemConfig()
        config.scanner.metasploit_enabled=False
        return SimpleNamespace(cfg=config, authorization=Authorization(auth_id="test", mode=mode,
            scope=self.guard.scope), pause_gate=None, blackboard=self.bb, builtin_ctx=self.ctx,
            audit=SimpleNamespace(log=lambda *a, **k: None), guard=self.guard,
            gateway=object(), agent_models={}, tools=self.registry(mode))

    async def test_specialist_dispatch_runs_script_in_parallel_and_keeps_request_prefix(self):
        from spiritscout.agents.scan_control import ScannerController
        from spiritscout.ctf.agents import CTFBaseAgent
        from spiritscout.gateway.gateway import ChatResponse

        infra = self.infra()
        control = ScannerController(infra, SimpleNamespace(), mission='分析现场机制', standing_rules='仅测试目标')
        self.addAsyncCleanup(control.close)
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []
        decisions = iter([
            {'decision': 'script', 'reason': '已观测接口的控制组实验',
             'arguments': {'code': 'print("observed")', 'language': 'python', 'action': 'analysis'}},
            {'decision': 'finish', 'reason': '独立实验结束，观察已交接'},
        ])
        async def chat(messages, **kwargs):
            import copy
            requests.append(copy.deepcopy(messages))
            return ChatResponse(json.dumps(next(decisions)), 'fixture', 1, 1, 0, 0)
        async def execute(name, args, **kwargs):
            self.assertEqual(name, 'sandbox_exec')
            self.assertEqual(args['code'], 'print("observed")')
            entered.set()
            await release.wait()
            return ToolResult(tool=name, ok=True, stdout='observed', invocation_id='script-evidence')
        gateway = SimpleNamespace(chat=chat)
        infra.tools = SimpleNamespace(call=execute)
        agent = object.__new__(CTFBaseAgent)
        agent.ctx = SimpleNamespace(builtin_ctx={'_scan_control': control}, audit=infra.audit)
        agent.agent_id = 'ctf-web-fixture'
        with patch('spiritscout.agents.scanner.AgentBoundGateway', return_value=gateway):
            status = await agent._run_tool_call_inner(None, None, {},
                {'name': 'scanner_control', 'arguments': {'action': 'status'}})
            revision = json.loads(status.text)['revision']
            result = await agent._run_tool_call_inner(None, None, {}, {'name': 'scanner_control',
                'arguments': {'action': 'dispatch', 'revision': revision, 'reason': '接口现场已有依据',
                              'target': 'http://fixture.test', 'goal': '独立对照实验',
                              'tools': ['sandbox_exec'], 'constraints': '仅此目标，不枚举其他接口'}})
            self.assertTrue(result.ok)
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(control.task.done(), '派遣回执不能等待扫描执行结束')
            release.set()
            await asyncio.wait_for(control.task, 1)
        self.assertEqual(requests[1][:len(requests[0])], requests[0], '追加新观察时不改写热前缀')
        clues = self.bb.query(BlackboardLayer.RECON_CLUE, data_filter=('clue_type', 'scanner_observation'))
        self.assertEqual(clues[0]['data']['evidence_refs'], ['script-evidence'])
        self.assertEqual(clues[0]['data']['detail']['summary'], 'observed')
        denied = await agent._run_tool_call_inner(None, None, {},
            {'name': 'scanner_control', 'arguments': {'action': 'allow'}})
        self.assertFalse(denied.ok, '专项角色不得解除主控禁止')

    async def test_worker_can_finish_without_scanning_even_when_scanner_is_available(self):
        infra = self.infra()
        with patch.object(NucleiScanner, "preflight", AsyncMock(return_value=ToolResult(tool="preflight", ok=True))), \
             patch("spiritscout.agents.scanner.chat_decision", AsyncMock(return_value=SimpleNamespace(
                 content='{"decision":"finish","reason":"仅附件分析，无网络扫描需要"}'))):
            task = start_scanner(infra, SimpleNamespace(), "attachment", mission="分析附件", standing_rules="")
            await task
        records = self.bb.query(BlackboardLayer.TASK_STATE)
        self.assertEqual(records[0]["data"]["status"], "done")
        self.assertEqual(self.bb.query(BlackboardLayer.RECON_CLUE), [])

    async def test_missing_tool_skips_without_model_call(self):
        infra = self.infra()
        with patch.object(NucleiScanner, "preflight", AsyncMock(return_value=ToolResult(tool="preflight", ok=False))), \
             patch("spiritscout.agents.scanner.chat_decision", new_callable=AsyncMock) as model:
            await start_scanner(infra, SimpleNamespace(), "target", mission="", standing_rules="")
        model.assert_not_awaited()
        self.assertEqual(self.bb.query(BlackboardLayer.TASK_STATE)[0]["data"]["status"], "skipped")

    async def test_wait_observes_updates_arriving_during_model_inference(self):
        async def decision(*args, **kwargs):
            if model.await_count == 1:
                self.bb.write(BlackboardLayer.RECON_CLUE, "service-version",
                    {"value": "fixture server version"}, agent_type=AgentType.RECON, updated_by="recon")
                return SimpleNamespace(content='{"decision":"wait","reason":"等环境指纹"}')
            return SimpleNamespace(content='{"decision":"finish","reason":"已收到新事实"}')
        with patch.object(NucleiScanner, "preflight", AsyncMock(return_value=ToolResult(tool="preflight", ok=True))), \
             patch("spiritscout.agents.scanner.chat_decision", AsyncMock(side_effect=decision)) as model:
            await asyncio.wait_for(start_scanner(self.infra(), SimpleNamespace(), "target",
                mission="", standing_rules=""), 2)
        self.assertEqual(model.await_count, 2)

    async def test_wait_ignores_own_observations_until_external_update(self):
        waiting = asyncio.Event()
        async def decision(*args, **kwargs):
            if model.await_count == 1:
                waiting.set()
                return SimpleNamespace(content='{"decision":"wait","reason":"等服务指纹"}')
            return SimpleNamespace(content='{"decision":"finish","reason":"收到指纹"}')
        with patch.object(NucleiScanner, "preflight", AsyncMock(return_value=ToolResult(tool="preflight", ok=True))), \
             patch("spiritscout.agents.scanner.chat_decision", AsyncMock(side_effect=decision)) as model:
            task = start_scanner(self.infra(), SimpleNamespace(), "target", mission="", standing_rules="")
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                self.bb.write(BlackboardLayer.RECON_CLUE, "own", {"value": "own observation"},
                    agent_type=AgentType.SCANNER, updated_by="scanner")
                await asyncio.sleep(1.1)
                self.assertEqual(model.await_count, 1)
                self.bb.write(BlackboardLayer.RECON_CLUE, "external", {"value": "service fingerprint"},
                    agent_type=AgentType.RECON, updated_by="recon")
                await asyncio.wait_for(task, 2)
            finally:
                await close_scanner(task)
        self.assertEqual(model.await_count, 2)

    async def test_worker_runs_scan_and_publishes_shared_observation(self):
        infra = self.infra()
        decisions = [SimpleNamespace(content=json.dumps({"decision": "scan", "reason": "验证现场指纹",
            "arguments": {"targets": ["http://fixture.test"], "tags": ["tech"], "action": "analysis"}})),
            SimpleNamespace(content='{"decision":"finish","reason":"线索已交接"}')]
        with patch.object(NucleiScanner, "preflight", AsyncMock(return_value=ToolResult(tool="preflight", ok=True))), \
             patch("spiritscout.agents.scanner.chat_decision", AsyncMock(side_effect=decisions)), \
             patch("spiritscout.tools.scanner.sandbox_exec", side_effect=self.fake_scan):
            await start_scanner(infra, SimpleNamespace(), "target", mission="测试", standing_rules="")
        observations = self.bb.query(BlackboardLayer.RECON_CLUE,
                                      data_filter=("clue_type", "scanner_observation"))
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["data"]["detail"]["result"]["findings"], 2)

    async def test_real_sandbox_cli_fixture_and_blackboard_pipeline(self):
        from spiritscout.tools.sandbox import sandbox_available

        if not sandbox_available():
            self.skipTest("本机无 bubblewrap，无法验证真实沙箱链路")
        # 夹具放在现有沙箱允许挂载的项目工具层；测试结束自动删除。
        with tempfile.TemporaryDirectory(dir=Path(sys.executable).parent, prefix="scan-fixture-") as fixture:
            tool = Path(fixture) / "nuclei-fixture"
            templates = Path(fixture) / "templates"
            templates.mkdir()
            tool.write_text('#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n'
                            'Path(sys.argv[sys.argv.index("-o")+1]).write_text('
                            'json.dumps({"template-id":"fixture","host":"http://fixture.test",'
                            '"info":{"severity":"info"}})+"\\n")\n')
            tool.chmod(0o700)
            service = NucleiScanner(ScannerConfig(binary=str(tool), templates_dir=str(templates)), self.bb)
            preflight = await service.preflight(**self.ctx)
            self.assertTrue(preflight.ok and preflight.returncode == 0, preflight.failure_reason())
            result = await service.run(["http://fixture.test"], tags=["fixture"], **self.ctx)
            self.assertTrue(result.ok, result.failure_reason())
            self.assertEqual(result.parsed["findings"], 1)

    async def test_runtime_waits_for_master_dispatch_and_cleans_up_on_return(self):
        from spiritscout.runtime import _run_master

        infra = self.infra()
        infra.gateway = SimpleNamespace(usage_report=lambda: {})
        infra.knowledge = None
        infra.ring = None
        infra.questions = None
        infra.convergence = None
        infra.bus = None
        infra.policy = PolicyEngine(RunMode.CTF, self.guard)
        infra.tools.journal = None
        infra.cfg.blackboard.sqlite_path = str(self.root / "bb.db")
        assembly = SimpleNamespace(notes=[], tactic_map=None, plan_provider=None, agents={})
        started = asyncio.Event()
        async def slow(**ctx):
            started.set()
            await asyncio.Event().wait()
        async def master_run(**kwargs):
            control = master_context.extra['scan_controller']
            self.assertIsNone(control.task)
            self.assertFalse(started.is_set())
            await control.apply({'action': 'dispatch', 'revision': control.revision,
                'reason': '按用户授权核对环境', 'target': 'http://fixture.test',
                'goal': '核对环境', 'tools': ['nuclei_scan']}, actor='master')
            await asyncio.wait_for(started.wait(), 1)
            return {"rounds_executed": 1}
        def master(context):
            nonlocal master_context
            master_context = context
            return SimpleNamespace(ctx=context, run=master_run)
        master_context = None
        verifier = SimpleNamespace(start=lambda: None, drain=AsyncMock(), stats=lambda: {}, close=AsyncMock())
        with patch("spiritscout.runtime.MasterAgent", side_effect=master), \
             patch("spiritscout.runtime._sub_ctx", return_value=None), \
             patch("spiritscout.agents.verifier.VerificationQueue", return_value=verifier), \
             patch.object(NucleiScanner, "preflight", side_effect=slow):
            summary = await asyncio.wait_for(_run_master(infra, assembly, "target",
                enable_llm_planning=True), 2)
        self.assertEqual(summary["rounds_executed"], 1)
        self.assertEqual(self.bb.read(BlackboardLayer.TASK_STATE, 'scanner-control')['status'], 'closed')
        self.assertTrue(any(row['data']['status'] == 'cancelled'
                            for row in self.bb.query(BlackboardLayer.TASK_STATE)))

    async def test_start_is_concurrent_and_close_cancels_waiting_worker(self):
        infra = self.infra()
        started = asyncio.Event()
        async def slow(**ctx):
            started.set()
            await asyncio.Event().wait()
        with patch.object(NucleiScanner, "preflight", side_effect=slow):
            task = start_scanner(infra, SimpleNamespace(), "target", mission="", standing_rules="")
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(task.done())
            await close_scanner(task)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.bb.query(BlackboardLayer.TASK_STATE)[0]["data"]["status"], "cancelled")

    async def test_lightweight_and_other_modes_do_not_spawn_extra_model(self):
        infra = self.infra()
        infra.cfg.gateway.lightweight_local = True
        self.assertIsNone(start_scanner(infra, None, "target", mission="", standing_rules=""))
        infra.cfg.gateway.lightweight_local = False
        infra.authorization.mode = RunMode.COMPETITION
        self.assertIsNone(start_scanner(infra, None, "target", mission="", standing_rules=""))
        infra.authorization.mode = RunMode.SRC
        infra.cfg.scanner.enabled = False
        self.assertIsNone(start_scanner(infra, None, "target", mission="", standing_rules=""))

    def test_scanner_role_has_tool_but_cannot_write_verified_findings(self):
        view = AgentToolView(self.registry(RunMode.CTF), AgentType.SCANNER)
        self.assertTrue(view.allows("nuclei_scan"))
        with self.assertRaises(PolicyViolation):
            self.bb.write(BlackboardLayer.VULN, "bad", {"status":"accepted", "poc_verified":True},
                          agent_type=AgentType.SCANNER, updated_by="scanner")


if __name__ == "__main__":
    unittest.main()
