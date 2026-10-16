"""环形数据总线（设计书 4.2.1 / 6.3 / 4.8.2 事件通道）。

适配器决策 A1：EventBus 抽象 + inprocess（默认）/ redis / nats 三后端，
配置一键切换，语义一致（主题分区、优先级、流量控制、幂等事件）。
"""

from spiritscout.bus.base import EventBus, Subscription
from spiritscout.bus.inprocess import InProcessBus


def create_bus(backend: str = "inprocess", **kwargs) -> EventBus:
    """按配置装配总线后端。"""
    if backend == "inprocess":
        return InProcessBus(
            queue_maxsize=kwargs.get("queue_maxsize", 10000),
            rate_limit_per_second=kwargs.get("rate_limit_per_second", 1000.0),
        )
    if backend == "redis":
        from spiritscout.bus.redis_bus import RedisBus

        return RedisBus(url=kwargs.get("redis_url", "redis://127.0.0.1:6379/0"),
                        channel_prefix=kwargs.get("channel_prefix", "spiritscout"),
                        connect_timeout_seconds=kwargs.get("connect_timeout_seconds", 5.0))
    if backend == "nats":
        from spiritscout.bus.nats_bus import NatsBus

        return NatsBus(url=kwargs.get("nats_url", "nats://127.0.0.1:4222"))
    raise ValueError(f"未知总线后端: {backend}")


__all__ = ["EventBus", "InProcessBus", "Subscription", "create_bus"]
