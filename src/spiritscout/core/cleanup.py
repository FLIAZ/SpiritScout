"""等待关键资源清理结束；取消等待不能提前放弃资源所有权。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

_T = TypeVar("_T")


async def finish_cleanup(work: Awaitable[_T]) -> _T:
    """重复取消只作用于等待者；清理真正结束后再传播取消或清理错误。"""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
