"""原件引用、最终提交件真实双环境回归，以及回执不能被改件复用。"""
from __future__ import annotations

import hashlib
import io
import json
import socket
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from test_single_executor import context

from spiritscout.awdp.builder import build_patch
from spiritscout.awdp.editor import edit_source
from spiritscout.awdp.originals import catalog, initial_context, source_manifest
from spiritscout.awdp.package import pack, read_package
from spiritscout.awdp.verification import (
    TestInputError,
    check_receipt,
    failure_signature,
    normalize_tests,
    prepare,
    verify_patch,
)
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.loop_recovery import activate
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec

FILES = {
    'app.py': 'from storage import read_document as load\ndef handle(query):\n    return load(query["name"])\n',
    'storage.py': 'from pathlib import Path\nROOT = Path(__file__).parent / "public"\n'
                  'def read_document(name):\n    return (ROOT / name).read_text()\n',
    'public/hello.txt': 'hello', 'public/a..b.txt': 'dots', 'public/目录/nested.txt': 'nested',
    'secret.txt': 'PRIVATE-SENTINEL'}
TESTS = [{'name': 'normal', 'kind': 'functional', 'code': '''import unittest
from app import handle
class Normal(unittest.TestCase):
    def test_normal(self):
        for name, value in [('hello.txt','hello'),('a..b.txt','dots'),('目录/nested.txt','nested')]:
            with self.subTest(name=name): self.assertEqual(handle({'name':name}), value)
'''}, {'name': 'boundaries', 'kind': 'security', 'code': '''import unittest
from pathlib import Path
from app import handle
class Boundary(unittest.TestCase):
    def test_boundaries(self):
        root = Path.cwd()
        (root/'public/link.txt').symlink_to('../secret.txt')
        for name in ['../secret.txt',str(root/'secret.txt'),'link.txt']:
            with self.subTest(name=name):
                try: value = handle({'name':name})
                except (ValueError,PermissionError,FileNotFoundError): continue
                self.assertNotIn('PRIVATE-SENTINEL', str(value))
'''}]


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'attachments').mkdir()
        archive = self.root / 'attachments/source.zip'
        with zipfile.ZipFile(archive, 'w') as stream:
            for name, text in FILES.items():
                stream.writestr('bundle/' + name, text)
        self.attachments = [str(archive)]
        ctx = context(self.root)
        ctx.mode = RunMode.AWDP
        ctx.builtin_ctx['workspace_root'] = str(self.root)
        ctx.tools = ToolRegistry(None)
        ctx.tools.register_builtin('sandbox_exec', sandbox_exec,
            ToolSpec(name='sandbox_exec', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
        self.task = Task(task_id='one', task_type='ctf_single', agent_type=self.agent.agent_type,
            target='', payload={'challenge': {'id': 'one', 'attachments': self.attachments}})
        self.route = SimpleNamespace(route_id='r', name='solve')

    def build(self, replacement, **options):
        original = catalog(self.root, 'one', self.attachments)
        entry = next(item for item in original['files'] if item['source'] == 'bundle/storage.py')
        modified = edit_source(self.root, 'one', {'source_ref': entry['source_ref'], 'edits': [{
            'search': '    return (ROOT / name).read_text()', 'replace': replacement}]}, self.attachments)
        return build_patch(self.root, 'one', self.attachments, {**options, 'files': [{
            'source_ref': entry['source_ref'], 'patched': modified['patched'], 'target': 'storage.py'}]})

    async def test_manifest_plain_tar_zip_checkout_and_stale_ref(self):
        for fmt in ('zip', 'tar', 'plain'):
            with self.subTest(fmt=fmt):
                path = self.root / 'attachments' / ('other.' + fmt)
                if fmt == 'tar':
                    with tarfile.open(path, 'w') as stream:
                        item = tarfile.TarInfo('目录/test.py')
                        item.size, item.mode = 8, 0o750
                        stream.addfile(item, io.BytesIO(b'print(1)'))
                elif fmt == 'zip':
                    with zipfile.ZipFile(path, 'w') as stream:
                        stream.writestr('目录/test.py', 'print(1)')
                else:
                    path.write_text('print(1)')
                args = [str(path)]
                listing = source_manifest(self.root, fmt, args, {'action': 'list'})
                ref = listing['files'][0]['source_ref']
                read = source_manifest(self.root, fmt, args, {'action': 'read', 'source_ref': ref})
                self.assertEqual(read['content'], 'print(1)')
                checked = source_manifest(self.root, fmt, args, {'action': 'checkout'})
                self.assertEqual((Path(checked['path']) / read['source']).read_text(), 'print(1)')
                path.write_text('changed')
                with self.assertRaises(ValueError):
                    source_manifest(self.root, fmt, args, {'action': 'read', 'source_ref': ref})

    async def test_startup_material_has_actual_checkout_and_same_source_references(self):
        initial = initial_context(self.root, 'one', self.attachments)
        self.assertTrue(initial['ok'])
        for entry in initial['files']:
            content = (Path(initial['path']) / entry['source']).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), entry['sha256'])
            self.assertEqual(entry['read_call']['name'], 'source_manifest')
            self.assertEqual(source_manifest(self.root, 'one', self.attachments, entry['read_call']['arguments'])['source_ref'],
                             entry['source_ref'])
        direct = {schema['function']['name'] for schema in self.agent._model_tool_schemas()}
        self.assertTrue({'source_manifest', 'edit_source', 'build_patch', 'verify_patch'} <= direct)

    async def test_archive_links_traversal_and_ambiguous_members_are_not_checkout_success(self):
        path = self.root / 'attachments/bad.zip'
        for names in (['../escape.py'], ['same.py', 'same.py']):
            with zipfile.ZipFile(path, 'w') as stream:
                for name in names:
                    stream.writestr(name, 'x')
            with self.assertRaises(ValueError):
                source_manifest(self.root, 'bad', [str(path)], {'action': 'checkout'})
        with zipfile.ZipFile(path, 'w') as stream:
            item = zipfile.ZipInfo('link')
            item.external_attr = 0o120777 << 16
            stream.writestr(item, '/etc/passwd')
        with self.assertRaisesRegex(ValueError, '链接'):
            source_manifest(self.root, 'bad', [str(path)], {'action': 'checkout'})

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_actual_good_patch_passes_and_final_submission_binds_receipt(self):
        built = self.build('    target = (ROOT / name).resolve()\n'
            '    if not target.is_relative_to(ROOT.resolve()):\n        raise ValueError("outside")\n'
            '    return target.read_text()')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass  # 宿主端口确实可连接；隔离环境必须无法访问它。
            network = {'name': 'offline', 'kind': 'functional', 'code':
                f'import socket\ntry:\n    with socket.create_connection(("127.0.0.1", {port}), timeout=1):\n'
                '        reachable = True\nexcept OSError:\n    reachable = False\n'
                'assert not reachable, "host loopback exposed"'}
            result = await verify_patch(self.agent, self.task, self.route,
                {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': [*TESTS, network]})
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['results']['before']['tests'][1]['failures'], 3)
        self.assertTrue(result['results']['after']['exact_changes'])
        receipt = check_receipt(self.root, 'one', result['verification_ref'], built['sha256'], self.attachments)
        self.assertEqual(receipt['network'], 'disabled')
        Path(result['report']).write_text('{"status":"forged"}')
        self.assertEqual(check_receipt(self.root, 'one', result['verification_ref'],
                                      built['sha256'], self.attachments)['status'], 'passed')
        # 普通模型沙箱看不见控制面的私有快照与回执。
        hidden = await sandbox_exec('test ! -e ' + str(self.root / '.awdp'), workspace_root=str(self.root),
                                    workspace_scope='one', guard_root=str(self.root))
        self.assertEqual(hidden.returncode, 0)
        incorrect = await self.agent._tool_submit_fix(self.task, self.route,
            {'archive': built['archive'], 'files': ['bundle/app.py'], 'description': 'wrong mapping',
             'verification_ref': result['verification_ref']})
        self.assertFalse(incorrect.ok, incorrect.text)
        self.assertEqual(self.agent.flag_manager.fixes, {})
        outcome = await self.agent._tool_submit_fix(self.task, self.route,
            {'archive': built['archive'], 'files': built['files'], 'description': 'test evidence',
             'verification_ref': result['verification_ref']})
        self.assertTrue(outcome.ok, outcome.text)
        self.assertIn('passed', outcome.text)
        path = Path(built['archive'])
        path.write_bytes(path.read_bytes() + b'\n# modified\n')
        with self.assertRaisesRegex(ValueError, '另一份提交件'):
            check_receipt(self.root, 'one', result['verification_ref'],
                          hashlib.sha256(path.read_bytes()).hexdigest(), self.attachments)

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_weak_patch_and_disabled_feature_fail_actual_tests(self):
        for replacement in ('    if "../" in name: raise ValueError("outside")\n'
                            '    return (ROOT / name).read_text()', '    raise ValueError("deny all")'):
            with self.subTest(replacement=replacement):
                built = self.build(replacement)
                result = await verify_patch(self.agent, self.task, self.route,
                    {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': TESTS})
                self.assertNotEqual(result['status'], 'passed', result)
                self.assertIn('PRIVATE-SENTINEL' if '../' in replacement else 'deny all',
                              json.dumps(result['results']))

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_platform_layouts_and_legacy_script_execute_final_bytes(self):
        for options in ({'entrypoint': 'update.sh'}, {'entrypoint': 'update.sh', 'package_dir': 'update'},
                        {'format': 'shell_script'}):
            with self.subTest(options=options):
                built = self.build('    target = (ROOT / name).resolve()\n'
                    '    if not target.is_relative_to(ROOT.resolve()): raise ValueError("outside")\n'
                    '    return target.read_text()', **options)
                result = await verify_patch(self.agent, self.task, self.route,
                    {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': TESTS})
                self.assertEqual(result['status'], 'passed', result)
                receipt = check_receipt(self.root, 'one', result['verification_ref'], built['sha256'], self.attachments)
                self.assertEqual(receipt['format'], built['format'])
                self.assertEqual(receipt['entrypoint'], built['entrypoint'])
                if built['format'] == 'tar_gzip':
                    final = Path(built['archive'])
                    files = read_package(io.BytesIO(final.read_bytes()))
                    # 只改包内权限也成为另一份提交件，不能复用旧回执。
                    data, mode = files[built['entrypoint']]
                    files[built['entrypoint']] = (data, mode ^ 0o100)
                    changed = pack(files)
                    final.write_bytes(changed)
                    with self.assertRaisesRegex(ValueError, '另一份提交件'):
                        check_receipt(self.root, 'one', result['verification_ref'],
                                      hashlib.sha256(changed).hexdigest(), self.attachments)
                    with self.assertRaises(ValueError):
                        await verify_patch(self.agent, self.task, self.route,
                            {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': TESTS})

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_repeated_verification_error_enters_review_despite_new_receipt_paths(self):
        built = self.build('    target = (ROOT / name).resolve()\n'
            '    if not target.is_relative_to(ROOT.resolve()): raise ValueError("outside")\n'
            '    return target.read_text()')
        bad = 'exec(open("storage.py").read())\nassert read_document("hello.txt") == "hello"'
        args = {'archive': built['archive'], 'source_prefix': 'bundle',
                'tests': [dict(test, code=bad) for test in TESTS]}
        state, receipts, fingerprints = {'messages': [], 'observations': []}, [], []
        for index in range(2):
            call = {'id': str(index), 'name': 'verify_patch', 'arguments': args}
            outcome = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
            self.assertFalse(outcome.ok, outcome.text)
            data = json.loads(outcome.text)
            self.assertEqual(data['status'], 'inconclusive')
            self.assertIn('__file__', str(data['issues']))
            receipts.append(data['verification_ref'])
            fingerprints.append(outcome.metadata['verification_failure'])
            state['messages'].append({'role': 'tool', 'tool_call_id': str(index), 'content': outcome.text})
            self.agent._observe_tool_result(state, call, outcome)
        self.assertNotEqual(receipts[0], receipts[1])
        self.assertEqual(fingerprints[0], fingerprints[1])
        self.assertEqual(state['review_due']['same_failure_count'], 2)
        self.assertTrue(activate(state))
        # 改件、改测试、实际错误变化都不能被当作相同失败。
        for key, value in [('patch_sha256', 'changed'), ('tests_sha256', 'changed'),
                           ('results', {'error': 'different failure'})]:
            self.assertNotEqual(failure_signature({**data, key: value}), fingerprints[-1])
        state.pop('loop_recovery')
        outcome = await self.agent._run_tool_call_inner(self.task, self.route, state,
            {**call, 'arguments': {**args, 'tests': TESTS}})
        self.assertTrue(outcome.ok, outcome.text)
        self.agent._observe_tool_result(state, call, outcome)
        self.assertNotIn('verify_patch_failure', state)

    async def test_syntax_preflight_stops_before_any_build_or_deployment(self):
        bad = [dict(test, code="assert True\n', {") for test in TESTS]
        with self.assertRaises(TestInputError) as failure:
            prepare(self.root, 'one', self.attachments, {'archive': 'does-not-exist', 'tests': bad})
        self.assertEqual(len(failure.exception.errors), 2)
        self.assertIn('code_file', str(failure.exception))
        self.assertFalse((self.root / '.awdp').exists())

    async def test_changed_invalid_test_strings_still_trigger_recovery(self):
        state = {'messages': [], 'observations': []}
        for index, code in enumerate(("assert True\n', {", "# changed\nassert True\n',")):
            call = {'id': str(index), 'name': 'verify_patch', 'arguments': {
                'archive': 'unused', 'tests': [dict(test, code=code) for test in TESTS]}}
            outcome = await self.agent._run_tool_call_inner(self.task, self.route, state, call)
            self.assertFalse(outcome.ok)
            data = json.loads(outcome.text)
            self.assertEqual(data['status'], 'invalid_tests')
            self.assertFalse(data['executed'])
            self.assertNotIn('verification_ref', data)
            state['messages'].append({'role': 'tool', 'tool_call_id': str(index), 'content': outcome.text})
            self.agent._observe_tool_result(state, call, outcome)
        self.assertEqual(state['review_due']['same_failure_count'], 2)
        self.assertTrue(activate(state))

    async def test_code_file_is_scoped_and_cannot_be_combined_with_code(self):
        path = self.root / 'sandbox/one/test.py'
        path.parent.mkdir(parents=True)
        path.write_text('assert True\n')
        test = {'name': 'normal', 'kind': 'functional', 'code_file': str(path)}
        self.assertEqual(normalize_tests(self.root, 'one', [test])[0]['code'], 'assert True\n')
        with self.assertRaisesRegex(ValueError, '二选一'):
            normalize_tests(self.root, 'one', [dict(test, code='assert True')])
        path.unlink()
        path.symlink_to(self.root / 'meta.json')
        (self.root / 'meta.json').write_text('assert True\n')
        with self.assertRaises((OSError, ValueError)):
            normalize_tests(self.root, 'one', [test])

    async def test_empty_optional_test_fields_do_not_conflict_with_real_input(self):
        path = self.root / 'sandbox/one/test.py'
        path.parent.mkdir(parents=True)
        path.write_text('assert True\n')
        base = {'name': 'normal', 'kind': 'functional'}
        for fields in ({'code': 'assert True\n', 'code_file': ''},
                       {'code': '', 'code_file': str(path)}):
            with self.subTest(fields=fields):
                self.assertEqual(normalize_tests(self.root, 'one', [{**base, **fields}])[0]['code'],
                                 'assert True\n')
        for fields in ({'code': '', 'code_file': ''}, {'code': None}, {'code_file': None},
                       {'code': 'assert True', 'code_file': str(path)}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                normalize_tests(self.root, 'one', [{**base, **fields}])

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_frozen_code_files_and_main_guard_run_in_real_sandbox(self):
        built = self.build('    target = (ROOT / name).resolve()\n'
            '    if not target.is_relative_to(ROOT.resolve()): raise ValueError("outside")\n'
            '    return target.read_text()')
        normal = ('from app import handle\ndef test():\n'
                  '    assert handle({"name":"hello.txt"}) == "hello"\n'
                  'if __name__ == "__main__":\n    test()\n')
        tests = []
        for index, test in enumerate([dict(TESTS[0], code=normal), TESTS[1]]):
            path = self.root / f'sandbox/one/test_{index}.py'
            path.write_text(test['code'])
            tests.append({'name': test['name'], 'kind': test['kind'], 'code_file': str(path)})
        args = {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': tests}
        receipt, directory = prepare(self.root, 'one', self.attachments, args)
        saved = self.root / directory / 'before/attachments/0.py'
        Path(tests[0]['code_file']).write_text('assert False\n')
        self.assertEqual(saved.read_text(), normal)
        self.assertEqual(receipt['tests'][0]['code'], normal)
        Path(tests[0]['code_file']).write_text(normal)
        result = await verify_patch(self.agent, self.task, self.route, args)
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['results']['before']['tests'][0]['assertions'], 1)

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_empty_skip_or_import_errors_never_prove_reproduction(self):
        built = self.build('    return "not private"')
        for code in ('', 'import missing_awdp_dependency',
                     'import unittest\n@unittest.skip("no")\nclass T(unittest.TestCase):\n def test_it(self): pass'):
            tests = [TESTS[0], dict(TESTS[1], code=code or '# no test cases')]
            result = await verify_patch(self.agent, self.task, self.route,
                {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': tests})
            self.assertEqual(result['status'], 'inconclusive', result)

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_tests_cannot_modify_the_program_to_make_a_fix_pass(self):
        built = self.build('    return "not private"')
        poisoned = [dict(test, code='from pathlib import Path\nPath("app.py").write_text("# forged")\n'
                                    + test['code']) for test in TESTS]
        result = await verify_patch(self.agent, self.task, self.route,
            {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': poisoned})
        self.assertEqual(result['status'], 'inconclusive', result)

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_large_unicode_tests_run_from_frozen_files_without_argv_overflow(self):
        built = self.build('    target = (ROOT / name).resolve()\n'
            '    if not target.is_relative_to(ROOT.resolve()): raise ValueError("outside")\n'
            '    return target.read_text()')
        tests = [dict(test, code=test['code'] + '\n#' + '注' * 14000) for test in TESTS]
        result = await verify_patch(self.agent, self.task, self.route,
            {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': tests})
        self.assertEqual(result['status'], 'passed', result)

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_plain_assert_scripts_count_executed_assertions_only(self):
        built = self.build('    target = (ROOT / name).resolve()\n'
            '    if not target.is_relative_to(ROOT.resolve()): raise ValueError("outside")\n'
            '    return target.read_text()')
        tests = [{'name': 'normal', 'kind': 'functional',
            'code': 'from app import handle\nassert handle({"name":"hello.txt"}) == "hello"'},
            {'name': 'boundary', 'kind': 'security', 'code': 'from app import handle\n'
             'try: value = handle({"name":"../secret.txt"})\nexcept ValueError: value = ""\n'
             'assert "PRIVATE-SENTINEL" not in value'}]
        result = await verify_patch(self.agent, self.task, self.route,
            {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': tests})
        self.assertEqual(result['status'], 'passed', result)
        self.assertTrue(result['ok'])
        tests[1]['code'] = 'def never_called():\n    assert False\nprint("PASS")'
        result = await verify_patch(self.agent, self.task, self.route,
            {'archive': built['archive'], 'source_prefix': 'bundle', 'tests': tests})
        self.assertEqual(result['status'], 'inconclusive', result)
        self.assertFalse(result['ok'])
        self.assertTrue(result['issues'])


if __name__ == '__main__':
    unittest.main()
