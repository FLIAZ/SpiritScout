"""会话档案：从持久化黑板与产出目录读取，不从事件流猜测事实。"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any, BinaryIO

from spiritscout.core.audit import sanitize_record, tool_preview

_KINDS = {"fact", "hypothesis", "evidence", "finding", "asset", "file", "outcome"}


def open_artifact(workspace: Path, name: str) -> BinaryIO:
    """通过目录描述符逐层打开，拒绝检查后被替换为链接的竞态。"""
    parts = Path(name).parts
    if (Path(name).is_absolute() or not parts or parts[0] not in {"artifacts", "reports"}
            or any(p.startswith(".") for p in parts)):
        raise FileNotFoundError(name)
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise FileNotFoundError(name)
        return os.fdopen(fd, "rb")
    finally:
        os.close(directory)


def _evidence(value: dict[str, Any]) -> dict[str, Any]:
    """引用索引仅公开定位信息，URL 查询值和请求内容不进入档案。"""
    result = {key: value[key] for key in ("id", "record_id", "kind", "method", "status_code", "note", "title")
              if key in value}
    if value.get("url"):
        result["url"] = tool_preview({"url": value["url"]})
    return result


def artifact_path(workspace: Path, name: str) -> Path | None:
    """仅允许本会话产出与报告；逐级拒绝符号链接及目录穿越。"""
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or relative.parts[0] not in {"artifacts", "reports"}:
        return None
    if any(part in {".", ".."} or part.startswith(".") for part in relative.parts):
        return None
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    if not current.is_file() or not current.resolve().is_relative_to(workspace.resolve()):
        return None
    return current


def _files(workspace: Path) -> list[dict[str, Any]]:
    items = []
    for folder in ("artifacts", "reports"):
        root = workspace / folder
        if not root.is_dir() or root.is_symlink():
            continue
        for parent, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                             and not (Path(parent) / d).is_symlink())
            for name in sorted(names):
                path = Path(parent) / name
                relative = path.relative_to(workspace).as_posix()
                try:
                    if artifact_path(workspace, relative) is None:
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                items.append({"id": f"file:{relative}", "kind": "file", "title": name,
                              "text": relative, "path": relative, "size": stat.st_size,
                              "source": folder, "updated_at": stat.st_mtime,
                              "status": "已保存", "refs": []})
    return items


def read_deliverables(workspace: Path, *, kind: str = "all", query: str = "",
                      offset: int = 0, limit: int = 60) -> dict[str, Any]:
    """只读已有数据库，返回完整分类计数与独立分页，不受状态页采样影响。"""
    if kind != "all" and kind not in _KINDS:
        raise ValueError("未知档案分类")
    items: list[dict[str, Any]] = []
    db = workspace / "bb.db"
    if db.is_file():
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='bb_records'").fetchone()
            rows = conn.execute("SELECT * FROM bb_records WHERE layer IN "
                                "('topology','asset','vuln','recon_clue','task_state') "
                                "ORDER BY updated_at DESC,record_id").fetchall() if exists else []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            origin = {"record_id": row["record_id"], "layer": row["layer"],
                      "version": row["version"], "source": row["updated_by"],
                      "updated_at": row["updated_at"]}
            def add(category: str, key: str, title: Any, text: Any, *, _origin: dict[str, Any] = origin, **extra: Any) -> None:
                items.append({**_origin, "id": f'{_origin["layer"]}:{_origin["record_id"]}:{key}',
                              "kind": category, "title": str(title or "未命名条目"),
                              "text": str(text or ""), "refs": [], **extra})
            if row["record_id"] == "business-context":
                evidence = {str(e.get("id")): _evidence(e) for e in data.get("evidence", []) if isinstance(e, dict)}
                for category, field in (("fact", "facts"), ("hypothesis", "hypotheses")):
                    for index, value in enumerate(data.get(field, [])):
                        if not isinstance(value, dict):
                            continue
                        refs = [ref for ref in value.get("evidence_refs", []) if ref in evidence]
                        supported = category == "fact" and bool(refs)
                        add("fact" if supported else "hypothesis", f"{field}:{index}",
                            value.get("statement"), value.get("statement"),
                            status="有证据引用" if supported else "待验证",
                            refs=[{**evidence[ref], "id": ref} for ref in refs])
                for ref, value in evidence.items():
                    add("evidence", ref, value.get("title") or value.get("url") or ref,
                        value.get("note") or value.get("kind"), status="已观测",
                        refs=[value], target=tool_preview({"url": value.get("url")}))
                for index, gap in enumerate(data.get("gaps", [])):
                    add("hypothesis", f"gap:{index}", gap, gap, status="信息缺口")
            elif data.get("kind") == "finding":
                text = data.get("text") or data.get("note") or data.get("value") or data.get("summary")
                add("finding", "finding", data.get("title") or text, text,
                    status="待核验", target=tool_preview({"url": data.get("url")}))
            elif row["layer"] == "asset":
                value = data.get("value")
                add("asset", "asset", tool_preview({"url": value}) or value, data.get("asset_type"),
                    status="已登记", target=tool_preview({"url": data.get("value")}))
            elif row["layer"] == "vuln":
                add("finding", "vuln", data.get("title"), data.get("description") or data.get("vuln_type"),
                    status=str(data.get("status") or "candidate"),
                    target=tool_preview({"url": data.get("endpoint")}))
            elif row["layer"] == "recon_clue":
                category = "evidence" if data.get("clue_type") == "user_reply" else "finding"
                add(category, "clue", data.get("detail") or data.get("clue_type"),
                    data.get("value"), status="用户补充" if category == "evidence" else "待核验")
            elif row["layer"] == "task_state":
                task = data.get("task") or {}
                if task.get("result_summary"):
                    add("outcome", "task", (task.get("payload") or {}).get("node_title")
                        or task.get("task_type"), task["result_summary"],
                        status=str(task.get("status") or "未记录状态"))
    items.extend(_files(workspace))
    # 旧记录也走脱敏口，响应只含白名单投影，不返回原始请求体和头部。
    items = sanitize_record({"detail": {"items": items}})["detail"]["items"]
    counts = {category: sum(item["kind"] == category for item in items) for category in sorted(_KINDS)}
    needle = query.strip().casefold()
    filtered = [item for item in items if (kind == "all" or item["kind"] == kind)
                and (not needle or needle in json.dumps(item, ensure_ascii=False).casefold())]
    return {"items": filtered[offset:offset + limit], "total": len(filtered), "counts": counts,
            "offset": offset, "limit": limit, "has_more": offset + limit < len(filtered)}
