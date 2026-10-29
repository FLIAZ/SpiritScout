"""精确去重的并发订阅、身份隔离与失败恢复。"""
import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace

from spiritscout.core.attempts import exact_key
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import ToolKind, ToolResult, ToolSpec


class ExactAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_followers_share_success_and_cancel_independently(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def read(**kwargs):
            calls.append(kwargs)
            entered.set()
            await release.wait()
            return ToolResult(tool='read', ok=True, stdout='observation')

        registry = ToolRegistry(SimpleNamespace(), max_retries=0)
        registry.register_builtin('read', read, ToolSpec('read', ToolKind.BUILTIN, reuse_ttl_seconds=30))
        producer = asyncio.create_task(registry.call('read', {'url': 'http://fixture.test'}))
        await entered.wait()
        follower = asyncio.create_task(registry.call('read', {'url': 'http://fixture.test'}))
        await asyncio.sleep(0)
        follower.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await follower
        release.set()
        first = await producer
        second = await registry.call('read', {'url': 'http://fixture.test'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.source_invocation_id, first.invocation_id)
        self.assertEqual(second.reuse, 'cache')
        self.assertNotEqual(first.invocation_id, second.invocation_id)

    async def test_identity_environment_refresh_and_verifier_are_separate(self) -> None:
        count = 0

        async def read(**kwargs):
            nonlocal count
            count += 1
            return ToolResult(tool='read', ok=True, stdout=str(count))

        registry = ToolRegistry(SimpleNamespace())
        registry.register_builtin('read', read, ToolSpec('read', ToolKind.BUILTIN, reuse_ttl_seconds=30))
        base = ExecutionScope(agent_id='a', identity_id='tenant-a', environment_id='instance-1')
        for scope in (base, replace(base, agent_id='b'), replace(base, identity_id='tenant-b'),
                      replace(base, environment_id='instance-2'), replace(base, agent_id='verifier')):
            token = current_execution_scope.set(scope)
            try:
                await registry.call('read', {})
            finally:
                current_execution_scope.reset(token)
        self.assertEqual(count, 4)
        await registry.call('read', {}, _fresh=True)
        self.assertEqual(count, 5)

    def test_complete_parameters_cannot_collide_after_old_truncation(self) -> None:
        self.assertNotEqual(exact_key('read', '/', {'a': 'x' * 300 + 'one'}),
                            exact_key('read', '/', {'a': 'x' * 300 + 'two'}))
        self.assertEqual(exact_key('read', '/', {'b': 2, 'a': 1}), exact_key('read', '/', {'a': 1, 'b': 2}))


class RealHTTPAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_singleflight_params_auth_refresh_and_write_invalidation(self) -> None:
        from spiritscout.core.models import Scope
        from spiritscout.policy.scope import ScopeGuard
        from spiritscout.tools.builtin import BUILTIN_SPECS, http_probe

        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def respond(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                requests.append(request)
                if len(requests) == 1:
                    entered.set()
                    await release.wait()
                body = b'local-observation'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 17\r\nConnection: close\r\n\r\n' + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(respond, '127.0.0.1', 0)
        self.addCleanup(server.close)
        url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/probe'
        registry = ToolRegistry(SimpleNamespace(), max_retries=0)
        registry.register_builtin('http_probe', http_probe, BUILTIN_SPECS['http_probe'])
        guard = ScopeGuard(Scope(allowed_urls=[url]))

        async def fetch(actor='a', **params):
            token = current_execution_scope.set(ExecutionScope(agent_id=actor, identity_id='test-user', environment_id='fixture-1'))
            try:
                return await registry.call('http_probe', {'url': url, **params}, guard=guard)
            finally:
                current_execution_scope.reset(token)

        first = asyncio.create_task(fetch())
        await entered.wait()
        second = asyncio.create_task(fetch('b'))
        await asyncio.sleep(0)
        release.set()
        one, two = await asyncio.gather(first, second)
        self.assertTrue(one.ok and two.ok)
        self.assertEqual(len(requests), 1)
        self.assertEqual(two.source_invocation_id, one.invocation_id)
        self.assertEqual(two.reuse, 'shared')
        for suffix in ('one', 'two'):
            await fetch(params={'q': 'x' * 500 + suffix})
        self.assertEqual(len(requests), 3)
        await fetch(headers={'Authorization': 'Bearer fixture-a'})
        await fetch(headers={'Authorization': 'Bearer fixture-b'})
        self.assertEqual(len(requests), 5)
        await fetch(refresh=True)
        self.assertEqual(len(requests), 6)
        await fetch(method='POST', action='analysis')
        await fetch()
        self.assertEqual(len(requests), 8, '写入后不能复用写入前的 GET 观察')
        await fetch('verifier')
        self.assertEqual(len(requests), 9, '独立复测必须实际发送请求')
        server.close()
        await server.wait_closed()


class AttemptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_producer_cancellation_releases_claim_for_waiter(self) -> None:
        entered = asyncio.Event()
        calls = 0

        async def read(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await asyncio.Event().wait()
            return ToolResult(tool='read', ok=True)

        registry = ToolRegistry(SimpleNamespace(), max_retries=0)
        registry.register_builtin('read', read, ToolSpec('read', ToolKind.BUILTIN, reuse_ttl_seconds=30))
        producer = asyncio.create_task(registry.call('read', {}))
        await entered.wait()
        waiter = asyncio.create_task(registry.call('read', {}))
        await asyncio.sleep(0)
        producer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await producer
        result = await asyncio.wait_for(waiter, 1)
        self.assertTrue(result.ok)
        self.assertEqual(calls, 2)
        self.assertFalse(registry.attempts.pending)

    async def test_failed_result_and_expired_result_are_not_reused(self) -> None:
        calls = 0

        async def read(**kwargs):
            nonlocal calls
            calls += 1
            return ToolResult(tool='read', ok=calls > 1)

        registry = ToolRegistry(SimpleNamespace(), max_retries=0)
        registry.register_builtin('read', read, ToolSpec('read', ToolKind.BUILTIN, reuse_ttl_seconds=0.01))
        self.assertFalse((await registry.call('read', {})).ok)
        self.assertTrue((await registry.call('read', {})).ok)
        await asyncio.sleep(0.02)
        await registry.call('read', {})
        self.assertEqual(calls, 3)

    async def test_cache_hit_does_not_bypass_current_policy(self) -> None:
        from unittest.mock import AsyncMock

        from spiritscout.core.exceptions import PolicyViolation

        execute = AsyncMock(return_value=ToolResult(tool='read', ok=True))
        policy = SimpleNamespace(automatic_approval=True, ensure_tool_allowed=AsyncMock())
        registry = ToolRegistry(SimpleNamespace(), policy=policy)
        registry.register_builtin('read', execute, ToolSpec('read', ToolKind.BUILTIN, reuse_ttl_seconds=30))
        await registry.call('read', {})
        policy.ensure_tool_allowed.side_effect = PolicyViolation('权限已更新')
        denied = await registry.call('read', {})
        self.assertFalse(denied.ok)
        self.assertEqual(denied.execution_status, 'denied')
        execute.assert_awaited_once()

class CTFApprovalReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_reusable_calls_record_one_real_approval_each_without_duplicate_insert(self) -> None:
        import tempfile
        from pathlib import Path

        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import RunMode, Scope
        from spiritscout.policy.engine import PolicyEngine
        from spiritscout.policy.scope import ScopeGuard
        from spiritscout.tools.journal import ExecutionJournal

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bb = Blackboard(root / 'bb.db')
            self.addCleanup(bb.close)
            journal = ExecutionJournal(bb, root, 'approval-regression')
            policy = PolicyEngine(RunMode.CTF, ScopeGuard(Scope(local_analysis=True)))
            registry = ToolRegistry(SimpleNamespace(), policy=policy, journal=journal)
            called = []

            async def observe(**kwargs):
                called.append(kwargs)
                return ToolResult(tool='read', ok=True, stdout='original response')

            registry.register_builtin('read', observe, ToolSpec('read', ToolKind.BUILTIN, reuse_ttl_seconds=30))
            first = await registry.call('read', {})
            cached = await registry.call('read', {})
            fresh = await registry.call('read', {}, _fresh=True)
            self.assertTrue(first.ok and cached.ok and fresh.ok)
            self.assertEqual(len(called), 2)
            self.assertEqual(cached.reuse, 'cache')
            with bb.transaction() as conn:
                rows = conn.execute('SELECT invocation_id FROM approval_records').fetchall()
            self.assertEqual({row[0] for row in rows}, {first.invocation_id, cached.invocation_id, fresh.invocation_id})
