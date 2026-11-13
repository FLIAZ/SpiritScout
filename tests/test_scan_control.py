"""主控派遣、实时撤回、旧指令竞争与扫描工具执行门；不调用真实模型。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.scan_control import ScannerController
from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.hypotheses import upsert
from spiritscout.config.settings import SystemConfig
from spiritscout.core.models import (
    AgentType,
    Authorization,
    BlackboardLayer,
    RunMode,
    Scope,
)
from spiritscout.tools.metasploit import MetasploitScanner
from spiritscout.tools.scanner import NucleiScanner
from spiritscout.tools.spec import ToolResult


class ScanControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bb = Blackboard(self.root / 'bb.db')
        self.addAsyncCleanup(self.bb.aclose)
        self.infra = SimpleNamespace(blackboard=self.bb, audit=Mock(), cfg=SystemConfig())
        self.control = ScannerController(self.infra, None, mission='测试任务', standing_rules='用户原始规则')
        self.hypothesis_ref = upsert(
            self.bb, claim='目标可能暴露特定组件漏洞', subject='http://fixture.test',
            test_type='cve', technical_basis={'tags': ['tech'], 'cve_ids': ['CVE-2026-0001']},
            actor='master', agent_type=AgentType.MASTER,
        )['hypothesis_ref']
        self.addAsyncCleanup(self.control.close)

    def command(self, action='dispatch', **changes):
        return {'action': action, 'revision': self.control.revision, 'reason': '依据用户许可与当前规则',
                'target': 'http://fixture.test', 'goal': '核实 HTTP 环境',
                'hypothesis_ref': self.hypothesis_ref, 'tools': ['nuclei_scan'], **changes}

    async def test_nuclei_dispatch_requires_active_hypothesis_and_technical_basis(self):
        missing = await self.control.apply(
            self.command(hypothesis_ref=''), actor='master')
        self.assertFalse(missing['ok'])
        self.assertIn('漏洞假设', missing['error'])

        unsupported = upsert(
            self.bb, claim='仅有通用存活信息', subject='http://fixture.test',
            test_type='generic', actor='master', agent_type=AgentType.MASTER,
        )['hypothesis_ref']
        no_basis = await self.control.apply(
            self.command(hypothesis_ref=unsupported), actor='master')
        self.assertFalse(no_basis['ok'])
        self.assertIn('技术依据', no_basis['error'])

        from spiritscout.blackboard.hypotheses import transition

        transition(
            self.bb, self.hypothesis_ref, state='exhausted', reason='最小实验已完成',
            actor='master', agent_type=AgentType.MASTER,
        )
        terminal = await self.control.apply(self.command(), actor='master')
        self.assertFalse(terminal['ok'])
        self.assertIn('活动', terminal['error'])

    async def test_idle_does_not_start_worker_or_allow_registered_scanning_tools(self):
        with patch('spiritscout.agents.scanner._run', new_callable=AsyncMock) as worker, \
             patch('spiritscout.tools.scanner.sandbox_exec', new_callable=AsyncMock) as nuclei, \
             patch('spiritscout.tools.metasploit.sandbox_exec', new_callable=AsyncMock) as msf:
            self.assertIsNone(self.control.task)
            ctx = {'_scan_control': self.control, 'workspace_root': str(self.root)}
            result = await NucleiScanner(self.infra.cfg.scanner, self.bb).run(
                ['http://fixture.test'], tags=['tech'], **ctx)
            self.assertFalse(result.ok)
            result = await MetasploitScanner(self.infra.cfg.scanner, self.bb).run(
                'auxiliary/scanner/http/http_version', {}, operation='run', **ctx)
            self.assertFalse(result.ok)
            worker.assert_not_awaited()
            nuclei.assert_not_awaited()
            msf.assert_not_awaited()

    def test_natural_worker_completion_updates_control_status_and_result(self):
        self.control.status = 'running'
        self.control.permitted = True
        self.control.worker_id = 'scanner-worker'

        self.control.worker_finished('scanner-worker', 'done', '只确认根域，未发现子域')

        self.assertEqual(self.control.status, 'done')
        self.assertFalse(self.control.permitted)
        self.assertEqual(self.control.last_result['action'], 'worker_complete')
        self.assertIn('未发现子域', self.control.last_result['summary'])
        saved = self.bb.read(BlackboardLayer.TASK_STATE, 'scanner-control')
        self.assertEqual(saved['status'], 'done')

    def test_incomplete_worker_delivery_is_not_coerced_to_done(self):
        self.control.status = 'running'
        self.control.permitted = True
        self.control.worker_id = 'scanner-worker'

        self.control.worker_finished(
            'scanner-worker', 'incomplete', '只确认根域，未发现子域')

        self.assertEqual(self.control.status, 'incomplete')
        self.assertTrue(self.control.last_result['ok'])

    async def test_stopping_background_scan_does_not_interrupt_other_work(self):
        scan_entered, other_entered = asyncio.Event(), asyncio.Event()
        other_can_finish, other_finished = asyncio.Event(), asyncio.Event()

        async def scan():
            scan_entered.set()
            await asyncio.Event().wait()

        async def other_work():
            other_entered.set()
            await other_can_finish.wait()
            other_finished.set()

        async def worker(*args, **kwargs):
            await self.control.run_tool('nuclei_scan', scan)

        other = asyncio.create_task(other_work())
        try:
            await asyncio.wait_for(other_entered.wait(), 1)
            with patch('spiritscout.agents.scanner._run', side_effect=worker):
                result = await asyncio.wait_for(self.control.apply(self.command(), actor='master'), 1)
                self.assertTrue(result['ok'])
                await asyncio.wait_for(scan_entered.wait(), 1)
                scan_task = self.control.task
                self.assertFalse(scan_task.done())
                self.assertFalse(other.done())
                stopped = await asyncio.wait_for(self.control.apply(self.command('stop'), actor='master'), 1)
                self.assertTrue(stopped['ok'])
                self.assertTrue(scan_task.cancelled())
                self.assertEqual(self.control.calls, set())
                self.assertFalse(other.done(), '停止扫描只能撤回扫描自己的任务')
                other_can_finish.set()
                await asyncio.wait_for(other_finished.wait(), 1)
                await other
        finally:
            if not other.done():
                other.cancel()
            await asyncio.gather(other, return_exceptions=True)

    async def test_dispatch_is_parallel_and_pause_resume_preserves_assignment(self):
        entered = asyncio.Event()
        assignments = []
        async def worker(*args, **kwargs):
            assignments.append(kwargs['delegation'])
            entered.set()
            await asyncio.Event().wait()
        with patch('spiritscout.agents.scanner._run', side_effect=worker):
            result = await self.control.apply(self.command(), actor='master')
            self.assertTrue(result['ok'])
            await asyncio.wait_for(entered.wait(), 1)
            first = self.control.task
            self.assertFalse(first.done())
            await self.control.apply(self.command('pause'), actor='master')
            self.assertTrue(first.cancelled())
            self.assertFalse(self.control.permitted)
            entered.clear()
            await self.control.apply(self.command('resume'), actor='master')
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(assignments[0], assignments[1])
            self.assertIsNot(self.control.task, first)

    async def test_forbid_cancels_inflight_tools_and_stale_dispatch_cannot_restart(self):
        with patch('spiritscout.agents.scanner._run', new_callable=AsyncMock):
            await self.control.apply(self.command(), actor='master')
            old = self.command()
            started, cleaned = asyncio.Event(), asyncio.Event()
            async def execute():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            caller = asyncio.create_task(self.control.run_tool('nuclei_scan', execute))
            await asyncio.wait_for(started.wait(), 1)
            await self.control.apply(self.command('forbid'), actor='master')
            self.assertTrue(cleaned.is_set())
            self.assertFalse((await caller).ok)
            self.assertFalse((await self.control.apply(old, actor='old-plan'))['ok'])
            self.assertFalse((await self.control.apply(self.command(), actor='master'))['ok'])
            self.assertTrue(self.control.forbidden)
            await self.control.apply(self.command('allow'), actor='master')
            self.assertFalse(self.control.permitted, '解除禁止不自动启动或重放')
            self.assertEqual(self.control.assignment, {})

    async def test_new_user_message_holds_scanning_before_model_handles_it(self):
        with patch('spiritscout.agents.scanner._run', new_callable=AsyncMock):
            await self.control.apply(self.command(), actor='master')
            old = self.command()
            await self.control.hold_for_user('new-message')
            self.assertFalse(self.control.permitted)
            self.assertEqual(self.control.status, 'paused')
            revision = self.control.revision
            await self.control.hold_for_user('new-message')
            self.assertEqual(self.control.revision, revision, '同条消息重试不自激更新')
            self.assertFalse((await self.control.apply(old, actor='stale-plan'))['ok'])
            denied = AsyncMock(return_value=ToolResult(tool='metasploit_scan', ok=True))
            await self.control.apply(self.command('resume'), actor='master')
            self.assertFalse((await self.control.run_tool('metasploit_scan', denied)).ok)
            denied.assert_not_awaited()

    async def test_main_controller_payload_and_actual_decision_receipt(self):
        master = object.__new__(MasterAgent)
        master.agent_id = 'master'
        master.ctx = SimpleNamespace(extra={'scan_controller': self.control})
        self.assertIn('metasploit_scan', master._scanner_context()['capabilities'])
        await master._apply_scanner_control({'scan_control': self.command('forbid')})
        self.assertTrue(self.control.forbidden)
        self.assertTrue(master.ctx.extra['last_scan_control']['ok'])
        result = await master._apply_scanner_control({'scan_control': self.command()})
        self.assertFalse(result['ok'])
        self.assertIsNone(self.control.task)

    async def test_wildcard_src_starts_readonly_discovery_without_waiting_for_plan(self):
        master = object.__new__(MasterAgent)
        master.agent_id = 'master'
        control = SimpleNamespace(
            forbidden=False, status='idle', revision=4,
            apply=AsyncMock(return_value={'ok': True, 'action': 'dispatch'}))
        master.ctx = SimpleNamespace(extra={'scan_controller': control})
        authorization = Authorization(
            auth_id='auth', mode=RunMode.SRC,
            scope=Scope(allowed_domains=['*.example.test']))

        result = await master._start_wildcard_discovery(
            authorization, 'https://example.test')

        self.assertTrue(result['ok'])
        command = control.apply.await_args.args[0]
        self.assertEqual(command['target'], '*.example.test')
        self.assertEqual(command['tools'], ['asset_discover'])
        self.assertNotIn('nuclei_scan', command['tools'])

    async def test_non_wildcard_src_does_not_auto_start_scanner(self):
        master = object.__new__(MasterAgent)
        master.agent_id = 'master'
        control = SimpleNamespace(
            forbidden=False, status='idle', revision=0, apply=AsyncMock())
        master.ctx = SimpleNamespace(extra={'scan_controller': control})
        authorization = Authorization(
            auth_id='auth', mode=RunMode.SRC,
            scope=Scope(allowed_domains=['example.test']))

        result = await master._start_wildcard_discovery(
            authorization, 'https://example.test')

        self.assertIsNone(result)
        control.apply.assert_not_awaited()

    async def test_forbid_survives_continuation_without_auto_resuming_old_assignment(self):
        await self.control.apply(self.command('forbid'), actor='master')
        previous_revision = self.control.revision
        resumed = ScannerController(self.infra, None, mission='续聊', standing_rules='原规则')
        try:
            self.assertTrue(resumed.forbidden)
            self.assertFalse(resumed.permitted)
            self.assertGreater(resumed.revision, previous_revision)
            self.assertIsNone(resumed.task)
            command = {**self.command(), 'revision': resumed.revision}
            self.assertFalse((await resumed.apply(command, actor='master'))['ok'])
        finally:
            await resumed.close()

    async def test_user_feed_suspends_scan_even_if_main_model_fails(self):
        master = object.__new__(MasterAgent)
        master.agent_id = 'master'
        inbox = [{'statement_id': 'new-rule', 'text': '本次不要扫描'}]
        master.ctx = SimpleNamespace(extra={'scan_controller': self.control, 'user_inbox': inbox},
                                     blackboard=self.bb, audit=Mock())
        with patch('spiritscout.agents.scanner._run', new_callable=AsyncMock):
            await self.control.apply(self.command(), actor='master')
            async def failed(*args, **kwargs):
                self.assertFalse(self.control.permitted, '模型处理新规则之前已挂起扫描')
                return None
            master._llm_user_chat = AsyncMock(side_effect=failed)
            self.assertFalse(await master._respond_user_feed())
            self.assertEqual(self.control.status, 'paused')
            self.assertEqual(len(inbox), 1)


if __name__ == '__main__':
    unittest.main()
