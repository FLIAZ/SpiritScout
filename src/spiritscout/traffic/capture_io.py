"""目录删除前必须等本次证据写入结束；与采集进程解耦以避免循环导入。"""
from __future__ import annotations

import asyncio
from typing import Any


async def wait_io(function: Any, *args: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
