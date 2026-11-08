"""轻量路线的原文目录与模型笔记；不从助手正文推断事实或替模型规划。"""
from __future__ import annotations

import copy
import json
from typing import Any

from spiritscout.ctf.evidence_memory import SCHEMA, execute, snapshot, source_digest
from spiritscout.ctf.evidence_review import result_reference

MEMORY_KEY = 'light_evidence_memory'
MEMORY_GUIDANCE = (
    'evidence_memory 保存你选择的发现、假设、排除路径和待验证缺口，最新修订跨归档保留。'
    'fact/rejected 必须引用原始工具 message_index 与逐字 quote；hypothesis 可无来源。'
    'matched 仅表示引用匹配，不证明判断正确。发现反证时修订笔记，不用自己的旧分析证明自己。'
    '长任务的重要早期字段、来源与待验证缺口及时记入笔记，不能靠记住所有旧回合。'
    'history_read(view=index, find=原文片段) 搜索消息；view=content 读消息原文，'
    'view=output 按 message_index 搜索或分页读取已落盘的完整输出，不必重新执行。'
    '消息原文也可能只有预览，检索未命中不代表完整输出没有；has_more 时用 next_call 续读。'
)
MEMORY_SCHEMA = copy.deepcopy(SCHEMA)
MEMORY_SCHEMA['function']['description'] = (
    '持久证据笔记。read 查当前 revision；update 按 base_revision 更新。'
    'fact/rejected 需原始工具编号和逐字 quote，hypothesis 可无来源。最新修订始终保留，引用匹配不等于判断正确。')
HISTORY_SCHEMA = {'type': 'function', 'function': {
    'name': 'history_read', 'description': (
        'index 搜索工具消息/参数，content 读消息，默认 message 读 JSON。'
        '消息可能是预览；output 按 message_index 读落盘输出。find 精确搜索；next_call 续页。'),
    'parameters': {'type': 'object', 'properties': {
        'message_index': {'type': 'integer'}, 'instruction_id': {'type': 'string'},
        'view': {'type': 'string', 'enum': ['index', 'message', 'content', 'output']},
        'find': {'type': 'string', 'maxLength': 200},
        'offset': {'type': 'integer', 'description': 'index 为原始消息游标；其他视图为字符偏移'},
        'length': {'type': 'integer', 'minimum': 1, 'maximum': 4000},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20},
        'expected_sha256': {'type': 'string', 'description': '续读来源指纹'},
        'output_part': {'type': 'string', 'enum': ['current', 'previous'], 'description': '日志轮转代'},
    }}}}

# 回读和笔记不成为新的独立来源，避免目录递归膨胀和笔记自证。
_DERIVED_TOOLS = {'history_read', 'evidence_memory', 'note', 'blackboard_read', 'blackboard_write',
                  'exploration_write', 'graph_read', 'asset_link', 'steering_feedback'}


