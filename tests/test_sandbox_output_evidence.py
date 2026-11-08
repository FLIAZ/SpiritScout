"""真实沙箱输出证据：比较原始字节，不将有损文本或头尾预览相同当作完整相同。"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_single_executor import context

from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.evidence_review import observe, review_row
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec


@unittest.skipUnless(sandbox_available(), '需要现有 Linux/bwrap')
class SandboxOutputEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        ctx = context(RunMode.CTF)
        ctx.builtin_ctx = {'workspace_root': self.folder.name, '_run_mode': RunMode.CTF, '_lightweight_local': True}
        ctx.tools = ToolRegistry(None)
        ctx.tools.register_builtin('sandbox_exec', sandbox_exec, ToolSpec(
            name='sandbox_exec', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))
        self.task = Task(task_id='bytes', agent_type=self.agent.agent_type, task_type='ctf_single', target='',
                         payload={'challenge': {'id': 'bytes'}})
        self.route = SimpleNamespace(name='r')
        self.state = {'messages': [], 'observations': []}

    async def run_code(self, code):
        return await self.agent._tool_sandbox(self.task, self.route, self.state, 'python',
                                              {'code': code, 'action': 'analysis'})

    async def test_lossy_preview_does_not_merge_different_binary_observations(self):
        first = await self.run_code('import sys; sys.stdout.buffer.write(bytes([255]))')
        second = await self.run_code('import sys; sys.stdout.buffer.write(bytes([254]))')
        repeated = await self.run_code('import sys; sys.stdout.buffer.write(b"\\xff")')
        self.assertNotEqual(first.metadata['observation']['digest'], second.metadata['observation']['digest'])
        self.assertNotIn('与此前', second.text)
        self.assertIn('无法无损', second.text)
        self.assertIn('与此前 1 次调用完全一致', repeated.text)
        self.assertEqual(first.metadata['observation']['comparison_scope'], 'raw_streams')

    async def test_different_unshown_middle_is_not_called_identical(self):
        first = await self.run_code('import sys; sys.stdout.buffer.write(b"A"*4000+b"X"+b"Z"*4000)')
        second = await self.run_code('import sys; sys.stdout.buffer.write(b"A"*4000+b"Y"+b"Z"*4000)')
        self.assertEqual(first.metadata['observation']['preview'], second.metadata['observation']['preview'])
        self.assertNotEqual(first.metadata['observation']['digest'], second.metadata['observation']['digest'])
        self.assertNotIn('与此前', second.text)

    async def test_capture_proves_exact_bytes_and_distinguishes_valid_replacement_character(self):
        for content, lossy in ((b'\x00\xff\xfe', True), ('中文\ufffd'.encode(), False)):
            result = await sandbox_exec(f'import sys; sys.stdout.buffer.write({content!r})', language='python',
                workspace_root=self.folder.name, workspace_scope='bytes', _run_mode=RunMode.CTF, _lightweight_local=True)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.returncode, 0, result.stderr)
            capture = result.parsed['stdout_capture']
            self.assertTrue(capture['complete'])
            self.assertEqual(capture['sha256'], hashlib.sha256(content).hexdigest())
            self.assertEqual(capture['bytes'], len(content))
            self.assertEqual(capture['text_decode_lossy'], lossy)
            self.assertEqual(Path(result.parsed['output_file']).read_bytes(), content)

    async def test_review_preserves_error_and_output_reference_alongside_stdout(self):
        result = await self.run_code('import sys; print("image dimensions 705x564"); '
                                    'sys.stderr.write("trace frame\\n"*100+"AttributeError: missing fixture operation"); '
                                    'sys.exit(1)')
        self.state['messages'].append({'role': 'tool', 'tool_call_id': 'error', 'content': result.text})
        observe(self.state, 'python', {'code': 'fixture', 'action': 'analysis'}, result)
        reviewed = review_row(self.state['evidence_rows'][-1], compact=True)
        self.assertIn('missing fixture operation', reviewed['result_preview'])
        self.assertIn('image dimensions 705x564', reviewed['result_preview'])
        self.assertEqual(reviewed['output_file'], result.metadata['output_file'])
        self.assertIn('missing fixture operation', self.state['observations'][-1]['result'])

    async def test_partial_or_lossy_display_is_not_complete_validation_evidence(self):
        for code, partial in (("print('V'*4000)", True), ("print('verified')", False),
                              ('import sys; sys.stdout.buffer.write(bytes([255]))', True)):
            result = await self.run_code(code)
            self.assertTrue(result.ok)
            self.assertEqual(result.metadata['observation']['partial'], partial)
