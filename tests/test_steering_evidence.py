"""督导识别摘要及来源，进度数字和投递状态不消耗新一轮模型调用。"""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.evidence import SupervisionGate
from spiritscout.agents.ring import RingLedger
from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer


class SteeringEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.bb = Blackboard(Path(self.folder.name) / 'bb.db')
        self.addCleanup(self.bb.close)
        self.master = object.__new__(MasterAgent)
        self.master.ctx = SimpleNamespace(blackboard=self.bb, audit=Mock(), ring=RingLedger(), extra={})

    def digest(self, progress, note):
        self.bb.write(BlackboardLayer.TASK_STATE, 't', {
            'task': {'status': 'running', 'agent_type': 'ctf_web'},
            'routes': [{'route_id': 'r', 'name': 'solve', 'status': 'running',
                        'progress': progress, 'notes': [note]}]})
        return self.master._progress_digest(['t'], include_supervision=True)

    async def watch(self, snapshots, steering=None):
        checked = asyncio.Event()
        reads = 0
        def digest(ids, **kwargs):
            nonlocal reads
            result = snapshots[min(reads, len(snapshots) - 1)]
            reads += 1
            if reads > len(snapshots) + 3:
                checked.set()
            return result
        self.master._STEER_INTERVAL = .001
        self.master._progress_digest = digest
        self.master._llm_steer = AsyncMock(side_effect=steering) if steering else AsyncMock(return_value=[])
        watcher = asyncio.create_task(self.master._steering_watch(['t']))
        try:
            await asyncio.wait_for(checked.wait(), 1)
            return self.master._llm_steer.await_count
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def test_metric_and_version_changes_do_not_retrigger_supervision(self):
        first = self.digest(.1, '正在核对入口响应')
        second = self.digest(.2, '正在核对入口响应')
        self.assertNotEqual(first['tasks'][0]['blackboard_ref'], second['tasks'][0]['blackboard_ref'])
        self.assertEqual(await self.watch([first, second]), 0)

    async def test_changed_summary_tail_stays_readable_without_triggering_supervision(self):
        first = self.digest(.1, 'x' * 600 + '未确认')
        second = self.digest(.2, 'x' * 600 + '已出现新响应')
        route = first['tasks'][0]['routes'][0]
        self.assertEqual(len(route['notes'][0]), 500)
        self.assertEqual(route['notes_view']['kind'], 'agent_summary')
        self.assertTrue(route['notes_view']['partial'])
        self.assertEqual(route['notes'], second['tasks'][0]['routes'][0]['notes'])
        self.assertNotEqual(route['notes_view']['digest'], second['tasks'][0]['routes'][0]['notes_view']['digest'])
        ref = second['tasks'][0]['blackboard_ref']
        record, version = self.bb.read_versioned(BlackboardLayer(ref['layer']), ref['record_id'])
        self.assertEqual(version, ref['version'])
        self.assertTrue(record['routes'][0]['notes'][0].endswith('新响应'))
        self.assertEqual(await self.watch([first, second]), 0)

    def test_actual_result_changes_blockers_and_pending_feedback_gate_calls(self):
        from spiritscout.tools.invocation import ExecutionScope
        from spiritscout.tools.journal import ExecutionJournal
        from spiritscout.tools.spec import ToolResult

        journal = ExecutionJournal(self.bb, Path(self.folder.name), 'supervision')
        gate = SupervisionGate()
        digest = self.digest(.1, '正在验证')
        self.assertFalse(gate.ready(digest, journal, self.master.ctx.ring))
        def record(identifier, status=200, body='same', cookie='session=one'):
            journal.begin(identifier, 'http_probe', 'same-request', 'safe', scope=ExecutionScope(task_id='t'))
            journal.transition(identifier, 'succeeded', result=ToolResult(tool='http_probe', ok=True,
                stdout='元数据和保存路径每次都变化 '+identifier,
                parsed={'status_code': status, 'body_sha256': body, 'final_url': 'https://fixture.test/',
                        'body_path': identifier, 'headers': {'date': identifier, 'set-cookie': cookie}}))
        record('one')
        self.assertTrue(gate.ready(digest, journal, self.master.ctx.ring))
        record('two')
        self.assertFalse(gate.ready(self.digest(.9, '新回合文案'), journal, self.master.ctx.ring))
        instruction = self.master.ctx.ring.steer('ctf_web', '验证新证据', task_id='t')
        record('three', body='changed')
        for status in ('delivered', 'accepted'):
            self.master.ctx.ring.steering_feedback('ctf_web', task_id='t', route_id='r',
                instruction_id=instruction, status=status)
            self.assertFalse(gate.ready(digest, journal, self.master.ctx.ring))
        self.master.ctx.ring.steering_feedback('ctf_web', task_id='t', route_id='r',
            instruction_id=instruction, status='completed')
        self.assertTrue(gate.ready(digest, journal, self.master.ctx.ring), '反馈后消费等待期间的新证据')
        self.assertFalse(gate.ready(digest, journal, self.master.ctx.ring))
        record('four', body='changed', cookie='session=two')
        self.assertTrue(gate.ready(digest, journal, self.master.ctx.ring), '相同正文的新会话状态不能被去重')
        digest['tasks'][0]['_supervision'][0]['blockers'] = [{'type': 'authentication_required'}]
        self.assertTrue(gate.ready(digest, journal, self.master.ctx.ring))
        self.assertFalse(gate.ready(digest, journal, self.master.ctx.ring))
        digest['tasks'][0]['_supervision'][0]['blockers'] = []
        self.assertTrue(gate.ready(digest, journal, self.master.ctx.ring), '解除阻塞也是实际状态变化')

    async def test_notes_do_not_trigger_but_fresh_results_are_not_round_limited(self):
        snapshots = []
        for index in range(6):
            snapshot = self.digest(index / 10, str(index))
            snapshot['tasks'][0]['executions'] = [{'tool_name': 'sandbox_exec', 'params_digest': 'same',
                'execution_status': 'succeeded', 'exit_code': 0, 'result_digest': str(index)}]
            snapshots.append(snapshot)
        self.assertEqual(await self.watch(snapshots), 6, '所有真实新结果均可继续触发，不设固定轮数上限')

    async def test_pending_other_task_does_not_block_independent_supervision(self):
        self.master.ctx.ring.steer('ctf_web', '等待执行', task_id='t')
        pending = self.digest(.1, '等待')
        pending['tasks'][0]['executions'] = [{'tool_name': 'shell', 'params_digest': 'one',
            'execution_status': 'failed', 'result_digest': 'blocked'}]
        independent = {**pending, 'tasks': [*pending['tasks'], {
            'task_id': 'other', 'agent': 'ctf_web', 'routes': [],
            'executions': [{'tool_name': 'shell', 'params_digest': 'two',
                            'execution_status': 'succeeded', 'result_digest': 'new'}]}]}
        self.assertEqual(await self.watch([pending, independent]), 1)

    async def test_instruction_arriving_during_model_wait_is_not_overwritten(self):
        snapshot = self.digest(.1, '结果已产生')
        snapshot['tasks'][0]['executions'] = [{'tool_name': 'read', 'execution_status': 'succeeded',
                                              'result_digest': 'new'}]
        async def decide(*args):
            self.master.ctx.ring.steer('ctf_web', '用户新要求', task_id='t')
            return [{'agent': 'ctf_web', 'task_id': 't', 'hint': '迟到的督导建议'}]
        self.assertEqual(await self.watch([snapshot], steering=decide), 1)
        self.assertEqual(self.master.ctx.ring.steering_for('ctf_web', task_id='t')['hint'], '用户新要求')

    async def test_local_sdk_replay_reduces_calls_and_preserves_prefix_usage_and_cache(self):
        from spiritscout.config.settings import GatewayConfig, ModelEntry
        from spiritscout.core.audit import NullAuditLogger
        from spiritscout.gateway import ModelGateway, ModelRegistry

        requests = []
        async def serve(reader, writer):
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                            if line.lower().startswith(b'content-length:'))
                request = json.loads(await reader.readexactly(size))
                requests.append(request)
                model = {'id': 'fixture', 'created': 1, 'model': 'fixture'}
                message = {'role': 'assistant', 'content': '{"steering":[]}'}
                usage = {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110,
                         'prompt_tokens_details': {'cached_tokens': 80}}
                if request.get('stream'):
                    chunk = {**model, 'object': 'chat.completion.chunk'}
                    events = [
                        {**chunk, 'choices': [{'index': 0, 'delta': message, 'finish_reason': None}]},
                        {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
                        {**chunk, 'choices': [], 'usage': usage},
                    ]
                    data = (''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
                            + 'data: [DONE]\n\n').encode()
                    content_type = b'text/event-stream'
                else:
                    data = json.dumps({**model, 'object': 'chat.completion', 'usage': usage,
                        'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}]}).encode()
                    content_type = b'application/json'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: ' + content_type
                             + b'\r\nContent-Length: ' + str(len(data)).encode()
                             + b'\r\nConnection: close\r\n\r\n' + data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        snapshots = []
        for index in range(12):
            snapshot = self.digest(index / 12, f'第 {index} 轮说明')
            if index >= 1:
                snapshot['tasks'][0]['executions'] = [{'tool_name': 'http_probe', 'params_digest': 'same',
                    'execution_status': 'succeeded', 'result_digest': 'first' if index < 5 else 'changed'}]
            if index >= 9:
                snapshot['tasks'][0]['_supervision'][0]['blockers'] = [{'type': 'authentication_required'}]
            snapshots.append(snapshot)
        original = copy.deepcopy(snapshots)
        measured = []
        try:
            port = server.sockets[0].getsockname()[1]
            for optimized in (False, True):
                master = object.__new__(MasterAgent)
                gateway = ModelGateway(ModelRegistry([ModelEntry(model='fixture', provider='openai_compatible',
                    base_url=f'http://127.0.0.1:{port}/v1', api_key='local-fixture')]), GatewayConfig(max_retries=0))
                master.agent_id = 'master-fixture'
                master.ctx = SimpleNamespace(gateway=gateway, audit=NullAuditLogger(), ring=RingLedger(),
                    extra={}, blackboard=self.bb)
                master._world_state = lambda: {}
                master._scanner_context = lambda: {}
                master._mission = lambda: '按当前证据决定是否需要介入'
                master._system_prompt = lambda: '稳定的主控职责与工具契约'
                gate = SupervisionGate()
                start = len(requests)
                for snapshot in snapshots:
                    if not optimized or gate.ready(snapshot, None, master.ctx.ring):
                        self.assertEqual(await master._llm_steer(snapshot), [])
                measured.append((gateway.budget, requests[start:]))
            old, new = measured
            self.assertEqual([len(item[1]) for item in measured], [12, 3])
            self.assertEqual([item[0].used_tokens for item in measured], [1320, 330])
            self.assertEqual([item[0].cache_snapshot()['hit_rate'] for item in measured], [.8, .8])
            self.assertEqual(old[1][0]['messages'][0], new[1][0]['messages'][0], '保留同一 system 缓存前缀')
            self.assertTrue(all(call['call_status'] == 'completed' and call['sdk_attempts'] == 1
                                for budget, _ in measured for call in budget.call_log))
            self.assertNotIn('_supervision', str(requests), '本地触发信号不能膨胀模型输入')
            self.assertEqual(snapshots, original, '去重不改写真实现场与任务状态')
        finally:
            server.close()
            await server.wait_closed()


if __name__ == '__main__':
    unittest.main()
