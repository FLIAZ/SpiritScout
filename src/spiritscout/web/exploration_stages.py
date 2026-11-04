"""只读投影已有公开进展；独立于聊天分页，不写黑板或模型上下文。"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any

_LIMIT = 512


class _Stages:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.identity: tuple[int, int] | None = None
        self.offset = 0
        self.rows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.has_more = False

    def consume(self, event: dict[str, Any]) -> None:
        kind, d, actor = event.get('type'), event.get('detail') or {}, event.get('actor', '')
        task, route, turn = d.get('task_id'), d.get('route_id') or d.get('route') or '', d.get('turn')
        if not task:
            return
        if kind in {'task.done', 'task.failed', 'task.cancelled', 'task.pruned'}:
            for row in self.rows.values():
                if row['task_id'] == task and row['actor'] == actor and row['status'] in {'running', 'waiting'}:
                    row['status'] = kind.split('.')[1]
            return
        if kind == 'agent.tool_result':
            for row in self.rows.values():
                if ((row['actor'], row['task_id'], row['route_id']) == (actor, task, route)
                        and d.get('id') in row.get('pending_calls', [])):
                    row['pending_calls'].remove(d['id'])
                    for tool in row.get('tools', []):
                        if tool['id'] == d['id']:
                            tool.update(status='failed' if d.get('ok') is False else 'done',
                                        note=str(d.get('note') or '')[:200])
                    if d.get('ok') is False:
                        row['status'] = 'failed'
                    elif not row['pending_calls'] and row['status'] in {'running', 'waiting'}:
                        row['status'] = 'done'
            return
        # 无回合号时只接受明确的 step_id，不能根据时间猜归属。
        step = turn if type(turn) is int and turn > 0 else d.get('step_id')
        if not step:
            return
        key = json.dumps([actor, task, route, step], ensure_ascii=False, separators=(',', ':'))
        if kind == 'agent.progress':
            text = d.get('text')
            if not isinstance(text, str) or not text.strip():
                return
            for row in self.rows.values():
                if (row['actor'], row['task_id'], row['route_id']) == (actor, task, route) and row['status'] == 'running' and row['id'] != '__process_stage:' + key:
                    row['status'] = 'waiting' if row.get('pending_calls') else 'done'
            old = self.rows.get(key, {})
            from spiritscout.gateway.progress import public_exploration

            semantic = public_exploration(d.get('exploration'))
            self.rows[key] = {**old, **({'exploration': semantic} if semantic else {}), 'id': '__process_stage:' + key, 'kind': 'milestone',
                              'actor': actor, 'task_id': task, 'route_id': route, 'turn': turn or 0,
                              'step_id': d.get('step_id', ''), 'title': text.strip().split('\n')[0][:100],
                              'body': text.strip()[:2400], 'status': old.get('status', 'running'),
                              'sequence': event.get('seq', 0), 'tool_call_ids': old.get('tool_call_ids', []),
                              'pending_calls': old.get('pending_calls', [])}
            if len(self.rows) > _LIMIT:
                self.rows.popitem(last=False)
                self.has_more = True
        elif kind == 'agent.tool_call':
            # 无公开说明的执行也只存归属，不生成画布节点；用于尝试详情与产出定位。
            if key not in self.rows:
                self.rows[key] = {'id': '__process_stage:' + key, 'kind': 'execution',
                                  'actor': actor, 'task_id': task, 'route_id': route, 'turn': turn or 0,
                                  'step_id': d.get('step_id', ''), 'title': '', 'body': '',
                                  'status': 'running', 'sequence': event.get('seq', 0),
                                  'tool_call_ids': [], 'pending_calls': []}
                if len(self.rows) > _LIMIT:
                    self.rows.popitem(last=False)
                    self.has_more = True
            calls = [c.get('id') for c in d.get('calls', []) if isinstance(c, dict) and c.get('id')]
            tools = self.rows[key].setdefault('tools', [])
            for call in d.get('calls', []):
                if isinstance(call, dict) and call.get('id') and not any(t['id'] == call['id'] for t in tools):
                    tools.append({'id': call['id'], 'name': str(call.get('name') or '工具'), 'status': 'running'})
            self.rows[key]['tool_call_ids'] = list(dict.fromkeys(self.rows[key]['tool_call_ids'] + calls))
            self.rows[key]['pending_calls'] = list(dict.fromkeys(self.rows[key]['pending_calls'] + calls))


@lru_cache(maxsize=32)
def _cache(path: str) -> _Stages:
    return _Stages()


def read_public_stages(path: Path) -> dict[str, Any]:
    """首次回放，后续只读新增完整行；截断／替换后重建，半行留待下轮。"""
    cache = _cache(str(path.resolve()))
    with cache.lock:
        try:
            with path.open('rb') as stream:
                import os

                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if cache.identity != identity or stat.st_size < cache.offset:
                    cache.identity, cache.offset = identity, 0
                    cache.rows.clear()
                    cache.has_more = False
                stream.seek(cache.offset)
                while line := stream.readline():
                    if not line.endswith(b'\n'):
                        break
                    cache.offset = stream.tell()
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            cache.consume(event)
                    except (ValueError, TypeError, AttributeError):
                        continue
        except FileNotFoundError:
            return {'items': [], 'has_more': False}
        rows = copy.deepcopy(list(cache.rows.values()))
        _apply_annotations(path, rows)
        return {'items': rows, 'has_more': cache.has_more}


def public_digest(rows: list[dict[str, Any]]) -> str:
    """历史展示注释绑定公开原文；原文改变后不沿用旧归类。"""
    source = [(r['task_id'], r['route_id'], r['turn'], r.get('body', ''))
              for r in rows if r.get('body')]
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _apply_annotations(path: Path, rows: list[dict[str, Any]]) -> None:
    """可选的历史展示侧归类，只读，不进入模型上下文或黑板。"""
    from spiritscout.gateway.progress import public_exploration

    try:
        source = path.with_name('topology-intents.json')
        if source.stat().st_size > 128 * 1024:
            return
        data = json.loads(source.read_text())
        if data.get('version') != 1 or data.get('source_digest') != public_digest(rows):
            return
        assignments = {}
        for group in data.get('groups', []):
            semantic = public_exploration(group)
            if semantic is None or not isinstance(group.get('turns'), list):
                return
            for turn in group['turns']:
                if type(turn) is not int or turn < 1:
                    return
                key = (group.get('task_id'), group.get('route_id'), turn)
                if key in assignments:
                    return
                assignments[key] = semantic
        for row in rows:
            semantic = assignments.get((row['task_id'], row['route_id'], row['turn']))
            if semantic and not row.get('exploration'):
                row.update(exploration=semantic, semantic_origin='reviewed_public_progress')
    except (OSError, ValueError, TypeError, AttributeError):
        return
