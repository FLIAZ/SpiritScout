"""从有界审计窗口投影执行过程；不调用模型，不把观察冒充已验证发现。"""
from __future__ import annotations

import copy
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from spiritscout.web.events import read_activity_page

_CACHE: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
_LOCK = threading.Lock()


def project_process(events: list[dict[str, Any]]) -> dict[str, Any]:
    """按明确任务/路线/回合关联；旧事件仅在出处唯一时补归属。"""
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    active: dict[tuple[str, str, str], dict[str, Any]] = {}
    aliases: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    calls: dict[tuple, str] = {}
    internal_calls: set[tuple] = set()
    pending_outputs: dict[tuple[str, str, str], list[str]] = {}

    def edge(source: str, target: str, kind: str) -> None:
        item = {'source': source, 'target': target, 'kind': kind}
        if source != target and item not in edges:
            edges.append(item)

    for event in events:
        kind = str(event.get('type') or '')
        d = event.get('detail') or {}
        if kind in {'task.done', 'task.failed', 'task.cancelled', 'task.pruned'}:
            for owner, current in active.items():
                if owner[1] == d.get('task_id'):
                    current['status'] = kind.split('.')[1]
            continue
        if kind not in {'agent.turn', 'agent.delta', 'agent.tool_call',
                        'agent.tool_result', 'agent.note', 'agent.conclude'}:
            continue
        actor = str(event.get('actor') or '')
        tid, rid = str(d.get('task_id') or ''), str(d.get('route_id') or '')
        route = str(d.get('route') or '')
        if tid and rid:
            key = (actor, tid, rid)
            for name in (rid, route):
                if name:
                    aliases.setdefault((actor, name), set()).add(key)
        else:
            candidates = aliases.get((actor, rid or route), set()) if rid or route else set(active)
            candidates = {k for k in candidates if k[0] == actor and (not tid or k[1] == tid)}
            if len(candidates) != 1:
                continue
            key = next(iter(candidates))
            _, tid, rid = key
        if kind == 'agent.tool_call':
            internal_calls.update((*key, str(c.get('id') or ''))
                                  for c in d.get('calls') or [] if c.get('name') == 'investigate')
        turn = d.get('turn')
        step = active.get(key)
        if isinstance(turn, int) and not isinstance(turn, bool) and turn > 0:
            sid = '__process_' + json.dumps([actor, tid, rid, turn], ensure_ascii=False)
            if sid not in nodes:
                nodes[sid] = {'id': sid, 'kind': 'step', 'task_id': tid, 'route_id': rid,
                              'route': route, 'actor': actor, 'turn': turn,
                              'title': f'第 {turn} 步', 'body': '', 'status': 'running',
                              'seq': event.get('seq'), 'ts': event.get('ts')}
                if step and turn > step['turn']:
                    sources = pending_outputs.pop(key, [])
                    for source in sources or [step['id']]:
                        edge(source, sid, 'context' if source != step['id'] else 'sequence')
                    if sources and all(nodes[source]['turn'] != step['turn'] for source in sources):
                        edge(step['id'], sid, 'sequence')
                    step['status'] = 'done'
            step = nodes[sid]
            if key not in active or turn >= active[key]['turn']:
                active[key] = step
        if step is None:
            continue
        if kind == 'agent.delta' and d.get('kind') in {'text', 'reset'}:
            step['body'] = ('' if d['kind'] == 'reset' else
                            (step['body'] + str(d.get('text') or ''))[-4000:])
        if kind == 'agent.tool_call':
            for call in d.get('calls') or []:
                if call.get('name') != 'investigate':
                    calls[(*key, str(call.get('id') or ''))] = step['id']
                    if step['title'] == f"第 {step['turn']} 步":
                        step['title'] += f" · {str(call.get('name') or '执行')}"
        if kind in {'agent.tool_result', 'agent.note'}:
            if (d.get('name') == 'investigate'
                    or (*key, str(d.get('id') or '')) in internal_calls):
                continue  # 外层调度回执不是新的现场发现。
            source = calls.get((*key, str(d.get('id') or '')), step['id'])
            base = nodes[source]
            nid = '__process_event_' + json.dumps([actor, tid, rid, event.get('seq'),
                                                   d.get('id'), kind], ensure_ascii=False)
            text = d.get('text') if kind == 'agent.note' else d.get('result', d.get('note', ''))
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False)
            label = '发现记录' if kind == 'agent.note' else ('执行失败' if d.get('ok') is False else '工具观察')
            note = str(d.get('note') or '')
            summary = ' → '.join(note.split(' → ')[1:]) or note
            node = {**base, 'id': nid, 'kind': 'finding' if kind == 'agent.note' else 'observation',
                    'title': label + (' · ' + ' '.join(summary.split())[:90] if summary else
                                     f" · {d['name']}" if d.get('name') else ''),
                    'body': text[:4000], 'status': 'failed' if d.get('ok') is False else 'done',
                    'seq': event.get('seq'), 'ts': event.get('ts')}
            nodes[nid] = node
            edge(source, nid, 'produces')
            pending_outputs.setdefault(key, []).append(nid)
            if d.get('output_file'):
                aid = nid + ':artifact'
                nodes[aid] = {**node, 'id': aid, 'kind': 'artifact', 'title': '输出产物',
                              'body': str(d['output_file'])[:2000]}
                edge(nid, aid, 'produces')
                pending_outputs[key].append(aid)
        if kind == 'agent.conclude':
            step['status'] = 'done'
            step['body'] = str(d.get('summary') or step['body'])[:4000]
    return {'nodes': list(nodes.values()), 'edges': edges}


def read_process_graph(path: Path) -> dict[str, Any]:
    """最多保留最近 320 个完整活动；按文件版本缓存，避免轮询重复解码。"""
    try:
        stat = path.stat()
    except OSError:
        return {'nodes': [], 'edges': [], 'has_more': False}
    key = (str(path.resolve()), stat.st_ino, stat.st_size, stat.st_mtime_ns)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    page = read_activity_page(path, limit=320)
    graph = {**project_process([row['event'] for row in page['items']]),
             'has_more': page['has_more']}
    with _LOCK:
        # 同一文件只保留当前版本，缓存不会随每次写入累积。
        for previous in list(_CACHE):
            if previous[0] == key[0]:
                del _CACHE[previous]
        _CACHE[key] = graph
        while len(_CACHE) > 16:
            _CACHE.popitem(last=False)
    return copy.deepcopy(graph)
