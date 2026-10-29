"""项目式技能的文本文件契约与参考正文投影。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


def project_body(files: Any) -> str:
    """文件是原文，合并正文只用于沿用现有知识检索。"""
    if not isinstance(files, dict) or not 1 <= len(files) <= 1000:
        raise ValueError("Skill 文件须为 1–1000 个相对路径到文本的映射")
    if not isinstance(files.get("SKILL.md"), str) or not files["SKILL.md"].strip():
        raise ValueError("Skill 根目录须包含非空 SKILL.md")
    total = 0
    for name, content in files.items():
        if not isinstance(name, str) or not name or len(name) > 1024:
            raise ValueError("Skill 文件路径无效")
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or str(path) != name
                or any(char in name for char in ("\\", ":", "\x00")) or name == "."):
            raise ValueError("Skill 文件须使用安全的相对路径")
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("Skill 文件须为不含 NUL 的文本")
        if any(str(parent) in files for parent in path.parents if str(parent) != "."):
            raise ValueError("Skill 文件路径与目录冲突")
        total += len(content)
        if total > 100_000:
            raise ValueError("Skill 文件文本合计不能超过 100000 字符")
    body = files["SKILL.md"]
    for name in sorted(files):
        if name != "SKILL.md":
            body += f"\n\n---\n\n## 包内参考文件：{name}\n\n{files[name]}"
    return body
