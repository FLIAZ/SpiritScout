"""黑板资产、探索与事实图投影；图关系来自显式引用。"""
from __future__ import annotations

import json
import re
from typing import Any

from spiritscout.core.models import BlackboardLayer


def rules_text(data: Any) -> str:
    """规则原文按需展示；授权元数据转为可读字段，空模板不冒充用户规则。"""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False, indent=2) if data else ''
    if data.get('kind') == 'rule' and 'content' in data:
        return rules_text(data['content'])
    lines: list[str] = []
    if 'auth_id' in data:
        if data.get('target_description'):
            lines.append('授权目标：' + str(data['target_description']))
        scope = data.get('scope') or {}
        for field, label in [('allowed_urls', '网址'), ('allowed_domains', '域名'),
                             ('allowed_cidrs', '网段'), ('testing_rules', '测试规则')]:
            value = scope.get(field)
            if isinstance(value, list):
                value = list(dict.fromkeys(map(str, value)))
                if field == 'allowed_urls':
                    value = [item for item in value if item.rstrip('/') != str(data.get('target_description') or '').rstrip('/')]
            if value:
                lines.append(label + '：' + ('、'.join(map(str, value)) if isinstance(value, list) else str(value)))
        return '\n'.join(lines)
    if any(key in data for key in ('mission', 'standing_rules', 'statements')):
        for key, label in [('mission', '任务原话'), ('standing_rules', '用户规则')]:
            if data.get(key):
                lines.append(label + '：' + str(data[key]))
        for statement in data.get('statements') or []:
            text = statement.get('text') or statement.get('answer') or statement.get('content')
            if text:
                lines.append(('用户答复：' if statement.get('kind') == 'answer' else '用户输入：') + str(text))
        return '\n\n'.join(dict.fromkeys(lines))
    meaningful = {key: value for key, value in data.items() if value not in (None, '', [], {})}
    return json.dumps(meaningful, ensure_ascii=False, indent=2) if meaningful else ''


