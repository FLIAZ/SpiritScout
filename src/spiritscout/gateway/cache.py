"""缓存计量和前缀诊断；只记录哈希与长度，不保存提示词正文。"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any


def prefix_change_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    for row in calls:
        reason = (row.get('cache_diagnostics') or {}).get('reason', 'unavailable')
        reasons[reason] = reasons.get(reason, 0) + 1
    return {'reasons': reasons, 'scope': 'gateway_canonical_request',
            'notice': '本地相邻请求诊断，不是供应商缓存失效原因；重启或被淘汰的样本视为首次观察。'}


def cache_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """实际命中只采用端点用量；未知值单列，输出不混入输入命中率。"""
    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        measured = [row for row in rows if row.get("cached_input_tokens") is not None]
        inputs = sum(row.get("input_tokens", 0) for row in measured)
        cached = sum(min(row["cached_input_tokens"], row.get("input_tokens", 0)) for row in measured)
        priced = [r for r in rows if r.get("estimated_cost_cny_min") is not None
                  and r.get("estimated_cost_cny_max") is not None]
        parts = {}
        for row in rows:
            for key, value in (row.get("context_parts") or {}).items():
                parts[key] = parts.get(key, 0) + value
        return {"estimated_cost_cny_min": sum(r["estimated_cost_cny_min"] for r in priced),
                "estimated_cost_cny_max": sum(r["estimated_cost_cny_max"] for r in priced),
                "priced_calls": len(priced), "unpriced_calls": len(rows) - len(priced),
                "context_parts": parts, "measured_calls": len(measured), "unmeasured_calls": len(rows) - len(measured),
                "input_tokens": inputs, "cached_input_tokens": cached,
                "uncached_input_tokens": inputs - cached,
                "total_input_tokens": sum(row.get("input_tokens", 0) for row in rows),
                "output_tokens": sum(row.get("output_tokens", 0) for row in rows),
                "hit_rate": cached / inputs if inputs else None,
                "prefix_changes": prefix_change_summary(rows)}

    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in calls:
        key = (row.get("model", ""), row.get("actor", "gateway"), row.get("task_type", ""),
               row.get("cache_scope", ""), row.get('model_key') or row.get('provider_ref', ''))
        groups.setdefault(key, []).append(row)
    return {**summarize(calls), "groups": [
        {"model": key[0], "actor": key[1], "task_type": key[2], "cache_scope": key[3],
         "model_key": key[4], **summarize(rows)}
        for key, rows in groups.items()]}


class PrefixTracker:
    """比较相邻请求的完整公共前缀；字符诊断不冒充服务端缓存命中。"""

    def __init__(self, capacity: int = 128) -> None:
        self.capacity = capacity
        self.previous: OrderedDict[tuple[str, ...], tuple[bytes, ...]] = OrderedDict()
        self.components: OrderedDict[tuple[str, ...], dict[str, Any]] = OrderedDict()

    def observe(self, key: tuple[str, ...], messages: list[dict[str, Any]],
                tools: list[dict[str, Any]] | None, *, conversation_key: tuple[str, ...] | None = None,
                model_identity: tuple[str, ...] | None = None,
                request_options: dict[str, Any] | None = None) -> dict[str, Any]:
        def encode(value: Any) -> bytes:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        tool_bytes = encode(tools or [])
        message_bytes = encode(messages)
        raw = tool_bytes + b"\n" + message_bytes
        parts = {"tools": len(tool_bytes), "messages": len(message_bytes)}
        for role in ("system", "user", "assistant", "tool"):
            parts[role] = sum(len(encode(m)) for m in messages if m.get("role") == role)
        parts["reasoning"] = sum(len(str(m.get("reasoning_content") or "").encode()) for m in messages)
        def digest(value: Any) -> str:
            return hashlib.sha256(encode(value)).hexdigest()

        stream = conversation_key if conversation_key is not None else key
        current = {'identity': digest(model_identity if model_identity is not None else key), 'tools': digest(tools or []),
                   'options': digest(request_options or {}),
                   'messages': [(m.get('role', ''), digest(m)) for m in messages]}
        old = self.components.pop(stream, None)
        reason, first = 'first_observation', None
        changed = []
        if old is not None:
            changed = [part for part in ('identity', 'options', 'tools') if old[part] != current[part]]
            first = next((i for i, (a, b) in enumerate(zip(old['messages'], current['messages'])) if a != b), None)
            if first is not None:
                changed.append('system' if 'system' in (old['messages'][first][0], current['messages'][first][0])
                               else 'history')
            elif len(old['messages']) > len(messages):
                first = len(messages)
                changed.append('history_truncated')
            if 'identity' in changed:
                reason = 'model_or_endpoint_changed'
            elif 'options' in changed:
                reason = 'request_options_changed'
            elif 'tools' in changed:
                reason = 'tool_catalog_changed'
            elif changed:
                reason = changed[0] + '_changed' if changed[0] != 'history_truncated' else 'history_truncated'
            else:
                reason = 'append_only' if len(messages) > len(old['messages']) else 'unchanged'
        self.components[stream] = current
        if len(self.components) > self.capacity:
            self.components.popitem(last=False)
        diagnostics = {'scope': 'gateway_canonical_request', 'reason': reason, 'changed_components': changed,
            'first_changed_message_index': first,
            'first_changed_role': messages[first].get('role') if first is not None and first < len(messages) else None,
            'component_sha256': {part: current[part] for part in ('identity', 'options', 'tools')},
            'message_count': len(messages), 'previous_message_count': len(old['messages']) if old else None}
        diagnostics['component_sha256'].update({part: digest([row for row in current['messages']
            if (row[0] == 'system') == (part == 'system')]) for part in ('system', 'history')})
        # 固定块哈希使新增消息不改变已比较前缀；只计完整块，结果为保守下界。
        blocks = tuple(hashlib.sha256(raw[i:i + 256]).digest()
                       for i in range(0, len(raw), 256))
        previous = self.previous.pop(key, ())
        matched = 0
        for before, after in zip(previous, blocks):
            if before != after:
                break
            matched += 1
        reused = min(matched * 256, len(raw))
        self.previous[key] = blocks
        if len(self.previous) > self.capacity:
            self.previous.popitem(last=False)
        return {"request_bytes": len(raw), "reused_prefix_bytes": reused,
                "new_suffix_bytes": len(raw) - reused, "context_parts": parts,
                'cache_diagnostics': diagnostics}
