"""只读版本基线：旧台账不冒充当前版本，未知尝试不冒充零用量。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.token_report import build_report
from spiritscout import __version__
from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import NullAuditLogger
from spiritscout.gateway.attempts import AttemptTrace
from spiritscout.gateway.backends import MockBackend
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.gateway.usage import UsageLedger


class TokenReportTests(unittest.TestCase):
    def test_cancelled_attempt_usage_is_separate_and_unknown_is_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            session=root/'sessions/sess-cancelled'
            session.mkdir(parents=True)
            records=[{'attempt_id':'a','status':'cancelled','usage_status':'reported',
                      'runtime_version':__version__,'input_tokens':100,'output_tokens':25},
                     {'attempt_id':'b','status':'cancelled','usage_status':'unknown','runtime_version':__version__}]
            (session/'audit.jsonl').write_text('\n'.join(json.dumps({'type':'model.attempt_finished','detail':row})
                                                        for row in records))
            report=build_report(root)
            self.assertEqual(report['summary']['calls'],0)
            self.assertEqual(report['audit_attempts']['attempts'],2)
            self.assertEqual(report['audit_attempts']['reported_input_tokens'],100)
            self.assertEqual(report['audit_attempts']['unreported_attempts'],1)
            self.assertEqual(report['audit_attempts']['output_tokens_unknown_attempts'],1)

    def test_versions_and_unknown_metrics_are_separated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = UsageLedger(root / 'sessions' / 'sess-fixture' / 'usage.db')
            ledger.append({'ts':1,'call_id':'legacy','input_tokens':1000,'cached_input_tokens':900,'output_tokens':20})
            ledger.append({'ts':2,'call_id':'new','runtime_version':__version__,'model':'fixture','cache_scope':'meta_planner',
                           'input_tokens':100,'cached_input_tokens':80,'output_tokens':30,'sdk_attempts':2,'unreported_attempts':1})
            ledger.append({'ts':3,'call_id':'unknown','runtime_version':__version__,'input_tokens':50,'output_tokens':10})
            report = build_report(root)
            self.assertEqual(report['summary']['calls'], 2)
            self.assertEqual(report['summary']['hit_rate'], .8)
            self.assertEqual(report['summary']['unmeasured_calls'], 1)
            self.assertEqual(report['summary']['sdk_attempts'], 2)
            self.assertEqual(report['summary']['attempts_unknown_calls'], 1)
            self.assertEqual(report['summary']['unreported_attempts'], 1)
            all_versions = build_report(root, version=None)
            self.assertEqual(all_versions['summary']['calls'], 3)
            self.assertEqual({group['runtime_version'] for group in all_versions['groups']}, {__version__, 'legacy'})

    def test_reading_missing_workspace_does_not_create_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'missing'
            self.assertEqual(build_report(root)['summary']['calls'], 0)
            self.assertFalse(root.exists())

    def test_attempt_totals_keep_unreported_usage_unknown(self) -> None:
        trace = AttemptTrace(NullAuditLogger(), 'fixture', {'call_id':'fixture'})
        trace.start(streaming=False)
        trace.usage(SimpleNamespace(prompt_tokens=100, completion_tokens=10))
        trace.finish('replaced_by_retry')
        trace.start(streaming=False)
        self.assertEqual(trace.metrics(), {'sdk_attempts':2,'unreported_attempts':1,
                         'reported_attempt_input_tokens':100,'reported_attempt_output_tokens':10})
        empty = AttemptTrace(NullAuditLogger(), 'fixture', {'call_id':'empty'})
        self.assertIsNone(empty.metrics()['reported_attempt_input_tokens'])


class TokenMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_metrics_do_not_enter_model_messages(self) -> None:
        gateway = ModelGateway(ModelRegistry([ModelEntry(model='fixture', provider='mock')]),
                               mock_backend=MockBackend(script=['{"ok":true}']))
        messages = [{'role':'system','content':'稳定的系统前缀'}, {'role':'user','content':'不可变原文'}]
        await gateway.chat(messages, preserve_context=True)
        row = gateway.budget.call_log[-1]
        self.assertEqual(row['runtime_version'], __version__)
        self.assertEqual(row['sdk_attempts'], 0)
        sent = gateway._mock.calls[-1]
        self.assertEqual(sent['messages'], messages)
        self.assertNotIn('runtime_version', json.dumps(sent))
