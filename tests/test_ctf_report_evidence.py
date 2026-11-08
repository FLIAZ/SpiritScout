"""CTF 技术报告的真实证据、回退、脚本落盘与会话下载。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content='只有文字的模型报告', finish_reason='stop')))
        body = await writeup_body(gateway, digest=digest)
        self.assertIn('```bash', body, '纯文字模型答复也补上实际技术证据')
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
        self.assertEqual(report_jobs.status(self.root)['status'], 'done')

    async def test_markdown_wrapper_removed_without_removing_real_python_fence(self):
        gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(
            content='```python\nprint(1)\n```', finish_reason='stop')))
        body = await writeup_body(gateway, digest={})
        self.assertEqual(body, '```python\nprint(1)\n```')

    async def test_template_fallback_contains_code_and_ignores_scanner_control_records(self):
        from spiritscout.core.models import BlackboardLayer
        from spiritscout.runtime import _render_writeup

        journal, _ = self.evidence()
        self.bb.write(BlackboardLayer.TASK_STATE, 'scanner-control', {'kind': 'scanner_control', 'status': 'idle'})
        infra = SimpleNamespace(blackboard=self.bb, workspace=self.root, tools=SimpleNamespace(journal=journal))
        path = await _render_writeup(infra, [{'id': 'one', 'title': '本地题目'}], SimpleNamespace(all=lambda: []),
            {}, self.root / 'reports' / 'fallback.md', contest='ctf', enable_llm=False,
            fallback_context={'flag_stats': {}})
        self.assertIn('```bash', path.read_text())
        self.assertIn('函数／类定位', path.read_text())

    async def test_material_limits_never_turn_complete_code_into_unmarked_partial_code(self):
        code = 'print("full code")\n' * 100
        digest = {'routes_notes': ['note' * 10000] * 10,
                  'technical_evidence': {'source_files': [{'content': code, 'excerpt': False}], 'executions': []}}
        projected = json.loads(_writer_material(digest))
        self.assertEqual(projected['technical_evidence']['source_files'][0]['content'], code)
        self.assertNotIn('_context_projection', digest)


if __name__ == '__main__':
    unittest.main()
