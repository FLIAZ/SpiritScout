"""成本完整性、实际成果、调用归属及模型档案只读诊断。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.token_report import build_report
from spiritscout import __version__
from spiritscout.config.settings import ModelEntry, SystemConfig, WorkspaceConfig
from spiritscout.gateway.attempts import current_attempt_trace
from spiritscout.gateway.costs import configured_usd, cost_per_result, cost_summary
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.model_diagnostics import model_diagnostics
from spiritscout.gateway.outcome_costs import session_outcome_costs
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.gateway.usage import bind_usage
from spiritscout.web.dashboard import usage_metrics
from spiritscout.web.model_routes import mount_model_routes


class OutcomeCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def save(self, name, data):
        (self.root / name).write_text(json.dumps(data))

    def rows(self):
        return [{'ts': i + 1, 'call_id': str(i), 'task_id': task, 'input_tokens': 100, 'output_tokens': 20,
                 'tokens': 120, 'cached_input_tokens': 80, 'usage_status': 'reported',
                 'call_status': 'failed' if i == 1 else 'completed', 'runtime_version': __version__,
                 'estimated_cost_usd': 1, 'usd_pricing_status': 'configured',
                 'estimated_cost_cny_min': 5, 'estimated_cost_cny_max': 6}
                for i, task in enumerate(('solve', 'failed', '', 'verify-v1'))]

    def snapshot(self, rows=None):
        rows = self.rows() if rows is None else rows
        return {'calls': len(rows), 'used_tokens': sum(r['tokens'] for r in rows), 'call_series': rows}

    def evidence(self):
        self.save('flags.json', [
            {'flag_id': 'a', 'challenge_id': 'local', 'accepted': None, 'submitted': False},
            {'flag_id': 'b', 'challenge_id': 'remote', 'accepted': True, 'submitted': True},
            {'flag_id': 'c', 'challenge_id': 'bad', 'accepted': False, 'submitted': True}])
        self.save('flags.completion.json', {
            'local': {'answer_ids': ['a'], 'completed': True},
            'stale': {'answer_ids': ['missing'], 'completed': True},
            'patch': {'answer_ids': ['fix'], 'completed': True}})
        self.save('flags.fixes.json', {'fix': {'challenge_id': 'patch'}})
        with closing(sqlite3.connect(self.root / 'bb.db')) as conn:
            conn.execute('CREATE TABLE bb_records(layer TEXT,record_id TEXT,data TEXT)')
            for tid, status, cid in (('solve', 'done', 'local'), ('failed', 'failed', 'local'),
                                     ('no-call', 'done', 'unresolved')):
                conn.execute('INSERT INTO bb_records VALUES(?,?,?)', ('task_state', tid, json.dumps({
                    'task': {'task_id': tid, 'status': status, 'payload': {'challenge': {'id': cid}}}})))
            for key, status, verdict in (('v1', 'accepted', 'confirmed'), ('v2', 'accepted', 'inconclusive')):
                conn.execute('INSERT INTO bb_records VALUES(?,?,?)', ('vuln', key, json.dumps({
                    'status': status, 'poc_verified': True, 'exploitable': True, 'verification': {'verdict': verdict}})))
            conn.commit()

    def test_results_are_not_task_done_and_shared_failed_costs_are_included(self):
        self.evidence()
        report = session_outcome_costs(self.root, self.snapshot())
        self.assertEqual(report['outcomes'], {'platform_accepted_flags': 1, 'local_completed_challenges': 1,
            'delivered_patches': 1, 'independently_verified_vulnerabilities': 1})
        self.assertEqual(report['cost_per_result']['local_completed_challenges']['usd'], {'min': 4, 'max': 4})
        self.assertEqual(report['cost_per_result']['platform_accepted_flags']['cny'], {'min': 20, 'max': 24})
        self.assertEqual(report['shared_or_unattributed']['calls'], 1)
        tasks = {t['task_id']: t for t in report['tasks']}
        self.assertEqual(tasks['failed']['failed_calls'], 1)
        self.assertTrue(tasks['verify-v1']['verified_vulnerability'])
        self.assertEqual(tasks['no-call']['calls'], 0)
        self.assertEqual(report['execution_status_counts']['done'], 2)
        challenge = next(c for c in report['challenges'] if c['challenge_id'] == 'local')
        self.assertEqual(challenge['direct_execution']['calls'], 2)
        self.assertAlmostEqual(report['summary']['cache_hit_rate'], .8)

    def test_unknown_usage_prices_and_baseline_never_become_full_cost_per_result(self):
        self.evidence()
        for missing in ('price', 'usage', 'retry', 'baseline', 'version', 'read_error'):
            with self.subTest(missing=missing):
                snapshot = self.snapshot()
                row = snapshot['call_series'][0]
                if missing == 'price':
                    row['estimated_cost_usd'] = None
                    row['usd_pricing_status'] = 'unknown_cache_price'
                    row['cost_usd'] = 100  # 旧兼容金额不能盖过明确的未知标记。
                elif missing == 'usage':
                    row['usage_status'] = 'partial'
                elif missing == 'retry':
                    row['unreported_attempts'] = 1
                elif missing == 'baseline':
                    snapshot['calls'] += 10
                elif missing == 'version':
                    row['runtime_version'] = 'old'
                else:
                    (self.root / 'flags.completion.json').write_text('{broken')
                result = session_outcome_costs(self.root, snapshot, version=__version__)
                self.assertIsNone(result['cost_per_result']['local_completed_challenges']['usd'])
                if missing == 'price':
                    self.assertEqual(result['summary']['cost']['usd']['known_min'], 3)
                if missing == 'read_error':
                    self.assertTrue(result['read_errors'])

    def test_duplicate_calls_and_disputed_answers_are_not_counted_twice(self):
        self.evidence()
        self.save('flags.completion.json', {'local': {'completed': True, 'answer_ids': ['a'],
                                                     'disputed_answer_ids': ['a']}})
        rows = self.rows()
        snapshot = self.snapshot(rows)
        snapshot['call_series'] = rows + [rows[0]]
        result = session_outcome_costs(self.root, snapshot)
        self.assertEqual(result['summary']['calls'], 4)
        self.assertEqual(result['outcomes']['local_completed_challenges'], 0)
        self.assertIsNone(result['cost_per_result']['local_completed_challenges']['usd'])

    def test_prices_count_cache_reads_writes_and_keep_unknown_separate(self):
        entry = ModelEntry(model='fixture', cost_input_per_mtok=2, cost_output_per_mtok=8,
                           cost_cached_input_per_mtok=.2, cost_cache_write_per_mtok=2.5)
        usage = {'prompt_tokens': 1000, 'completion_tokens': 100, 'cached_input_tokens': 600, 'cache_write_tokens': 100}
        metrics = configured_usd(entry, usage)
        self.assertAlmostEqual(metrics['estimated_cost_usd'], (300*2 + 600*.2 + 100*2.5 + 100*8)/1e6)
        self.assertEqual(configured_usd(ModelEntry(model='no-prices'), usage)['usd_pricing_status'], 'unknown_prices')
        entry.cost_cached_input_per_mtok = None
        unknown = configured_usd(entry, usage)
        self.assertIsNone(unknown['estimated_cost_usd'])
        self.assertEqual(usage_metrics([{'cost_usd': 99, **unknown}])['cost']['unpriced_calls'], 1)
        self.assertEqual(configured_usd(entry, {**usage, 'cached_input_tokens': None})['usd_pricing_status'], 'unknown_cache_usage')
        legacy = cost_summary([{'cost_usd': 1, 'usage_status': 'reported'}])
        self.assertEqual(legacy['usd']['known_min'], 1)
        self.assertIsNone(cost_per_result(legacy, 1)['usd'])

    def test_report_exports_sessions_and_corrupt_usage_without_writing_sources(self):
        folder = self.root / 'sessions' / 'sess-fixture'
        folder.mkdir(parents=True)
        path = folder / 'usage.json'
        path.write_text(json.dumps(self.snapshot()))
        before = path.read_bytes()
        result = build_report(self.root)
        self.assertEqual(result['sessions'][0]['session_id'], 'sess-fixture')
        self.assertEqual(result['summary']['cost']['usd']['known_min'], 4)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((folder / 'bb.db').exists())
        self.assertFalse((folder / 'usage.db').exists())
        path.write_text('invalid')
        self.assertTrue(build_report(self.root)['unreadable_files'])

    def test_session_endpoint_returns_results_and_rejects_missing_session(self):
        from spiritscout.web.app import create_app
        from spiritscout.web.sessions import Session

        app = create_app(SystemConfig(workspace=WorkspaceConfig(root=str(self.root))), [])
        session = Session(session_id='sess-fixture', mode='ctf', target='fixture',
                          workspace=self.root, status='completed')
        app.state.manager._sessions[session.session_id] = session
        self.evidence()
        self.save('usage.json', self.snapshot())
        with TestClient(app, base_url='http://127.0.0.1') as client:
            response = client.get('/api/sessions/sess-fixture/outcome-costs')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['outcomes']['local_completed_challenges'], 1)
            self.assertEqual(client.get('/api/sessions/missing/outcome-costs').status_code, 404)


class ModelDiagnosticsTests(unittest.TestCase):
    def test_official_reference_does_not_overwrite_config_or_apply_to_proxy(self):
        official = ModelEntry(model='glm-5.3', provider_ref='official', api_key='secret-fixture',
            base_url='https://open.bigmodel.cn/api/anthropic', provider='anthropic_compatible',
            reasoning_effort_levels=['low', 'medium', 'high', 'xhigh', 'max'])
        proxy = official.model_copy(update={'provider_ref': 'proxy', 'base_url': 'https://proxy.invalid'})
        before = official.model_dump()
        result = model_diagnostics([official, proxy], [
            {'model_key': official.key, 'sent_reasoning_effort': 'low'},
            {'model': official.key}, {'model_key': proxy.key, 'sent_reasoning_effort': ''}])
        self.assertEqual(official.model_dump(), before)
        self.assertNotIn('secret-fixture', json.dumps(result))
        a, b = result['models']
        self.assertIn('reasoning_levels_differ_from_reference', a['issues'])
        self.assertEqual(a['sent_effort_counts'], {'low': 1})
        self.assertEqual(a['effort_unknown_calls'], 1)
        self.assertEqual(b['sent_effort_counts'], {'endpoint_default': 1})
        self.assertFalse(b['reference'])
        self.assertFalse(result['live_verified'])

    def test_diagnostics_route_is_read_only_and_does_not_call_provider(self):
        app = FastAPI()
        mount_model_routes(app, SystemConfig())
        with patch('spiritscout.config.load_model_config', return_value=[ModelEntry(model='fixture')]), \
                patch.object(ModelGateway, 'chat', side_effect=AssertionError('no paid calls')), TestClient(app) as client:
            response = client.get('/api/models/diagnostics')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['models'][0]['model'], 'fixture')


class UsagePricePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_time_prices_effort_and_identity_survive_ledger(self):
        entry = ModelEntry(model='fixture', provider='mock', provider_ref='supplier',
            context_window=4096, reasoning_effort_levels=['low'], cost_input_per_mtok=2,
            cost_output_per_mtok=8, cost_cached_input_per_mtok=.2)
        gateway = ModelGateway(ModelRegistry([entry]))

        async def complete(*args, **kwargs):
            trace = current_attempt_trace.get()
            trace.start(streaming=False)
            trace.usage({'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_cache_hit_tokens': 80})
            return {'content': 'ok', 'prompt_tokens': 100, 'completion_tokens': 20, 'cached_input_tokens': 80,
                    'usage_status': 'reported', 'latency_ms': 1, 'finish_reason': 'stop'}

        with tempfile.TemporaryDirectory() as folder:
            ledger = bind_usage(gateway, Path(folder) / 'usage.json')
            with patch.object(gateway._mock, 'complete', side_effect=complete):
                await gateway.chat([{'role': 'user', 'content': 'fixture'}], model=entry.key, reasoning_effort='high')
            row = ledger.read()['call_series'][0]
        self.assertEqual(row['model_key'], entry.key)
        self.assertEqual(row['provider_ref'], 'supplier')
        self.assertEqual(row['requested_reasoning_effort'], 'high')
        self.assertEqual(row['sent_reasoning_effort'], '')
        self.assertEqual(row['configured_context_window'], 4096)
        self.assertEqual(row['usd_rate_snapshot']['cached_input'], .2)
        self.assertAlmostEqual(row['estimated_cost_usd'], (20*2 + 80*.2 + 20*8)/1e6)