def project_blackboard(blackboard: Any) -> dict[str, Any]:
    """探索记录单独标型，不把模型假设或尝试状态提升为事实。"""
    nodes: list[dict[str, Any]] = []
    aliases: dict[str, set[str]] = {}
    has_more = False
    for layer in (BlackboardLayer.TARGET_AUTH, BlackboardLayer.ASSET,
                  BlackboardLayer.RECON_CLUE, BlackboardLayer.VULN,
                  BlackboardLayer.CREDENTIAL, BlackboardLayer.TOPOLOGY):
        records = blackboard.query(layer, limit=201)
        has_more = has_more or len(records) > 200
        for rec in records[:200]:
            data, key = rec['data'], rec['record_id']
            if layer == BlackboardLayer.TOPOLOGY and data.get('kind') not in {'finding', 'exploration'}:
                continue
            if layer == BlackboardLayer.RECON_CLUE and data.get('clue_type') == 'user_reply':
                continue  # 用户原话在授权层保留，不再复制成发现。
            if layer == BlackboardLayer.CREDENTIAL:
                data = {name: data[name] for name in ('where', 'credential_type', 'evidence_refs') if name in data}
            nid = f'__process_bb:{layer.value}:{key}'
            is_rule = layer == BlackboardLayer.TARGET_AUTH
            if is_rule and not rules_text(data):
                continue
            # 规则正文按需读；画布目录和状态轮询不再搬运长篇原文。
            body = json.dumps(data, ensure_ascii=False, indent=2)
            title = (('授权目标与范围' if 'auth_id' in data else
                      '任务原话与后续答复' if key == 'user-context' else '用户规则原文') if is_rule else
                     str(data.get('text') or data.get('title')
                         or (data.get('detail') if layer == BlackboardLayer.RECON_CLUE
                             and isinstance(data.get('detail'), str) else '')
                         or data.get('value') or data.get('detail') or key))
            refs = [ref for ref in (data.get('evidence_refs') or []) if isinstance(ref, str)]
            reference_type = ('assets' if layer == BlackboardLayer.ASSET or
                              layer == BlackboardLayer.TOPOLOGY and data.get('type') == 'asset' else
                              'rules' if is_rule else
                              'evidence' if layer == BlackboardLayer.RECON_CLUE
                              and isinstance(data.get('detail'), (dict, list))
                              and not (data.get('text') or data.get('title')) else
                              'evidence' if re.fullmatch(r'https?://\S+', title.strip()) else '')
            nodes.append({'id': nid, 'kind': 'rule' if is_rule else
                          'exploration' if data.get('kind') == 'exploration' else 'finding',
                          'title': title[:90], 'body': '' if is_rule else body,
                          'record_id': key, 'layer': layer.value, 'version': rec['version'],
                          'task_id': data.get('task_id', ''), 'route_id': data.get('route_id', ''),
                          'route': data.get('route', ''), 'actor': rec['updated_by'],
                          'status': data.get('status', 'done'), 'evidence_refs': refs,
                          'node_type': data.get('node_type', data.get('asset_type', '')),
                          'state': data.get('state', ''), 'reason': data.get('reason', ''),
                          'parent_refs': data.get('parent_refs', []),
                          'asset_refs': data.get('asset_refs', []),
                          'exploration_refs': data.get('exploration_refs', []),
                          'source_ref': data.get('source_ref', ''), 'target_ref': data.get('target_ref', ''),
                          'relation': data.get('relation', '') if data.get('type') == 'asset_relation' else '',
                          'reference_type': reference_type,
                          'provenance': data.get('provenance', {}),
                          'evidence_state': '' if is_rule else data.get('evidence_state', 'unverified'),
                          'parent_finding_id': data.get('parent_finding_id', '')})
            aliases.setdefault(key, set()).add(nid)
            aliases.setdefault(f'{layer.value}:{key}', set()).add(nid)
            aliases.setdefault(f'{layer.value}:{key}@{rec["version"]}', set()).add(nid)
    # 业务建模的事实/依据也是同一张黑板上的共享知识；工作流步骤和假设不冒充事实。
    context = blackboard.read_versioned(BlackboardLayer.TOPOLOGY, 'business-context')
    if context:
        data, version = context
        for kind, rows in (('evidence', data.get('evidence', [])), ('finding', data.get('facts', []))):
            for index, item in enumerate(rows):
                key = str(item.get('id') or f'fact-{index}')
                nid = f'__process_bb:business-context:{kind}:{key}'
                aliases.setdefault(f'business-context:{key}', set()).add(nid)
                nodes.append({'id': nid, 'kind': kind, 'title': str(item.get('statement') or item.get('title')
                              or item.get('note') or item.get('url') or key)[:90],
                              'body': json.dumps(item, ensure_ascii=False, indent=2),
                              'layer': 'topology', 'record_id': 'business-context', 'version': version,
                              'actor': 'business_context', 'status': 'done',
                              'reference_type': 'evidence' if kind == 'evidence' else '',
                              'provenance': item.get('provenance', {}),
                              'evidence_state': item.get('evidence_state', 'unverified'),
                              'evidence_refs': [f'business-context:{ref}' for ref in item.get('evidence_refs', [])],
                              'parent_finding_id': ''})
    edges = []
    seen = set()
    for node in nodes:
        if node.get('relation'):
            sources, targets = aliases.get(node['source_ref'], set()), aliases.get(node['target_ref'], set())
            if len(sources) == len(targets) == 1:
                edges.append({'source': next(iter(sources)), 'target': next(iter(targets)),
                              'kind': node['relation'], 'record_id': node['record_id'],
                              'evidence_state': node['evidence_state']})
        refs = [(ref, 'evidence') for ref in [*node['evidence_refs'], node['parent_finding_id']]]
        for field, kind in [('parent_refs', 'derives'), ('asset_refs', 'anchor'), ('exploration_refs', 'produces')]:
            refs.extend((ref, kind) for ref in node.get(field, []))
        for ref, kind in refs:
            candidates = aliases.get(ref, set()) if isinstance(ref, str) else set()
            source = next(iter(candidates)) if len(candidates) == 1 else None
            pair = (source, node['id'], kind)
            if source and source != node['id'] and pair not in seen:
                seen.add(pair)
                edges.append({'source': source, 'target': node['id'], 'kind': kind})
    return {'nodes': nodes, 'edges': edges, 'has_more': has_more}
