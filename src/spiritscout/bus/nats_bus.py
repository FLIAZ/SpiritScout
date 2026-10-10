"""NATS 总线后端（设计书 7 技术栈：NATS 高性能环形消息总线）。

需外部 NATS 服务；主题映射为 NATS subject（spiritscout.bb.asset 等），
原生支持 ``*`` 通配订阅。
"""

from __future__ import annotations

import asyncio
import uuid

from spiritscout.bus.base import EventBus, EventHandler, Subscription
from spiritscout.core.models import Event


class NatsBus(EventBus):
    def __init__(self, url: str = "nats://127.0.0.1:4222", subject_prefix: str = "spiritscout"):
        self._url = url
        self._prefix = subject_prefix
        self._nc = None
        self._subs: dict[str, Subscription] = {}
        self._nats_subs: list = []
        self._published = 0
        self._delivered = 0
        self._handler_errors = 0

    def _subject(self, topic: str) -> str:
        return f"{self._prefix}.{topic}"

    async def start(self) -> None:
        import nats as nats_lib  # 延迟导入

        self._nc = await nats_lib.connect(self._url)
        # 重放已有订阅（start 前注册的）
        for sub in list(self._subs.values()):
            await self._subscribe_nats(sub)

    async def stop(self) -> None:
        for s in self._nats_subs:
            try:
                await s.unsubscribe()
            except Exception:  # noqa: BLE001, S110
                pass
        self._nats_subs.clear()
        if self._nc:
            await self._nc.drain()

    async def publish(self, event: Event) -> None:
        if not event.event_id:
            event.event_id = f"evt-{uuid.uuid4().hex[:12]}"
        assert self._nc is not None
        await self._nc.publish(self._subject(event.topic), event.model_dump_json().encode())
        self._published += 1

    def subscribe(self, pattern: str, handler: EventHandler, sub_id: str = "") -> Subscription:
        sub_id = sub_id or f"sub-{uuid.uuid4().hex[:8]}"
        sub = Subscription(pattern=pattern, handler=handler, sub_id=sub_id)
        self._subs[sub_id] = sub
        if self._nc is not None:
            asyncio.create_task(self._subscribe_nats(sub))
        return sub

    def unsubscribe(self, sub_id: str) -> None:
        self._subs.pop(sub_id, None)

    def stats(self) -> dict:
        return {
            "backend": "nats",
            "url": self._url,
            "published": self._published,
            "delivered": self._delivered,
            "handler_errors": self._handler_errors,
            "subscriptions": len(self._subs),
        }

    async def _subscribe_nats(self, sub: Subscription) -> None:
        assert self._nc is not None
        nats_pattern = self._subject(sub.pattern)

        async def _cb(msg) -> None:
            try:
                event = Event.model_validate_json(msg.data.decode())
            except Exception:  # noqa: BLE001
                return
            if event.event_id in sub.seen_event_ids:
                return
            sub.seen_event_ids.add(event.event_id)
            try:
                await sub.handler(event)
                self._delivered += 1
            except Exception:  # noqa: BLE001
                self._handler_errors += 1

        self._nats_subs.append(await self._nc.subscribe(nats_pattern, cb=_cb))