def call_details(call: dict[str, Any]) -> tuple[str, Any]:
    function = call.get('function') or {}
    name, arguments = function.get('name', ''), function.get('arguments', {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            pass
    if name in {'tool_invoke', 'revise_step'} and isinstance(arguments, dict):
        name, arguments = arguments.get('name', ''), arguments.get('arguments', {})
    return name, arguments


def tool_records(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只匹配同一 assistant 批次的真实结果，编号始终指向原始历史。"""
    calls = {}
    rows = []
    for index, message in enumerate(messages):
        if message.get('role') != 'tool':
            calls = {c.get('id'): call_details(c) for c in message.get('tool_calls', [])}
            continue
        name, arguments = calls.get(message.get('tool_call_id'), ('', {}))
        if not name or name in _DERIVED_TOOLS:
            continue
        rows.append({'message_index': index, 'tool': name, 'arguments': arguments,
                     'message': message})
    return rows


def _short(value: Any, length: int = 100) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return text if len(text) <= length else text[:length // 2] + '…' + text[-length // 2:]


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    """索引仅投影工具已返回的结构字段，不把模型分析提升为结论。"""
    arguments = row['arguments']
    preview = ({k: _short(v, 60) if isinstance(v, (str, dict, list)) else v
                for k, v in arguments.items() if k not in {'offset', 'limit'}}
               if isinstance(arguments, dict) else _short(arguments))
    if len(json.dumps(preview, ensure_ascii=False)) > 400:
        preview = _short(preview, 400)
    result = {'tool': row['tool'], 'arguments_preview': preview}
    observation = row['message'].get('_light_observation') or {}
    if type(observation.get('ok')) is bool:
        result['ok'] = observation['ok']
    if observation.get('output_file'):
        result['output_read_call'] = {'name': 'history_read', 'arguments': {
            'message_index': row['message_index'], 'view': 'output'}}
        result['message_is_preview'] = observation.get('partial', False)
    try:
        data = json.loads(row['message'].get('content') or '')
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        if isinstance(data.get('function'), dict):
            result['function'] = {key: value if type(value) is int else _short(value)
                                  for key in ('name', 'addr') if (value := data['function'].get(key)) is not None}
        for key in ('ok', 'operation'):
            if key in data:
                result[key] = data[key] if type(data[key]) is bool else _short(data[key], 40)
    return result


def catalog_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一查询的页号合列；仍保留不同结果的来源，不宣称它们内容相同。"""
    grouped = {}
    for row in rows:
        if '_catalog_identity' not in row:
            row['_catalog_identity'] = _identity(row)
            arguments = row['arguments']
            # 分组用完整参数，缩略路径碰撞不能合并不同来源。
            query = {k: v for k, v in arguments.items() if k not in {'offset', 'limit'}} if isinstance(arguments, dict) else arguments
            row['_catalog_key'] = json.dumps([row['tool'], query, row['_catalog_identity'].get('function'),
                                             row['_catalog_identity'].get('ok')],
                                              ensure_ascii=False, sort_keys=True)
        identity, key = row['_catalog_identity'], row['_catalog_key']
        item = grouped.setdefault(key, {**identity, 'message_indices': []})
        item['message_indices'].append(row['message_index'])
    result = []
    for item in grouped.values():
        indices = item['message_indices']
        result.append({**item, 'message_indices': indices if len(indices) <= 8 else indices[:2] + indices[-6:],
                       **({'total_results': len(indices)} if len(indices) > 8 else {})})
    return result


def validate_history_args(args: dict[str, Any]) -> str | None:
    view, query = args.get('view', 'message'), args.get('find')
    offset, length, limit = args.get('offset', 0), args.get('length', 2000), args.get('limit', 8)
    digest = args.get('expected_sha256')
    if args.get('instruction_id') is not None and (args.get('message_index') is not None or view == 'output'):
        return 'message_index 与 instruction_id 不能混用；output 只接受执行来源 message_index'
    if (view not in ('index', 'message', 'content', 'output') or type(offset) is not int or offset < 0
            or type(length) is not int or not 1 <= length <= 4000
            or type(limit) is not int or not 1 <= limit <= 20
            or query is not None and (not isinstance(query, str) or not 1 <= len(query) <= 200)
            or digest is not None and (not isinstance(digest, str) or len(digest) != 64
                                      or any(c not in '0123456789abcdef' for c in digest))):
        return '需要合法 view、非负 offset、1–4000 的 length、1–20 的 limit、1–200 字的 find 和有效来源指纹'
    return None


def history_source(state: dict[str, Any], index: Any) -> dict[str, Any]:
    messages = state.get('messages', [])
    if type(index) is not int or not 0 <= index < len(messages) or messages[index].get('role') != 'tool':
        return {}
    source = dict(messages[index].get('_light_observation') or {})
    # 兼容升级前的检查点，不能把消息正文中的路径当成可信的执行来源。
    if not source.get('output_file'):
        rows = [*state.get('evidence_rows', []), *[
            entry['first_observation'] for entry in state.get('evidence_seen', {}).values()
            if entry.get('first_observation')]]
        for row in rows:
            if result_reference(row, messages).get('message_index') == index:
                source = {**row, **source}
                break
    return source


def history_page(state: dict[str, Any], args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """显式续读参数与最近回读位置；重复读取仍返回请求的页，不暗中移动游标。"""
    if not result.get('ok'):
        return result
    view = result['view']
    if result.get('has_more'):
        continuation = {key: args[key] for key in ('message_index', 'instruction_id', 'length', 'limit', 'output_part')
                        if key in args}
        continuation.update(view=view, offset=result['next_offset'])
        if view == 'index' and 'find' in args:
            continuation['find'] = args['find']
        if result.get('source_sha256'):
            continuation['expected_sha256'] = result['source_sha256']
        result['next_call'] = {'name': 'history_read', 'arguments': continuation}
    if view in ('content', 'message'):
        source = history_source(state, args.get('message_index'))
        if source.get('output_file'):
            result['message_is_preview'] = bool(source.get('partial'))
            result['output_read_call'] = {'name': 'history_read', 'arguments': {
                'view': 'output', 'message_index': args['message_index'],
                **({'find': args['find']} if 'find' in args else {})}}
    identity = {key: args[key] for key in ('message_index', 'instruction_id', 'find', 'output_part') if key in args}
    identity['view'] = view
    progress = state.setdefault('history_read_progress', [])
    previous = next((row for row in progress if row['source'] == identity), {})
    same_page = bool(previous and previous.get('offset') == result.get('offset', args.get('offset', 0))
                     and previous.get('next_offset') == result.get('next_offset')
                     and previous.get('source_sha256') == result.get('source_sha256'))
    repeat = previous.get('same_page_count', 1) + 1 if same_page else 1
    result['same_page_count'] = repeat
    if same_page:
        result['pagination_note'] = '这页已经读过；如需后续内容直接调用 next_call，若找不到字段请核对 output_read_call。'
    progress[:] = [row for row in progress if row['source'] != identity][-3:] + [{
        'source': identity, 'offset': result.get('offset', args.get('offset', 0)),
        'next_offset': result.get('next_offset'), 'has_more': result.get('has_more', False),
        'source_sha256': result.get('source_sha256'), 'same_page_count': repeat,
        **{key: result[key] for key in ('next_call', 'output_read_call') if key in result}}]
    return result


def read_history(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    view = args.get('view', 'message')
    query = args.get('find')
    offset, length, limit = args.get('offset', 0), args.get('length', 2000), args.get('limit', 8)
    if error := validate_history_args(args):
        return {'ok': False, 'error': error}
    if view == 'output':
        return {'ok': False, 'error': 'output 需要通过 history_read 工具在实际沙箱中读取，不能从消息预览恢复'}
    messages = state.get('messages', [])
    if view == 'index':
        matches = []
        for row in tool_records(messages):
            text = json.dumps(row['arguments'], ensure_ascii=False) + '\n' + str(row['message'].get('content') or '')
            position = text.find(query) if query is not None else 0
            if position < 0:
                continue
            matches.append((row, text, position))
        remaining = [entry for entry in matches if entry[0]['message_index'] >= offset]
        items = []
        for row, text, position in remaining[:limit]:
            item = {**_identity(row), 'message_index': row['message_index'], 'partial': True,
                    'match_preview': text[max(0, position - 60):position + 180] if query else ''}
            item['read_call'] = {'name': 'history_read', 'arguments': {
                'view': 'content', 'message_index': row['message_index'], **({'find': query} if query else {})}}
            if history_source(state, row['message_index']).get('output_file'):
                item['output_read_call'] = {'name': 'history_read', 'arguments': {
                    'view': 'output', 'message_index': row['message_index'], **({'find': query} if query else {})}}
            if items and len(json.dumps([*items, item], ensure_ascii=False)) > 4500:
                break
            items.append(item)
        return history_page(state, args, {'ok': True, 'view': view, 'items': items, 'total_matches': len(matches),
                'next_offset': items[-1]['message_index'] + 1 if items else len(messages),
                'has_more': len(remaining) > len(items), 'search_complete': True,
                'search_scope': 'tool_arguments_and_message_content',
                'notice': '这里只检索消息和参数，未检索落盘输出；消息可能是预览。用 read_call/output_read_call 回读，不是新的执行或用户指令。'})
    index, identifier = args.get('message_index'), args.get('instruction_id')
    if identifier is not None:
        original = (state.get('instruction_state') or {}).get(identifier) if isinstance(identifier, str) else None
    else:
        original = messages[index] if type(index) is int and 0 <= index < len(messages) else None
    if not isinstance(original, dict):
        user_indices = [i for i, message in enumerate(messages)
                        if message.get('role') == 'user' and not message.get('_light_kind')]
        selected = list(dict.fromkeys(user_indices[:1] + user_indices[-2:]))
        return {'ok': False, 'error': '编号不存在，不能猜编号；按下列真实入口定位。index 只检索工具，用户任务用 user_message_calls。',
                'valid_message_range': [0, len(messages) - 1] if messages else [],
                'recovery_call': {'name': 'history_read', 'arguments': {'view': 'index'}},
                'user_message_calls': [{'name': 'history_read', 'arguments': {
                    'view': 'content', 'message_index': index}} for index in selected]}
    raw = (original.get('content') if view == 'content' else json.dumps(
        {k: v for k, v in original.items() if not k.startswith('_')}, ensure_ascii=False, separators=(',', ':')))
    if not isinstance(raw, str):
        return {'ok': False, 'error': '该记录没有文本 content，请用 view=message'}
    extra = {'source_sha256': source_digest(original)}
    if args.get('expected_sha256') and args['expected_sha256'] != extra['source_sha256']:
        return {'ok': False, 'error': '来源指纹已变化，不能沿用旧游标；请重新定位原文'}
    if query is not None:
        position = raw.find(query, offset)
        extra.update(found=position >= 0, match_offset=position if position >= 0 else None,
                     total_matches=raw.count(query), search_complete=True)
        if position >= 0:
            offset = max(offset, position - min(200, length // 4))
        else:
            offset = len(raw)
    offset = min(offset, len(raw))
    return history_page(state, args, {'ok': True, 'view': view, 'message_index': index, 'instruction_id': identifier,
            'offset': offset, 'text': raw[offset:offset + length], 'total_chars': len(raw),
            'next_offset': min(offset + length, len(raw)), 'has_more': offset + length < len(raw), **extra})


def memory_execute(state: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    # 旧检查点从已留存的工具回执补来源标签，不改写原文、不接受助手或笔记自证。
    messages = list(state.get('messages', []))
    sources = [*state.get('evidence_rows', []), *[
        entry['first_observation'] for entry in state.get('evidence_seen', {}).values() if entry.get('first_observation')]]
    actual = {row['message_index']: row for row in tool_records(messages)}
    for source in sources:
        index = result_reference(source, messages).get('message_index')
        if (index in actual and actual[index]['tool'] == source.get('name')
                and source.get('tool_call_id') in (None, messages[index].get('tool_call_id'))
                and not messages[index].get('_light_observation')):
            messages[index] = {**messages[index], '_light_observation': {
                'tool': source['name'], 'ok': source.get('ok'), 'invocation_id': source.get('invocation_id', '')}}
    working = {**state, 'messages': messages}
    result = execute(working, args, memory_key=MEMORY_KEY, observation_key='_light_observation', max_chars=4000)
    if result['ok'] and MEMORY_KEY in working:
        state[MEMORY_KEY] = working[MEMORY_KEY]
    return result


def working_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """从持久观察重建小型工作黑板；不依赖模型主动记笔记，不替它推断事实。"""
    messages = state.get('messages', [])
    observations = {}
    for key, entry in state.get('evidence_seen', {}).items():
        row = entry.get('first_observation')
        if row and row.get('name') not in _DERIVED_TOOLS:
            observations[key] = {**row, 'same_result_count': entry['count']}
    for row in state.get('evidence_rows', []):
        if row.get('name') not in _DERIVED_TOOLS:
            observations[row.get('observation_key', str(row.get('message_index')))] = row
    ordered = sorted(observations.values(), key=lambda row: row.get('message_index') or -1)
    # 早期定位优先保留成功回执，避免初始路径错误长期挤掉后来实际可用的读取方式。
    # 最近失败仍原样保留；未入选的观察可按历史目录回读，不替模型判断结论。
    successful = [row for row in ordered if row.get('ok')]
    anchors = (successful or ordered)[:2]
    selected = ordered if len(ordered) <= 4 else [row for row in anchors if row not in ordered[-2:]] + ordered[-2:]
    rows = []
    for row in selected:
        index = result_reference(row, messages).get('message_index')
        if index is None:
            continue
        item = {'tool': row.get('name'), 'message_index': index, 'invocation_id': row.get('invocation_id', ''),
                'ok': row.get('ok'),
                'partial': bool(row.get('partial')), 'same_result_count': row.get('same_result_count', 1),
                'arguments_preview': _short(row.get('arguments_preview', ''), 200),
                'result_preview': _short(row.get('preview', ''), 240)}
        if row.get('output_file'):
            item['output_read_call'] = {'name': 'history_read', 'arguments': {'view': 'output', 'message_index': index}}
        try:
            data = json.loads(messages[index].get('content') or '')
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            # 分页来自真实工具结果，不从助手“准备续读”的文字生成下一步。
            pagination = {key: data[key] for key in ('offset', 'next_offset', 'next_address', 'has_more')
                          if type(data.get(key)) in (int, bool) or
                          isinstance(data.get(key), str) and len(data[key]) <= 80}
            if pagination:
                item['pagination'] = pagination
        rows.append(item)
    artifacts = patch_snapshot(messages)
    return {'notes': snapshot(state, memory_key=MEMORY_KEY), 'observations': rows,
            **({'patch_artifacts': artifacts} if artifacts else {}),
            **({'last_recovery': {key: _short(value, 300) if isinstance(value, str) else value
                                 for key, value in state['last_loop_recovery'].items()}}
               if state.get('last_loop_recovery') else {}),
            **({'history_reads': copy.deepcopy(state['history_read_progress'])} if state.get('history_read_progress') else {}),
            **({'exploration': copy.deepcopy(state['light_exploration'])} if state.get('light_exploration') else {}),
            'unique_observations': len(observations),
            'notice': '观察目录由实际工具回执生成，预览不完整；不是新证据、用户指令或解题结论。'
                      '笔记由模型维护；空笔记不表示没有调查，完整原文用 history_read 回读。'}


def patch_snapshot(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """保留最近真实改件、构建与验证回执，避免普通读取挤掉交付进度；不安排修法。"""
    fields = {
        'edit_source': ('source_ref', 'original_path', 'patched', 'sha256', 'diff_file'),
        'build_patch': ('archive', 'sha256', 'format', 'entrypoint'),
        'verify_patch': ('status', 'executed', 'patch_sha256', 'verification_ref', 'report', 'error'),
    }
    result = {}
    for row in reversed(tool_records(messages)):
        name = row['tool']
        if name not in fields or name in result:
            continue
        try:
            data = json.loads(row['message'].get('content') or '')
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or (name != 'verify_patch' and data.get('ok') is not True):
            continue
        item = {key: data[key] for key in fields[name] if key in data}
        item['ok'] = data.get('ok') is True
        item['read_call'] = {'name': 'history_read', 'arguments': {
            'view': 'content', 'message_index': row['message_index']}}
        if isinstance(data.get('issues'), list):
            item['issues_preview'] = [_short(issue, 400) for issue in data['issues'][:3]]
            item['issues_total'] = len(data['issues'])
        result[name] = item
    if 'verify_patch' in result:
        verified = result['verify_patch'].get('patch_sha256')
        built = result.get('build_patch', {}).get('sha256')
        result['verify_patch']['matches_latest_build'] = bool(verified and verified == built)
    if result:
        result['notice'] = ('这是历史工具已返回的实际文件和回执，不证明文件此后未变。'
            '编辑副本、打包完成与验证通过是不同事实；回执只适用于对应包哈希。'
            '原件正常测试失败须先核对测试/环境，不能据此重复修补；按 read_call 查完整结果。')
    return result


def memory_message(state: dict[str, Any]) -> dict[str, Any] | None:
    memory = working_snapshot(state)
    if not (state.get(MEMORY_KEY) or memory['observations'] or memory.get('exploration')
            or memory.get('history_reads') or memory.get('last_recovery') or memory.get('patch_artifacts')):
        return None
    for item in memory['notes']['items']:
        item['sources'] = [{'message_index': s['message_index'], 'quote_preview': _short(s['quote'], 160),
                            'status': s['status']} for s in item['sources']]
    return {'role': 'user', '_context_contract': True, '_light_kind': 'memory', 'content': (
        '当前工作黑板（观察自动保留；模型笔记仅此修订有效，旧笔记不再沿用）：\n'
        + json.dumps(memory, ensure_ascii=False, separators=(',', ':')))}
