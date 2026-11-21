"""模型 SDK 的异步冷加载；导入不占用 Web 事件循环。"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from types import ModuleType


async def load_litellm() -> ModuleType:
    # 与产品入口保持一致；库被独立调用时也不在导入期拉远端价格表。
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    module = sys.modules.get("litellm")
    if module is not None and not getattr(getattr(module, "__spec__", None), "_initializing", False):
        return module
    # Python 导入锁负责合并并发导入；等待锁和首次构建模型都在工作线程上。
    return await asyncio.to_thread(importlib.import_module, "litellm")
