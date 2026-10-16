"""真实 Redis 集成测试；仅连接显式提供的隔离测试实例，不清库。"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
import unittest
import uuid

from spiritscout.bus.redis_bus import RedisBus
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.models import Event

_TEST_URL = os.environ.get("SPIRITSCOUT_TEST_REDIS_URL")


@unittest.skipUnless(_TEST_URL, "需要 SPIRITSCOUT_TEST_REDIS_URL 指定隔离 Redis")
class RedisNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"ss:test:{uuid.uuid4().hex}"
        self.sender = RedisBus(_TEST_URL, channel_prefix=self.prefix)
        self.receiver = RedisBus(_TEST_URL, channel_prefix=self.prefix)
        self.other = RedisBus(_TEST_URL, channel_prefix=self.prefix + "-other")
        self.queue: asyncio.Queue = asyncio.Queue()
        self.other_queue: asyncio.Queue = asyncio.Queue()
        self.receiver.subscribe("bb.*", self.queue.put)
        self.other.subscribe("*", self.other_queue.put)
        for bus in [self.sender, self.receiver, self.other]:
            await bus.start()
            self.addAsyncCleanup(bus.stop)

    async def test_before_start_and_dynamic_subscriptions_isolation_and_redaction(self) -> None:
        await self.sender.publish(Event(topic="bb.asset", data_id="a", payload={"secret": "marker"}))
        event = await asyncio.wait_for(self.queue.get(), 2)
        self.assertEqual(event.data_id, "a")
        self.assertEqual(event.payload, {})
        self.assertTrue(self.other_queue.empty())
        dynamic: asyncio.Queue = asyncio.Queue()
        sub = self.receiver.subscribe("task.*", dynamic.put)
        await self.sender.publish(Event(topic="task.result", data_id="t"))
        self.assertEqual((await asyncio.wait_for(dynamic.get(), 2)).data_id, "t")
        self.receiver.unsubscribe(sub.sub_id)
        await self.sender.publish(Event(topic="task.result", data_id="ignored"))
        await self.sender.publish(Event(topic="bb.asset", data_id="barrier"))
        await asyncio.wait_for(self.queue.get(), 2)
        self.assertTrue(dynamic.empty())
        self.assertNotIn("redis://", json.dumps(self.sender.stats()))

    async def test_restart_keeps_subscription_and_deduplicates_replay(self) -> None:
        await self.receiver.stop()
        await self.receiver.start()
        event = Event(topic="bb.asset", event_id="stable-event", data_id="a")
        await self.sender.publish(event)
        await self.sender.publish(event)
        await self.sender.publish(Event(topic="bb.asset", data_id="barrier"))
        self.assertEqual((await asyncio.wait_for(self.queue.get(), 2)).data_id, "a")
        self.assertEqual((await asyncio.wait_for(self.queue.get(), 2)).data_id, "barrier")
        self.assertTrue(self.queue.empty())

    async def test_shared_rate_is_atomic_across_connections_and_sessions(self) -> None:
        key = self.prefix + ":rate"
        started = time.monotonic()
        times: list[float] = []

        async def acquire(bus: RedisBus) -> None:
            await bus.acquire_request_slot(20, key)
            times.append(time.monotonic())

        await asyncio.gather(*(acquire(bus) for bus in
                               [self.sender, self.receiver, self.other, self.sender]))
        self.assertGreaterEqual(times[-1] - started, 0.13)
        self.assertTrue(all(b - a >= 0.035 for a, b in itertools.pairwise(times)))
        await self.sender.stop()
        with self.assertRaises(ConfigurationError):
            await self.sender.acquire_request_slot(20, key)


class RedisConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_backend_has_readable_error_without_credentials(self) -> None:
        bus = RedisBus("redis://:private-marker@127.0.0.1:1/0", connect_timeout_seconds=0.1)
        with self.assertRaises(ConfigurationError) as caught:
            await bus.start()
        self.assertNotIn("private-marker", str(caught.exception))
        self.assertNotIn("private-marker", json.dumps(bus.stats()))
        self.assertFalse(bus.stats()["connected"])
        await bus.stop()


@unittest.skipUnless(os.environ.get("SPIRITSCOUT_TEST_DOCKER") == "1",
                     "需要 SPIRITSCOUT_TEST_DOCKER=1 启动隔离 Redis 容器")
class RedisOutageTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_server_restart_redelivers_persistent_outbox(self) -> None:
        import socket
        import subprocess
        import tempfile
        from pathlib import Path

        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import BlackboardLayer

        name = "spiritscout-redis-test-" + uuid.uuid4().hex[:12]

        async def docker(*args: str) -> str:
            result = await asyncio.to_thread(subprocess.run, ["docker", *args],
                                             check=True, capture_output=True, text=True)
            return result.stdout.strip()

        # 显式分配端口，避免 Docker 重启时重新分配临时宿主端口。
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            host_port = reservation.getsockname()[1]
        await docker("run", "--detach", "--name", name, "-p", f"127.0.0.1:{host_port}:6379",
                     "redis:7-alpine", "redis-server", "--save", "", "--appendonly", "no")
        bus = None
        bb = None
        try:
            port = (await docker("port", name, "6379/tcp")).rsplit(":", 1)[1]
            bus = RedisBus(f"redis://127.0.0.1:{port}/0", channel_prefix=name,
                           connect_timeout_seconds=0.3)
            await bus.start()
            with tempfile.TemporaryDirectory() as tmp:
                bb = Blackboard(Path(tmp) / "bb.db", bus=bus)
                try:
                    await docker("stop", name)
                    bb.write(BlackboardLayer.TASK_STATE, "outage", {"saved": True})
                    await asyncio.sleep(0.05)
                    with bb.transaction() as conn:
                        self.assertEqual(conn.execute("SELECT count(*) FROM bb_event_outbox "
                                                      "WHERE delivered=0").fetchone()[0], 1)
                    self.assertEqual(bb.read(BlackboardLayer.TASK_STATE, "outage"), {"saved": True})
                    await docker("start", name)
                    async with asyncio.timeout(8):
                        while True:
                            with bb.transaction() as conn:
                                pending = conn.execute("SELECT count(*) FROM bb_event_outbox "
                                                       "WHERE delivered=0").fetchone()[0]
                            if pending == 0:
                                break
                            await asyncio.sleep(0.1)
                    self.assertEqual(bb.read(BlackboardLayer.TASK_STATE, "outage"), {"saved": True})
                finally:
                    await bb.aclose()
                    bb = None
        finally:
            if bb is not None:
                await bb.aclose()
            if bus is not None:
                await bus.stop()
            await docker("rm", "--force", name)
