"""实际工具沙箱的用法查询与完整日志回读；不依赖模型猜测或 Mock 执行。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_single_executor import context

from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.light_memory import memory_execute, read_history, working_snapshot
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec


@unittest.skipUnless(sandbox_available(), '需要现有 Linux/bwrap')
class LightToolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        ctx = context(RunMode.CTF)
        ctx.journal = None
        ctx.builtin_ctx = {'workspace_root': folder.name, '_run_mode': RunMode.CTF, '_lightweight_local': True}
        ctx.tools = ToolRegistry(None)
        ctx.tools.register_builtin('sandbox_exec', sandbox_exec, ToolSpec(
            name='sandbox_exec', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))
        self.task = Task(task_id='recovery', agent_type=self.agent.agent_type, task_type='ctf_single', target='',
                         payload={'challenge': {'id': 'recovery'}})
        self.route = SimpleNamespace(name='solve', route_id='r')
        self.state = {'messages': [], 'observations': []}

    async def call(self, name, arguments):
        call = {'name': name, 'arguments': arguments, 'id': str(len(self.state['messages']))}
        self.state['messages'].append({'role': 'assistant', 'tool_calls': [{
            'id': call['id'], 'function': {'name': name, 'arguments': json.dumps(arguments)}}]})
        result = await self.agent._run_tool_call_inner(self.task, self.route, self.state, call)
        self.state['messages'].append({'role': 'tool', 'tool_call_id': call['id'], 'content': result.text})
        self.agent._observe_tool_result(self.state, call, result)
        return result

    async def test_full_log_search_and_cursor_reach_middle_missing_from_message_preview(self):
        result = await self.call('python', {'code': "import sys\nprint('A'*6000 + 'HIDDEN-MIDDLE-原文\\nPROOF' + 'Z'*6000)", 'action': 'analysis'})
        self.assertTrue(result.ok, result.text)
        self.assertNotIn('HIDDEN-MIDDLE', result.text)
        self.assertEqual(result.metadata['output_read_call']['arguments'], {
            'view': 'output', 'message_index': 1, 'offset': 0})
        self.assertIn('"display_complete": false', self.state['messages'][1]['content'])
        index = 1
        message = read_history(self.state, {'message_index': index, 'view': 'content', 'find': 'HIDDEN-MIDDLE'})
        self.assertFalse(message['found'])
        self.assertTrue(message['message_is_preview'])
        outcome = await self.call(**message['output_read_call'])
        found = json.loads(outcome.text)
        self.assertTrue(found['found'], found)
        self.assertIn('HIDDEN-MIDDLE-原文', found['text'])
        self.assertEqual(found['match_offset'], 6000)
        self.assertTrue(found['source_complete'])
        self.assertEqual(found['source_invocation_id'], result.metadata['invocation_id'])
        self.assertEqual(found['evidence_source'], {'message_index': len(self.state['messages']) - 1})
        # 原日志页的观察可支持模型笔记，但不是再次执行或新的候选验证。
        note = {'action': 'update', 'base_revision': 0, 'upsert': [{
            'id': 'middle', 'kind': 'fact', 'text': '原日志含中间字段',
            'sources': [{'message_index': index, 'quote': 'HIDDEN-MIDDLE-原文\nPROOF'}]}]}
        wrong_source = memory_execute(self.state, note)
        self.assertFalse(wrong_source['ok'])
        recovered = wrong_source['available_quote_sources']
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]['message_index'], found['evidence_source']['message_index'])
        note['upsert'][0]['sources'] = recovered
        memory = memory_execute(self.state, note)
        self.assertTrue(memory['ok'], memory)
        self.assertEqual(len(self.state['evidence_rows']), 1)
        call = {'name': 'history_read', 'arguments': {'view': 'output', 'message_index': index, 'length': 4000}}
        chunks = []
        while call:
            page = json.loads((await self.call(**call)).text)
            self.assertTrue(page['ok'], page)
            chunks.append(page['text'])
            call = page.get('next_call')
        self.assertEqual(''.join(chunks), Path(result.metadata['output_file']).read_text())
        self.assertFalse(working_snapshot(self.state)['history_reads'][-1]['has_more'])

    async def test_changed_missing_rotated_and_linked_logs_are_not_claimed_complete(self):
        result = await self.call('python', {'code': "print('中文'*4000)", 'action': 'analysis'})
        log = Path(result.metadata['output_file'])
        original = log.read_bytes()
        first = json.loads((await self.call('history_read', {'view': 'output', 'message_index': 1, 'length': 4000})).text)
        self.assertTrue(first['ok'], first)
        self.assertLess(len(first['text']), 4000)  # UTF-8/JSON budget must still return an intact page.
        log.write_text('changed')
        stale = json.loads((await self.call(**first['next_call'])).text)
        self.assertFalse(stale['ok'])
        self.assertIn('指纹', stale['error'])
        first_read_changed = json.loads((await self.call('history_read', {'view': 'output', 'message_index': 1})).text)
        self.assertFalse(first_read_changed['ok'])
        self.assertEqual(first_read_changed['source_integrity'], 'not_verified')
        log.write_bytes(original)
        self.state['messages'][1]['_light_observation']['output_rotated'] = True
        rotated = json.loads((await self.call('history_read', {'view': 'output', 'message_index': 1})).text)
        self.assertFalse(rotated['source_complete'])
        log.unlink()
        missing = await self.call('history_read', {'view': 'output', 'message_index': 1})
        self.assertFalse(missing.ok)
        log.symlink_to('/etc/passwd')
        linked = await self.call('history_read', {'view': 'output', 'message_index': 1})
        self.assertFalse(linked.ok)
        self.assertNotIn('root:', linked.text)
        fabricated = await self.call('history_read', {'view': 'output', 'message_index': 0, 'path': '/etc/passwd'})
        self.assertFalse(fabricated.ok)

    async def test_rotated_generations_keep_their_own_capture_fingerprints(self):
        with patch('spiritscout.tools.sandbox._LOG_ROTATE_BYTES', 1024):
            await self.call('python', {'code': "import sys; sys.stdout.write('A'*1024+'B'*1024+'C'*300)",
                                       'action': 'analysis'})
        current = json.loads((await self.call('history_read', {'view': 'output', 'message_index': 1})).text)
        self.assertTrue(current['ok'], current)
        self.assertEqual(current['text'], 'C' * 300)
        self.assertFalse(current['source_complete'])
        previous = json.loads((await self.call(**current['previous_output_call'])).text)
        self.assertTrue(previous['ok'], previous)
        self.assertEqual(previous['text'], 'B' * 1024)
        self.assertEqual(previous['source_integrity'], 'matched_capture')
        self.assertNotEqual(current['source_sha256'], previous['source_sha256'])

    async def test_python_api_and_cli_help_use_actual_sandbox_and_deferred_path(self):
        missing = await self.call('tool_lookup', {'name': 'cyclic_find'})
        self.assertFalse(missing.ok)
        self.assertIn('tool_help', missing.text)
        schema = await self.call('tool_lookup', {'name': 'tool_help'})
        self.assertTrue(schema.ok)
        api = await self.call('tool_invoke', {'name': 'tool_help', 'arguments': {
            'kind': 'python', 'name': 'pathlib.Path.read_text'}})
        self.assertTrue(api.ok, api.text)
        self.assertIn('signature', api.text)
        self.assertIn('encoding', api.text)
        cli = await self.call('tool_help', {'kind': 'cli', 'name': 'python3'})
        self.assertTrue(cli.ok, cli.text)
        self.assertIn('usage:', cli.text)
        absent = await self.call('tool_help', {'kind': 'python', 'name': 'json.NO_SUCH_FUNCTION'})
        self.assertFalse(absent.ok)
        self.assertIn('AttributeError', absent.text)
        wrong_module = await self.call('tool_help', {'kind': 'python', 'name': 'json.decoder.dumps'})
        self.assertFalse(wrong_module.ok)
        self.assertIn('json.dumps', wrong_module.text)
        self.assertIn('next_calls', wrong_module.text)
        rejected = await self.call('tool_help', {'kind': 'cli', 'name': 'python3; touch injected'})
        self.assertFalse(rejected.ok)
        self.assertFalse((self.root / 'sandbox/recovery/injected').exists())
