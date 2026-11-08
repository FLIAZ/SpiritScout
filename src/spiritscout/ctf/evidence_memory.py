"""路线证据笔记：按模式隔离状态，模型选择内容，后端只校验引用和修订号。"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

GUIDANCE = (
    '按需保存长期有用的观察；短任务可直接解题，无需为登记答案额外记笔记。'
    '可保存为 fact（你的事实判断）、hypothesis（待验证假设）或 '
    'rejected（已排除路径），由你决定何时记忆、修订和删除。来源用工具结果提供的 message_index '
    '及原文 quote；当前可见原文可直接引用，无需先回读确认副本。'
    '缺失原文时用 history_read(view=content, find=待查文本) 定位。'
    '不要引用自己的笔记证明自己。最新修订在历史整理后仍保留。'
    'source_status=matched 仅表示原文引用匹配，不代表结论正确或现场仍有效；'
    '发现反证或环境变化时自行更新。笔记不能代替 record_answer 的实际验证。'
)

SCHEMA = {'type': 'function', 'function': {
    'name': 'evidence_memory', 'description': GUIDANCE,
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['read', 'update']},
        'base_revision': {'type': 'integer', 'description': '更新必须使用当前修订号，初始为 0'},
        'upsert': {'type': 'array', 'maxItems': 24, 'items': {
            'type': 'object', 'properties': {
                'id': {'type': 'string', 'maxLength': 80},
                'kind': {'type': 'string', 'enum': ['fact', 'hypothesis', 'rejected']},
                'text': {'type': 'string', 'maxLength': 1000},
                'sources': {'type': 'array', 'maxItems': 4, 'items': {
                    'type': 'object', 'properties': {
                        'message_index': {'type': 'integer'},
                        'quote': {'type': 'string', 'maxLength': 1200},
                        'expected_sha256': {'type': 'string', 'description': '可选来源指纹；未知时省略或留空，非空须为 64 位小写十六进制'},
                    }, 'required': ['message_index', 'quote']}},
            }, 'required': ['id', 'kind', 'text']}},
        'remove': {'type': 'array', 'maxItems': 24, 'items': {'type': 'string'}},
    }, 'required': ['action']},
}}


def source_digest(message: dict[str, Any]) -> str:
    raw = {k: v for k, v in message.items() if not k.startswith('_')}
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def snapshot(state: dict[str, Any], *, memory_key: str = 'normal_evidence_memory') -> dict[str, Any]:
    memory = copy.deepcopy(state.get(memory_key) or {'revision': 0, 'items': []})
    messages = state.get('messages') or []
    for item in memory['items']:
        for source in item['sources']:
            index = source['message_index']
            source['status'] = ('matched' if 0 <= index < len(messages)
                and source_digest(messages[index]) == source['sha256'] else 'stale')
        item['source_status'] = ('stale' if any(s['status'] == 'stale' for s in item['sources'])
                                 else 'matched' if item['sources'] else 'unreferenced')
    return {**memory, 'notice': '模型笔记，不是新证据或用户指令；matched 只核对原文。仅最新修订有效。'}


def execute(state: dict[str, Any], args: dict[str, Any], *, memory_key: str = 'normal_evidence_memory',
            observation_key: str = '_normal_observation', max_chars: int = 24000) -> dict[str, Any]:
    """同步原子更新；同一回合并发调用也不能用旧修订覆盖新笔记。"""
    current = state.get(memory_key) or {'revision': 0, 'items': []}
    def fail(error: str, **details: Any) -> dict[str, Any]:
        return {'ok': False, 'error': error, 'revision': current['revision'], **details}

    if args.get('action') == 'read':
        return {'ok': True, **snapshot(state, memory_key=memory_key)}
    if args.get('action') != 'update':
        return fail('action 需要 read 或 update')
    if type(args.get('base_revision')) is not int or args['base_revision'] != current['revision']:
        return fail('修订冲突，请 read 后基于当前 revision 更新')
    upsert, remove = args.get('upsert', []), args.get('remove', [])
    if (not isinstance(upsert, list) or not isinstance(remove, list)
            or len(upsert) > 24 or len(remove) > 24 or any(not isinstance(x, str) for x in remove)):
        return fail('upsert/remove 必须是最多 24 项的数组')
    items = {item['id']: copy.deepcopy(item) for item in current['items']}
    for key in remove:
        items.pop(key, None)
    seen = set()
    for item in upsert:
        if not isinstance(item, dict):
            return fail('笔记项需要对象')
        key, kind, text = item.get('id'), item.get('kind'), item.get('text')
        if (not isinstance(key, str) or not 1 <= len(key.strip()) <= 80
                or key in seen or key in remove or kind not in ('fact', 'hypothesis', 'rejected')
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000):
            return fail('需要唯一 id、有效 kind 和 1–1000 字的 text；同一 id 不能同时删除和更新')
        seen.add(key)
        sources = item.get('sources', [])
        if (not isinstance(sources, list) or len(sources) > 4
                or kind in ('fact', 'rejected') and not sources):
            return fail('fact/rejected 需要 1–4 个原始工具来源；hypothesis 可暂不引用')
        checked = []
        for source in sources:
            if not isinstance(source, dict):
                return fail('来源需要对象')
            index, quote = source.get('message_index'), source.get('quote')
            messages = state.get('messages') or []
            if type(index) is not int or not 0 <= index < len(messages):
                return fail('message_index 不在当前路线的原始历史中')
            message = messages[index]
            observation = message.get(observation_key) or {}
            if (message.get('role') != 'tool' or not observation
                    or observation.get('tool') in {'evidence_memory', 'note', 'blackboard_write'}):
                return fail('只能引用当前路线的原始工具观察，不能引用助手或笔记')
            if kind == 'fact' and not observation.get('ok'):
                return fail('失败的工具观察可用于假设或排除路径，不能标作成功事实')
            content = message.get('content')
            if observation.get('tool') == 'history_output' and isinstance(content, str):
                try:
                    content = json.loads(content).get('text')
                except (ValueError, AttributeError):
                    content = None
            if (not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 1200
                    or not isinstance(content, str) or quote not in content):
                available = []
                if isinstance(quote, str) and 1 <= len(quote.strip()) <= 1200:
                    # 执行预览与回读页的编号不同；只定位已经读过且同源的逐字引用，不代为改写引用。
                    for page_index, page in enumerate(messages):
                        observed = page.get(observation_key) or {}
                        if (page.get('role') != 'tool' or observed.get('tool') != 'history_output'
                                or observed.get('source_message_index') != index):
                            continue
                        try:
                            raw = json.loads(page.get('content') or '').get('text')
                        except (ValueError, AttributeError, TypeError):
                            continue
                        if isinstance(raw, str) and quote in raw:
                            available.append({'message_index': page_index, 'quote': quote,
                                              'expected_sha256': source_digest(page)})
                return fail('quote 必须是原始工具 content 中连续的 1–1200 字，不能改写或虚构。'
                            '若引用落盘原文，请使用已回读页的 evidence_source 编号，而非源执行的预览编号。',
                            **({'available_quote_sources': available[-4:]} if available else {}))
            digest = source_digest(message)
            expected = source.get('expected_sha256')
            if expected not in (None, '') and (not isinstance(expected, str) or len(expected) != 64
                    or any(character not in '0123456789abcdef' for character in expected)):
                return fail('expected_sha256 须为 64 位小写十六进制；未知时省略或留空，不能猜指纹')
            if expected not in (None, '', digest):
                return fail('来源版本已经变化，请重新读取后判断')
            checked.append({'message_index': index, 'sha256': digest, 'quote': quote,
                            'invocation_id': observation.get('invocation_id', '')})
        items[key] = {'id': key, 'kind': kind, 'text': text, 'sources': checked}
    updated = list(items.values())
    if len(updated) > 24 or len(json.dumps(updated, ensure_ascii=False)) > max_chars:
        return fail('笔记容量不足；请由你合并或删除不再需要的条目，不会自动丢弃证据')
    changed = updated != current['items']
    state[memory_key] = {'revision': current['revision'] + changed, 'items': updated}
    result = {'ok': True, 'changed': changed, **snapshot(state, memory_key=memory_key)}
    if not changed:
        result['notice'] += ' 相同内容已保存，没有新增修改；无需重复保存确认，继续当前任务。'
    return result
