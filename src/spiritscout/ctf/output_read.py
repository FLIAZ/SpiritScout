"""按执行来源回读沙箱日志；完整文件留在沙箱，仅返回有指纹的文本页。"""
from __future__ import annotations

import json
import shlex

_SCRIPT = r'''
import hashlib, json, os, stat, sys
from pathlib import Path
path, offset, length, query, expected, captured = json.loads(sys.argv[1])
try:
    source = Path(path)
    if source.parent != Path.cwd() or source.is_symlink():
        raise ValueError('日志不在当前题目的工作目录或已被链接替换')
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('日志不是普通文件')
        data = stream.read(16 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    if len(data) > 16 * 1024 * 1024:
        raise ValueError('单代日志超过 16 MiB，请在沙箱内按目标字段读取')
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('读取期间日志变化，请重新读取')
    digest = hashlib.sha256(data).hexdigest()
    if captured and captured != digest:
        raise ValueError('日志指纹与执行时保存的原始字节不符，不能当作该执行的原文')
    if expected and expected != digest:
        raise ValueError('来源指纹已变化，不能沿用旧游标；重新定位来源后从 offset=0 读取')
    raw = data.decode('utf-8')
    if '\x00' in raw:
        raise ValueError('日志含 NUL，不作为完整文本展示；请用 python 按字节/实际编码读取')
    result = {'ok': True, 'source_sha256': digest, 'total_chars': len(raw), 'total_bytes': len(data)}
    if query is not None:
        position = raw.find(query, offset)
        result.update(found=position >= 0, match_offset=position if position >= 0 else None,
                      total_matches=raw.count(query), search_complete=True)
        offset = max(offset, position - min(200, length // 4)) if position >= 0 else len(raw)
    offset = min(offset, len(raw))
    size = min(length, len(raw) - offset)
    while True:
        result.update(offset=offset, text=raw[offset:offset + size], next_offset=offset + size,
                      has_more=offset + size < len(raw))
        encoded = json.dumps(result, ensure_ascii=True)
        if len(encoded) <= 3400:
            break
        size //= 2
    print(encoded)
except (OSError, ValueError) as error:
    print(json.dumps({'ok': False, 'error': str(error)[:500]}))
'''


async def read_output(agent, task, arguments, source):
    """source 只能来自当前会话的执行记录，不接收模型任意指定的宿主路径。"""
    part = arguments.get('output_part', 'current')
    if part not in ('current', 'previous'):
        return {'ok': False, 'error': 'output_part 需要 current 或 previous'}
    path = source.get('output_file' if part == 'current' else 'previous_output_file')
    if not path:
        return {'ok': False, 'error': '此来源未保存该代输出；不能从展示预览恢复未留存的字节'}
    payload = json.dumps([path, arguments.get('offset', 0), arguments.get('length', 2000),
                          arguments.get('find'), arguments.get('expected_sha256'),
                          source.get('output_sha256' if part == 'current' else 'previous_output_sha256')], ensure_ascii=False)
    result = await agent.ctx.tools.call('sandbox_exec', {
        'code': 'python3 -I -c ' + shlex.quote(_SCRIPT) + ' ' + shlex.quote(payload),
        'language': 'shell', 'action': 'analysis', 'timeout': 15, 'log_name': 'history_output'},
        **agent._tool_ctx(task))
    if not result.ok or result.returncode:
        return {'ok': False, 'error': '日志回读未完成：' + (result.error or result.stderr)[:500]}
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {'ok': False, 'error': '日志回读未返回完整结构化结果，不作为完整证据'}
    data.update(view='output', message_index=arguments['message_index'], output_part=part,
                output_file=path, source_invocation_id=source.get('invocation_id', ''),
                source_execution_ok=source.get('ok'), source_rotated=bool(source.get('output_rotated')),
                search_scope='selected_log_generation',
                source_integrity=('matched_capture' if source.get(
                    'output_sha256' if part == 'current' else 'previous_output_sha256') else 'capture_hash_unavailable')
                    if data.get('ok') else 'not_verified',
                notice='实际保存的 stdout/stderr 合并日志；回读不代表重新执行。轮转后仅保留最近两代。')
    if data.get('ok'):
        data['source_complete'] = source.get('output_complete') is True and not source.get('output_rotated')
        data['unread_before'] = data['offset'] > 0
        if part == 'current' and source.get('previous_output_file'):
            data['previous_output_call'] = {'name': 'history_read', 'arguments': {
                'view': 'output', 'message_index': arguments['message_index'], 'output_part': 'previous',
                **({'find': arguments['find']} if 'find' in arguments else {})}}
    return data
