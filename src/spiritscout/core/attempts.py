"""精确调用去重：单事件循环内原子占位，同伴订阅在途结果；失败不缓存。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any


def exact_key(intent: str, target: str, parameters: Any, context: Any = None) -> str:
    """完整参数规范编码后摘要，不截断值，不把语义相似当成相同。"""
    payload = json.dumps([intent, target, parameters, context], sort_keys=True,
                         ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return "attempt-" + hashlib.sha256(payload.encode()).hexdigest()


class AttemptLedger:
    def __init__(self, capacity: int = 256) -> None:
        self.capacity = capacity
        self.pending: dict[str, asyncio.Future[Any]] = {}
        self.completed: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    async def execute(self, key: str, perform: Callable[[], Awaitable[Any]], *, ttl: float) -> tuple[Any, str]:
        """没有 await 的检查/占位是原子的；订阅者取消不取消执行者。"""
        while True:
            cached = self.completed.get(key)
            if cached and cached[0] > time.monotonic():
                self.completed.move_to_end(key)
                return copy.deepcopy(cached[1]), "cache"
            self.completed.pop(key, None)
            future = self.pending.get(key)
            if future is not None:
                result = await asyncio.shield(future)
                if result is not None:
                    return copy.deepcopy(result), "shared"
                continue  # 执行者失败/取消没有有效结果，重新争取执行权。
            future = asyncio.get_running_loop().create_future()
            self.pending[key] = future
            try:
                result = await perform()
                if result.ok and getattr(result, "effect_status", "none") == "none":
                    self.completed[key] = (time.monotonic() + ttl, copy.deepcopy(result))
                    while len(self.completed) > self.capacity:
                        self.completed.popitem(last=False)
                    future.set_result(copy.deepcopy(result))
                else:
                    future.set_result(None)
                return result, "executed"
            finally:
                if not future.done():
                    future.set_result(None)
                if self.pending.get(key) is future:
                    del self.pending[key]

    def snapshot(self) -> dict[str, int]:
        return {"running": len(self.pending), "reusable": sum(
            expiry > time.monotonic() for expiry, _ in self.completed.copy().values())}
