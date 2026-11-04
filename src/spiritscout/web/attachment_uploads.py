"""将暂存附件材料化到会话目录，文件夹保留结构且同名根目录不覆盖。"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def materialize_uploads(sources: list[Path], destination: Path, *, move: bool = False) -> list[dict[str, Any]]:
    destination.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for source in sources:
        dest = destination / source.name
        serial = 2
        while dest.exists():
            dest = destination / f"{source.stem}-{serial}{source.suffix}"
            serial += 1
        if source.is_dir():
            # 不跟随指向暂存目录外的符号链接。
            if any(p.is_symlink() for p in source.rglob("*")):
                raise ValueError("暂存附件包含符号链接")
            shutil.copytree(source, dest)
            files = sorted(p for p in dest.rglob("*") if p.is_file())
            if not files:
                files = [dest]  # 空目录也保留并向会话提供路径。
        else:
            with source.open("rb") as reader, dest.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
            files = [dest]
        for file in files:
            rel = file.relative_to(destination).as_posix()
            records.append({"kind": "attachment", "name": rel,
                            "path": str(file.resolve()), "rel": f"attachments/{rel}",
                            "size": file.stat().st_size if file.is_file() else 0})
        if move:
            if source.is_dir():
                shutil.rmtree(source)
            else:
                source.unlink()
    return records
