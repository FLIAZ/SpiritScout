"""沙箱目录身份：作用域规范化与不跟随链接的目录访问。"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def sandbox_scope(scope: str | None) -> str:
    """安全短 ID 保留旧路径；变形 ID 用原文哈希区分，避免不同题目共用目录。"""
    raw = str(scope or "")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")[:80]
    if raw == safe:
        return safe
    # ~ 不在可原样保留的字符集中，生成名不会与未变形的原始 ID 撞名。
    return f"{safe[:15]}~{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def sandbox_directory(scope: str | None) -> Path:
    safe = sandbox_scope(scope)
    return Path("sandbox") / safe if safe else Path("sandbox")


def sandbox_tmp_directory(scope: str | None) -> Path:
    # 控制父目录不能位于沙箱可写挂载内，否则可被模型替换后重新挂载。
    safe = sandbox_scope(scope)
    return Path(".sandbox-tmp") / ("topic-" + safe if safe else "session")


@contextmanager
def open_directory(root: Path, relative: Path, *, create: bool = False) -> Iterator[int]:
    """逐层固定目录描述符；调用者须在挂载和文件写入期间保持描述符打开。"""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("目录必须是工作区内的相对路径")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)
