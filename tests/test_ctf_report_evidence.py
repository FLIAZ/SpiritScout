"""CTF 技术报告的真实证据、回退、脚本落盘与会话下载。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import NullAuditLogger
from spiritscout.ctf import report_jobs
from spiritscout.ctf.report_evidence import (
    collect_evidence,
    save_writeup,
    technical_appendix,
)
from spiritscout.ctf.writeup import _writer_material, build_digest, writeup_body
from spiritscout.tools.invocation import ExecutionScope
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.tools.spec import ToolResult


class ReportEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bb = Blackboard(self.root / 'bb.db')
        self.addAsyncCleanup(self.bb.aclose)

    def evidence(self):
        journal = ExecutionJournal(self.bb, self.root, 'test-run')
        journal.begin('inv-code', 'sandbox_exec', 'digest', 'safe', params={'command': 'python exp.py'},
                      scope=ExecutionScope(task_id='task-one', route_id='route-one'))
        journal.transition('inv-code', 'failed', result=ToolResult(tool='sandbox_exec', ok=False,
            returncode=1, stdout='observed output', stderr='actual error', parsed={'workdir': str(self.root / 'sandbox')}))
        source = self.root / 'sandbox' / 'exp.py'
        source.parent.mkdir()
        source.write_text('def solve(value):\n    return value + 1\nprint(solve(1))\n')
        return journal, source

    async def test_exact_command_output_code_and_task_attribution_reach_writer(self):
        journal, source = self.evidence()
        digest = build_digest(challenges=[{'id': 'one'}], flags=[], run_summary={'dag_nodes': [{'task_ids': ['task-one']}]},
                              blackboard=self.bb, journal=journal, workspace=self.root)
        evidence = digest['technical_evidence']
        self.assertEqual(evidence['executions'][0]['parameters']['command'], 'python exp.py')
        self.assertEqual(evidence['source_files'][0]['content'], source.read_text())
        self.assertEqual(evidence['source_files'][0]['start_line'], 1)
        self.assertEqual(evidence['source_files'][0]['symbols'][0], {'name': 'solve', 'line': 1, 'end_line': 2})
        appendix = technical_appendix(digest)
        self.assertIn('```bash\npython exp.py', appendix)
        self.assertIn('actual error', appendix)
        self.assertIn('退出码：1', appendix)
        self.assertIn('def solve', appendix)
        text = '# 求解过程\n\n脚本输出如下，但退出码为 1，尚未证明求解成立。\n\n```bash\npython exp.py\n```\n\n```text\nobserved output\n```'
        gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content=text, finish_reason='stop')))
        body = await writeup_body(gateway, digest=digest)
        self.assertEqual(body, text, '成品来自模型，不由后端拼接原始证据')
        messages = gateway.chat.await_args.args[0]
        self.assertIn('函数', messages[0]['content'])
        self.assertEqual(json.loads(messages[1]['content'])['technical_evidence'], evidence)
        self.assertEqual(collect_evidence(journal, None, {'other-task'})['executions'], [])

    async def test_source_symlinks_do_not_read_external_files(self):
        (self.root / 'artifacts').mkdir()
        external = self.root / 'private.py'
        external.write_text('private_external_marker')
        (self.root / 'artifacts' / 'exp.py').symlink_to(external)
        self.assertEqual(collect_evidence(workspace=self.root)['source_files'], [])

    async def test_exp_is_embedded_in_markdown_without_script_files_or_execution(self):
        report = self.root / 'reports' / 'writeup.md'
        sentinel = self.root / 'must-not-execute'
        code = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('unsafe to execute')"
        body = '# 复现\n\n### exp.py\n\n```python\n' + code + '\n```\n\n报告整理的脚本未执行。'
        save_writeup(report, body)
        self.assertEqual(report.read_text(), body)
        self.assertFalse(sentinel.exists())
        self.assertEqual(list(self.root.rglob('*.py')), [])
        self.assertEqual(list(report.parent.iterdir()), [report])

    async def test_background_fallback_keeps_command_and_output_code_blocks(self):
        journal, _ = self.evidence()
        digest = {'flags': [], 'technical_evidence': collect_evidence(journal, self.root)}
        task = report_jobs.enqueue(self.root, self.root / 'reports' / 'fallback.md', digest,
                                   None, NullAuditLogger(), enabled=False)
        await task
        text = (self.root / 'reports' / 'fallback.md').read_text()
        self.assertIn('```bash', text)
        self.assertIn('actual error', text)
        self.assertEqual(report_jobs.status(self.root)['status'], 'materials')
        self.assertIsNone(report_jobs.enqueue(self.root, self.root / 'reports' / 'fallback.md', digest,
                                             None, NullAuditLogger(), enabled=False))

    async def test_markdown_wrapper_removed_without_removing_real_python_fence(self):
        text = '# 示例\n\n执行以下代码得到 1。\n\n```python\nprint(1)\n```'
        gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(
            content='```markdown\n' + text + '\n```', finish_reason='stop')))
        body = await writeup_body(gateway, digest={})
        self.assertEqual(body, text)

    async def test_incomplete_or_code_only_writer_output_is_not_a_finished_report(self):
        for content, finish in (('', 'length'), ('# 半截\n\n只写了一半', 'length'),
                                ('# exp.py\n\n```python\nprint(1)\n```', 'stop')):
            with self.subTest(content=content, finish=finish):
                gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(
                    content=content, finish_reason=finish)))
                audit = Mock()
                self.assertIsNone(await writeup_body(gateway, digest={}, audit=audit))
                self.assertEqual(gateway.chat.await_count, 2)
                for call in gateway.chat.await_args_list:
                    self.assertIsNone(call.kwargs['max_tokens'], '输出容量遵从模型档案，不固定截断思考和正文')
                self.assertFalse(any(call.args[2].get('result') == 'ok' for call in audit.log.call_args_list))

    async def test_retry_has_feedback_and_clean_prose_is_not_extended_with_a_code_dump(self):
        requests = []
        responses = iter([SimpleNamespace(content='', finish_reason='length'),
                          SimpleNamespace(content='# 解题过程\n\n根据响应中的键位映射还原答案。', finish_reason='stop')])

        async def respond(messages, **kwargs):
            requests.append(json.loads(json.dumps(messages)))
            return next(responses)

        gateway = SimpleNamespace(chat=AsyncMock(side_effect=respond))
        digest = {'technical_evidence': {'executions': [{'stdout': 'raw-output-marker'}]}}
        body = await writeup_body(gateway, digest=digest)
        self.assertNotIn('raw-output-marker', body)
        self.assertEqual(len(requests[0]), 2)
        self.assertEqual(len(requests[1]), 3, '附上纠错要求而非原样重试')
        self.assertEqual(requests[0], requests[1][:2], '缓存可复用的系统与材料前缀保持一致')

    async def test_enabled_writer_failure_keeps_evidence_but_does_not_publish_a_report(self):
        journal, _ = self.evidence()
        digest = {'flags': [], 'technical_evidence': collect_evidence(journal, self.root)}
        audit = Mock()
        gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content='', finish_reason='length')))
        report = self.root / 'reports' / 'failed.md'
        await report_jobs.enqueue(self.root, report, digest, gateway, audit)
        state = report_jobs.status(self.root)
        self.assertEqual(state['status'], 'failed')
        self.assertTrue(state['error'])
        self.assertFalse(report.exists(), '失败不能用原始证据充当模型报告')
        self.assertEqual(json.loads((self.root / 'report_job.json').read_text())['digest'], digest)
        self.assertFalse(any(call.args[0] == 'report.ready' for call in audit.log.call_args_list))

    async def test_runtime_report_entry_uses_the_same_failure_aware_queue(self):
        import asyncio

        from spiritscout.runtime import _render_writeup

        gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content='', finish_reason='length')),
                                  selected_model=lambda: '')
        infra = SimpleNamespace(blackboard=self.bb, workspace=self.root, tools=SimpleNamespace(journal=None),
                                gateway=gateway, agent_models={}, audit=NullAuditLogger())
        summary = {}
        report = await _render_writeup(infra, [{'id': 'one'}], SimpleNamespace(all=lambda: []), summary,
                                      self.root / 'reports' / 'runtime.md', contest='ctf', enable_llm=True,
                                      fallback_context={})
        self.assertEqual(summary['report_job']['status'], 'queued')
        await asyncio.gather(*list(report_jobs._TASKS.values()))
        self.assertEqual(report_jobs.status(self.root)['status'], 'failed')
        self.assertFalse(report.exists())

    async def test_pending_or_failed_writeup_does_not_expose_an_old_report_as_the_current_one(self):
        from spiritscout.web.session_models import Session

        reports = self.root / 'reports'
        reports.mkdir()
        old = reports / 'previous.md'
        old.write_text('# 上一轮的报告')
        session = Session(session_id='fixture', mode='ctf', target='fixture', workspace=self.root)
        self.assertEqual(session.report_path(), old, '没有后台任务的历史会话保持兼容')
        snapshot = self.root / 'report_job.json'
        for state in ('queued', 'running', 'failed'):
            with self.subTest(state=state):
                snapshot.write_text(json.dumps({'status': state, 'report_path': str(reports / 'current.md')}))
                self.assertIsNone(session.report_path())
                self.assertTrue(old.exists(), '保留旧文件供历史追溯')
        snapshot.write_text(json.dumps({'status': 'done', 'report_path': str(old)}))
        self.assertEqual(session.report_path(), old)

    async def test_answer_evidence_and_challenge_sources_precede_later_noise_and_dependency_trees(self):
        from spiritscout.core.models import FlagCandidate

        journal, _ = self.evidence()
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        (artifacts / 'analysis.md').write_text('分析草稿：依据测量推导出解题链。')
        sandbox = self.root / 'sandbox' / 'one'
        vendor = sandbox / 'A-dependency' / 'Doc'
        vendor.mkdir(parents=True)
        for index in range(30):
            (vendor / f'demo{index}.py').write_text('irrelevant dependency source\n' * 100)
        challenge = sandbox / 'challenge'
        challenge.mkdir()
        (challenge / 'chal.py').write_text('def check(value):\n    return value\n')
        for index in range(10):
            invocation = f'inv-noise-{index}'
            journal.begin(invocation, 'sandbox_exec', 'digest', 'safe', params={'command': 'echo noise'},
                          scope=ExecutionScope(task_id='task-one', route_id='route-one'))
            journal.transition(invocation, 'succeeded', result=ToolResult(tool='sandbox_exec', ok=True,
                               stdout='unrelated diagnostics' * 200))
        flag = FlagCandidate(flag_id='f', challenge_id='one', value='fixture', evidence_refs=['inv-code'],
                             answer_review={'derivation': '从源码得到算法', 'validation': '失败结果需保留'})
        digest = build_digest(challenges=[{'id': 'one', 'description': '分析 `chal.py`'}], flags=[flag],
            run_summary={'dag_nodes': [{'task_ids': ['task-one']}]}, blackboard=self.bb,
            journal=journal, workspace=self.root)
        evidence = digest['technical_evidence']
        self.assertIn('inv-code', [row['invocation_id'] for row in evidence['executions']])
        self.assertEqual(evidence['source_files'][0]['path'], 'sandbox/one/challenge/chal.py')
        self.assertEqual(evidence['source_files'][0]['content'], (challenge / 'chal.py').read_text())
        self.assertEqual(evidence['analysis_notes'][0]['content'], (artifacts / 'analysis.md').read_text())
        self.assertEqual(digest['flags'][0]['answer_review'], flag.answer_review)

    async def test_no_model_mode_saves_materials_not_a_template_solution(self):
        from spiritscout.core.models import BlackboardLayer
        from spiritscout.runtime import _render_writeup

        journal, _ = self.evidence()
        self.bb.write(BlackboardLayer.TASK_STATE, 'scanner-control', {'kind': 'scanner_control', 'status': 'idle'})
        infra = SimpleNamespace(blackboard=self.bb, workspace=self.root, tools=SimpleNamespace(journal=journal),
                                audit=NullAuditLogger())
        path = await _render_writeup(infra, [{'id': 'one', 'title': '本地题目'}], SimpleNamespace(all=lambda: []),
            {}, self.root / 'reports' / 'fallback.md', contest='ctf', enable_llm=False,
            fallback_context={'flag_stats': {}})
        self.assertIn('```bash', path.read_text())
        self.assertIn('函数／类定位', path.read_text())
        self.assertIn('待整理', path.read_text())
        self.assertEqual(report_jobs.status(self.root)['status'], 'materials')

    async def test_material_limits_never_turn_complete_code_into_unmarked_partial_code(self):
        code = 'print("full code")\n' * 100
        digest = {'routes_notes': ['note' * 10000] * 10,
                  'technical_evidence': {'source_files': [{'content': code, 'excerpt': False}], 'executions': []}}
        projected = json.loads(_writer_material(digest))
        self.assertEqual(projected['technical_evidence']['source_files'][0]['content'], code)
        self.assertNotIn('_context_projection', digest)


if __name__ == '__main__':
    unittest.main()
