"""事实来源由运行时记录；证据状态反映可追溯性，不替模型证明语义结论。"""
from __future__ import annotations

import copy
import sqlite3
from typing import Any

from spiritscout.core.models import AgentType, BlackboardLayer

_FACT_LAYERS = {BlackboardLayer.ASSET, BlackboardLayer.RECON_CLUE, BlackboardLayer.VULN,
                BlackboardLayer.CREDENTIAL}


def is_fact(layer: BlackboardLayer, data: dict[str, Any]) -> bool:
    return layer in _FACT_LAYERS or layer == BlackboardLayer.TOPOLOGY and data.get('kind') == 'finding'


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (name,)).fetchone() is not None


def invocation_source(conn: sqlite3.Connection, ref: str) -> dict[str, Any] | None:
    if not _table(conn, 'tool_invocations'):
        return None
    row = conn.execute('SELECT * FROM tool_invocations WHERE invocation_id=?', (ref,)).fetchone()
    if row is None and _table(conn, 'tool_provenance'):
        matches = conn.execute('SELECT i.* FROM tool_invocations i JOIN tool_provenance p '
                               'ON i.invocation_id=p.invocation_id WHERE p.tool_call_id=? LIMIT 2', (ref,)).fetchall()
        row = matches[0] if len(matches) == 1 else None
    if row is None:
        return None
    seen: set[str] = set()
    reused_from = ''
    detail: dict[str, Any] = {}
    while row['invocation_id'] not in seen:
        seen.add(row['invocation_id'])
        meta = conn.execute('SELECT * FROM tool_provenance WHERE invocation_id=?',
                            (row['invocation_id'],)).fetchone() if _table(conn, 'tool_provenance') else None
        detail = dict(meta) if meta else {}
        source = detail.get('source_invocation_id')
        if not source:
            break
        original = conn.execute('SELECT * FROM tool_invocations WHERE invocation_id=?', (source,)).fetchone()
        if original is None:
            break
        reused_from = ref
        row = original
    data = dict(row)
    return {'kind': 'execution', 'ref': ref, 'resolved': True, 'invocation_id': data['invocation_id'],
            'tool': data['tool_name'], 'params_digest': data['params_digest'],
            'agent_id': data.get('agent_id', ''), 'task_id': data.get('task_id', ''),
            'route_id': data.get('route_id', ''), 'identity_id': data.get('identity_id', ''),
            'step_id': detail.get('step_id', ''), 'turn': detail.get('turn', 0),
            'tool_call_id': detail.get('tool_call_id', ''), 'environment_id': detail.get('environment_id', ''),
            'execution_status': data['execution_status'], 'reused_via': reused_from}


