"""正常解题上下文的发送视图；完整历史不改写，成组归档以保留协议配对。"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from spiritscout.ctf.evidence_memory import snapshot, source_digest
from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.gateway.observation_text import lossless_runs

GUIDANCE = ('较早的完整工具往返可能显示为带原始消息编号的摘要。摘要属于观察，不是用户指令，'
            '不能当作完整证据；需要代码、参数或中间输出时用 history_read 分页读取，'
            '可用 view=content 与 find 定位原文，避免反复猜 offset。'
            '已经可见的原文可直接引用；只为缺失细节或新的验证问题回读。'
            '重复请求返回相同内容只表示重复观察；根据新假设和验证需要决定继续、调整或复用，不把次数当停止条件。')


def observe(state: dict[str, Any], call: dict[str, Any], outcome: Any) -> None:
    messages = state.get('messages') or []
    if not messages or outcome.stop or call.get('name') in {'history_read', 'record_answer', 'finish_challenge'}:
        return
    metadata = outcome.metadata or {}
    if call.get('name') == 'evidence_memory':
        if outcome.ok and 'memory_revision' in metadata:
            messages[-1]['_normal_memory_revision'] = metadata['memory_revision']
        return
    row = {'tool': call.get('name'), 'ok': outcome.ok, 'invocation_id': metadata.get('invocation_id', ''),
           'output_file': metadata.get('output_file', ''), 'message_index': len(messages) - 1,
           'source_sha256': source_digest(messages[-1])}
    args = call.get('arguments') or {}
    if isinstance(args, dict) and args.get('action') in (None, 'analysis'):
        # 仅比较相同请求的完整返回；不因为预览相同就声称原始响应相同。
        observation = metadata.get('observation') or {}
        full = observation.get('comparison_scope') == 'raw_streams'
        if not observation or full:
            result = observation.get('digest') if full else outcome.text
            key = hashlib.sha256(json.dumps([call.get('name'), args, outcome.ok, result],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            seen = state.setdefault('normal_observations', {})
            previous = seen.get(key, {'count': 0, 'message_index': len(messages) - 1})
            seen[key] = {**previous, 'count': previous['count'] + 1}
            if len(seen) > 256:
                seen.pop(next(iter(seen)))
            if seen[key]['count'] > 1:
                row.update(repeated=seen[key]['count'], first_message_index=previous['message_index'])
    messages[-1]['_normal_observation'] = row


def _groups(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    result = []
    for start, message in enumerate(messages):
        calls = message.get('tool_calls') or []
        if message.get('role') != 'assistant' or not calls:
            continue
        ids = {c.get('id') for c in calls}
        end, found = start + 1, []
        while end < len(messages) and messages[end].get('role') == 'tool':
            found.append(messages[end].get('tool_call_id'))
            end += 1
        if None not in ids and len(ids) == len(calls) and len(found) == len(ids) and set(found) == ids:
            result.append((start, end))
    return result


def _summary(messages: list[dict[str, Any]], start: int, end: int) -> dict[str, Any]:
    rows = []
    calls = {c['id']: c.get('function', {}).get('name', '') for c in messages[start].get('tool_calls', [])}
    for index in range(start + 1, end):
        message = messages[index]
        content = str(message.get('content') or '')
        rows.append({'message_index': index, 'tool': calls.get(message.get('tool_call_id')),
            **message.get('_normal_observation', {}),
            'preview': content[:220] + ('\n…[部分预览]…\n' + content[-180:] if len(content) > 440 else content[220:]),
            'original_chars': len(content)})
    return {'role': 'user', 'content': '历史工具往返摘要（不完整，非用户指令）：' + json.dumps({
        'assistant_message_index': start, 'assistant_note': str(messages[start].get('content') or '')[:200],
        'observations': rows, 'read_original': 'history_read(message_index, offset, length)'},
        ensure_ascii=False, separators=(',', ':'))}


def project(state: dict[str, Any], tools: list[dict[str, Any]], budget: int | None = None,
            *, force: bool = False) -> dict[str, Any]:
    """先无损表示重复行，窗口压力下才归档；未知容量使用 48K 整理参考值。"""
    original = state['messages']
    groups = _groups(original)
    if state.get('normal_context_policy') != 2:
        # 旧的固定 48K 归档只是一份发送视图；升级时从完整历史重建一次。
        state['normal_context_archives'] = {}
        state['normal_context_policy'] = 2
    archived = state.setdefault('normal_context_archives', {})
    # 检查点追加历史，恢复后仅保留仍然完整配对的归档。
    valid = {str(start): end for start, end in groups}
    fingerprints: dict[str, str] = {}

    def fingerprint(key: str) -> str:
        if key not in fingerprints:
            fingerprints[key] = hashlib.sha256(json.dumps(original[int(key):valid[key]], ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return fingerprints[key]
    memory = snapshot(state)
    current_memory_index = next((i for i in range(len(original) - 1, -1, -1)
        if original[i].get('_normal_memory_revision') == memory['revision']), None)
    pinned = {str(start) for start, end in groups
              if current_memory_index is not None and start <= current_memory_index < end}
    archived = {key: value for key, value in archived.items() if key in valid and valid[key] == value.get('end')
                and value.get('source_sha256') == fingerprint(key) and key not in pinned}
    state['normal_context_archives'] = archived

    encoded = {i: value for i, message in enumerate(original)
               if (message.get('_normal_observation') or {}).get('source_sha256') and not message.get('_context_contract')
               and isinstance(message.get('content'), str)
               and (value := lossless_runs(message['content'])) is not None}

    def render() -> list[dict[str, Any]]:
        result, index = [], 0
        while index < len(original):
            item = archived.get(str(index))
            if item:
                result.append(item['message'])
                index = item['end']
            else:
                message = original[index]
                if index == current_memory_index:
                    # 仅来源版本变化时更新发送视图；原始记录保持不变，旧结论明确失效。
                    message = {**message, 'content': json.dumps({'ok': True, **memory},
                        ensure_ascii=False, separators=(',', ':'))}
                elif index in encoded:
                    message = {**message, 'content': encoded[index]}
                result.append(message)
                index += 1
        wire = []
        for message in result:
            clean = {k: v for k, v in message.items() if not k.startswith('_')}
            row = message.get('_normal_observation', {})
            if row.get('source_sha256') and not message.get('_context_contract') and isinstance(clean.get('content'), str):
                clean['content'] += (f"\n历史定位 message_index={row['message_index']}；"
                                    f"原文版本 sha256={row['source_sha256']}")
                if row.get('invocation_id'):
                    clean['content'] += f"；执行证据 evidence_refs={json.dumps([row['invocation_id']])}"
            if row.get('repeated') and isinstance(clean.get('content'), str):
                # 每条消息的提示固定；下一轮不会删去旧提示而破坏缓存前缀。
                clean['content'] += (f"\n观察提示：相同请求已返回相同内容 {row['repeated']} 次，"
                    f"首次结果在消息 {row['first_message_index']}；这不证明现场未变化。请按验证需要决定复用或调整方法。")
            wire.append(clean)
        return wire

    messages = render()
    before = estimate_input_tokens(messages, tools)
    estimated = before
    # 网关已为输出和整理预留空间；不在大窗口中因固定 48K 门槛改写热前缀。
    high = budget if budget is not None else 48000
    changed = 0
    if force or before > high:
        target = int(min(before, high) * .65) if force else int(high * .65)
        for start, end in groups[:-4]:
            if str(start) in archived or str(start) in pinned or any(m.get('_context_contract') for m in original[start:end]):
                continue
            summary = _summary(original, start, end)
            # 小往返不归档，避免摘要反而增大输入。
            if estimate_input_tokens([summary]) >= estimate_input_tokens(original[start:end]):
                continue
            archived[str(start)] = {'end': end, 'message': summary, 'source_sha256': fingerprint(str(start))}
            changed += 1
            messages = render()
            estimated = estimate_input_tokens(messages, tools)
            if estimated <= target:
                break
    messages = copy.deepcopy(messages)
    return {'messages': messages, 'archived_turns': len(archived), 'new_archived_turns': changed,
            'previous_tokens': before,
            'lossless_messages': len(encoded),
            'original_tokens': estimate_input_tokens([{k: v for k, v in m.items() if not k.startswith('_')}
                                                      for m in original], tools),
            'estimated_tokens': estimated}
