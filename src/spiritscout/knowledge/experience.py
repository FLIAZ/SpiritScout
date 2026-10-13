"""经验库：智能体实战经验的自我进化存储（与知识库分离）。

设计意图：知识库存的是静态参考（方法论条目/平台规则/载荷），
经验库存的是**实战中验证过的思维碎片**——不是解题步骤，而是
「这类场景下我学到了什么」的可复用洞察。每次会话收尾时灵枢
提炼，后续会话的智能体可检索前人经验。

数据隔离：全局单库（workspace/experience.db），跨会话持久。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.ids import new_id


class ExperienceStore:
    """全局经验库：写入（灵枢提炼）+ 检索（智能体查询）。"""

    def __init__(self, db_path: str | Path, *, audit=None):
        self.db_path = str(db_path)
        self.audit = audit or NullAuditLogger()
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
        self._conn.commit()

    def add(self, insight: str, *, context: str = "",
            tags: list[str] | None = None,
            source_session: str = "", source_model: str = "") -> str:
        eid = new_id("exp")
        self._conn.execute(
            "INSERT OR IGNORE INTO insights VALUES (?,?,?,?,?,?,?)",
            (eid, insight[:600], context[:300],
             json.dumps(tags or []), source_session, source_model, time.time()))
        self._conn.commit()
        self.audit.log("experience.added", "master", {
            "insight_id": eid, "insight": insight[:200]})
        return eid

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """关键词检索（简化版 FTS：LIKE 匹配 context + insight + tags）。"""
        q = f"%{query.strip()}%"
        rows = self._conn.execute(
            "SELECT * FROM insights WHERE insight LIKE ? OR context LIKE ? "
            "OR tags LIKE ? ORDER BY ts DESC LIMIT ?",
            (q, q, q, limit)).fetchall()
        cols = ["insight_id", "insight", "context", "tags",
                "source_session", "source_model", "ts"]
        return [dict(zip(cols, r)) for r in rows]

    def recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM insights ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        cols = ["insight_id", "insight", "context", "tags",
                "source_session", "source_model", "ts"]
        return [dict(zip(cols, r)) for r in rows]

    def close(self):
        self._conn.close()
