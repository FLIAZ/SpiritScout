"""竞赛连续调度：中榜释放名额时，不等待同批或同题其他任务。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, Task, TaskStatus
from spiritscout.ctf.platform import PlatformAdapter, parse_platform_profile
from spiritscout.web.app import _read_state
from spiritscout.web.sessions import Session
from tests.test_platform_adapter import PROFILE, _challenge


class ContinuousCompetitionTests(unittest.IsolatedAsyncioTestCase):
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
