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
            elif data.get("kind") == "secretary":
                decisions = {item["id"]: item for item in data.get("decisions", []) if isinstance(item, dict) and "id" in item}
                for item in data.get("suggestions", []):
                    decision = decisions.get(item["id"], {})
                    add("outcome", item["id"], "参谋建议：" + item.get("summary", ""),
                        decision.get("reason") or item.get("benefit"),
                        status={"accepted": "已采纳", "partial": "部分采纳", "rejected": "未采纳", "needs_evidence": "待补证"}.get(decision.get("verdict"), "待评估"),
                        refs=[{"id": ref, "kind": "版本化记录"} for ref in item.get("evidence_refs", [])])
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


def read_record_history(workspace: Path, layer: str, record_id: str,
                        before: int = 0, limit: int = 20) -> dict[str, Any]:
    """只读分页历史；升级前未留存的旧版本不伪造，现值不自动作为历史事实。"""
    db = workspace / "bb.db"
    if not db.is_file():
        return {"items": [], "has_more": False}
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='bb_record_history'").fetchone():
            return {"items": [], "has_more": False}
        rows = conn.execute(
            "SELECT version,data,updated_by,updated_at FROM bb_record_history "
            "WHERE layer=? AND record_id=? AND (?=0 OR version<?) ORDER BY version DESC LIMIT ?",
            (layer, record_id, before, before, limit + 1)).fetchall()
    items = [{"version": row["version"], "updated_by": row["updated_by"],
              "updated_at": row["updated_at"], "data": json.loads(row["data"])} for row in rows[:limit]]
    return {"items": sanitize_record({"detail": {"items": items}})["detail"]["items"],
            "has_more": len(rows) > limit}


def read_vulnerability_page(workspace: Path, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """完整漏洞列表按页只读，与状态卡片的全量统计独立。"""
    db = workspace / "bb.db"
    if not db.is_file():
        return {"items": [], "total": 0}
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='bb_records'").fetchone():
            return {"items": [], "total": 0}
        total = conn.execute("SELECT COUNT(*) FROM bb_records WHERE layer='vuln'").fetchone()[0]
        rows = conn.execute("SELECT data FROM bb_records WHERE layer='vuln' "
                            "ORDER BY updated_at DESC,record_id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    fields = ("vuln_id", "title", "vuln_type", "severity", "status", "cvss_score", "poc_verified", "endpoint", "verification", "false_positive_reason")
    items = [{key: data.get(key) for key in fields} for row in rows if isinstance(data := json.loads(row[0]), dict)]
    return {"items": sanitize_record({"detail": {"items": items}})["detail"]["items"], "total": total}
