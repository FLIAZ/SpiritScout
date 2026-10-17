"""经验库：智能体实战经验的自我进化存储（与知识库分离）。

设计意图：知识库存的是静态参考（方法论条目/平台规则/载荷），
经验库存的是**实战中验证过的思维碎片**——不是解题步骤，而是
「这类场景下我学到了什么」的可复用洞察。会话收尾（含被中止/失败）
时灵枢提炼，后续会话的智能体开工时召回、执行中可经 kb_search 检索。

数据隔离：全局单库（experience.root/experience.db），跨会话持久。
Web 控制台在会话隔离时把 experience.root 指向共享目录（而非会话
工作区），保证提炼出的经验对后续所有会话可见。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.ids import new_id

_INSIGHT_CAP = 600
_CONTEXT_CAP = 300

_COLUMNS = ["insight_id", "insight", "context", "tags",
            "source_session", "source_model", "ts"]


def experience_db_path(root: str, workspace: str) -> Path:
    """经验库文件路径：root 非空用 root，否则跟随工作区（CLI 兼容语义）。"""
    base = Path(root) if str(root or "").strip() else Path(workspace)
    return base / "experience.db"


def _normalize(row: tuple[Any, ...]) -> dict[str, Any]:
    """行 → 字典；tags 从 JSON 串还原为列表（坏数据按空列表处理）。"""
    item = dict(zip(_COLUMNS, row))
    try:
        tags = json.loads(item.get("tags") or "[]")
    except (TypeError, ValueError):
        tags = []
    item["tags"] = [str(tag) for tag in tags] if isinstance(tags, list) else []
    return item


def format_insights(items: list[dict[str, Any]], *, max_insight: int = 240,
                    max_context: int = 120, max_source: int = 60) -> list[str]:
    """召回格式：``[标签] 洞察（适用：场景；来源：目标）``——标签帮模型判断同场景。"""
    lines: list[str] = []
    for item in items:
        text = str(item.get("insight") or "").strip()
        if not text:
            continue
        tags = "、".join(str(tag) for tag in (item.get("tags") or [])[:3])
        line = f"[{tags}] {text[:max_insight]}" if tags else text[:max_insight]
        extras: list[str] = []
        context = str(item.get("context") or "").strip()
        if context:
            extras.append(f"适用：{context[:max_context]}")
        source = str(item.get("source_session") or "").strip()
        if source:
            extras.append(f"来源：{source[:max_source]}")
        if extras:
            line += "（" + "；".join(extras) + "）"
        lines.append(line)
    return lines


def read_recent(db_path: str | Path, *, limit: int = 6) -> dict[str, Any]:
    """只读快照（面板/状态展示用）：不建表、不写盘，缺失或损坏返回空。"""
    path = Path(db_path)
    if not path.exists():
        return {"total": 0, "items": []}
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            total = conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM insights ORDER BY ts DESC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {"total": 0, "items": []}
    return {"total": int(total), "items": [_normalize(row) for row in rows]}


class ExperienceStore:
    """全局经验库：写入（灵枢提炼）+ 检索（智能体查询）。"""

    def __init__(self, db_path: str | Path, *, audit=None):
        self.db_path = str(db_path)
        self.audit = audit or NullAuditLogger()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("""CREATE TABLE IF NOT EXISTS insights (
            insight_id TEXT PRIMARY KEY,
            insight TEXT NOT NULL,
            context TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            source_session TEXT DEFAULT '',
            source_model TEXT DEFAULT '',
            ts REAL NOT NULL
        )""")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_insights_ts ON insights(ts DESC)")
        self._conn.commit()

    def add(self, insight: str, *, context: str = "",
            tags: list[str] | None = None,
            source_session: str = "", source_model: str = "") -> str:
        """写入一条经验；完全重复的洞察直接跳过（返回空串）。"""
        text = insight.strip()[:_INSIGHT_CAP]
        if not text:
            return ""
        dup = self._conn.execute(
            "SELECT insight_id FROM insights WHERE insight = ? LIMIT 1",
            (text,)).fetchone()
        if dup:
            self.audit.log("experience.duplicate", "master", {
                "insight_id": dup[0], "insight": text[:200]})
            return ""
        eid = new_id("exp")
        self._conn.execute(
            "INSERT OR IGNORE INTO insights VALUES (?,?,?,?,?,?,?)",
            (eid, text, context.strip()[:_CONTEXT_CAP],
             json.dumps([str(tag) for tag in (tags or [])][:8], ensure_ascii=False),
             source_session[:200], source_model, time.time()))
        self._conn.commit()
        self.audit.log("experience.added", "master", {
            "insight_id": eid, "insight": text[:200]})
        return eid

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """关键词检索（简化版 FTS：LIKE 匹配 context + insight + tags）。"""
        q = f"%{query.strip()}%"
        rows = self._conn.execute(
            "SELECT * FROM insights WHERE insight LIKE ? OR context LIKE ? "
            "OR tags LIKE ? ORDER BY ts DESC LIMIT ?",
            (q, q, q, limit)).fetchall()
        return [_normalize(row) for row in rows]

    def recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM insights ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [_normalize(row) for row in rows]

    def count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM insights").fetchone()[0])

    def close(self):
        self._conn.close()
