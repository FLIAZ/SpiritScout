"""前台任务优先级与进程内后台槽；报告和经验提炼共用同一份计数。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

# 同一进程内：前台解题优先，后台提炼串行且仅在无前台任务时运行。
# 前台计数同时按工作区记账：单题报告只让位于本会话的执行，
# 不被其他并行会话的前台任务饿死。
_FOREGROUND_DEPTH = 0
_FOREGROUND_BY_WORKSPACE: dict[str, int] = {}
_BACKGROUND_ACTIVE = 0
_BACKGROUND_COOLDOWN_UNTIL = 0.0
_BACKGROUND_COOLDOWN_SECONDS = 2.0


def _foreground_started(workspace: Path | None = None) -> None:
    global _FOREGROUND_DEPTH
    _FOREGROUND_DEPTH += 1
    if workspace is not None:
        key = str(workspace)
        _FOREGROUND_BY_WORKSPACE[key] = _FOREGROUND_BY_WORKSPACE.get(key, 0) + 1


def _foreground_finished(workspace: Path | None = None) -> None:
    global _FOREGROUND_DEPTH
    _FOREGROUND_DEPTH = max(0, _FOREGROUND_DEPTH - 1)
    if workspace is None:
        return
    key = str(workspace)
    left = max(0, _FOREGROUND_BY_WORKSPACE.get(key, 0) - 1)
    if left:
        _FOREGROUND_BY_WORKSPACE[key] = left
    else:
        _FOREGROUND_BY_WORKSPACE.pop(key, None)


def workspace_foreground_active(workspace: Path | str) -> bool:
    """该工作区（会话）当前是否有前台执行在跑。"""
    return _FOREGROUND_BY_WORKSPACE.get(str(workspace), 0) > 0


@contextlib.asynccontextmanager
async def _background_slot() -> Any:
    """后台低优先槽：前台活跃时等待，且同进程只跑一个提炼调用。"""
    global _BACKGROUND_ACTIVE, _BACKGROUND_COOLDOWN_UNTIL
    while (_FOREGROUND_DEPTH > 0 or _BACKGROUND_ACTIVE > 0
           or time.monotonic() < _BACKGROUND_COOLDOWN_UNTIL):
        await asyncio.sleep(1.0)
    _BACKGROUND_ACTIVE += 1
    try:
        yield
    finally:
        _BACKGROUND_ACTIVE = max(0, _BACKGROUND_ACTIVE - 1)
        _BACKGROUND_COOLDOWN_UNTIL = time.monotonic() + _BACKGROUND_COOLDOWN_SECONDS
