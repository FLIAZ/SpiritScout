"""未完成题的暂停、取消工作与销毁实例分别决策。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import TaskStatus


class InstanceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setup_master(self):
        dag = TaskDAG()
        node = dag.add_node('CTF-WEB', '调查', meta={'challenge': {'id': 'c1'}})
        dag.mark(node.node_id, TaskStatus.RUNNING)
        platform = SimpleNamespace(close_instance=AsyncMock(return_value=True))
        master = object.__new__(MasterAgent)
        master.agent_id = 'test'
        master.ctx = SimpleNamespace(audit=NullAuditLogger(), extra={
            'mode_prompt': '自主竞赛', 'platform': platform})
        return master, dag, node, platform

    async def test_deferral_alone_keeps_running_work_and_instance(self):
        master, dag, node, platform = self.setup_master()
        await master._apply_competition_decisions(dag, {'defer_challenges': [
            {'id': 'c1', 'reason': '暂不增加派工'}]})
        self.assertTrue(node.meta['deferred'])
        self.assertEqual(node.status, TaskStatus.RUNNING)
        platform.close_instance.assert_not_awaited()

    async def test_explicit_cancellation_preserves_instance(self):
        master, dag, node, platform = self.setup_master()
        await master._apply_competition_decisions(dag, {'defer_challenges': [
            {'id': 'c1', 'reason': '换方向接力', 'cancel_running': True}]})
        self.assertEqual(node.status, TaskStatus.CANCELLED)
        platform.close_instance.assert_not_awaited()

    async def test_explicit_release_closes_instance_and_cancels_node(self):
        master, dag, node, platform = self.setup_master()
        await master._apply_competition_decisions(dag, {'defer_challenges': [
            {'id': 'c1', 'reason': '现场可重建，腾名额', 'release_instance': True}]})
        self.assertEqual(node.status, TaskStatus.CANCELLED)
        platform.close_instance.assert_awaited_once()

    async def test_string_true_does_not_authorize_release(self):
        master, dag, node, platform = self.setup_master()
        await master._apply_competition_decisions(dag, {'defer_challenges': [
            {'id': 'c1', 'reason': '暂缓', 'release_instance': 'true'}]})
        self.assertEqual(node.status, TaskStatus.RUNNING)
        platform.close_instance.assert_not_awaited()


class CompetitionRuntimeCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuation_preserves_only_successful_internal_returns(self) -> None:
        import asyncio
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from spiritscout.competition.rules import CompetitionRules
        from spiritscout.config.settings import (
            ModelEntry,
            SystemConfig,
            WorkspaceConfig,
        )
        from spiritscout.core.models import Authorization, RunMode, Scope
        from spiritscout.runtime import run_competition_engagement

        for preserve, failure in [(False, None), (True, None),
                                  (True, RuntimeError('失败')), (True, asyncio.CancelledError())]:
            with self.subTest(preserve=preserve, failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp), audit_log_path=str(root / 'audit.jsonl'))
                cfg.blackboard.sqlite_path = str(root / 'bb.db')
                cfg.knowledge.sqlite_path = str(root / 'kb.db')
                cfg.experience.root = str(root / 'experience')
                adapter = SimpleNamespace(
                    precheck=AsyncMock(), list_challenges=AsyncMock(return_value=[]),
                    has_completion_signal=lambda: True, is_completed=lambda _: False,
                    facts=[], submit=AsyncMock(), summary=lambda: {},
                    close_all=AsyncMock(), aclose=AsyncMock())
                master = AsyncMock(return_value={}, side_effect=failure)
                with patch('spiritscout.ctf.platform.PlatformAdapter', return_value=adapter), \
                        patch('spiritscout.runtime._run_master', master), \
                        patch('spiritscout.runtime._schedule_background_distill', return_value=None):
                    async def run():
                        return await run_competition_engagement(
                            cfg=cfg, models=[ModelEntry(model='fixture', provider='mock')],
                            authorization=Authorization(auth_id='test', mode=RunMode.COMPETITION, human_confirmed=True,
                                                        scope=Scope(local_analysis=True)),
                            rules=CompetitionRules.from_dict({}), challenges=[],
                            platform_profile={'base_url': 'http://fixture.test', 'ops': {
                                'list': {'path': '/challenges'}}},
                            preserve_instances_on_return=preserve)
                    if failure is not None:
                        with self.assertRaises(type(failure)):
                            await run()
                    else:
                        await run()
                self.assertEqual(adapter.close_all.await_count, int(not preserve or failure is not None))
                adapter.aclose.assert_awaited_once()


class LiveDispatchDeferralTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferral_only_cancels_running_coroutine_when_explicit(self) -> None:
        import asyncio
        import json
        from unittest.mock import Mock

        import httpx

        from spiritscout.core.models import AgentType, Task
        from spiritscout.ctf.platform import PlatformAdapter, parse_platform_profile
        from tests.test_competition_continuous import ContinuousCompetitionTests
        from tests.test_platform_adapter import PROFILE, _challenge

        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)
                def handle(request):
                    if request.url.path.endswith('/challenges'):
                        return httpx.Response(200, json={'data': {'challenges': [_challenge('c1')]}})
                    return httpx.Response(200, json={'container_addr': 'http://c1.box'})
                adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
                started, stopped, applied, hold = (asyncio.Event() for _ in range(4))
                await adapter.list_challenges()
                await adapter.ensure_instance({'id': 'c1'})
                adapter.close_instance = AsyncMock()

                async def solver(task):
                    started.set()
                    try:
                        await hold.wait()
                    finally:
                        stopped.set()

                master = ContinuousCompetitionTests()._master(adapter, solver, planning=True)
                master.ctx.ring = Mock()
                original = master._apply_competition_decisions
                async def apply(dag, decision):
                    await original(dag, decision)
                    applied.set()
                master._apply_competition_decisions = apply
                async def steer(ids, decisions):
                    await started.wait()
                    decisions.put_nowait({'defer_challenges': [{
                        'id': 'c1', 'reason': '保留现场，调整派工', 'cancel_running': cancel}]})
                    await hold.wait()
                master._steering_watch = steer
                dag = TaskDAG()
                node = dag.add_node('CTF-WEB', '调查', meta={'challenge': {'id': 'c1'}})
                dag.mark(node.node_id, TaskStatus.RUNNING)
                task = Task(task_id='t1', task_type='ctf_solve', target='http://c1.box',
                            agent_type=AgentType.CTF_WEB, payload={'challenge': {'id': 'c1'}})
                dispatch = asyncio.create_task(master._dispatch({
                    'dag': dag.to_dict(), 'round_no': 0, 'target': task.target,
                    'tasks_dispatched': [{'node_id': node.node_id, 'task': json.loads(task.model_dump_json())}]}))
                try:
                    await asyncio.wait_for(applied.wait(), 1)
                    self.assertEqual(stopped.is_set(), cancel)
                    adapter.close_instance.assert_not_awaited()
                    self.assertEqual(adapter.active_instance_ids(), {'c1'})
                finally:
                    hold.set()
                    dispatch.cancel()
                    await asyncio.gather(dispatch, return_exceptions=True)
                    await adapter.aclose()
