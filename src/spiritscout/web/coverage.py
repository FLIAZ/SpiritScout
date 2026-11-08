"""按资产、身份和测试方向只读投影探索记录，证据可定位不等于结论成立。"""
from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from spiritscout.blackboard.provenance import evidence_view, resolve_source
from spiritscout.core.audit import sanitize_record
from spiritscout.core.storage_codec import unpack_json
from spiritscout.web.deliverables import artifact_path

STATUSES = {'untested': '未见尝试', 'running': '进行中', 'blocked': '受阻',
            'pending': '待验证', 'refuted': '本次未复现', 'confirmed': '已独立确认'}
_LIMIT = 5000


def _refs(data: dict, key: str) -> list[str]:
    values = data.get(key, [])
    return [v for v in values if isinstance(v, str) and v][:100] if isinstance(values, list) else []


def _evidence(conn: sqlite3.Connection, workspace: Path, ref: str, *, detail: bool = False) -> dict:
    try:
        file = artifact_path(workspace, ref)
    except (ValueError, OSError):
        file = None
    if file:
        return {'ref': ref, 'resolved': True, 'kind': 'file', 'file': ref, 'state': 'available'}
    source = resolve_source(conn, ref)
    result = {**source, 'state': 'available' if source.get('resolved') else 'missing'}
    if source.get('kind') == 'blackboard':
        if source['layer'] == 'credential':
            return {'ref': ref, 'resolved': False, 'state': 'restricted'}
        row = conn.execute('SELECT data,version FROM bb_records WHERE layer=? AND record_id=?',
                           (source['layer'], source['record_id'])).fetchone()
        version = ref.rsplit('@', 1)[-1] if '@' in ref else ''
        if not row or row['version'] != source['version'] or version and not version.isdigit():
            return {**result, 'resolved': False, 'state': 'changed'}
        data = evidence_view(conn, unpack_json(row['data']))
        result.update(evidence_state=data.get('evidence_state', 'unverified'),
                      confirmed=(source['layer'] == 'vuln' and data.get('status') == 'accepted'
                                 and data.get('verification', {}).get('verdict') == 'confirmed'
                                 and data.get('evidence_state') == 'independently_verified'))
        if detail:
            result['data'] = data
    return result


def read_evidence(workspace: Path, ref: str) -> dict:
    db = workspace / 'bb.db'
    if not db.is_file():
        return {'ref': ref, 'resolved': False, 'state': 'missing'}
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='bb_records'").fetchone():
            return {'ref': ref, 'resolved': False, 'state': 'missing'}
        result = _evidence(conn, workspace, ref, detail=True)
    return sanitize_record({'detail': result})['detail']


def read_coverage(workspace: Path, *, query: str = '', status: str = '', identity: str = '',
                  test_type: str = '', offset: int = 0, limit: int = 50) -> dict[str, Any]:
    if status and status not in STATUSES:
        raise ValueError('未知覆盖状态')
    empty = {'items': [], 'total': 0, 'counts': {}, 'identities': [], 'test_types': [],
             'truncated': False, 'has_more': False}
    db = workspace / 'bb.db'
    if not db.is_file():
        return empty
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='bb_records'").fetchone():
            return empty
        # 单次一致快照；不写黑板，不按 URL 或时间推断身份、结论或图关系。
        conn.execute('BEGIN')
        source = conn.execute("SELECT layer,record_id,version,data FROM bb_records "
                              "WHERE layer IN ('asset','topology','vuln') ORDER BY updated_at DESC,layer,record_id LIMIT ?",
                              (_LIMIT + 1,)).fetchall()
        records = {f"{r['layer']}:{r['record_id']}": {**dict(r), 'data': unpack_json(r['data'])} for r in source[:_LIMIT]}
        assets = {key: row for key, row in records.items() if row['layer'] == 'asset' or
                  row['data'].get('kind') == 'finding' and row['data'].get('type') == 'asset'}
        findings: dict[str, list[str]] = {}
        for key, row in records.items():
            for ref in _refs(row['data'], 'exploration_refs'):
                findings.setdefault(ref.split('@')[0], []).extend([key + '@' + str(row['version']),
                                                                  *_refs(row['data'], 'evidence_refs')])
        rows, touched = [], set()
        checked: dict[str, dict] = {}

        def evidence(ref: str) -> dict:
            if ref not in checked:
                checked[ref] = _evidence(conn, workspace, ref)
            return checked[ref]

        for key, row in records.items():
            data = row['data']
            if data.get('kind') != 'exploration':
                continue
            refs = list(dict.fromkeys(_refs(data, 'evidence_refs') + findings.get(key, [])))[:100]
            node_status = {'open': 'untested', 'running': 'running', 'blocked': 'blocked',
                           'refuted': 'refuted'}.get(data.get('state'), 'pending')
            sources = [evidence(ref) for ref in refs]
            if any(e.get('confirmed') for e in sources):
                node_status = 'confirmed'
            for asset in _refs(data, 'asset_refs') or ['']:
                asset_key = asset.split('@')[0]
                touched.add(asset_key)
                asset_data = assets.get(asset_key, {}).get('data', {})
                rows.append({'ref': key + '@' + str(row['version']), 'asset_ref': asset,
                    'asset': str(asset_data.get('value') or asset_data.get('text') or asset or '未关联资产'),
                    'identity': str(data.get('identity') or ''), 'test_type': str(data.get('test_type') or ''),
                    'status': node_status, 'text': str(data.get('text') or ''), 'reason': str(data.get('reason') or ''),
                    'evidence': sources, 'evidence_missing': sum(not e.get('resolved') for e in sources)})
        for key, row in assets.items():
            if key not in touched:
                rows.append({'ref': key + '@' + str(row['version']), 'asset_ref': key,
                    'asset': str(row['data'].get('value') or row['data'].get('text') or key),
                    'identity': '', 'test_type': '', 'status': 'untested', 'text': '未见关联探索记录',
                    'reason': '', 'evidence': [], 'evidence_missing': 0})
        rows.sort(key=lambda row: (row['asset'], row['identity'], row['test_type'], row['ref']))
        counts = dict(Counter(row['status'] for row in rows))
        identities = sorted({row['identity'] for row in rows if row['identity']})
        types = sorted({row['test_type'] for row in rows if row['test_type']})
        rows = [row for row in rows if (not status or row['status'] == status)
                and (not identity or row['identity'] == identity) and (not test_type or row['test_type'] == test_type)
                and (not query or query.casefold() in ' '.join(str(row[k]) for k in ('asset', 'identity', 'test_type', 'text', 'reason')).casefold())]
    return sanitize_record({'detail': {'items': rows[offset:offset + limit], 'total': len(rows),
        'counts': counts, 'identities': identities, 'test_types': types,
        'truncated': len(source) > _LIMIT, 'has_more': offset + limit < len(rows)}})['detail']
