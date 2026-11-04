"""会话经验的共享存储与检索。

会话结束后提炼适用场景和经验，保存在 experience.root/experience.db。
独立于会话工作区，供后续会话检索。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.exceptions import SpiritScoutError
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


def _visible_clause(conn: sqlite3.Connection, alias: str = "insights") -> str:
    """只读兼容旧库；整理隐藏另存，不改变质量状态。"""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='insight_hidden'").fetchone():
        return f"NOT EXISTS (SELECT 1 FROM insight_hidden h WHERE h.insight_id={alias}.insight_id)"
    return "1=1"


def _with_quality(conn: sqlite3.Connection, rows: list[tuple]) -> list[dict[str, Any]]:
    """旧库只读兼容；引用仅证明来源可追溯，不把经验标成已验证规律。"""
    items = [_normalize(row) for row in rows]
    metadata = {}
    if items and conn.execute("SELECT 1 FROM sqlite_master WHERE name='insight_quality'").fetchone():
        for start in range(0, len(items), 400):
            batch = items[start:start + 400]
            slots = ','.join('?' for _ in batch)
            metadata.update({key: (status, refs) for key, status, refs in conn.execute(
                f'SELECT insight_id,status,sources FROM insight_quality WHERE insight_id IN ({slots})',
                [item['insight_id'] for item in batch])})
    for item in items:
        status, raw = metadata.get(item['insight_id'], ('legacy', '[]'))
        try:
            sources = json.loads(raw)
        except (ValueError, TypeError):
            sources = []
        item.update(quality_status=status, sources=[s for s in sources if isinstance(s, dict)] if isinstance(sources, list) else [])
    if items and conn.execute("SELECT 1 FROM sqlite_master WHERE name='insight_hidden'").fetchone():
        hidden = dict(conn.execute('SELECT insight_id,merged_into FROM insight_hidden'))
        merges = {key: json.loads(raw) for key, raw in conn.execute(
            "SELECT merged_id,original_ids FROM insight_merges WHERE restored_at IS NULL")}
        for item in items:
            item['merged_into'] = hidden.get(item['insight_id'], '')
            item['merged_from'] = merges.get(item['insight_id'], [])
    return items


def _terms(text: str) -> set[str]:
    """本地词项匹配，仅用于检索排序，不分类任务或规定解题路线。"""
    words = set(re.findall(r'[a-z][a-z0-9_+-]{2,}', text.casefold()))
    for phrase in re.findall(r'[\u4e00-\u9fff]+', text):
        words.update(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return words - {'测试', '安全', '任务', '进行', '分析', '需要', '可以', '目标', '发现',
                    '场景', '观察', '结论', '经验', 'http', 'https', 'www', 'com'}


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
            label = '历史观察，需核验，来源' if item.get('quality_status') == 'referenced' else '来源'
            extras.append(f"{label}：{source[:max_source]}")
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
            visible = _visible_clause(conn)
            total = conn.execute(f"SELECT COUNT(*) FROM insights WHERE {visible}").fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM insights WHERE {visible} ORDER BY ts DESC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
            items = _with_quality(conn, rows)
        finally:
            conn.close()
    except sqlite3.Error:
        return {"total": 0, "items": []}
    return {"total": int(total), "items": items}


def read_page(db_path: str | Path, *, q: str = "", tag: str = "",
              limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """经验提炼页数据：过滤分页 + 标签分布/来源统计（只读，坏库返回空态）。

    q 关键词命中 insight/context/tags 任意字段；tag 精确匹配一个标签
    （标签以 JSON 数组存储，LIKE 匹配带引号串避免前缀误命中）。
    """
    empty: dict[str, Any] = {"total": 0, "items": [], "has_more": False,
                             "tags": [], "sources": 0, "last_ts": None}
    path = Path(db_path)
    if not path.exists():
        return empty
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            where, args = [_visible_clause(conn)], []
            if q.strip():
                where.append("(insight LIKE ? OR context LIKE ? OR tags LIKE ?)")
                like = f"%{q.strip()}%"
                args += [like, like, like]
            if tag.strip():
                where.append("tags LIKE ?")
                args += [f'%"{tag.strip()}"%']
            clause = f" WHERE {' AND '.join(where)}" if where else ""
            total = conn.execute(
                f"SELECT COUNT(*) FROM insights{clause}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM insights{clause}"
                " ORDER BY ts DESC LIMIT ? OFFSET ?",
                [*args, max(1, int(limit)), max(0, int(offset))]).fetchall()
            # 标签分布与来源数始终对全量统计（过滤只作用于列表）
            tag_rows = conn.execute(
                f"SELECT tags FROM insights WHERE {_visible_clause(conn)}").fetchall()
            src_row = conn.execute(
                "SELECT COUNT(DISTINCT source_session), MAX(ts)"
                f" FROM insights WHERE {_visible_clause(conn)}").fetchone()
            items = _with_quality(conn, rows)
        finally:
            conn.close()
    except sqlite3.Error:
        return empty
    counter: dict[str, int] = {}
    for (raw,) in tag_rows:
        try:
            tags = json.loads(raw or "[]")
        except (TypeError, ValueError):
            tags = []
        if isinstance(tags, list):
            for item in tags:
                counter[str(item)] = counter.get(str(item), 0) + 1
    return {
        "total": int(total), "items": items,
        "has_more": offset + len(items) < int(total),
        "tags": [{"tag": t, "count": c} for t, c in
                 sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))],
        "sources": int(src_row[0] or 0),
        "last_ts": float(src_row[1]) if src_row and src_row[1] else None,
    }


def delete_insight(db_path: str | Path, insight_id: str, *,
                   audit: Any = None) -> dict[str, Any] | None:
    """人工审核删除：按 ID 删一条经验，返回被删快照（未命中返回 None）。

    经验是模型产出、用户随时可审的仓库——提炼跑偏（如把单题解题步骤
    当成可复用事实）时用户可就地清理；删除动作经 audit 留痕（actor=user）。
    库故障（锁/损坏）显式抛出：不能假装「未命中」，让用户以为条目已删。
    """
    path = Path(db_path)
    if not path.exists() or not str(insight_id or "").strip():
        return None
    try:
        conn = sqlite3.connect(str(path))
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute(
                    "SELECT * FROM insights WHERE insight_id = ?",
                    (insight_id,)).fetchone()
                if row is None:
                    return None
                item = _with_quality(conn, [row])[0]
                if item.get('merged_into') or item.get('merged_from'):
                    raise SpiritScoutError("整理中的原件和合并稿不可删除；请先恢复原件")
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='insight_quality'").fetchone():
                    conn.execute('DELETE FROM insight_quality WHERE insight_id=?', (insight_id,))
                conn.execute("DELETE FROM insights WHERE insight_id = ?",
                             (insight_id,))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SpiritScoutError(
            f"经验库操作失败（{type(exc).__name__}）: {str(exc)[:200]}") from exc
    if audit is not None:
        audit.log("experience.deleted", "user", {
            "insight_id": insight_id,
            "insight": str(item.get("insight") or "")[:200]})
    return item


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
        self._conn.execute("""CREATE TABLE IF NOT EXISTS insight_quality (
            insight_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            sources TEXT NOT NULL DEFAULT '[]'
        )""")
        self._conn.execute("""CREATE TABLE IF NOT EXISTS insight_hidden (
            insight_id TEXT PRIMARY KEY, merged_into TEXT NOT NULL
        )""")
        self._conn.execute("""CREATE TABLE IF NOT EXISTS insight_merges (
            merged_id TEXT PRIMARY KEY, original_ids TEXT NOT NULL,
            snapshots TEXT NOT NULL, created_at REAL NOT NULL, restored_at REAL
        )""")
        self._conn.execute("""CREATE TABLE IF NOT EXISTS insight_merge_previews (
            token TEXT PRIMARY KEY, snapshots TEXT NOT NULL, draft TEXT NOT NULL,
            created_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
        )""")
        self._conn.commit()

    def add(self, insight: str, *, context: str = "",
            tags: list[str] | None = None,
            source_session: str = "", source_model: str = "",
            sources: list[dict[str, Any]] | None = None) -> str:
        """写入经验；同文同场景才去重，字符近似只供人工整理候选。"""
        text = insight.strip()[:_INSIGHT_CAP]
        if not text:
            return ""
        sources = [{'ref': source['ref'], 'source_session': source_session,
                    'evidence_state': source.get('evidence_state', 'unverified')}
                   for source in (sources or []) if isinstance(source, dict)
                   and isinstance(source.get('ref'), str) and source_session
                   and source.get('source_session') == source_session
                   and isinstance(source.get('evidence_state', 'unverified'), str)
                   and source.get('evidence_state') not in {'conflicting', 'expired'}][:5]
        dup = self._conn.execute(
            "SELECT insight_id FROM insights WHERE insight = ? AND context = ? LIMIT 1",
            (text, context.strip()[:_CONTEXT_CAP])).fetchone()
        if dup:
            # 同一句话后来补齐来源可进入相关召回，已撤回／过期记录不自动复活。
            if sources:
                prior = self._conn.execute('SELECT status,sources FROM insight_quality WHERE insight_id=?',
                                           (dup[0],)).fetchone()
                if prior and prior[0] == 'referenced':
                    try:
                        existing = json.loads(prior[1])
                    except (ValueError, TypeError):
                        existing = []
                    combined = existing if isinstance(existing, list) else []
                    for source in sources:
                        if source not in combined:
                            combined.append(source)
                    self._conn.execute('UPDATE insight_quality SET sources=? WHERE insight_id=?',
                                       (json.dumps(combined, ensure_ascii=False), dup[0]))
                changed = self._conn.execute("INSERT INTO insight_quality VALUES(?, 'referenced', ?) "
                                             "ON CONFLICT(insight_id) DO UPDATE SET status='referenced',sources=excluded.sources "
                                             "WHERE insight_quality.status IN ('candidate','legacy')",
                                             (dup[0], json.dumps(sources, ensure_ascii=False))).rowcount
                if changed:
                    self._conn.execute("UPDATE insights SET source_session=? WHERE insight_id=?",
                                       (source_session[:200], dup[0]))
                self._conn.commit()
            self.audit.log("experience.duplicate", "master", {
                "insight_id": dup[0], "insight": text[:200]})
            return ""
        eid = new_id("exp")
        self._conn.execute(
            "INSERT OR IGNORE INTO insights VALUES (?,?,?,?,?,?,?)",
            (eid, text, context.strip()[:_CONTEXT_CAP],
             json.dumps([str(tag) for tag in (tags or [])][:8], ensure_ascii=False),
             source_session[:200], source_model, time.time()))
        self._conn.execute('INSERT INTO insight_quality VALUES(?,?,?)',
                           (eid, 'referenced' if sources else 'candidate', json.dumps(sources, ensure_ascii=False)))
        self._conn.commit()
        self.audit.log("experience.added", "master", {
            "insight_id": eid, "insight": text[:200]})
        return eid

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """关键词检索（简化版 FTS：LIKE 匹配 context + insight + tags）。"""
        q = f"%{query.strip()}%"
        rows = self._conn.execute(
            f"SELECT * FROM insights WHERE {_visible_clause(self._conn)} AND (insight LIKE ? OR context LIKE ? "
            "OR tags LIKE ?) ORDER BY ts DESC LIMIT ?",
            (q, q, q, limit)).fetchall()
        return _with_quality(self._conn, rows)

    def recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            f"SELECT * FROM insights WHERE {_visible_clause(self._conn)} ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return _with_quality(self._conn, rows)

    def recall(self, query: str, *, limit: int = 6,
               source_validator: Callable[[dict[str, Any]], bool] | None = None) -> list[dict[str, Any]]:
        """只自动召回有引用且相关的历史观察；同一输入下按分数、身份稳定排序。"""
        terms = _terms(query)
        if not terms:
            return []
        rows = self._conn.execute("SELECT i.* FROM insights i JOIN insight_quality q USING(insight_id) "
                                  f"WHERE q.status='referenced' AND {_visible_clause(self._conn, 'i')}").fetchall()
        ranked = []
        for item in _with_quality(self._conn, rows):
            if not item['sources']:
                continue
            context = _terms(' '.join(item['tags']) + ' ' + item['context'])
            body = _terms(item['insight'])
            score = len(terms & context) * 3 + len(terms & body)
            if score:
                ranked.append((score, item['insight_id'], item))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        selected = []
        for _, _, item in ranked:
            if len(selected) >= max(0, limit):
                break
            if source_validator is None or any(source_validator(source) for source in item['sources']):
                selected.append(item)
        return selected

    def count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM insights").fetchone()[0])

    def close(self):
        self._conn.close()