def resolve_source(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    execution = invocation_source(conn, ref)
    if execution:
        return execution
    parts = ref.rsplit('@', 1)
    name = parts[0]
    layer_name, separator, key = name.partition(':')
    if separator and layer_name in {layer.value for layer in BlackboardLayer}:
        rows = conn.execute('SELECT layer,record_id,version FROM bb_records WHERE layer=? AND record_id=?',
                            (layer_name, key)).fetchall()
    else:
        rows = conn.execute('SELECT layer,record_id,version FROM bb_records WHERE record_id=? LIMIT 2', (name,)).fetchall()
    if len(rows) != 1:
        return {'ref': ref, 'resolved': False}
    row = rows[0]
    version = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else row['version']
    return {'kind': 'blackboard', 'ref': ref, 'resolved': True, 'layer': row['layer'],
            'record_id': row['record_id'], 'version': version}


def evidence_view(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    provenance = data.get('provenance')
    if not isinstance(provenance, dict):
        if isinstance(data.get('facts'), list) and isinstance(data.get('evidence'), list):
            return {**data, **{key: [evidence_view(conn, item) if isinstance(item, dict) else item for item in data[key]]
                              for key in ('facts', 'evidence')}}
        return data  # 旧记录不伪造来源；首次更新时由写入口补齐。
    out = copy.deepcopy(data)
    observed = False
    stale = False
    for source in out['provenance'].get('sources', []):
        if source.get('kind') == 'execution':
            current = invocation_source(conn, source['invocation_id'])
            if current:
                source['execution_status'] = current['execution_status']
                observed |= current['execution_status'] == 'succeeded'
        elif source.get('kind') == 'blackboard':
            row = conn.execute('SELECT version FROM bb_records WHERE layer=? AND record_id=?',
                               (source['layer'], source['record_id'])).fetchone()
            stale |= row is None or row['version'] != source['version']
    assessment = out.get('assessment')
    assessment = assessment if isinstance(assessment, str) else None
    if assessment in {'conflicting', 'expired'} and out.get('assessment_reason'):
        state = assessment
    elif stale:
        state = 'expired'
    elif out['provenance'].get('independent_verification'):
        state = 'independently_verified'
    else:
        state = ('observed' if provenance.get('origin') == 'tool' else 'supported') if observed else 'unverified'
    out['evidence_state'] = state
    return out


def annotate(conn: sqlite3.Connection, layer: BlackboardLayer, data: dict[str, Any], *,
             actor: str, now: str, agent_type: AgentType, record_id: str = "") -> dict[str, Any]:
    if layer == BlackboardLayer.TOPOLOGY and record_id == 'business-context':
        out = copy.deepcopy(data)
        evidence = {item.get('id'): item.get('record_id') for item in out.get('evidence', []) if isinstance(item, dict)}
        for key in ('facts', 'evidence'):
            for item in out.get(key, []):
                if not isinstance(item, dict):
                    continue
                refs = item.get('evidence_refs', []) if key == 'facts' else [item.get('record_id', '')]
                decorated = annotate(conn, BlackboardLayer.TOPOLOGY,
                    {'kind': 'finding', 'evidence_refs': [evidence.get(ref) or ref for ref in refs if isinstance(ref, str)]},
                    actor=actor, now=now, agent_type=agent_type)
                item.update(provenance=decorated['provenance'], evidence_state=decorated['evidence_state'])
        return out
    if not is_fact(layer, data):
        return data
    from spiritscout.tools.invocation import current_execution_scope, current_invocation

    scope, invocation = current_execution_scope.get(), current_invocation.get()
    out = copy.deepcopy(data)
    raw_refs = out.get('evidence_refs')
    refs = [ref for ref in raw_refs if isinstance(ref, str)] if isinstance(raw_refs, list) else []
    if isinstance(out.get('parent_finding_id'), str) and out['parent_finding_id']:
        refs.append(out['parent_finding_id'])
    if invocation:
        refs.append(invocation.invocation_id)
    verification = out.get('verification')
    verification = verification if isinstance(verification, dict) else {}
    independently_verified = bool(layer == BlackboardLayer.VULN and actor == 'verifier' and agent_type == AgentType.VERIFIER
        and out.get('status') == 'accepted' and verification.get('verdict') == 'confirmed')
    if independently_verified:
        refs.extend(item['execution_id'] for key in ('evidence', 'controls')
                    for item in verification.get(key, []) if isinstance(item, dict) and item.get('execution_id'))
    sources = [resolve_source(conn, ref) for ref in dict.fromkeys(refs)]
    producer = {'agent_id': scope.agent_id if scope and scope.agent_id else actor,
                'task_id': scope.task_id if scope else '', 'route_id': scope.route_id if scope else '',
                'step_id': scope.step_id if scope else '', 'turn': scope.turn if scope else 0,
                'tool_call_id': scope.tool_call_id if scope else '', 'tool_name': scope.tool_name if scope else '',
                'invocation_id': invocation.invocation_id if invocation else ''}
    out['provenance'] = {'producer': producer, 'sources': sources, 'recorded_at': now,
                         'origin': 'tool' if invocation else 'agent' if scope else 'legacy',
                         'independent_verification': independently_verified}
    return evidence_view(conn, out)
