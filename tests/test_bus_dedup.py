"""总线事件幂等去重：重复事件不重复执行，去重窗口有界不随会话增长。"""

from __future__ import annotations

import asyncio
import unittest

from spiritscout.bus.base import SEEN_EVENT_WINDOW, Subscription, remember_event
from spiritscout.bus.inprocess import InProcessBus
from spiritscout.core.models import Event


async def _noop(event: Event) -> None:
    return None


class SeenWindowTests(unittest.TestCase):
    def test_duplicate_event_is_rejected_once(self) -> None:
        sub = Subscription(pattern="bb.*", handler=_noop)
        self.assertTrue(remember_event(sub, "e1"))
        self.assertFalse(remember_event(sub, "e1"))

    def test_window_evicts_oldest_and_stays_bounded(self) -> None:
        sub = Subscription(pattern="bb.*", handler=_noop)
        for i in range(SEEN_EVENT_WINDOW + 10):
            remember_event(sub, f"e{i}")
        self.assertEqual(len(sub.seen_event_ids), SEEN_EVENT_WINDOW)
        self.assertNotIn("e0", sub.seen_event_ids)          # 最旧已淘汰
        self.assertIn(f"e{SEEN_EVENT_WINDOW + 9}", sub.seen_event_ids)
        # 窗口语义：防近期重放，不承诺永久记忆——淘汰后旧 id 不再拦截
        self.assertTrue(remember_event(sub, "e0"))


class InProcessDedupDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_event_delivers_once(self) -> None:
        bus = InProcessBus()
        await bus.start()
        self.addAsyncCleanup(bus.stop)
        seen: list[str] = []

        async def handler(event: Event) -> None:
            seen.append(event.event_id)

        bus.subscribe("bb.*", handler)
        event = Event(topic="bb.vuln", event_id="evt-1")
        await bus.publish(event)
        await bus.publish(event)
        await asyncio.sleep(0.3)   # 等派发循环消化队列
        self.assertEqual(seen, ["evt-1"])


if __name__ == "__main__":
    unittest.main()
