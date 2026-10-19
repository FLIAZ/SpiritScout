"""Redis Pub/Sub 总线后端（设计书 7 技术栈：NATS + Redis Pub/Sub）。

需外部 Redis 服务；语义与 inprocess 后端一致。事件以 JSON 序列化，
订阅侧做幂等去重。Redis 优先级语义弱于进程内队列，按到达顺序派发
（高优先级事件由消费方队列自行排序）。
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid

from spiritscout.bus.base import EventBus, EventHandler, Subscription, remember_event
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.models import Event


class RedisBus(EventBus):
    def __init__(self, url: str = "redis://127.0.0.1:6379/0", channel_prefix: str = "spiritscout",
                 connect_timeout_seconds: float = 5.0):
        self._timeout = connect_timeout_seconds
        self._connected = False
        self._connection_errors = 0
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
        if self._running:
            return
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise ConfigurationError("Redis 后端缺少依赖，请安装 spiritscout[redis]") from None
        try:
            self._redis = aioredis.from_url(
                self._url, decode_responses=True,
                socket_connect_timeout=self._timeout, socket_timeout=self._timeout,
                max_connections=16, health_check_interval=15)
            await self._redis.ping()
            self._pubsub = self._redis.pubsub()
            # 统一监听会话前缀，具体订阅在本地匹配；无动态订阅的异步竞态。
            await self._pubsub.psubscribe(self._channel("*"))
            async with asyncio.timeout(self._timeout):
                while True:
                    ack = await self._pubsub.get_message(timeout=0.2)
                    if ack and ack.get("type") == "psubscribe":
                        break
            self._connected = True
            self._running = True
            self._listener = asyncio.create_task(self._listen_loop(), name="redis-bus-listener")
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception:  # noqa: BLE001
            await self.stop()
            # 不附底层异常，避免 URL 中的认证信息进入状态与审计。
            raise ConfigurationError("Redis 连接或订阅失败，请检查服务、凭据与网络；未切换后端") from None

    async def stop(self) -> None:
        self._running = False
        self._connected = False
        if self._listener:
            self._listener.cancel()
            await asyncio.gather(self._listener, return_exceptions=True)
            self._listener = None
        try:
            if self._pubsub is not None:
                await self._pubsub.aclose()
        finally:
            self._pubsub = None
            if self._redis is not None:
                await self._redis.aclose()
                self._redis = None

    async def publish(self, event: Event) -> None:
        if not event.event_id:
            event.event_id = f"evt-{uuid.uuid4().hex[:12]}"
        if self._redis is None or not self._running:
            raise ConfigurationError("Redis 总线未启动")
        try:
            # Redis 只传通知引用；结果、凭据和脚本由会话本地存储保管。
            notification = event.model_copy(update={"payload": {}})
            await self._redis.publish(self._channel(event.topic), notification.model_dump_json())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self._connected = False
            self._connection_errors += 1
            raise ConfigurationError("Redis 发布失败，通知须从持久化状态补送") from None
        self._published += 1

    def subscribe(self, pattern: str, handler: EventHandler, sub_id: str = "") -> Subscription:
        sub_id = sub_id or f"sub-{uuid.uuid4().hex[:8]}"
        sub = Subscription(pattern=pattern, handler=handler, sub_id=sub_id)
        self._subs[sub_id] = sub
        return sub

    async def acquire_request_slot(self, rps: float, key: str) -> None:
        """Redis 服务端时钟与原子许可，多会话共享探测速率；断线不绕过。"""
        if not math.isfinite(rps) or rps <= 0:
            raise ValueError("共享请求速率必须为有限正数")
        interval = max(1, math.ceil(1000 / rps))
        script = """
        local t = redis.call('TIME')
        local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
        local next_at = tonumber(redis.call('GET', KEYS[1]) or '0')
        if next_at > now then return next_at - now end
        redis.call('SET', KEYS[1], now + tonumber(ARGV[1]), 'PX', tonumber(ARGV[1]) * 2)
        return 0
        """
        while True:
            if self._redis is None or not self._running:
                raise ConfigurationError("Redis 共享限速不可用，未派发请求")
            try:
                wait_ms = int(await self._redis.eval(script, 1, key, interval))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                raise ConfigurationError("Redis 共享限速失败，未绕过限速执行") from None
            if wait_ms <= 0:
                return
            await asyncio.sleep(wait_ms / 1000)

    def unsubscribe(self, sub_id: str) -> None:
        self._subs.pop(sub_id, None)

    def stats(self) -> dict:
        return {
            "backend": "redis",
            "connected": self._connected,
            "connection_errors": self._connection_errors,
            "namespace": self._prefix,
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
                self._connected = False
                self._connection_errors += 1
                await asyncio.sleep(0.5)
                continue
            self._connected = True
            if not msg or msg.get("type") not in ("message", "pmessage"):
                continue
            try:
                event = Event.model_validate(json.loads(msg["data"]))
            except Exception:  # noqa: BLE001, S112
                continue
            for sub in list(self._subs.values()):
                if not sub.matches(event.topic):
                    continue
                # 事件幂等：同一订阅者对重复 event_id 不重复执行（6.9），
                # 去重窗口有界（长会话内存不随事件数增长）
                if not remember_event(sub, event.event_id):
                    continue
                try:
                    await sub.handler(event)
                    self._delivered += 1
                except Exception:  # noqa: BLE001
                    self._handler_errors += 1
