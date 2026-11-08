"""错误原文定位、冻结源码预检、真实沙箱打包入口的定向回归。"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_single_executor import context

from spiritscout.awdp.editor import EditMatchError, edit_source
from spiritscout.awdp.package import extract_package, read_package
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import offline_execution, sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec


class MatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / 'attachments/app.rb'
        self.path.parent.mkdir()

    def edit(self, edits):
        return edit_source(self.root, 'one', {'path': str(self.path),
            'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(), 'edits': edits})

    def test_whitespace_error_returns_bound_range_without_writing_then_explicit_retry(self):
        original = 'class Example\r\n  def value\r\n    "中文"\r\n  end\r\nend\r\n'
        self.path.write_bytes(original.encode())
        with self.assertRaises(EditMatchError) as caught:
            self.edit([{'search': '    def value\n    "中文"\n  end', 'replace': '  def value\n    2\n  end'}])
        result = caught.exception.result
        self.assertFalse(result['executed'])
        self.assertFalse((self.root / 'sandbox').exists())
        candidate, = result['candidates']
        self.assertEqual((candidate['start_line'], candidate['end_line']), (2, 4))
        edited = self.edit([{'search': '', 'search_ref': candidate['search_ref'],
                             'replace': '  def value\r\n    2\r\n  end'}])
        self.assertEqual(Path(edited['patched']).read_bytes(), original.replace('"中文"', '2').encode())
        self.assertEqual(self.path.read_bytes(), original.encode())
        self.path.write_text(original + '# changed\n')
        with self.assertRaisesRegex(ValueError, '哈希'):
            self.edit([{'search_ref': candidate['search_ref'], 'replace': 'other'}])

    def test_ambiguity_overlap_and_non_whitespace_changes_are_not_automatic_edits(self):
        self.path.write_text('a = "two words"\na = "two words"\n')
        with self.assertRaises(EditMatchError) as caught:
            self.edit([{'search': 'a = "two words"', 'replace': 'a = 3'}])
        candidates = caught.exception.result['candidates']
        self.assertEqual(len(candidates), 2)
        entry = {'search_ref': candidates[0]['search_ref'], 'replace': 'a = 3'}
        with self.assertRaisesRegex(ValueError, '重叠'):
            self.edit([entry, entry])
        with self.assertRaises(ValueError):
            self.edit([{**entry, 'search': 'a = "two words"'}])
        with self.assertRaises(EditMatchError) as changed:
            self.edit([{'search': ' a = "twowords"', 'replace': 'a = 3'}])
        self.assertEqual(changed.exception.result['candidates'], [])
        edited = self.edit([entry])
        self.assertEqual(Path(edited['patched']).read_text(), 'a = 3\na = "two words"\n')


@unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
class BuildRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'attachments').mkdir()
        (self.root / 'sandbox/one').mkdir(parents=True)
        ctx = context(RunMode.AWDP)
        ctx.builtin_ctx = {'workspace_root': str(self.root), '_run_mode': RunMode.AWDP, '_lightweight_local': True}
        ctx.tools = ToolRegistry(None)
        self.mutate = None

        async def execute(**kwargs):
            self.assertTrue(offline_execution.get())
            if self.mutate:
                self.mutate()
            return await sandbox_exec(**kwargs)

        ctx.tools.register_builtin('sandbox_exec', execute,
            ToolSpec(name='sandbox_exec', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
        self.task = Task(task_id='one', task_type='ctf_single', agent_type=self.agent.agent_type,
            target='', payload={'challenge': {'id': 'one'}})
        self.route = SimpleNamespace(route_id='r', name='solve')

    async def build(self, name, original, modified):
        source = self.root / 'attachments' / name
        source.write_text(original)
        patched = self.root / 'sandbox/one' / name
        patched.write_text(modified)
        self.task.payload['challenge']['attachments'] = [str(source)]
        result = await self.agent._run_tool_call(self.task, self.route, {}, {'name': 'build_patch',
            'arguments': {'files': [{'source': name, 'patched': str(patched)}]}})
        self.assertFalse(offline_execution.get())
        return result, json.loads(result.text)

    @unittest.skipUnless(shutil.which('ruby'), '需要 Ruby')
    async def test_ruby_constant_in_method_blocked_then_valid_archive_applies(self):
        original = 'class Example\n  def value\n    1\n  end\nend\n'
        bad = original.replace('    1', '    ALLOWED = [1]')
        outcome, result = await self.build('app.rb', original, bad)
        self.assertFalse(outcome.ok, result)
        self.assertEqual(result['status'], 'invalid_source')
        self.assertIn('dynamic constant assignment', str(result))
        self.assertNotIn('archive', result)
        self.assertNotIn('已生成', outcome.note)
        self.assertFalse(list((self.root / 'sandbox/one').rglob('*.tar.gz')))
        state = {}
        call = {'name': 'build_patch', 'arguments': {'files': []}}
        for _ in range(2):
            self.agent._observe_tool_result(state, call, outcome)
        self.assertEqual(state['review_due']['tool'], 'build_patch')
        fixed = original.replace('    1', '    allowed = [1]\n    allowed.first')
        outcome, result = await self.build('app.rb', original, fixed)
        self.assertTrue(outcome.ok, result)
        self.assertTrue(result['syntax_checks']['complete'])
        deploy = self.root / 'deploy'
        deploy.mkdir()
        (deploy / 'app.rb').write_text(original)
        with open(result['archive'], 'rb') as stream:
            entry = extract_package(stream, self.root / 'unpacked')
        applied = subprocess.run(['sh', str(entry), str(deploy)], capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((deploy / 'app.rb').read_text(), fixed)

    async def test_python_syntax_rejected_and_packaging_uses_checked_snapshot(self):
        outcome, result = await self.build('app.py', 'value = 1\n', 'def broken(:\n')
        self.assertFalse(outcome.ok, result)
        self.assertEqual(result['status'], 'invalid_source')
        self.mutate = lambda: (self.root / 'sandbox/one/app.py').write_text('def broken(:\n')
        outcome, result = await self.build('app.py', 'value = 1\n', 'value = 2\n')
        self.assertTrue(outcome.ok, result)
        with open(result['archive'], 'rb') as stream:
            members = read_package(stream)
        self.assertEqual(members['files/app.py'][0], b'value = 2\n')

    async def test_unhandled_language_is_explicitly_unchecked(self):
        outcome, result = await self.build('app.rs', 'fn main() {}', 'fn main() { println!("ok"); }')
        self.assertTrue(outcome.ok, result)
        self.assertFalse(result['syntax_checks']['complete'])
        self.assertEqual(result['syntax_checks']['files'][0]['status'], 'skipped')

    async def test_invalid_json_reports_not_executed_and_edit_error_retains_candidates(self):
        outcome = await self.agent._run_tool_call(self.task, self.route, {}, {'name': 'verify_patch',
            'arguments': {}, 'parse_error': 'unterminated string'})
        result = json.loads(outcome.text)
        self.assertFalse(outcome.ok)
        self.assertFalse(result['executed'])
        self.assertIn('code_file', result['note'])
        source = self.root / 'attachments/app.rb'
        source.write_text('  def value\n    1\n  end\n')
        outcome = await self.agent._run_tool_call(self.task, self.route, {}, {'name': 'edit_source',
            'arguments': {'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'edits': [{'search': '    def value\n    1\n  end', 'replace': '  def value\n    2\n  end'}]}})
        result = json.loads(outcome.text)
        self.assertFalse(outcome.ok)
        self.assertEqual(result['status'], 'edit_mismatch')
        self.assertEqual(len(result['candidates']), 1)


if __name__ == '__main__':
    unittest.main()
