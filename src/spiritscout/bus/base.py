"""总线抽象：发布-订阅 + 主题分区 + 优先级 + 流量控制。

主题命名约定（4.2.1 主题分区）：
- ``bb.<layer>``      黑板层数据变更事件（如 bb.asset / bb.vuln）
- ``task.dispatch``   主 Agent 任务下发
- ``task.result``     子 Agent 结果上报
- ``flag.submit``     Flag 提交
- ``audit.alert``     高危告警（RCE/凭据/Flag 优先消费，6.9）
订阅支持通配：``bb.*`` 匹配全部黑板层事件。
"""

from __future__ import annotations

import abc
import fnmatch
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from spiritscout.core.models import Event

EventHandler = Callable[[Event], Awaitable[None]]


@dataclass
class Subscription:
    pattern: str
    handler: EventHandler
    sub_id: str = ""
    seen_event_ids: set[str] = field(default_factory=set)  # 事件幂等去重（6.9）

    def matches(self, topic: str) -> bool:
        return fnmatch.fnmatchcase(topic, self.pattern)


class EventBus(abc.ABC):
    """环形数据总线抽象基类。"""

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def publish(self, event: Event) -> None:
        """发布事件。携带 event_id + version，消费方按版本去重幂等。"""

    @abc.abstractmethod
    def subscribe(self, pattern: str, handler: EventHandler, sub_id: str = "") -> Subscription:
        """注册订阅（同步注册即可，派发异步进行）。"""

    @abc.abstractmethod
    def unsubscribe(self, sub_id: str) -> None: ...

    @abc.abstractmethod
    def stats(self) -> dict:
        """流量与队列统计（监控审计用）。"""
