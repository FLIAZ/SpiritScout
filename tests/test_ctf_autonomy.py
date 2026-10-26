"""普通 CTF 保持路线连续性，人工提交回执不诱发重复探测。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.ring import RingLedger
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, RunMode, Task, TaskStatus
from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.ctf.flags import FlagManager, SubmitResult
from spiritscout.runtime import run_ctf_engagement


class CTFAutonomyTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_is_delivered_without_waiting_for_experience_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / 'report.md'
            report.write_text('完整解题报告和来源证据', encoding='utf-8')
            infra = SimpleNamespace(workspace=root, experience=object(), bus=None,
                                    audit=NullAuditLogger(), close=AsyncMock())
            pending = asyncio.get_running_loop().create_future()

            async def old_blocking_distill(*args, **kwargs):
                await pending

            with patch('spiritscout.runtime._build_infra', AsyncMock(return_value=infra)), \
                    patch('spiritscout.runtime._make_assembler'), \
                    patch('spiritscout.runtime._run_master', AsyncMock(return_value={
                        'rounds_executed': 1})), \
                    patch('spiritscout.runtime._render_writeup', AsyncMock(return_value=report)), \
                    patch('spiritscout.runtime._schedule_background_distill',
                          return_value=pending) as schedule, \
                    patch('spiritscout.runtime._distill_best_effort', old_blocking_distill), \
                    patch('spiritscout.runtime._foreground_started'), \
                    patch('spiritscout.runtime._foreground_finished'):
                result = await asyncio.wait_for(run_ctf_engagement(
                    cfg=SimpleNamespace(), models=[], authorization=SimpleNamespace(mode=RunMode.CTF),
                    challenges=[{'id': 'c1'}], report_path=report), timeout=0.5)
                schedule.assert_called_once()
                self.assertFalse(pending.done())
                self.assertEqual(result.run_summary['experience_distill']['status'], 'queued')
                self.assertEqual(Path(result.report_path).read_text(encoding='utf-8'),
                                 '完整解题报告和来源证据')
                infra.close.assert_awaited_once()
            pending.cancel()

    async def test_dispatch_preserves_ctf_route_and_explicit_user_instruction(self) -> None:
        for mode, expected in ((RunMode.CTF, 0), (RunMode.SRC, 1),
                               (RunMode.COMPETITION, 1)):
            with self.subTest(mode=mode):
                master = object.__new__(MasterAgent)
                master.agent_id = 'master-test'
                ring = RingLedger()
                ring.steer('ctf_web', '用户补充的新证据', task_id='t1')
                observations = []

                async def solve(task: Task) -> None:
                    for _ in range(4):
                        observations.append(ring.steering_for('ctf_web', task_id='t1')['hint'])
                        await asyncio.sleep(0)
                    task.status = TaskStatus.DONE

                master.ctx = SimpleNamespace(
                    pause_gate=None, enable_llm_planning=True, ring=ring,
                    agents={AgentType.CTF_WEB: SimpleNamespace(run=solve)},
                    audit=NullAuditLogger(), extra={'run_mode': mode.value}, blackboard=None)
                master._assign_dispatch_plan = AsyncMock()
                master._steering_watch = AsyncMock()
                master._platform_release = AsyncMock()
                master._write_dag_snapshot = Mock()
                dag = TaskDAG()
                node = dag.add_node('CTF-WEB', '连续自主探索')
                task = Task(task_id='t1', task_type='ctf_solve', target='local',
                            agent_type=AgentType.CTF_WEB)
                result = await master._dispatch({
                    'dag': dag.to_dict(), 'round_no': 0, 'tasks_dispatched': [
                        {'node_id': node.node_id, 'task': task.model_dump(mode='json')}]})
                self.assertEqual(master._steering_watch.await_count, expected)
                self.assertEqual(observations, ['用户补充的新证据'] * 4)
                self.assertEqual(TaskDAG.from_dict(result['dag']).get(node.node_id).status,
                                 TaskStatus.DONE)

    @staticmethod
    def _solver(submit_fn=None) -> tuple:
        agent = object.__new__(CTFBaseAgent)
        agent.agent_id = 'ctf-web-test'
        agent.agent_type = AgentType.CTF_WEB
        agent.ctx = SimpleNamespace(audit=NullAuditLogger(), mode=RunMode.CTF)
        agent.flag_manager = FlagManager(submit_fn=submit_fn)
        agent._record_flag = Mock()
        task = Task(task_id='t1', task_type='ctf_solve', target='local',
                    agent_type=AgentType.CTF_WEB, payload={'challenge': {'id': 'c1'}})
        return agent, task, SimpleNamespace(name='solve')

    async def test_manual_receipt_keeps_model_judgment_and_reports_no_submission(self) -> None:
        agent, task, route = self._solver()
        for _ in range(2):
            out = await agent._tool_submit_flag(task, route, {}, {
                'flag': 'SYC{fixture}', 'note': '已有完整来源与独立复现证据'})
            self.assertFalse(out.stop)
            self.assertIn('未配置提交接口', out.text)
            self.assertIn('人工提交', out.text)
            self.assertIn('无需重复', out.text)
            self.assertNotIn('已登记并提交', out.text)
        self.assertEqual(agent.flag_manager.stats()['accepted'], 0)
        self.assertEqual(agent.flag_manager.stats()['submitted'], 0)

    async def test_configured_but_unresolved_endpoint_is_not_manual_success(self) -> None:
        async def unresolved(value: str, challenge: str) -> SubmitResult:
            return SubmitResult(None, '仍在判定')

        agent, task, route = self._solver(unresolved)
        agent.flag_manager.max_attempts = 1
        out = await agent._tool_submit_flag(task, route, {}, {'flag': 'SYC{fixture}'})
        self.assertFalse(out.stop)
        self.assertIn('未获平台判定', out.text)
        self.assertNotIn('人工提交', out.text)
