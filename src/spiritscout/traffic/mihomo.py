"""代理页和会话共用 Mihomo 运行时定位，不依赖 Web 进程的工作目录。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def executable() -> str | None:
    configured = os.environ.get('SPIRITSCOUT_MIHOMO')
    local = Path(__file__).resolve().parents[3] / '.tools' / 'mihomo'
    path = Path(configured).expanduser() if configured else local
    if path.is_file() and os.access(path, os.X_OK):
        return str(path.resolve())
    # 显式指定的无效路径不能静默替换成另一份内核。
    return None if configured else shutil.which('mihomo') or shutil.which('verge-mihomo')
