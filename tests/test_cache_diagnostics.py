from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from spiritscout.gateway.cache import PrefixTracker, cache_summary
from spiritscout.gateway.usage import UsageLedger


class CacheDiagnosticsTests(unittest.TestCase):
    def test_attribute_first_change_and_do_not_store_content(self):
        tracker = PrefixTracker()
        messages = [{'role': 'system', 'content': 'private-system'}, {'role': 'user', 'content': 'private-request'}]
        tool = [{'type': 'function', 'function': {'name': 'read', 'parameters': {}}}]
        def run(model='m1', tools=tool, options=None):
            return tracker.observe((model,), messages, tools, conversation_key=('route1',), request_options=options)
        self.assertEqual(run()['cache_diagnostics']['reason'], 'first_observation')
        self.assertEqual(run()['cache_diagnostics']['reason'], 'unchanged')
        messages.append({'role': 'assistant', 'content': 'new observation'})
        self.assertEqual(run()['cache_diagnostics']['reason'], 'append_only')
        messages[0]['content'] += ' changed'
        diagnostic = run()['cache_diagnostics']
        self.assertEqual((diagnostic['reason'], diagnostic['first_changed_message_index']), ('system_changed', 0))
        messages[1]['content'] += ' changed'
        self.assertEqual(run()['cache_diagnostics']['reason'], 'history_changed')
        messages.pop()
        self.assertEqual(run()['cache_diagnostics']['reason'], 'history_truncated')
        self.assertEqual(run(tools=[])['cache_diagnostics']['reason'], 'tool_catalog_changed')
        self.assertEqual(run(tools=[], options={'reasoning': 'high'})['cache_diagnostics']['reason'], 'request_options_changed')
        metric = run(model='m2', tools=[], options={'reasoning': 'high'})
        self.assertEqual(metric['cache_diagnostics']['reason'], 'model_or_endpoint_changed')
        self.assertEqual(metric['reused_prefix_bytes'], 0)
        self.assertNotIn('private', str(tracker.__dict__))
        self.assertNotIn('private', json.dumps(metric))

    def test_persistence_coverage_and_provider_identity(self):
        metric = PrefixTracker().observe(('m1',), [{'role': 'user', 'content': 'test'}], [])
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger(Path(tmp) / 'usage.db')
            ledger.append({'ts': 1, 'call_id': 'one', 'model': 'same', 'model_key': 'a::same',
                           'input_tokens': 100, 'cached_input_tokens': 60, **metric})
            rows = ledger.read()['call_series']
        self.assertEqual(rows[0]['cache_diagnostics'], metric['cache_diagnostics'])
        other = copy.deepcopy(rows[0])
        other.update(model_key='b::same', cache_diagnostics={}, cached_input_tokens=None)
        report = cache_summary([*rows, other])
        self.assertEqual(len(report['groups']), 2)
        self.assertEqual(report['hit_rate'], .6)
        self.assertEqual(report['unmeasured_calls'], 1)
        self.assertEqual(report['prefix_changes']['reasons'], {'first_observation': 1, 'unavailable': 1})

    def test_eviction_and_route_isolation_are_cold_observations(self):
        tracker = PrefixTracker(capacity=1)
        for key in ('one', 'two', 'one'):
            metrics = tracker.observe((key,), [{'role': 'user', 'content': key}], [])
            self.assertEqual(metrics['cache_diagnostics']['reason'], 'first_observation')
            self.assertEqual(metrics['reused_prefix_bytes'], 0)

    def test_system_role_edit_is_not_misclassified_as_model_switch(self):
        tracker = PrefixTracker()
        for role in ('first', 'second'):
            metrics = tracker.observe(('model', role), [{'role': 'system', 'content': f'ROLE: {role}'}], [],
                conversation_key=('route',), model_identity=('model', 'endpoint'))
        self.assertEqual(metrics['cache_diagnostics']['reason'], 'system_changed')
