"""独立付费测试的计量、等待和跨进程保守恢复；不发付费请求。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx

from scripts.benchmark_support import LiveBudget, LiveMeter, TestBudgetExceeded


class LiveBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_sdk_retry_does_not_deadlock_waiting_for_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            budget=LiveBudget(Path(tmp)/'cost.json',limit_cny=.1)
            await budget.reserve('same:1',b'{}',8192)
            with self.assertRaises(TestBudgetExceeded):
                await asyncio.wait_for(budget.reserve('same:1',b'{}',8192),1)
            self.assertEqual(budget.rows[0]['status'],'unknown')

    async def test_offpeak_policy_uses_verified_period_and_refuses_peak_boundary(self) -> None:
        for hour,minute,allowed in [(19,20,True),(8,50,False),(14,0,False)]:
            with self.subTest(hour=hour),tempfile.TemporaryDirectory() as tmp,patch(
                    'scripts.benchmark_support.datetime') as clock:
                clock.now.return_value=datetime(2026,10,26,hour,minute,tzinfo=ZoneInfo('Asia/Shanghai'))
                budget=LiveBudget(Path(tmp)/'cost.json',off_peak_only=True)
                if allowed:
                    key=await budget.reserve('call',b'{}',8192)
                    budget.usage(key,{'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':80})
                    self.assertAlmostEqual(budget.committed_cny,(20+80*.02+20*4)/1e6)
                else:
                    with self.assertRaises(TestBudgetExceeded):
                        await budget.reserve('call',b'{}',8192)
                    self.assertEqual(budget.rows,[])

    def test_cli_requires_explicit_live_flag_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'must-not-exist'
            result=subprocess.run([sys.executable,'-m','scripts.live_cache_benchmark',
                '--output',str(out),'--budget-cny','1','--previous-cost-cny','.2421573'],
                capture_output=True,text=True,env={**os.environ,'LITELLM_LOCAL_MODEL_COST_MAP':'True'})
            self.assertEqual(result.returncode,2)
            self.assertIn('--run-live',result.stderr)
            self.assertFalse(out.exists())

    async def test_inflight_reservation_waits_for_usage_instead_of_cancelling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            budget = LiveBudget(Path(tmp)/'cost.json',limit_cny=.12)
            first = await budget.reserve('first', b'{}',8192)
            waiting = asyncio.create_task(budget.reserve('second',b'{}',8192))
            await asyncio.sleep(.01)
            self.assertFalse(waiting.done())
            budget.usage(first,{'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':80})
            second = await asyncio.wait_for(waiting,1)
            self.assertNotEqual(first,second)
            self.assertLess(budget.committed_cny,.12)

    async def test_reported_usage_is_idempotent_and_missing_usage_keeps_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            budget = LiveBudget(Path(tmp)/'cost.json',limit_cny=1,previous_cost_cny=.2421573)
            key = await budget.reserve('call:1',b'{}',8192)
            reserved = budget.committed_cny
            budget.usage(key,{'prompt_tokens':100})
            self.assertEqual(budget.committed_cny,reserved)
            usage={'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':80}
            budget.usage(key,usage)
            cost=budget.committed_cny
            budget.usage(key,usage)
            self.assertEqual(budget.committed_cny,cost)
            self.assertAlmostEqual(cost,.2421573+(20*2+80*.04+20*8)/1e6)

    async def test_stopped_process_restores_unknown_reservation_without_waiting_forever(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'cost.json'
            budget=LiveBudget(path,limit_cny=.1)
            await budget.reserve('old',b'{}',8192)
            restored=LiveBudget(path,limit_cny=.1)
            with self.assertRaises(TestBudgetExceeded):
                await asyncio.wait_for(restored.reserve('new',b'{}',8192),1)
            self.assertEqual(restored.rows[0]['status'],'unknown')
            self.assertNotIn('api_key',json.dumps(restored.rows))

    async def test_cancelled_waiter_never_creates_a_billable_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            budget=LiveBudget(Path(tmp)/'cost.json',limit_cny=.1)
            await budget.reserve('first',b'{}',8192)
            task=asyncio.create_task(budget.reserve('second',b'{}',8192))
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(len(budget.rows),1)

    async def test_streamed_sse_and_json_both_release_reservation(self) -> None:
        for sse in (False,True):
            with self.subTest(sse=sse), tempfile.TemporaryDirectory() as tmp:
                budget=LiveBudget(Path(tmp)/'cost.json')
                data=json.dumps({'usage':{'prompt_tokens':100,'completion_tokens':20,
                                         'prompt_cache_hit_tokens':80}}).encode()
                if sse:
                    data=b'data: '+data+b'\r\n\r\ndata: [DONE]\r\n\r\n'
                def handler(request):
                    return httpx.Response(200,content=data,headers={
                        'content-type':'text/event-stream' if sse else 'application/json'})
                with LiveMeter(budget):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        request=client.build_request('POST','https://api.deepseek.com/chat/completions',
                            json={'model':'deepseek-flash','max_tokens':8192})
                        response=await client.send(request,stream=True)
                        await response.aread()
                        await response.aclose()
                self.assertEqual(budget.rows[0]['status'],'reported')
                self.assertEqual(budget.rows[0]['cached_input_tokens'],80)
