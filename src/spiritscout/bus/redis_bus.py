"""Redis Pub/Sub 总线后端（设计书 7 技术栈：NATS + Redis Pub/Sub）。

需外部 Redis 服务；语义与 inprocess 后端一致。事件以 JSON 序列化，
订阅侧做幂等去重。Redis 优先级语义弱于进程内队列，按到达顺序派发
（高优先级事件由消费方队列自行排序）。
"""

from __future__ import annotations

import asyncio
import json
import uuid

from spiritscout.bus.base import EventBus, EventHandler, Subscription
from spiritscout.core.models import Event


class RedisBus(EventBus):
    def __init__(self, url: str = "redis://127.0.0.1:6379/0", channel_prefix: str = "spiritscout"):
        self._url = url
        self._prefix = channel_prefix
        self._redis = None
        self._pubsub = None
        self._subs: dict[str, Subscription] = {}
        self._listener: asyncio.Task | None = None
        self._running = False
        self._published = 0
        self._delivered = 0
        self._handler_errors = 0

    def _channel(self, topic: str) -> str:
        return f"{self._prefix}:{topic}"

    async def start(self) -> None:
        import redis.asyncio as aioredis  # 延迟导入：未配置 redis 后端时无依赖

        self._redis = aioredis.from_url(self._url, decode_responses=True)
        await self._redis.ping()
        self._pubsub = self._redis.pubsub()
        self._running = True
        self._listener = asyncio.create_task(self._listen_loop(), name="redis-bus-listener")

    async def stop(self) -> None:
        self._running = False
        if self._listener:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.close()
        if self._redis:
            await self._redis.aclose()

    async def publish(self, event: Event) -> None:
        if not event.event_id:
            event.event_id = f"evt-{uuid.uuid4().hex[:12]}"
        assert self._redis is not None
        await self._redis.publish(self._channel(event.topic), event.model_dump_json())
        self._published += 1

    def subscribe(self, pattern: str, handler: EventHandler, sub_id: str = "") -> Subscription:
        sub_id = sub_id or f"sub-{uuid.uuid4().hex[:8]}"
        sub = Subscription(pattern=pattern, handler=handler, sub_id=sub_id)
        self._subs[sub_id] = sub
        # 动态补充 psubscribe（pubsub 通道模式）
        if self._pubsub is not None:
            asyncio.create_task(
                self._pubsub.psubscribe(self._channel(pattern.replace("*", "*")))
            )
        return sub

    def unsubscribe(self, sub_id: str) -> None:
        self._subs.pop(sub_id, None)

    def stats(self) -> dict:
        return {
            "backend": "redis",
            "url": self._url,
            "published": self._published,
            "delivered": self._delivered,
            "handler_errors": self._handler_errors,
            "subscriptions": len(self._subs),
        }

    async def _listen_loop(self) -> None:
        assert self._pubsub is not None
        while self._running:
            try:
                msg = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.2
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.5)
                continue
            if not msg or msg.get("type") not in ("message", "pmessage"):
                continue
            try:
                event = Event.model_validate(json.loads(msg["data"]))
            except Exception:  # noqa: BLE001, S112
                continue
            for sub in list(self._subs.values()):
                if not sub.matches(event.topic) or event.event_id in sub.seen_event_ids:
                    continue
                sub.seen_event_ids.add(event.event_id)
                try:
                    await sub.handler(event)
                    self._delivered += 1
                except Exception:  # noqa: BLE001
                    self._handler_errors += 1
