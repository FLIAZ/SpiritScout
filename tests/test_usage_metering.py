"""流式累计快照、协议口径和失败 / 重试用量不能漏记或重复相加。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.gateway.attempts import AttemptTrace, current_attempt_trace
from spiritscout.gateway.backends import LiteLLMBackend
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.metering import normalize_usage
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.gateway.usage import bind_usage
from spiritscout.web.dashboard import usage_metrics


class UsageNormalizationTests(unittest.TestCase):
    def test_protocols_and_cache_are_not_double_counted(self):
        for usage in (
            {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_tokens_details': {'cached_tokens': 60, 'cache_creation_tokens': 10}},
            {'input_tokens': 100, 'output_tokens': 20, 'input_tokens_details': {'cached_tokens': 60}, 'cache_creation_input_tokens': None},
            {'input_tokens': 30, 'output_tokens': 20, 'cache_read_input_tokens': 60, 'cache_creation_input_tokens': 10},
            SimpleNamespace(prompt_tokens=100, completion_tokens=20, cache_read_input_tokens=60, cache_creation_input_tokens=10),
        ):
            normalized = normalize_usage(usage)
            self.assertEqual(normalized['input_tokens'], 100)
            self.assertEqual(normalized['output_tokens'], 20)
            self.assertEqual(normalized['cached_input_tokens'], 60)
        self.assertEqual(normalize_usage({'prompt_tokens': -1, 'output_tokens': True}), {})
        self.assertNotIn('cached_input_tokens', normalize_usage({'prompt_tokens': 1}))


class UsageAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_stream_frames_merge_without_summing_snapshots(self):
        async def stream():
            for usage in ({'prompt_tokens': 100, 'completion_tokens': 0, 'prompt_tokens_details': {'cached_tokens': 80, 'cache_creation_tokens': 10}},
                          {'prompt_tokens': 0, 'completion_tokens': 7},
                          {'completion_tokens': 12}, {'completion_tokens': 12}):
                yield SimpleNamespace(usage=usage, choices=[])
        trace = AttemptTrace(NullAuditLogger(), 'test', {'call_id': 'stream'})
        trace.start(streaming=True)
        token = current_attempt_trace.set(trace)
        try:
            result = await LiteLLMBackend()._consume_stream(stream(), None)
        finally:
            current_attempt_trace.reset(token)
        self.assertEqual((result['prompt_tokens'], result['completion_tokens']), (100, 12))
        self.assertEqual(result['cached_input_tokens'], 80)
        self.assertEqual(result['cache_write_tokens'], 10)
        self.assertEqual(trace.accounted_usage()['prompt_tokens'], 100)
        self.assertEqual(trace.accounted_usage()['completion_tokens'], 12)

    async def test_retry_and_failed_or_cancelled_usage_persist_once(self):
        for outcome in ('completed', 'failed', 'cancelled'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                gateway = ModelGateway(ModelRegistry([ModelEntry(model='fixture', provider='mock', cost_input_per_mtok=1, cost_output_per_mtok=2)]))
                ledger = bind_usage(gateway, Path(tmp) / 'usage.json')

                async def complete(*args, **kwargs):
                    trace = current_attempt_trace.get()
                    trace.start(streaming=True)
                    trace.usage({'prompt_tokens': 100, 'completion_tokens': 5, 'prompt_cache_hit_tokens': 80})
                    trace.finish('replaced_by_retry')
                    trace.start(streaming=True)
                    trace.usage({'prompt_tokens': 50, 'completion_tokens': 3, 'prompt_cache_hit_tokens': 20})
                    if outcome == 'cancelled':
                        raise asyncio.CancelledError()
                    if outcome == 'failed':
                        raise ValueError('stream disconnected')
                    trace.finish('completed')
                    return {'content': 'ok', 'prompt_tokens': 50, 'completion_tokens': 3, 'cached_input_tokens': 20}

                with patch.object(gateway._mock, 'complete', side_effect=complete):
                    if outcome == 'completed':
                        response = await gateway.chat([{'role': 'user', 'content': 'fixture'}])
                        self.assertEqual(response.prompt_tokens, 50, '返回响应保留最后一次的用量')
                    else:
                        with self.assertRaises(asyncio.CancelledError if outcome == 'cancelled' else SpiritScoutError):
                            await gateway.chat([{'role': 'user', 'content': 'fixture'}])
                snapshot = ledger.read()
                self.assertEqual(snapshot['used_tokens'], 158)
                self.assertEqual(len(snapshot['call_series']), 1)
                row = snapshot['call_series'][0]
                self.assertEqual(row['input_tokens'], 150)
                self.assertEqual(row['output_tokens'], 8)
                self.assertEqual(row['cached_input_tokens'], 100)
                self.assertEqual(row['call_status'], outcome)
                self.assertAlmostEqual(row['cost_usd'], .000166)
                self.assertEqual(usage_metrics([row])['tokens'], 158)

    async def test_missing_usage_remains_unknown_and_survives_storage(self):
        trace = AttemptTrace(NullAuditLogger(), 'test', {'call_id': 'unknown'})
        trace.start(streaming=False)
        self.assertEqual(trace.accounted_usage()['usage_status'], 'unknown')
        trace.start(streaming=False)
        trace.usage({'prompt_tokens': 100, 'completion_tokens': 5, 'prompt_cache_hit_tokens': 80})
        result = trace.accounted_usage()
        self.assertEqual(result['usage_status'], 'partial')
        self.assertEqual(result['prompt_tokens'], 100)
        self.assertEqual(trace.metrics()['unreported_attempts'], 1)
        metrics = usage_metrics([{'usage_status': 'partial', 'tokens': 105, **trace.metrics()}])
        self.assertEqual(metrics['usage_incomplete_calls'], 1)
        self.assertEqual(metrics['unreported_attempts'], 1)
