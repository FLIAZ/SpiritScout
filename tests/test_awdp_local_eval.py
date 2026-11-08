"""验证评测器的默认无请求行为和回归判据；不请求真实模型。"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spiritscout.tools.sandbox import sandbox_available

SPEC = importlib.util.spec_from_file_location('eval_awdp_local',
    Path(__file__).parents[1] / 'scripts/eval_awdp_local.py')
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)


class LocalEvalTests(unittest.IsolatedAsyncioTestCase):
    def test_no_change_control_does_not_count_giving_up_or_unnecessary_edits_as_success(self):
        def finish(complete):
            return {'type': 'agent.tool_call', 'detail': {'calls': [{
                'name': 'finish_challenge', 'arguments': {'complete': complete, 'no_fix_needed': True}}]}}
        accepted = {'type': 'awdp.review_completed', 'detail': {'no_fix_needed': True}}
        self.assertFalse(EVAL.no_change_control([finish(False)])['completed_as_requested'])
        self.assertFalse(EVAL.no_change_control([])['completed_as_requested'])
        self.assertFalse(EVAL.no_change_control([finish(True)])['completed_as_requested'])
        self.assertTrue(EVAL.no_change_control([finish(True), accepted])['completed_as_requested'])
        control = EVAL.no_change_control([{'type': 'awdp.edit_source', 'detail': {'ok': True}}, finish(True), accepted])
        self.assertEqual(control['successful_edits_or_builds'], ['awdp.edit_source'])

    def test_initial_mission_is_not_a_midrun_intervention_but_repeated_hints_are(self):
        first = {'content': 'audit fixture', 'message_id': 'initial'}
        repeat = {'content': 'audit fixture', 'message_id': 'followup'}
        answer = {'answer': 'continue', 'question_id': 'q'}
        audit = [{'type': 'user.message', 'detail': first}, {'type': 'model.call', 'detail': {}},
                 {'type': 'user.message', 'detail': repeat}, {'type': 'user.answered', 'detail': answer}]
        self.assertEqual(EVAL.dialogue_inputs(audit, 'audit fixture'), (first, [repeat, answer]))
        self.assertEqual(EVAL.dialogue_inputs(audit, 'different mission'), (None, [first, repeat, answer]))

    async def test_default_plan_never_requests_model_and_remote_urls_are_rejected(self):
        args = SimpleNamespace(base_url='http://127.0.0.1:8081/v1', model='fixture',
            deadline=30, case=None, run_local=False)
        with patch.object(EVAL, 'request') as request, patch('builtins.print'):
            await EVAL.main(args)
        request.assert_not_called()
        for url in ('https://api.example.com/v1', 'http://127.0.0.1@evil.example/v1',
                    'http://user:password@localhost:8081/v1', 'http://localhost:8081/v1?api_key=x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                EVAL.local_url(url)

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_holdout_uses_deployment_root_as_working_directory(self):
        from spiritscout.awdp.builder import build_patch

        case = {'files': {'lib/value.txt': 'old'},
                'check': 'assert Path("lib/value.txt").read_text() == "new"\n', 'needs_fix': True}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(EVAL.CASES, {'cwd_fixture': case}):
            root = Path(tmp)
            source = root / 'attachments/lib/value.txt'
            source.parent.mkdir(parents=True)
            source.write_text('old')
            patched = root / 'sandbox/one/value.txt'
            patched.parent.mkdir(parents=True)
            patched.write_text('new')
            built = build_patch(root, 'one', [str(source)],
                {'files': [{'source': 'lib/value.txt', 'patched': str(patched)}]})
            result = await EVAL.grade_patch(root, 'cwd_fixture', Path(built['archive']).read_bytes())
            self.assertTrue(result['passed'], result)

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap')
    async def test_holdout_runs_exact_script_and_rejects_weak_fixes(self):
        from spiritscout.awdp.builder import build_patch

        with tempfile.TemporaryDirectory() as tmp:
            for case_id in ('cross_file', 'ownership'):
                for repaired in (False, True, 'prefix') if case_id == 'cross_file' else (False, True):
                    root = Path(tmp) / f'{case_id}-{repaired}'
                    for name, content in EVAL.CASES[case_id]['files'].items():
                        path = root / 'attachments' / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content)
                    filename = 'storage.py' if case_id == 'cross_file' else 'app.py'
                    original = EVAL.CASES[case_id]['files'][filename]
                    if repaired == 'prefix':
                        text = original.replace('    return (ROOT / name).read_text()',
                            '    target = (ROOT / name).resolve()\n'
                            '    if not str(target).startswith(str(ROOT)):\n'
                            '        raise ValueError("outside")\n    return target.read_text()')
                    elif repaired:
                        text = (original.replace('    return (ROOT / name).read_text()',
                            '    target = (ROOT / name).resolve()\n'
                            '    if not target.is_relative_to(ROOT.resolve()):\n'
                            '        raise ValueError("outside")\n    return target.read_text()')
                            if case_id == 'cross_file' else original.replace(
                                '    return NOTES[note_id]["text"]',
                                '    if NOTES[note_id]["owner"] != current_user: raise PermissionError("owner")\n'
                                '    return NOTES[note_id]["text"]'))
                    else:
                        text = original + '# unchanged behavior\n'
                    path = root / 'sandbox/one' / filename
                    path.parent.mkdir(parents=True)
                    path.write_text(text)
                    built = build_patch(root, 'one',
                        [str(root / 'attachments' / name) for name in EVAL.CASES[case_id]['files']],
                        {'files': [{'source': filename, 'patched': str(path)}]})
                    script = Path(built['archive']).read_bytes()
                    result = await EVAL.grade_patch(root, case_id, script)
                    self.assertEqual(result['passed'], repaired is True, result)
                    self.assertEqual(result['sha256'], built['sha256'])


if __name__ == '__main__':
    unittest.main()
