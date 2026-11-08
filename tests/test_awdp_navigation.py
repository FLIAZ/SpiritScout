"""源码导航、精确编辑与实际沙箱/补丁链路；不把工具验证当作模型效果评测。"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_single_executor import context

from spiritscout.awdp.builder import build_patch
from spiritscout.awdp.context import query_index
from spiritscout.awdp.context_worker import run
from spiritscout.awdp.editor import edit_source
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec


def digest(data):
    return hashlib.sha256(data).hexdigest()


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'sandbox/one/source'
        self.source.mkdir(parents=True)

    def index(self):
        directory = self.root / 'artifacts/awdp-context/one/run'
        directory.mkdir(parents=True)
        report = run({'path': str(self.source), 'directory': str(directory), 'timeout': 10})
        self.index_path = str(directory / 'index.json')
        return report

    def query(self, action, **kwargs):
        return query_index(self.root, 'one', {'action': action, 'index': self.index_path, **kwargs})

    def test_python_cross_file_aliases_duplicate_names_and_exact_read(self):
        (self.source / 'app.py').write_text('from storage import read_item as fetch\n'
            'import storage as db\nimport package.storage\n'
            '@route("/read")\ndef handler(name):\n    return fetch(name), db.read_item(name), package.storage.read_item(name)\n'
            '# read_item(name) is only a comment\n')
        (self.source / 'storage.py').write_text('def read_item(name):\n    return open(name).read()\n')
        nested = self.source / 'vendor/tests'
        nested.mkdir(parents=True)
        (nested / 'other.py').write_text('class Other:\n    def read_item(self, name):\n        return "constant"\n')
        report = self.index()
        self.assertEqual(report['status'], 'completed')
        definitions = self.query('find', symbol='read_item', limit=1)
        self.assertEqual(definitions['total'], 2)
        self.assertTrue(definitions['has_more'])
        second = query_index(self.root, 'one', definitions['next_call']['arguments'])
        self.assertFalse(second['has_more'])
        self.assertNotEqual(definitions['items'][0]['file_id'], second['items'][0]['file_id'])
        refs = self.query('references', symbol='read_item')
        self.assertEqual(len(refs['items']), 3)
        self.assertEqual({x['match'] for x in refs['items']}, {'import_alias_candidate'})
        self.assertTrue(all(x['caller'] == 'handler' and not x['binding_verified'] for x in refs['items']))
        definition = definitions['items'][0]
        read = query_index(self.root, 'one', definition['read_call']['arguments'])
        self.assertIn('return open(name).read()', read['content'])
        self.assertEqual(read['sha256'], digest(Path(read['path']).read_bytes()))
        handler = self.query('find', symbol='handler')['items'][0]
        self.assertEqual(handler['line'], 4)  # 保留路由装饰器上下文。

    def test_text_windows_round_trip_long_lines_crlf_and_snapshot_hash(self):
        data = ('title\r\n' + '数据' * 6200 + '\r\nlast line').encode()
        (self.source / 'README.md').write_bytes(data)
        self.index()
        page = self.query('map', limit=1)
        args = page['items'][0]['read_call']['arguments'] | {'lines': 1}
        parts = []
        while True:
            value = query_index(self.root, 'one', args)
            self.assertLessEqual(len(value['content']), 5000)
            parts.append(value['content'])
            if not value['has_more']:
                break
            args = value['next_call']['arguments']
        self.assertEqual(''.join(parts).encode(), data)
        # 工作副本的修改不改变旧快照；编辑器另用实时哈希检查。
        (self.source / 'README.md').write_text('changed')
        self.assertEqual(self.query('read', file_id='f0000')['sha256'], digest(data))
        snapshot = Path(self.index_path).parent / 'files/f0000.txt'
        snapshot.write_text('tampered')
        with self.assertRaisesRegex(ValueError, '哈希'):
            self.query('read', file_id='f0000')

    def test_missing_ctags_and_parse_failure_are_explicit_not_empty_success(self):
        (self.source / 'ok.py').write_text('def valid():\n    pass\n')
        (self.source / 'bad.py').write_text('def broken(:\n')
        (self.source / 'app.php').write_text('<?php function hello() {}')
        (self.source / 'binary').write_bytes(b'\x00\xff')
        (self.source / 'linked.py').symlink_to(self.source / 'ok.py')
        with patch('spiritscout.awdp.context_worker.shutil.which', return_value=None):
            report = self.index()
        self.assertEqual(report['ctags'], 'unavailable')
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['coverage']['complete'])
        self.assertEqual(len(report['skipped']), 2)
        self.assertTrue(any(x['reason'] == 'python_parse_error' for x in report['errors']))
        self.assertEqual(self.query('find', symbol='valid')['total'], 1)
        with self.assertRaises(ValueError):
            query_index(self.root, 'two', {'action': 'map', 'index': self.index_path})

    @unittest.skipUnless(shutil.which('ctags'), '需要 Universal Ctags')
    def test_real_ctags_multilanguage_definitions_and_lexical_references(self):
        examples = {'app.php': '<?php\nfunction serve($x) { return file_get_contents($x); }\n',
            'app.js': 'export function serve(x) { return read(x); }\n',
            'app.ts': 'export function serve(x: string): string { return x; }\n',
            'App.java': 'class App { public String serve(String x) { return x; } }\n',
            'app.go': 'package main\nfunc serve(x string) string { return x }\n',
            'app.rb': 'def serve(x)\n  x\nend\n', 'app.c': 'int serve(int x) { return x; }\n',
            'app.cpp': 'int serve(int x) { return x; }\n',
            'app.py': 'def serve(x):\n    return x\n'}
        for name, text in examples.items():
            (self.source / name).write_text(text)
        (self.source / '.ctags').write_text('--invalid-option-from-attachment\n')
        report = self.index()
        self.assertEqual(report['ctags'], 'completed', report['errors'])
        self.assertEqual(report['status'], 'completed', report['errors'])
        result = self.query('find', symbol='serve', limit=12)
        items = result['items'][:]
        while result['has_more']:
            result = query_index(self.root, 'one', result['next_call']['arguments'])
            items.extend(result['items'])
        self.assertEqual({Path(x['path']).name for x in items}, set(examples))
        php = next(x for x in items if x['path'].endswith('.php'))
        refs = self.query('references', symbol='file_get_contents', file_id=php['file_id'])
        self.assertEqual(refs['items'][0]['match'], 'lexical_candidate')
        self.assertFalse(refs['items'][0]['binding_verified'])


class ExactEditTests(unittest.TestCase):
    setUp = NavigationTests.setUp

    def edit(self, path, edits, sha=None):
        return edit_source(self.root, 'one', {'path': str(path), 'sha256': sha or digest(path.read_bytes()),
                                               'edits': edits})

    def test_all_edits_validated_before_output_and_source_never_mutated(self):
        path = self.source / 'app.py'
        original = b'a = 1\r\nb = 2\r\n'
        path.write_bytes(original)
        path.chmod(0o640)
        bad_edits = [[{'search': 'a = 1', 'replace': 'a = 3'}, {'search': 'missing', 'replace': 'x'}],
            [{'search': 'a = 1', 'replace': 'new'}, {'search': '= 1', 'replace': 'other'}],
            [{'search': '\r\n', 'replace': '\n'}], [{'search': 'a', 'replace': 'a'}]]
        for entries in bad_edits:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.edit(path, entries)
        self.assertEqual(list((self.root / 'sandbox/one').iterdir()), [self.source])
        with self.assertRaisesRegex(ValueError, '哈希'):
            self.edit(path, [{'search': '1', 'replace': '3'}], '0' * 64)
        result = self.edit(path, [{'search': 'a = 1', 'replace': 'a = 3'}, {'search': 'b = 2', 'replace': 'b = 4'}])
        self.assertEqual(path.read_bytes(), original)
        output = Path(result['patched'])
        self.assertEqual(output.read_bytes(), b'a = 3\r\nb = 4\r\n')
        self.assertEqual(output.stat().st_mode & 0o777, 0o640)
        self.assertEqual(digest(output.read_bytes()), result['sha256'])
        self.assertFalse(result['syntax_verified'])
        self.assertEqual(result['original_path'], str(path))
        self.assertNotIn('source', result)  # 不与 build_patch.files.source 的原件成员名混淆。
        self.assertTrue(Path(result['diff_file']).is_file())

    def test_exact_edit_to_self_contained_patch_on_clean_original(self):
        original = self.root / 'attachments/bundle/app.py'
        original.parent.mkdir(parents=True)
        original.write_text('print("before")\n')
        edited = self.edit(original, [{'search': '"before"', 'replace': '"after"'}])
        result = build_patch(self.root, 'one', [str(original)], {'format': 'shell_script', 'files': [
            {'source': 'bundle/app.py', 'patched': edited['patched'], 'target': 'app.py'}]})
        deploy = self.root / 'deploy'
        deploy.mkdir()
        shutil.copyfile(original, deploy / 'app.py')
        shutil.rmtree(Path(edited['patched']).parent)  # 最终脚本不可依赖修改副本。
        applied = subprocess.run(['sh', result['archive'], str(deploy)], capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((deploy / 'app.py').read_text(), 'print("after")\n')
        self.assertEqual(original.read_text(), 'print("before")\n')

    def test_links_other_challenge_and_attachment_escape_are_rejected(self):
        path = self.source / 'file.py'
        path.write_text('old')
        link = self.source / 'link.py'
        link.symlink_to(path)
        outside = self.root / 'sandbox/two/out.py'
        outside.parent.mkdir()
        outside.write_text('old')
        for target in (link, outside, self.source / '../source/file.py'):
            with self.subTest(path=target), self.assertRaises(ValueError):
                self.edit(target, [{'search': 'old', 'replace': 'new'}])


class NavigationAgentTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(sandbox_available() and shutil.which('ctags'), '需要真实 bubblewrap 与 Universal Ctags')
    async def test_actual_sandbox_index_read_and_edit_tool_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'sandbox/one/source'
            source.mkdir(parents=True)
            (root / 'attachments').mkdir()
            path = source / 'app.php'
            path.write_text('<?php\nfunction serve($x) { return "before"; }\n')
            tools = ToolRegistry(None)
            tools.register_builtin('sandbox_exec', sandbox_exec,
                ToolSpec(name='sandbox_exec', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
            ctx = context(RunMode.AWDP)
            ctx.tools = tools
            ctx.builtin_ctx = {'workspace_root': tmp, '_run_mode': RunMode.AWDP, '_lightweight_local': True}
            agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
            task = Task(task_id='one', task_type='ctf_single', agent_type=agent.agent_type, target='',
                        payload={'challenge': {'id': 'one'}})
            route = SimpleNamespace(route_id='r', name='solve')

            async def invoke(name, args):
                result = await agent._run_tool_call(task, route, {}, {'name': 'tool_invoke',
                    'arguments': {'name': name, 'arguments': args}})
                self.assertTrue(result.ok, result.text)
                return json.loads(result.text)

            indexed = await invoke('source_context', {'action': 'index', 'path': 'source'})
            self.assertEqual(indexed['ctags'], 'completed', indexed)
            found = await invoke('source_context', {'action': 'find', 'index': indexed['index'], 'symbol': 'serve'})
            self.assertEqual(found['total'], 1)
            read = await invoke('source_context', found['items'][0]['read_call']['arguments'])
            direct = await invoke('source_context', {'action': 'read', 'path': 'source/app.php'})
            self.assertEqual(direct['content'], path.read_text())
            self.assertEqual(direct['sha256'], read['sha256'])
            edited = await invoke('edit_source', {'path': read['path'], 'sha256': read['sha256'],
                'edits': [{'search': '"before"', 'replace': '"after"'}]})
            self.assertIn('"after"', Path(edited['patched']).read_text())
            self.assertIn('"before"', path.read_text())


if __name__ == '__main__':
    unittest.main()
