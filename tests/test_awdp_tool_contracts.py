"""原文续读与验证错误反馈：模型可直接使用真实入口，不需要猜参数或路径。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from test_single_executor import context

from spiritscout.awdp.builder import build_patch
from spiritscout.awdp.originals import initial_context, source_manifest
from spiritscout.awdp.verification import (
    failure_profile,
    failure_signature,
    issues,
    normalize_tests,
    verify_patch,
)
from spiritscout.awdp.verify_worker import TEST_RUNNER
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec


class ToolContractTests(unittest.TestCase):
    def test_issue_profile_survives_test_renaming_without_conflating_exact_evidence(self):
        first = {'tests_sha256': 'first', 'results': {'before': {'tests': [{
            'name': 'normal', 'kind': 'functional', 'run': 1, 'failures': 1,
            'command_result': {'returncode': 0, 'actual_stdout': 'a', 'expected_stdout': 'b'}}]}}}
        second = json.loads(json.dumps(first))
        second['tests_sha256'] = 'second'
        second['results']['before']['tests'][0]['name'] = 'renamed'
        self.assertEqual(failure_profile(first), failure_profile(second))
        self.assertNotEqual(failure_signature(first), failure_signature(second))
        second['results']['before']['tests'][0]['failures'] = 0
        self.assertEqual(failure_profile(second), [])

    def test_native_probe_fields_are_exclusive_and_require_expected_output(self):
        base = {'name': 'normal', 'kind': 'functional', 'command': ['python3', '-c', 'print(1)'],
                'expected_stdout': '1', 'code': '', 'code_file': ''}
        normalized = normalize_tests(Path('/unused'), 'one', [base])
        self.assertIn('subprocess.run', normalized[0]['code'])
        for change in ({'expected_stdout': None}, {'command': []}, {'command': 'python3'},
                       {'code': 'assert True'}, {'command': ['python3', '\0']}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_tests(Path('/unused'), 'one', [{**base, **change}])

    def test_no_change_review_completes_without_fabricating_a_fix_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'flags.json'
            manager = FlagManager(local_only=True, fix_mode=True, store_path=path)
            ctx = context(RunMode.AWDP)
            agent = SingleCTFAgent(ctx, flag_manager=manager)
            task = Task(task_id='one', task_type='ctf_single', agent_type=agent.agent_type, target='',
                        payload={'challenge': {'id': 'one'}})
            self.assertFalse(agent._tool_finish_challenge(task, {'complete': True, 'reason': 'reviewed'}).ok)
            done = agent._tool_finish_challenge(task,
                {'complete': True, 'no_fix_needed': True, 'reason': 'Contract already satisfied; reviewed app.py'})
            self.assertTrue(done.ok)
            self.assertTrue(done.stop)
            self.assertEqual(manager.fixes, {})
            restored = FlagManager(local_only=True, fix_mode=True, store_path=path)
            self.assertEqual(restored.completed_ids(), {'one'})
            restored.reopen('one', 'new followup')
            self.assertEqual(restored.completed_ids(), set())
            self.assertFalse(FlagManager(local_only=True).confirm_no_fix_needed('one', 'reviewed'))
            restored.record_fix('one', ['app.py'], 'new change', '/work/patch.tar.gz', 'one')
            self.assertFalse(restored.confirm_no_fix_needed('one', 'reviewed'))

    def test_exception_assertions_require_an_executed_typed_handler_or_failed_assert(self):
        cases = [
            ('try:\n    int("x")\n    assert False\nexcept ValueError:\n    pass\n', 1, 0, 1),
            ('try:\n    int("1")\n    assert False\nexcept ValueError:\n    pass\n', 1, 1, 0),
            ('def unused():\n    try:\n        int("x")\n        assert False\n'
             '    except ValueError:\n        pass\n', 0, 0, 0),
            ('try:\n    int("x")\nexcept ValueError:\n    pass\n', 0, 0, 0),
            ('print("PASS")\n', 0, 0, 0),
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.py'
            for code, run, failures, checks in cases:
                with self.subTest(code=code):
                    path.write_text(code)
                    process = subprocess.run([sys.executable, '-I', '-c', TEST_RUNNER, folder, str(path)],
                                             capture_output=True, text=True, check=True)
                    result = json.loads(process.stdout)
                    self.assertEqual(result['run'], run)
                    self.assertEqual(result['failures'], failures)
                    self.assertEqual(result.get('expected_exception_checks', 0), checks)

    def test_original_preview_and_listing_continue_without_missing_text_or_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'attachments/source.zip'
            archive.parent.mkdir()
            content = '中文注释\r\n' * 1200
            with zipfile.ZipFile(archive, 'w') as stream:
                stream.writestr('00.txt', content)
                for index in range(1, 12):
                    stream.writestr(f'{index:02}.txt', str(index))
            attachments = [str(archive)]
            initial = initial_context(root, 'one', attachments)
            entry = initial['files'][0]
            observed, call = entry['content'], entry['next_call']
            while call:
                self.assertEqual(call['name'], 'source_manifest')
                page = source_manifest(root, 'one', attachments, call['arguments'])
                self.assertEqual(page['offset'], len(observed))
                observed += page['content']
                call = page.get('next_call')
            self.assertEqual(observed, content)
            page = source_manifest(root, 'one', attachments, {'action': 'list'})
            seen = list(page['files'])
            while call := page.get('next_call'):
                self.assertEqual(call['name'], 'source_manifest')
                page = source_manifest(root, 'one', attachments, call['arguments'])
                seen.extend(page['files'])
            self.assertEqual(len({item['source_ref'] for item in seen}), 12)
            for item in seen:
                self.assertEqual(item['read_call']['name'], 'source_manifest')
                read = source_manifest(root, 'one', attachments, item['read_call']['arguments'])
                self.assertEqual(read['source'], item['source'])

    def test_functional_failure_surfaces_actual_load_error_before_patch_advice(self):
        feedback = issues({'before': {'test_sources_unchanged': False, 'tests': [{
            'name': 'normal', 'kind': 'functional', 'run': 1, 'failures': 1,
            'log': 'AssertionError: subprocess returned LoadError: cannot load lib/example'}]}})
        self.assertIn('删除测试中写入/复制/重建应用的代码', feedback[0])
        self.assertIn('原件的正常用例尚未通过', feedback[1])
        self.assertIn('LoadError: cannot load lib/example', feedback[1])


@unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
class SubmissionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_test_groups_cannot_submit_and_corrected_tests_reuse_identical_package(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'attachments/app.py'
            source.parent.mkdir()
            source.write_text('def value(x):\n    return x\n')
            modified = root / 'sandbox/one/app.py'
            modified.parent.mkdir(parents=True)
            modified.write_text('def value(x):\n    if x < 1: raise ValueError("positive only")\n    return x\n')
            attachments = [str(source)]
            built = build_patch(root, 'one', attachments,
                {'files': [{'source': 'app.py', 'patched': str(modified)}]})
            ctx = context(RunMode.AWDP)
            ctx.builtin_ctx = {'workspace_root': str(root), '_run_mode': RunMode.AWDP,
                               '_lightweight_local': True}
            ctx.tools = ToolRegistry(None)
            ctx.tools.register_builtin('sandbox_exec', sandbox_exec,
                ToolSpec(name='sandbox_exec', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
            agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
            task = Task(task_id='one', task_type='ctf_single', agent_type=agent.agent_type, target='',
                payload={'challenge': {'id': 'one', 'attachments': attachments}})
            route = SimpleNamespace(route_id='r', name='solve')
            normal = 'from app import value\nassert value(1) == 1\n'
            boundary = ('from app import value\nrejected = False\ntry: value(0)\n'
                        'except ValueError: rejected = True\nassert rejected, "zero must be rejected"\n')
            tests = [{'name': 'normal', 'kind': 'functional', 'code': normal + boundary},
                     {'name': 'boundary', 'kind': 'security', 'code': boundary}]
            result = await verify_patch(agent, task, route, {'archive': built['archive'], 'tests': tests})
            self.assertEqual(result['status'], 'inconclusive')
            state = {'messages': []}
            for number in range(2):
                args = {'archive': built['archive'], 'files': built['files'],
                        'description': f'claim {number}', 'verification_ref': result['verification_ref']}
                outcome = await agent._tool_submit_fix(task, route, args)
                self.assertFalse(outcome.ok)
                data = json.loads(outcome.text)
                self.assertEqual(data['status'], 'verification_not_passed')
                self.assertIn('移入 security', str(data['issues']))
                self.assertEqual(agent.flag_manager.fixes, {})
                agent._observe_tool_result(state, {'name': 'submit_fix', 'arguments': args}, outcome)
            self.assertEqual(state['review_due']['same_failure_count'], 2)
            tests[0]['code'] = normal
            verified = await verify_patch(agent, task, route, {'archive': built['archive'], 'tests': tests})
            self.assertEqual(verified['status'], 'passed', verified)
            self.assertEqual(verified['patch_sha256'], result['patch_sha256'])
            tests[1]['code'] = ('from app import value\ntry:\n    value(0)\n'
                                '    assert False, "must reject zero"\nexcept ValueError:\n    pass\n')
            verified = await verify_patch(agent, task, route, {'archive': built['archive'], 'tests': tests})
            self.assertEqual(verified['status'], 'passed', verified)
            self.assertEqual(verified['results']['after']['tests'][1]['expected_exception_checks'], 1)
            probe_tests = [
                {'name': 'normal', 'kind': 'functional', 'command': ['python3', '-c',
                    'from app import value; print(value(1))'], 'expected_stdout': '1'},
                {'name': 'boundary', 'kind': 'security', 'command': ['python3', '-c',
                    'from app import value\ntry: value(0); print("accepted")\n'
                    'except ValueError: print("rejected")'], 'expected_stdout': 'rejected'},
            ]
            verified = await verify_patch(agent, task, route, {'archive': built['archive'], 'tests': probe_tests})
            self.assertEqual(verified['status'], 'passed', verified)
            public = json.loads(Path(verified['report']).read_text())
            self.assertEqual(public['results'], public['reports'])
            self.assertEqual(public['results']['after']['tests'][1]['command_result'],
                             verified['results']['after']['tests'][1]['command_result'])
            broken = [{**test, 'command': ['python3', '-c', 'import missing_fixture_module']} for test in probe_tests]
            failed = await verify_patch(agent, task, route, {'archive': built['archive'], 'tests': broken})
            self.assertEqual(failed['status'], 'inconclusive')
            self.assertGreater(failed['results']['before']['tests'][1]['errors'], 0)
            self.assertEqual(failed['results']['before']['tests'][1]['input_format'], 'command')
            self.assertIn('command 模式由工具比较 stdout 生成断言', str(failed['issues']))
            self.assertIn('missing_fixture_module', str(failed['issues']))
            self.assertNotIn('需要实际执行 assert', str(failed['issues']))
            self.assertEqual(failed['results']['before']['tests'][1]['command_result']['returncode'], 1)
            submitted = await agent._tool_submit_fix(task, route, {
                **args, 'verification_ref': verified['verification_ref']})
            self.assertTrue(submitted.ok, submitted.text)


if __name__ == '__main__':
    unittest.main()
