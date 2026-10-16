"""单进程异步总线（默认后端）。

语义与分布式后端一致：主题分区 Pub/Sub、事件优先级派发、令牌桶流量控制、
消费方幂等去重。单进程 MVP 阶段确定性最强；分布式部署时切换 redis/nats。
"""

from __future__ import annotations

import asyncio
import itertools
import time
import uuid

from spiritscout.bus.base import EventBus, EventHandler, Subscription
from spiritscout.core.models import Event


class InProcessBus(EventBus):
    def __init__(self, queue_maxsize: int = 10000, rate_limit_per_second: float = 1000.0):
        self._queue: asyncio.PriorityQueue[tuple[int, float, int, Event]] = asyncio.PriorityQueue(
            maxsize=queue_maxsize
        )
        self._subs: dict[str, Subscription] = {}
        self._dispatcher: asyncio.Task | None = None
        self._running = False
        self._rate_limit = rate_limit_per_second
        self._tokens = rate_limit_per_second
        self._last_refill = time.monotonic()
        self._published = 0
        self._delivered = 0
        self._dropped = 0
        self._handler_errors = 0
        self._sequence = itertools.count()

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="bus-dispatcher")

    async def stop(self) -> None:
        self._running = False
        if self._dispatcher:
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass
            self._dispatcher = None

    # ------------------------------------------------------------ 发布/订阅

    async def publish(self, event: Event) -> None:
        if not self._running:
            raise RuntimeError("进程内总线未启动")
        if not event.event_id:
            event.event_id = f"evt-{uuid.uuid4().hex[:12]}"
        await self._throttle()
        # 优先级队列：(priority 小者优先, 时间戳)
        item = (event.priority, event.ts.timestamp(), next(self._sequence), event)
        # 满队列施加背压，不能假报成功使 outbox 丢失补送机会。
        await self._queue.put(item)
        self._published += 1

    def subscribe(self, pattern: str, handler: EventHandler, sub_id: str = "") -> Subscription:
        sub_id = sub_id or f"sub-{uuid.uuid4().hex[:8]}"
        sub = Subscription(pattern=pattern, handler=handler, sub_id=sub_id)
        self._subs[sub_id] = sub
        return sub

    def unsubscribe(self, sub_id: str) -> None:
        self._subs.pop(sub_id, None)

    def stats(self) -> dict:
        return {
            "backend": "inprocess",
            "published": self._published,
            "delivered": self._delivered,
            "dropped": self._dropped,
            "handler_errors": self._handler_errors,
            "queue_size": self._queue.qsize(),
            "subscriptions": len(self._subs),
        }

    # ------------------------------------------------------------ 内部

    async def _throttle(self) -> None:
        """令牌桶流量控制（4.2.1）。"""
        if self._rate_limit <= 0:
            return
        while True:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rate_limit, self._tokens + elapsed * self._rate_limit)
            self._last_refill = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await asyncio.sleep((1 - self._tokens) / self._rate_limit)

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                _, _, _, event = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            for sub in list(self._subs.values()):
                if not sub.matches(event.topic):
                    continue
                # 事件幂等：同一订阅者对重复 event_id 不重复执行（6.9）
                if event.event_id in sub.seen_event_ids:
                    continue
                sub.seen_event_ids.add(event.event_id)
                try:
                    await sub.handler(event)
                    self._delivered += 1
                except Exception:  # noqa: BLE001 —— 单订阅者故障不影响总线（容错鲁棒性 2.1-3）
                    self._handler_errors += 1
            self._queue.task_done()
