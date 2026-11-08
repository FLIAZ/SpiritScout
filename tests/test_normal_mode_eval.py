from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts.normal_eval_profile import PreviousNormalProfile
from scripts.normal_mode_eval import Trial, comparison, fixture, summarize_runs
from spiritscout.config.settings import ModelEntry
from spiritscout.gateway.gateway import ChatResponse


def response(name, args):
    return ChatResponse(content='', model='mock', prompt_tokens=1, completion_tokens=1,
        cost_usd=0, latency_ms=0, tool_calls=[{'id': f'call-{name}', 'name': name, 'arguments': args}])


class NormalModeEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def trial(self, case='short', wrong=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data = fixture(case)
        trial = Trial(Path(temporary.name) / 'trial', ModelEntry(model='mock', provider='mock'), data, 'projected_memory')
        record = response('record_answer', {'answer': 'wrong' if wrong else data['expected'],
            'evidence_refs': ['observation-1'], 'review': {'requirements': '最大 valid 序号',
                'derivation': '比较有效性和序号', 'validation': '回查原文',
                'validation_evidence_refs': ['observation-1']}})
        replies = [response('read_fixture', {'name': 'ledger.txt'}), record]
        if case == 'publication_failure':
            replies.append(record)
        with patch.object(trial.gateway, 'chat', AsyncMock(side_effect=replies)):
            return await trial.run()

    async def test_real_publication_path_and_failure_recovery(self):
        result = await self.trial('publication_failure')
        self.assertTrue(result['correct'])
        self.assertTrue(result['failure_left_open'])
        self.assertEqual(result['publication_failures'], 1)

    async def test_success_does_not_mean_semantic_correctness_or_known_cost(self):
        result = await self.trial(wrong=True)
        self.assertTrue(result['completed'])
        self.assertTrue(result['wrong_completion'])
        report = summarize_runs([result])
        self.assertEqual(report['correct'], 0)
        self.assertIsNone(report['cost_per_correct_result']['cny'])

    def test_fixture_is_reproducible_and_route_answers_differ(self):
        self.assertEqual(fixture('long_history'), fixture('long_history'))
        self.assertNotEqual(fixture('route_isolation', 1)['expected'], fixture('route_isolation', 102)['expected'])

    async def test_memory_preparation_uses_supported_auto_tool_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial = Trial(Path(tmp) / 'trial', ModelEntry(model='mock', provider='mock'), fixture('long_history'), 'projected_memory')
            reply = response('evidence_memory', {'action': 'read'})
            with patch.object(trial.gateway, 'chat', AsyncMock(return_value=reply)) as chat:
                await trial.turn(prepare=True)
                self.assertEqual(chat.call_args.kwargs['tool_choice'], 'auto')
                self.assertEqual({s['function']['name'] for s in chat.call_args.kwargs['tools']},
                                 {'history_read', 'evidence_memory'})
            await trial.bb.aclose()

    async def test_normal_history_search_returns_exact_middle_quote_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial = Trial(Path(tmp) / 'trial', ModelEntry(model='mock', provider='mock'), fixture('long_history'), 'projected_memory')
            trial.seed_read('ledger.txt', 'seed')
            args = {'message_index': 3, 'view': 'content', 'find': '"sequence":17', 'length': 500}
            out = await trial.agent._run_tool_call_inner(trial.task, trial.route, trial.state,
                {'name': 'history_read', 'arguments': args})
            value = json.loads(out.text)
            self.assertTrue(value['found'])
            self.assertIn('"sequence":17', value['text'])
            raw = trial.state['messages'][3]['content']
            self.assertEqual(value['text'], raw[value['offset']:value['next_offset']])
            self.assertEqual(len(value['source_sha256']), 64)
            self.assertEqual(value['total_matches'], 1)
            self.assertEqual(value['evidence_refs'], ['observation-1'])
            args.update(find='"status":"valid"', length=100)
            out = await trial.agent._run_tool_call_inner(trial.task, trial.route, trial.state,
                {'name': 'history_read', 'arguments': args})
            value = json.loads(out.text)
            self.assertEqual(value['total_matches'], 2)
            self.assertEqual(value['matches_from_offset'], 2)
            self.assertTrue(value['search_complete'])
            args['offset'] = value['match_offsets'][0] + 1
            out = await trial.agent._run_tool_call_inner(trial.task, trial.route, trial.state,
                {'name': 'history_read', 'arguments': args})
            value = json.loads(out.text)
            self.assertEqual(value['total_matches'], 2)
            self.assertEqual(value['matches_from_offset'], 1)
            args.update(find='背景信息', offset=0)
            out = await trial.agent._run_tool_call_inner(trial.task, trial.route, trial.state,
                {'name': 'history_read', 'arguments': args})
            value = json.loads(out.text)
            self.assertEqual(value['total_matches'], 360)
            self.assertEqual(len(value['match_offsets']), 16)
            self.assertTrue(value['offsets_truncated'])
            self.assertTrue(value['search_complete'])
            args['find'] = 'nonexistent'
            out = await trial.agent._run_tool_call_inner(trial.task, trial.route, trial.state,
                {'name': 'history_read', 'arguments': args})
            self.assertFalse(json.loads(out.text)['found'])
            args['find'] = '['  # 精确字符串，不解释正则。
            out = await trial.agent._run_tool_call_inner(trial.task, trial.route, trial.state,
                {'name': 'history_read', 'arguments': args})
            self.assertTrue(out.ok)
            await trial.bb.aclose()

    async def test_external_cancellation_preserves_partial_result_and_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'trial'
            trial = Trial(root, ModelEntry(model='mock', provider='mock'), fixture('short'), 'projected_memory')
            with patch.object(trial.gateway, 'chat', AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await trial.run()
            result = json.loads((root / 'result.json').read_text())
            self.assertFalse(result['completed'])
            self.assertEqual(result['incomplete_reason'], 'cancelled')

    async def test_comparison_rejects_slowdown_and_wrong_completion(self):
        template = await self.trial()
        runs = [{**template, 'arm': arm, 'elapsed_seconds': elapsed} for arm, elapsed in
                [('full_history', 2)] * 3 + [('projected_memory', 3)] * 3]
        result = comparison(runs)
        self.assertFalse(result['passed'])
        self.assertFalse(result['cases'][0]['median_not_slower'])
        for row in runs[3:]:
            row.update(elapsed_seconds=1)
        self.assertTrue(comparison(runs)['passed'])
        runs[-1]['correct'] = False
        runs[-1]['wrong_completion'] = True
        self.assertFalse(comparison(runs)['passed'])
        self.assertFalse(comparison(runs[:1])['passed'])

    async def test_previous_profile_uses_frozen_schema_and_restores_current_modules(self):
        from spiritscout.ctf.evidence_memory import SCHEMA

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for file in ('src/spiritscout/ctf/agents.py', 'src/spiritscout/ctf/evidence_memory.py',
                         'src/spiritscout/gateway/normal_context.py'):
                dest = root / file
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, dest)
            with (root / 'src/spiritscout/ctf/evidence_memory.py').open('a') as stream:
                stream.write("\nSCHEMA['function']['description'] = 'snapshot-specific'\n")
            profile = PreviousNormalProfile(root)
            with profile.activate():
                trial = Trial(root / 'trial', ModelEntry(model='mock', provider='mock'), fixture('short'),
                              'previous_normal', profile=profile)
                memory = next(s for s in trial.tools if s['function']['name'] == 'evidence_memory')
                self.assertEqual(memory['function']['description'], 'snapshot-specific')
            from spiritscout.ctf.evidence_memory import SCHEMA as restored
            self.assertIs(restored, SCHEMA)
            self.assertNotEqual(restored['function']['description'], 'snapshot-specific')
            await trial.bb.aclose()
