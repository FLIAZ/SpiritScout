"""按需回读源码结构和原文，为小上下文模型提供定位依据。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
from pathlib import Path

from spiritscout.awdp.patches import _open_regular
from spiritscout.awdp.scanner import _source_path
from spiritscout.tools.sandbox_paths import (
    open_directory,
    sandbox_directory,
    sandbox_tmp_directory,
)

SCHEMA = {'type': 'function', 'function': {'name': 'source_context',
    'description': 'AWDP 源码导航：index 离线快照源码/配置并提取函数；map 分页列文件；find 查定义；references 查调用/别名线索；read 精确回读原文和哈希。索引不执行项目。调用线索不等于已证实漏洞，修改后须重新 index。',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'action': {'type': 'string', 'enum': ['index', 'map', 'find', 'references', 'read']},
        'path': {'type': 'string', 'description': 'index 的本题源码目录/文件；read 也可直接提供实际文件 path，无需先建索引；压缩包先安全解包'},
        'index': {'type': 'string', 'description': '后续操作使用返回的 index.json 路径'},
        'symbol': {'type': 'string', 'description': 'find/references 的精确符号名或限定名；同名候选全部保留'},
        'file_id': {'type': 'string', 'description': 'read 必填；find/references 可选，限定文件'},
        'line': {'type': 'integer', 'minimum': 1, 'description': 'read 起始行，默认 1'},
        'lines': {'type': 'integer', 'minimum': 1, 'maximum': 160, 'description': 'read 行窗口，默认 60；长窗口仍分页'},
        'offset': {'type': 'integer', 'minimum': 0, 'description': '结果页偏移；read 时是当前行窗口内字符偏移，续读请照 next_call'},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12},
        'timeout': {'type': 'integer', 'minimum': 5, 'maximum': 120}}, 'required': ['action']}}}


def _load(root: Path, challenge: str, raw: str) -> tuple[Path, dict]:
    if not isinstance(raw, str) or not raw:
        raise ValueError('需要 source_context 返回的 index 路径')
    path = Path(raw)
    if path.is_absolute():
        path = path.relative_to(root)
    base = Path('artifacts') / 'awdp-context' / sandbox_directory(challenge).name
    if not path.is_relative_to(base) or path.name != 'index.json':
        raise ValueError('只能读取本题 source_context 索引')
    with _open_regular(root, path) as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('索引过大，请缩小源码范围后重新 index')
    return path, json.loads(data)


def _text(root: Path, index: Path, item: dict) -> str:
    snapshot = item['snapshot']
    if not isinstance(snapshot, str) or not re.fullmatch(r'files/f[0-9]+\.[a-z]+', snapshot):
        raise ValueError('索引中的快照路径无效')
    with _open_regular(root, index.parent / snapshot) as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('快照哈希不一致，请重新 index；不能引用已变动原文')
    return data.decode('utf-8')


def _call(index_path: str, **arguments) -> dict:
    return {'name': 'source_context', 'arguments': {'index': index_path, **arguments}}


def _matches(name: str, query: str) -> bool:
    return name == query or name.endswith('.' + query)


def _references(root: Path, index: Path, item: dict, query: str):
    content = _text(root, index, item).splitlines()
    if item['navigation'] == 'python_ast':
        for call in item['calls']:
            match = 'call_name_candidate' if _matches(call['name'], query) else ''
            for entry in item['imports']:
                alias = entry['alias']
                if call['name'] == alias or call['name'].startswith(alias + '.'):
                    canonical = '.'.join(filter(None, [entry['module'], entry['name']]))
                    # import module as alias 与 from module import symbol as alias 分别展开。
                    if entry['type'] == 'import':
                        canonical = entry['module']
                    resolved = canonical + call['name'][len(alias):]
                    if _matches(resolved, query):
                        match = 'import_alias_candidate'
            if match:
                yield {**call, 'match': match, 'text': content[call['line'] - 1][:600]}
    else:
        pattern = re.compile(r'(?<![\w$])' + re.escape(query) + r'(?![\w$])')
        for number, line in enumerate(content, 1):
            if pattern.search(line):
                yield {'name': query, 'line': number, 'text': line[:600],
                       'match': 'lexical_candidate', 'binding_verified': False}


def query_index(root: Path, challenge: str, args: dict) -> dict:
    index, report = _load(root, challenge, args.get('index'))
    index_name = str(root / index)
    action, offset, limit = args['action'], args.get('offset', 0), args.get('limit', 6)
    result = {'ok': True, 'index': index_name, 'status': report['status'], 'coverage': report['coverage'],
              'ctags': report['ctags'], 'limitations': report['limitations'],
              'errors': report['errors'][:4], 'error_count': len(report['errors']),
              'skipped': report['skipped'][:4], 'skipped_count': len(report['skipped'])}
    files = report['files']
    if args.get('file_id'):
        files = [x for x in files if x['file_id'] == args['file_id']]
        if not files:
            raise ValueError('file_id 不在此索引中')
    if action == 'read':
        if not args.get('file_id'):
            raise ValueError('read 需要 file_id，可通过 map/find 获取')
        item = files[0]
        lines = _text(root, index, item).splitlines(keepends=True)
        start, size = args.get('line', 1), args.get('lines', 60)
        if start > max(1, len(lines)):
            raise ValueError('line 超出文件行数')
        window = ''.join(lines[start - 1:start - 1 + size])
        if offset > len(window):
            raise ValueError('offset 超出当前行窗口')
        text = window[offset:offset + 5000]
        next_offset = offset + len(text)
        more_window = next_offset < len(window)
        more_lines = start - 1 + size < len(lines)
        result.update(file_id=item['file_id'], path=item['path'], sha256=item['sha256'],
            line=start, end_line=min(start - 1 + size, len(lines)), offset=offset,
            content=text, window_complete=not more_window, has_more=more_window or more_lines,
            imports=item['imports'][:12], imports_partial=len(item['imports']) > 12)
        if result['has_more']:
            result['next_call'] = _call(index_name, action='read', file_id=item['file_id'], lines=size,
                line=start if more_window else start + size, offset=next_offset if more_window else 0)
        return result
    query = args.get('symbol')
    if action in {'find', 'references'} and (not isinstance(query, str) or not query.strip() or len(query) > 256):
        raise ValueError('find/references 需要 1–256 字符 symbol')
    def candidates():
        for item in files:
            info = {key: item[key] for key in ('file_id', 'path', 'sha256')}
            if action == 'map':
                yield {**info, 'lines': item['lines'], 'navigation': item['navigation'],
                       'definitions': len(item['symbols']), 'calls': len(item['calls']),
                       'symbols': [{key: symbol[key] for key in ('qualified', 'kind', 'line', 'end_line')}
                                   for symbol in item['symbols'][:6]],
                       'symbols_partial': len(item['symbols']) > 6,
                       'read_call': _call(index_name, action='read', file_id=item['file_id'])}
                continue
            records = (symbol for symbol in item['symbols'] if _matches(symbol['qualified'], query)) if action == 'find' \
                else _references(root, index, item, query)
            for record in records:
                line = record['line']
                count = min(80, (record.get('end_line') or line + 24) - line + 1)
                yield {**info, **record, 'read_call': _call(index_name, action='read',
                    file_id=item['file_id'], line=line, lines=max(1, count))}

    page, length, total, truncated, full = [], 0, 0, False, False
    for item in candidates():
        if total == 40000:
            truncated = True
            break
        position = total
        total += 1
        if position < offset or len(page) >= limit or full:
            continue
        length += len(json.dumps(item, ensure_ascii=False))
        if page and length > 6000:
            full = True
        else:
            page.append(item)
    next_offset = offset + len(page)
    result.update(items=page, total=total, offset=offset, has_more=next_offset < total,
                  matches_truncated=truncated)
    if truncated:
        result['query_note'] = '候选超过 40000 项，本次只保留前 40000 项；用 file_id 或限定符号名缩小检索。'
    if result['has_more']:
        result['next_call'] = _call(index_name, **{**args, 'index': index_name, 'offset': next_offset})
    return result


async def source_context(agent, task, route, args):
    from spiritscout.ctf.agents import _ToolOutcome

    try:
        if not isinstance(args, dict) or set(args) - set(SCHEMA['function']['parameters']['properties']):
            raise ValueError('source_context 参数不符合契约')
        action = args.get('action')
        if action not in {'index', 'map', 'find', 'references', 'read'}:
            raise ValueError('action 必须为 index/map/find/references/read')
        for key, default, low, high in [('offset', 0, 0, 10**9), ('limit', 6, 1, 12),
                ('line', 1, 1, 10**9), ('lines', 60, 1, 160), ('timeout', 30, 5, 120)]:
            value = args.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{key} 必须为 {low}–{high} 的整数')
        workspace = (agent.ctx.builtin_ctx or {}).get('workspace_root')
        if not workspace:
            raise ValueError('缺少会话工作区')
        root, challenge, metadata = Path(workspace).resolve(), agent.challenge_id(task), {}
        direct_read = action == 'read' and not args.get('index') and bool(args.get('path'))
        if action == 'index' or direct_read:
            source = _source_path(root, challenge, args.get('path'))
            if direct_read:
                host = Path(source)
                if not host.is_relative_to(root):
                    host = root / sandbox_tmp_directory(challenge) / host.relative_to('/tmp')
                if not host.is_file():
                    raise ValueError('read 的 path 必须为实际文件；目录请用 index')
            relative = Path('artifacts') / 'awdp-context' / sandbox_directory(challenge).name / os.urandom(8).hex()
            with open_directory(root, relative, create=True):
                pass
            timeout = args.get('timeout', 30)
            request = {'path': source, 'directory': str(root / relative), 'timeout': timeout}
            script = Path(__file__).with_name('context_worker.py').read_text()
            command = 'python3 -I -c ' + shlex.quote(script) + ' ' + shlex.quote(json.dumps(request, ensure_ascii=False))
            outcome = await agent.ctx.tools.call('sandbox_exec', {'code': command, 'language': 'shell',
                'action': 'analysis', 'timeout': timeout + 15, 'log_name': 'source_context'}, **agent._tool_ctx(task))
            metadata = {'invocation_id': outcome.invocation_id, 'output_file': outcome.parsed.get('output_file')}
            if not outcome.ok or outcome.returncode:
                return _ToolOutcome(ok=False, metadata=metadata, text='源码索引未完成：' +
                    (outcome.error or outcome.stderr or outcome.stdout)[-1200:])
            args = {**args, 'action': 'read' if direct_read else 'map', 'index': str(root / relative / 'index.json')}
            if direct_read:
                args['file_id'] = 'f0000'
        data = await asyncio.to_thread(query_index, root, challenge, args)
        agent.ctx.audit.log('awdp.source_context', agent.agent_id, {'task_id': task.task_id,
            'route_id': route.route_id, 'action': action, 'index': data['index'], 'status': data['status']})
        return _ToolOutcome(ok=True, metadata=metadata, text=json.dumps(data, ensure_ascii=False),
                            note='源码导航候选；需结合真实入口核对调用和数据流')
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        return _ToolOutcome(ok=False, text='源码导航失败：' + str(exc)[:600])
