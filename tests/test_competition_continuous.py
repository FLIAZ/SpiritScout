"""竞赛连续调度：中榜释放名额时，不等待同批或同题其他任务。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, BlackboardLayer, Task, TaskStatus
from spiritscout.ctf.platform import PlatformAdapter, parse_platform_profile
from spiritscout.web.app import _read_state
from spiritscout.web.sessions import Session
from tests.test_platform_adapter import PROFILE, _challenge


class ContinuousCompetitionTests(unittest.IsolatedAsyncioTestCase):
    def _master(self, adapter: PlatformAdapter, run, *, planning: bool = False) -> MasterAgent:
        master = object.__new__(MasterAgent)
        master.agent_id = 'refill-test'
        master._REFILL_INTERVAL = 0.005
        master._QUEUE_RETRY_INTERVAL = 0.01
        master.ctx = SimpleNamespace(
            pause_gate=None, enable_llm_planning=planning, ring=None,
            agents={AgentType.CTF_WEB: SimpleNamespace(run=run)},
            tactic_map={'CTF-WEB': (AgentType.CTF_WEB, 'ctf_solve')},
            policy=SimpleNamespace(guard=None), audit=NullAuditLogger(),
            extra={'platform': adapter, 'mode_prompt': '模型选题，空位持续补位'})
        master._write_dag_snapshot = Mock()
        master._rules = Mock(return_value='')
        master._assign_dispatch_plan = AsyncMock()
        return master

    async def test_ready_task_runs_before_next_slow_container_finishes_starting(self) -> None:
        """启动第二题的接口再慢，也不能让第一题已就绪的容器空等。"""
        first_running = asyncio.Event()
        second_starting = asyncio.Event()
        release_start = asyncio.Event()
        both_running = asyncio.Event()
        hold = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/challenges'):
                return httpx.Response(200, json={'data': {'challenges': [
                    _challenge(cid) for cid in ('c1', 'c2')]}})
            body = json.loads(request.content) if request.content else {}
            if request.url.path.endswith('/start') and body.get('unique_code') == 'c2':
                second_starting.set()
                await release_start.wait()
            return httpx.Response(200, json={'container_addr': 'http://fixture.box'})

        adapter = PlatformAdapter(parse_platform_profile({**PROFILE, 'limits': {'max_active': 3}}),
                                  continuous=True)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(adapter.aclose)
        await adapter.list_challenges()

        async def run(task: Task) -> None:
            if task.payload['challenge']['id'] == 'c1':
                first_running.set()
            else:
                both_running.set()
            await hold.wait()
            task.status = TaskStatus.DONE

        dag = TaskDAG()
        for cid in ('c1', 'c2'):
            dag.add_node('CTF-WEB', cid, meta={'challenge': {'id': cid}})
        master = self._master(adapter, run)
        dispatch = asyncio.create_task(master._dispatch({
            'dag': dag.to_dict(), 'round_no': 0, 'target': 'http://fixture.box',
            'tasks_dispatched': []}))
        try:
            await asyncio.wait_for(second_starting.wait(), 1)
            await asyncio.wait_for(first_running.wait(), 1)
            self.assertFalse(release_start.is_set())
            release_start.set()
            await asyncio.wait_for(both_running.wait(), 1)
        finally:
            hold.set()
            release_start.set()
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)

    async def test_all_slots_empty_after_start_failure_retry_without_other_task_events(self) -> None:
        """全部槽位为空时，失败退避到期也须自行唤醒，不等其他题结束。"""
        attempts = 0
        started = asyncio.Event()
        hold = asyncio.Event()

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.url.path.endswith('/challenges'):
                return httpx.Response(200, json={'data': {'challenges': [_challenge('c1')]}})
            if request.url.path.endswith('/start'):
                attempts += 1
                if attempts == 1:
                    return httpx.Response(503, json={'error': 'temporarily unavailable'})
            return httpx.Response(200, json={'container_addr': 'http://fixture.box'})

        adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)
        adapter._START_RETRY_BACKOFF = 0.02
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(adapter.aclose)
        await adapter.list_challenges()

        async def run(task: Task) -> None:
            started.set()
            await hold.wait()
            task.status = TaskStatus.DONE

        dag = TaskDAG()
        dag.add_node('CTF-WEB', 'c1', meta={'challenge': {'id': 'c1'}})
        master = self._master(adapter, run)
        dispatch = asyncio.create_task(master._dispatch({
            'dag': dag.to_dict(), 'round_no': 0, 'target': 'http://fixture.box',
            'tasks_dispatched': []}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(attempts, 2)
        finally:
            hold.set()
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)

    async def test_slow_idle_close_does_not_block_refilling_another_vacancy(self) -> None:
        """一道题慢速关闭时，另一个空位仍应立即启动后备题。"""
        closing = asyncio.Event()
        allow_close = asyncio.Event()
        next_running = asyncio.Event()
        hold = asyncio.Event()

        async def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/challenges'):
                return httpx.Response(200, json={'data': {'challenges': [
                    _challenge(cid) for cid in ('old', 'next')]}})
            body = json.loads(request.content) if request.content else {}
            if request.url.path.endswith('/close') and body.get('unique_code') == 'old':
                closing.set()
                await allow_close.wait()
            return httpx.Response(200, json={'container_addr': 'http://fixture.box'})

        adapter = PlatformAdapter(parse_platform_profile({**PROFILE, 'limits': {'max_active': 3}}),
                                  continuous=True)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(adapter.aclose)
        await adapter.list_challenges()
        await adapter.ensure_instance({'id': 'old'})

        async def run(task: Task) -> None:
            next_running.set()
            await hold.wait()
            task.status = TaskStatus.DONE

        dag = TaskDAG()
        old = dag.add_node('CTF-WEB', 'old', meta={'challenge': {'id': 'old'}})
        dag.mark(old.node_id, TaskStatus.DONE)
        dag.add_node('CTF-WEB', 'next', meta={'challenge': {'id': 'next'}})
        master = self._master(adapter, run)
        dispatch = asyncio.create_task(master._dispatch({
            'dag': dag.to_dict(), 'round_no': 0, 'target': 'http://fixture.box',
            'tasks_dispatched': []}))
        try:
            await asyncio.wait_for(closing.wait(), 1)
            await asyncio.wait_for(next_running.wait(), 1)
            self.assertFalse(allow_close.is_set())
        finally:
            allow_close.set()
            hold.set()
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)

    async def test_revisit_refills_without_waiting_for_last_running_challenge(self) -> None:
        """全部题均已尝试后，也能解除暂缓并在另一题结束前回访。"""
        hold = asyncio.Event()
        revisited = asyncio.Event()
        adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/challenges'):
                return httpx.Response(200, json={'data': {'challenges': [
                    _challenge(cid) for cid in ('slow', 'retry')]}})
            return httpx.Response(200, json={'container_addr': 'http://fixture.box'})

        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(adapter.aclose)
        await adapter.list_challenges()
        await adapter.ensure_instance({'id': 'slow'})

        async def run(task: Task) -> None:
            if task.payload['challenge']['id'] == 'retry':
                revisited.set()
            await hold.wait()
            task.status = TaskStatus.DONE

        dag = TaskDAG()
        slow = dag.add_node('CTF-WEB', 'slow', meta={'challenge': {'id': 'slow'}})
        dag.mark(slow.node_id, TaskStatus.RUNNING)
        old = dag.add_node('CTF-WEB@old', 'retry', meta={
            'challenge': {'id': 'retry'}, 'deferred': True, 'defer_reason': '保存断点'})
        dag.mark(old.node_id, TaskStatus.CANCELLED)
        task = Task(task_id='slow-task', task_type='ctf_solve', target='http://fixture.box',
                    agent_type=AgentType.CTF_WEB, payload={'challenge': {'id': 'slow'}})
        master = self._master(adapter, run, planning=True)
        master.ctx.extra['competition_context'] = {'challenges': [{'id': 'slow'}, {'id': 'retry'}]}
        master._llm_plan = AsyncMock(return_value={
            'retry_challenges': [{'id': 'retry', 'reason': '回访断点'}],
            'dag_nodes': [{'tactic': 'CTF-WEB@retry', 'title': 'retry', 'challenge_id': 'retry'}]})
        task.round_no = 1
        dispatch = asyncio.create_task(master._dispatch({
            'dag': dag.to_dict(), 'round_no': 1, 'target': 'http://fixture.box',
            'tasks_dispatched': [{'node_id': slow.node_id, 'task': task.model_dump(mode='json')}]}))
        try:
            await asyncio.wait_for(revisited.wait(), 1)
            self.assertFalse(hold.is_set())
            self.assertEqual(master._llm_plan.await_count, 1)
        finally:
            hold.set()
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)

    async def test_failed_queue_planning_retries_while_slow_task_is_still_running(self) -> None:
        """同一份缺题清单在一次规划失败后仍可重试，不能被永久标记成已处理。"""
        hold = asyncio.Event()
        planned = asyncio.Event()
        adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith('/challenges'):
                return httpx.Response(200, json={'data': {'challenges': [
                    _challenge(cid) for cid in ('c1', 'c2')]}})
            return httpx.Response(200, json={'container_addr': 'http://fixture.box'})

        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(adapter.aclose)
        await adapter.list_challenges()
        await adapter.ensure_instance({'id': 'c1'})

        async def run(task: Task) -> None:
            if task.payload['challenge']['id'] == 'c2':
                planned.set()
            await hold.wait()
            task.status = TaskStatus.DONE

        dag = TaskDAG()
        node = dag.add_node('CTF-WEB', 'c1', meta={'challenge': {'id': 'c1'}})
        dag.mark(node.node_id, TaskStatus.RUNNING)
        task = Task(task_id='c1', task_type='ctf_solve', target='http://fixture.box',
                    agent_type=AgentType.CTF_WEB, payload={'challenge': {'id': 'c1'}})
        master = self._master(adapter, run, planning=True)
        master.ctx.extra['competition_context'] = {'challenges': [{'id': 'c1'}, {'id': 'c2'}]}
        master._llm_plan = AsyncMock(side_effect=[None, {'dag_nodes': [
            {'tactic': 'CTF-WEB', 'title': 'c2', 'challenge_id': 'c2'}]}])
        dispatch = asyncio.create_task(master._dispatch({
            'dag': dag.to_dict(), 'round_no': 0, 'target': 'http://fixture.box',
            'tasks_dispatched': [{'node_id': node.node_id, 'task': task.model_dump(mode='json')}]}))
        try:
            await asyncio.wait_for(planned.wait(), 1)
            self.assertFalse(hold.is_set())
            self.assertEqual(master._llm_plan.await_count, 2)
        finally:
            hold.set()
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)

    async def test_exhausted_queue_is_replanned_while_other_challenges_are_running(self) -> None:
        """首批没有任何后备节点，也必须在慢题收尾前补题并维持三个席位。"""
        await self._check_exhausted_queue(solved=True)

    async def test_unsolved_conclusion_releases_slot_before_other_tasks_finish(self) -> None:
        """卡题主动留档收尾后也要立即关容器、启动后续题。"""
        await self._check_exhausted_queue(solved=False)

    async def test_supervisor_cancels_stuck_task_before_close_and_refills(self) -> None:
        """模型主动暂缓应退出在途执行，再关实例、补位，不等慢题收尾。"""
        await self._check_exhausted_queue(solved=False, deferred=True)

    async def _check_exhausted_queue(self, *, solved: bool, deferred: bool = False) -> None:
        with tempfile.TemporaryDirectory():
            events: list[str] = []
            completed: set[str] = set()
            ids = ('c1', 'c2', 'c3', 'c4', 'c5')

            def handle(request: httpx.Request) -> httpx.Response:
                op = request.url.path.rsplit('/', 1)[-1]
                body = json.loads(request.content) if request.content else {}
                cid = body.get('unique_code', '')
                if op == 'challenges':
                    return httpx.Response(200, json={'data': {'challenges': [
                        _challenge(c, done=c in completed) for c in ids]}})
                events.append(f'{op}:{cid}')
                if op == 'submit':
                    completed.add(cid)
                    return httpx.Response(200, json={'correct': True})
                return httpx.Response(200, json={'container_addr': f'http://{cid}.box'})

            adapter = PlatformAdapter(parse_platform_profile({
                **PROFILE, 'limits': {'max_active': 3}}), continuous=True)
            adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            self.addAsyncCleanup(adapter.aclose)
            await adapter.list_challenges()
            slow = asyncio.Event()
            next_started = asyncio.Event()
            planner_started = asyncio.Event()
            allow_plan = asyncio.Event()
            dag = TaskDAG()
            specs = []
            for cid in ids[:3]:
                await adapter.ensure_instance({'id': cid})
                node = dag.add_node('CTF-WEB', cid, meta={'challenge': {'id': cid}})
                task = Task(task_id=cid, task_type='ctf_solve', target='http://box',
                            agent_type=AgentType.CTF_WEB, round_no=0,
                            payload={'challenge': {'id': cid}})
                dag.mark(node.node_id, TaskStatus.RUNNING)
                specs.append({'node_id': node.node_id, 'task': task.model_dump(mode='json')})

            async def plan(state: dict) -> dict:
                self.assertTrue(state['queue_refill'])
                planner_started.set()
                await allow_plan.wait()
                return {'dag_nodes': [{'tactic': 'CTF-WEB', 'title': cid,
                                       'challenge_id': cid} for cid in ids[3:]]}

            async def run(task: Task) -> None:
                cid = task.payload['challenge']['id']
                if cid == 'c1':
                    await planner_started.wait()
                    if deferred:
                        try:
                            await slow.wait()
                        finally:
                            events.append('cancel:c1')
                        return
                    if solved:
                        await adapter.submit('flag{one}', cid)
                    allow_plan.set()
                    if not solved:
                        task.status = TaskStatus.DONE
                        return
                elif cid == 'c4':
                    next_started.set()
                await slow.wait()
                task.status = TaskStatus.DONE

            master = object.__new__(MasterAgent)
            master.agent_id = 'test-master'
            master.ctx = SimpleNamespace(
                pause_gate=None, enable_llm_planning=True, ring=Mock() if deferred else None,
                agents={AgentType.CTF_WEB: SimpleNamespace(run=run)},
                tactic_map={'CTF-WEB': (AgentType.CTF_WEB, 'ctf_solve')},
                policy=SimpleNamespace(guard=None), audit=NullAuditLogger(),
                extra={'platform': adapter, 'mission': '持续保持三个解题位',
                       'mode_prompt': '模型动态判断卡题并暂缓',
                       'competition_context': {'challenges': [{'id': cid} for cid in ids]}})
            master._llm_plan = plan
            master._assign_dispatch_plan = AsyncMock()
            master._write_dag_snapshot = Mock()
            master._rules = Mock(return_value='')
            watched: list[str] = []

            async def steer(task_ids: list[str], decisions: asyncio.Queue) -> None:
                await planner_started.wait()
                allow_plan.set()
                decisions.put_nowait({'defer_challenges': [
                    {'id': 'c1', 'reason': '重复探测无新线索，先做后续题'}]})
                watched.append(task_ids)
                await slow.wait()

            if deferred:
                master._steering_watch = steer
            dispatch = asyncio.create_task(master._dispatch({
                'dag': dag.to_dict(), 'round_no': 0, 'target': 'http://box',
                'tasks_dispatched': specs}))
            try:
                await asyncio.wait_for(next_started.wait(), 2)
                self.assertFalse(dispatch.done())
                self.assertFalse(slow.is_set())
                self.assertEqual(set(adapter._instances), {'c2', 'c3', 'c4'})
                self.assertLess(events.index('close:c1'), events.index('start:c4'))
                if deferred:
                    self.assertLess(events.index('cancel:c1'), events.index('close:c1'))
                    self.assertGreater(len(watched[0]), 3)
            finally:
                slow.set()
                dispatch.cancel()
                await asyncio.gather(dispatch, return_exceptions=True)

    async def test_submit_closes_and_starts_next_before_other_tasks_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events: list[str] = []
            completed: set[str] = set()

            def handle(request: httpx.Request) -> httpx.Response:
                op = request.url.path.rsplit('/', 1)[-1]
                body = json.loads(request.content) if request.content else {}
                cid = body.get('unique_code', '')
                if op == 'challenges':
                    return httpx.Response(200, json={'data': {'challenges': [
                        _challenge(c, done=c in completed) for c in ('c1', 'c2', 'c3')]}})
                events.append(f'{op}:{cid}')
                if op == 'submit':
                    completed.add(cid)
                    return httpx.Response(200, json={'correct': True})
                return httpx.Response(200, json={'container_addr': f'http://{cid}.box'})

            adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True,
                                      progress_path=Path(tmp) / 'competition_progress.json')
            adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            self.addAsyncCleanup(adapter.aclose)
            await adapter.list_challenges()
            await adapter.ensure_instance({'id': 'c1'})
            await adapter.ensure_instance({'id': 'c2'})
            slow = asyncio.Event()
            next_started = asyncio.Event()

            async def run(task: Task) -> None:
                cid = task.payload['challenge']['id']
                if cid == 'c1':
                    await adapter.submit('flag{one}', cid)
                    # 模拟同题另一条路线还没完成；补位不能等它。
                    await slow.wait()
                elif cid == 'c2':
                    await slow.wait()
                else:
                    next_started.set()
                    await slow.wait()
                task.status = TaskStatus.DONE

            dag = TaskDAG()
            specs = []
            for cid in ('c1', 'c2', 'c3'):
                node = dag.add_node('CTF-WEB', cid, meta={'challenge': {'id': cid}})
                if cid != 'c3':
                    task = Task(task_id=cid, task_type='ctf_solve', target='http://box',
                                agent_type=AgentType.CTF_WEB, round_no=0,
                                payload={'challenge': {'id': cid}})
                    dag.mark(node.node_id, TaskStatus.RUNNING)
                    specs.append({'node_id': node.node_id, 'task': task.model_dump(mode='json')})
            master = object.__new__(MasterAgent)
            master.agent_id = 'test-master'
            master.ctx = SimpleNamespace(
                pause_gate=None, enable_llm_planning=False, ring=None,
                agents={AgentType.CTF_WEB: SimpleNamespace(run=run)},
                tactic_map={'CTF-WEB': (AgentType.CTF_WEB, 'ctf_solve')},
                policy=SimpleNamespace(guard=None), audit=NullAuditLogger(),
                extra={'platform': adapter, 'mission': '连续解题'})
            master._write_dag_snapshot = Mock()
            master._rules = Mock(return_value='')
            dispatch = asyncio.create_task(master._dispatch({
                'dag': dag.to_dict(), 'round_no': 0, 'target': 'http://box',
                'tasks_dispatched': specs}))
            try:
                started = asyncio.create_task(next_started.wait())
                ready, _ = await asyncio.wait({started, dispatch}, timeout=2,
                                              return_when=asyncio.FIRST_COMPLETED)
                if dispatch in ready:
                    dispatch.result()
                if started not in ready:
                    started.cancel()
                    await asyncio.gather(started, return_exceptions=True)
                    self.fail(f"未及时补位：{events}")
                self.assertFalse(dispatch.done())
                self.assertLess(events.index('submit:c1'), events.index('close:c1'))
                self.assertLess(events.index('close:c1'), events.index('start:c3'))
                stored = json.loads(adapter.progress_path.read_text())
                self.assertEqual(stored['solved_ids'], ['c1'])
                slow.set()
                result = await asyncio.wait_for(dispatch, 2)
                self.assertEqual(len(result['tasks_dispatched']), 1)
            finally:
                slow.set()
                if not dispatch.done():
                    dispatch.cancel()
                await asyncio.gather(dispatch, return_exceptions=True)

    async def test_partial_flag_does_not_close_container(self) -> None:
        adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)
        self.addAsyncCleanup(adapter.aclose)
        paths = []

        def handle(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith('/challenges'):
                return httpx.Response(200, json={'data': {'challenges': [_challenge('c1')]}})
            return httpx.Response(200, json={'correct': True})

        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        await adapter.submit('flag{partial}', 'c1')
        self.assertFalse(any(p.endswith('/close') for p in paths))
        self.assertTrue(adapter.completion_queue.empty())


class ChallengeProgressStateTests(unittest.TestCase):
    def test_task_state_exposes_challenge_binding_without_full_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(session_id='sess-progress', mode='competition',
                              target='contest', workspace=Path(tmp))
            bb = Blackboard(session.bb_path, bus=None)
            try:
                bb.write(BlackboardLayer.TASK_STATE, 'task-live', {
                    'task': {'task_id': 'task-live', 'status': 'running',
                             'payload': {'challenge': {'id': 'e1-01', 'description': '题面'},
                                         'mission': '完整任务原文'}}},
                    agent_type=AgentType.CTF_WEB, updated_by='test')
            finally:
                bb.close()
            task = _read_state(session)['tasks'][0]
            self.assertEqual(task['challenge_id'], 'e1-01')
            self.assertEqual(task['status'], 'running')
            self.assertNotIn('payload', task)

    def test_state_reads_live_progress_and_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = Session(session_id='sess-progress', mode='competition',
                              target='contest', workspace=workspace)
            path = workspace / 'competition_progress.json'
            path.write_text(json.dumps({'solved_ids': ['c1'], 'unsolved_ids': ['c2'],
                                        'total_challenges': 2}))
            self.assertEqual(_read_state(session)['challenge_progress']['solved_ids'], ['c1'])
            path.write_text(json.dumps({'solved_ids': ['c1', 'c2'], 'unsolved_ids': [],
                                        'total_challenges': 2}))
            session = Session(session_id='sess-progress', mode='competition',
                              target='contest', workspace=workspace)
            progress = _read_state(session)['challenge_progress']
            self.assertEqual(progress['total_challenges'], 2)
            self.assertEqual(len(progress['solved_ids']), 2)
            session.mode = 'ctf'
            self.assertIsNone(_read_state(session)['challenge_progress'])

    def test_missing_or_corrupt_progress_does_not_break_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(session_id='sess-progress', mode='competition',
                              target='contest', workspace=Path(tmp))
            self.assertIsNone(_read_state(session)['challenge_progress'])
            (Path(tmp) / 'competition_progress.json').write_text('{broken')
            self.assertIsNone(_read_state(session)['challenge_progress'])
