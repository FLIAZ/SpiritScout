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
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from spiritscout.core.models import Event

EventHandler = Callable[[Event], Awaitable[None]]

# 事件幂等去重窗口（每订阅者）：长会话不随事件数无限涨；超出淘汰最旧记录
SEEN_EVENT_WINDOW = 4096


@dataclass
class Subscription:
    pattern: str
    handler: EventHandler
    sub_id: str = ""
    seen_event_ids: set[str] = field(default_factory=set)  # 事件幂等去重
    seen_order: deque[str] = field(default_factory=deque)  # 去重窗口内的事件顺序（有界淘汰）

    def matches(self, topic: str) -> bool:
        return fnmatch.fnmatchcase(topic, self.pattern)


def remember_event(sub: Subscription, event_id: str) -> bool:
    """事件幂等判定与登记：首次见返回 True，重复返回 False。

        窗口有上限：登记超出窗口时淘汰最旧记录——去重语义对近期重放仍成立，
        但集合不随会话时长无界增长（与各总线后端同口径）。
        """
    if event_id in sub.seen_event_ids:
        return False
    sub.seen_order.append(event_id)
    sub.seen_event_ids.add(event_id)
    if len(sub.seen_order) > SEEN_EVENT_WINDOW:
        sub.seen_event_ids.discard(sub.seen_order.popleft())
    return True


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
