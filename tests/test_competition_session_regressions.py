"""旧会话复盘暴露的事实丢失、实例入口覆盖与工具历史可见性回归。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.core.models import AgentType, TaskStatus
from spiritscout.ctf.platform import PlatformAdapter, parse_platform_profile
from spiritscout.gateway.context import decision_messages
from spiritscout.web.events import read_activity_page
from tests.test_platform_adapter import PROFILE


class CompetitionSessionRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_existing_instance_address_overrides_stale_platform_url(self) -> None:
        adapter = PlatformAdapter(parse_platform_profile(PROFILE), continuous=True)
        adapter._instances['c1'] = '10.1.2.3:8080'
        result = await adapter.ensure_instance({'id': 'c1', 'url': 'https://platform.example'})
        self.assertEqual(result['url'], 'http://10.1.2.3:8080')
        self.assertEqual(result['container_addr'], '10.1.2.3:8080')

    async def test_replanning_applies_all_retry_decisions_before_dispatch(self) -> None:
        dag = TaskDAG()
        challenges = [{'id': f'c{i}', 'url': f'http://c{i}.fixture'} for i in range(17)]
        for ch in challenges:
            node = dag.add_node(f"CTF-WEB@old-{ch['id']}", ch['id'], meta={
                'challenge': ch, 'deferred': True, 'defer_reason': '保存断点'})
            dag.mark(node.node_id, TaskStatus.CANCELLED)
        master = object.__new__(MasterAgent)
        master.agent_id = 'master-test'
        master.ctx = SimpleNamespace(
            audit=Mock(), pause_gate=None, plan_override=None, enable_llm_planning=True,
            agents={AgentType.CTF_WEB: object()},
            tactic_map={'CTF-WEB': (AgentType.CTF_WEB, 'ctf_solve')},
            extra={'mode_prompt': '自主比赛', 'competition_context': {'challenges': challenges}})
        master._ensure_business_gate = lambda nodes: nodes
        master._write_dag_snapshot = Mock()
        master._llm_plan = AsyncMock(return_value={
            'dag_nodes': [{'tactic': f"CTF-WEB@retry-{ch['id']}",
                           'challenge_id': ch['id'], 'title': ch['id']} for ch in challenges],
            'retry_challenges': [{'id': ch['id'], 'reason': '回访'} for ch in challenges]})
        result = await master._meta_plan({'target': 'https://platform.fixture',
                                         'round_no': 0, 'dag': dag.to_dict()})
        self.assertEqual(len(result['tasks_dispatched']), 17)
        for spec in result['tasks_dispatched']:
            task = spec['task']
            self.assertEqual(task['target'], task['payload']['challenge']['url'])
        self.assertTrue(all(not ch['deferred'] for ch in master._challenge_states(
            TaskDAG.from_dict(result['dag'])).values()))

    async def test_full_completion_and_cancellation_facts_survive_context_projection(self) -> None:
        dag = TaskDAG()
        for i in range(63):
            node = dag.add_node(f'CTF-WEB@{i}', str(i), meta={
                'challenge': {'id': str(i)}, 'cancel_reason': '平台已确认整题完成'})
            dag.mark(node.node_id, TaskStatus.CANCELLED)
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(extra={'platform': SimpleNamespace(
            continuous=True, active_instance_ids=lambda: set(), progress=lambda: {'solved_ids': list(map(str, range(46))),
                                               'unsolved_ids': list(map(str, range(46, 63)))})})
        status = master._competition_status(dag)
        messages = decision_messages('planner', {'competition_context': {'rules': '完整规则' * 4000},
            'competition_status': status, 'view': {'raw': '可节选观察' * 4000}}, max_chars=100)
        payload = json.loads(messages[-1]['content'])
        self.assertEqual(payload['competition_status'], status)
        self.assertEqual(len(status['unsolved_ids']), 17)
        self.assertEqual(len(status['challenges']), 63)
        self.assertEqual(status['challenges']['62']['nodes'][0]['cancel_reason'], '平台已确认整题完成')

    async def test_streaming_fragments_do_not_push_browser_activity_out_of_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            events = [
                {'type': 'agent.tool_call', 'actor': 'solver', 'detail': {
                    'calls': [{'id': 'call-browser', 'name': 'mcp__chrome__browser_navigate'}]}},
                {'type': 'agent.tool_result', 'actor': 'solver', 'detail': {'id': 'call-browser', 'ok': True}},
                *({'type': 'agent.delta', 'actor': 'solver', 'detail': {'kind': 'text', 'text': '字'}}
                  for _ in range(1000)),
                {'type': 'task.done', 'actor': 'solver', 'detail': {}},
            ]
            path.write_text(''.join(json.dumps(e) + '\n' for e in events))
            page = read_activity_page(path, limit=3)
            self.assertEqual(page['activity_count'], 3)
            self.assertEqual(len(page['items']), 4)
            self.assertEqual(page['items'][0]['event']['type'], 'agent.tool_call')
            self.assertEqual(page['items'][2]['event']['detail']['text'], '字' * 1000)
            self.assertEqual(page['cursor'], path.stat().st_size)
            self.assertFalse(page['has_more'])
            # 前后页的字节边界必须一致，避免续流重复或吞掉工具返回。
            latest = read_activity_page(path, limit=1)
            previous = read_activity_page(path, before=latest['older_cursor'], limit=2)
            self.assertEqual(previous['cursor'], latest['older_cursor'])
            self.assertEqual(previous['items'][0]['event']['type'], 'agent.tool_call')
            self.assertEqual(previous['items'][-1]['event']['detail']['text'], '字' * 1000)
