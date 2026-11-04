"""字符近似候选与可恢复整理；不调用模型，不删除或覆盖任何原件。"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import closing
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from spiritscout.core.ids import new_id
from spiritscout.knowledge.experience import (
    _CONTEXT_CAP,
    _INSIGHT_CAP,
    ExperienceStore,
    _with_quality,
)

DEFAULT_THRESHOLD = 0.90
MAX_GROUP = 6
PREVIEW_SECONDS = 900


def normalize(text: str) -> str:
    return re.sub(r'[\W_]+', '', str(text or '').casefold())


def similarity(left: str, right: str) -> float:
    """归一化字符相似度，不代表语义等价或场景相同。"""
    a, b = normalize(left), normalize(right)
    return SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0


def threshold_value(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.5 <= number <= 1:
        raise ValueError('字符相似度阈值必须在 0.5–1.0 之间')
    return number


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _unique(values: list[Any]) -> list[Any]:
    out = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def compose(items: list[dict[str, Any]]) -> dict[str, Any]:
    """只压缩完全相同的原文；不同限定条件完整保留，超限拒绝而非截断。"""
    insight = '；另：'.join(_unique([item['insight'] for item in items]))
    context = '；'.join(_unique([item['context'] for item in items if item['context']]))
    tags = _unique([tag for item in items for tag in item['tags']])
    if len(insight) > _INSIGHT_CAP or len(context) > _CONTEXT_CAP or len(tags) > 8:
        raise ValueError('完整合并稿超过现有长度或标签上限；请缩小分组，原件不变')
    sources = _unique([source for item in items for source in item['sources']])
    statuses = {item['quality_status'] for item in items}
    # 混合质量不提升；原件每条的质量、渠道和来源另有完整快照。
    status = next(iter(statuses)) if len(statuses) == 1 else 'candidate'
    if status == 'referenced' and not all(item['sources'] for item in items):
        status = 'candidate'
    sessions = _unique([item['source_session'] for item in items])
    return {'insight': insight, 'context': context, 'tags': tags,
            'sources': sources, 'quality_status': status,
            'source_session': sessions[0] if len(sessions) == 1 else '',
            'source_model': 'manual-merge'}


def candidates(db_path: str | Path, *, threshold: float = DEFAULT_THRESHOLD) -> dict[str, Any]:
    threshold = threshold_value(threshold)
    result: dict[str, Any] = {'groups': [], 'threshold': threshold,
                              'comparison': 'normalized_characters', 'scanned': 0,
                              'limit': 200, 'has_more': False}
    path = Path(db_path)
    if not path.exists():
        return result
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        try:
            rows = conn.execute('SELECT * FROM insights ORDER BY ts DESC,insight_id LIMIT 201').fetchall()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return result
            raise
        result['has_more'] = len(rows) > 200
        items = [item for item in _with_quality(conn, rows[:200])
                 if not item.get('merged_into') and not item.get('merged_from')]
    result['scanned'] = len(items)
    used: set[str] = set()
    for index, seed in enumerate(items):
        if seed['insight_id'] in used:
            continue
        group = [seed]
        for other in items[index + 1:]:
            if other['insight_id'] in used:
                continue
            if all(similarity(member['insight'], other['insight']) >= threshold for member in group):
                group.append(other)
                if len(group) == MAX_GROUP:
                    break
        if len(group) < 2:
            continue
        used.update(item['insight_id'] for item in group)
        score = min(similarity(a['insight'], b['insight']) for i, a in enumerate(group) for b in group[i + 1:])
        result['groups'].append({'items': group, 'score': round(score, 4)})
        if len(result['groups']) == 20:
            break
    return result


def _load_originals(conn: sqlite3.Connection, ids: list[str]) -> list[dict[str, Any]]:
    if not 2 <= len(ids) <= MAX_GROUP or len(set(ids)) != len(ids) or not all(isinstance(i, str) for i in ids):
        raise ValueError('请选择 2–6 条不同的原件')
    rows = conn.execute(f"SELECT * FROM insights WHERE insight_id IN ({','.join('?' for _ in ids)})", ids).fetchall()
    by_id = {item['insight_id']: item for item in _with_quality(conn, rows)}
    if len(by_id) != len(ids):
        raise ValueError('原件不存在，请刷新候选')
    items = [by_id[key] for key in ids]
    if any(item.get('merged_into') or item.get('merged_from') for item in items):
        raise ValueError('原件已被整理，请刷新或先恢复')
    return items


def preview(db_path: str | Path, ids: list[str]) -> dict[str, Any]:
    store = ExperienceStore(db_path)
    try:
        conn = store._conn
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            items = _load_originals(conn, ids)
            draft = compose(items)
            token = new_id('preview')
            conn.execute('DELETE FROM insight_merge_previews WHERE created_at < ?', (time.time() - PREVIEW_SECONDS,))
            conn.execute('INSERT INTO insight_merge_previews VALUES(?,?,?,?,0)',
                         (token, _dump(items), _dump(draft), time.time()))
        return {'token': token, 'items': items, 'draft': draft, 'expires_in': PREVIEW_SECONDS,
                'comparison': 'normalized_characters', 'requires_confirmation': True}
    finally:
        store.close()


def merge(db_path: str | Path, token: str, *, confirmed: bool = False) -> dict[str, Any]:
    if confirmed is not True:
        raise ValueError('必须预览并显式确认保留合并稿')
    store = ExperienceStore(db_path)
    try:
        conn = store._conn
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT snapshots,draft,created_at,consumed FROM insight_merge_previews WHERE token=?', (token,)).fetchone()
            if not row or row[3] or time.time() - row[2] > PREVIEW_SECONDS:
                raise ValueError('预览已过期或已使用，请重新预览')
            snapshots, draft = json.loads(row[0]), json.loads(row[1])
            ids = [item['insight_id'] for item in snapshots]
            current = _load_originals(conn, ids)
            if _dump(current) != row[0]:
                raise ValueError('原件或质量来源已变化，请重新预览')
            eid = new_id('exp')
            now = time.time()
            conn.execute('INSERT INTO insights VALUES(?,?,?,?,?,?,?)', (
                eid, draft['insight'], draft['context'], _dump(draft['tags']),
                draft['source_session'], draft['source_model'], now))
            conn.execute('INSERT INTO insight_quality VALUES(?,?,?)', (eid, draft['quality_status'], _dump(draft['sources'])))
            conn.execute('INSERT INTO insight_merges VALUES(?,?,?,?,NULL)', (eid, _dump(ids), row[0], now))
            conn.executemany('INSERT INTO insight_hidden VALUES(?,?)', [(key, eid) for key in ids])
            conn.execute('UPDATE insight_merge_previews SET consumed=1 WHERE token=?', (token,))
        return {'result': 'ok', 'merged_id': eid, 'original_ids': ids, 'originals_preserved': True}
    finally:
        store.close()


def restore(db_path: str | Path, merged_id: str) -> dict[str, Any]:
    store = ExperienceStore(db_path)
    try:
        conn = store._conn
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT original_ids,restored_at FROM insight_merges WHERE merged_id=?', (merged_id,)).fetchone()
            if not row:
                raise ValueError('没有找到整理记录')
            ids = json.loads(row[0])
            if row[1] is None:
                conn.execute('DELETE FROM insight_hidden WHERE merged_into=?', (merged_id,))
                conn.execute('INSERT INTO insight_hidden VALUES(?,?)', (merged_id, f'restored:{merged_id}'))
                conn.execute('UPDATE insight_merges SET restored_at=? WHERE merged_id=?', (time.time(), merged_id))
        return {'result': 'restored', 'merged_id': merged_id, 'original_ids': ids,
                'originals_preserved': True}
    finally:
        store.close()
