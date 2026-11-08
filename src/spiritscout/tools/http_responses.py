"""会话内 HTTP 原文快照：预览与正文分离，本地读取不再请求目标站点。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import uuid
from pathlib import Path
from typing import Any

from spiritscout.tools.invocation import current_invocation
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec


def model_preview(result: ToolResult) -> str:
    """模型预览与内部检测窗口独立；原文定位和完整性先于正文，防止后续裁剪丢引用。"""
    info, text = result.parsed, result.stdout
    if not info.get('body_ref'):
        return text
    shortened = bool(info.get('preview_truncated')) or len(text) > 3500
    if len(text) > 3500:
        text = text[:2600] + '\n...[展示省略，非完整源码]...\n' + text[-850:]
    header = {key: info.get(key) for key in
              ('body_ref', 'body_chars', 'download_complete', 'response_is_partial', 'text_decode_lossy')}
    header['preview_truncated'] = shortened
    return json.dumps(header, ensure_ascii=False, separators=(',', ':')) + '\n' + text


def save_response(workspace: str, content: bytes, text: str, metadata: dict[str, Any],
                  request: dict[str, Any] | None = None) -> dict[str, Any]:
    # 沙箱 cwd 位于 sandbox/<题目>/；返回相对项目根的路径会让后续原文读取失败。
    root = (Path(workspace) / 'http-responses').resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    ref = uuid.uuid4().hex
    folder = root / ref
    folder.mkdir(mode=0o700)
    invocation = current_invocation.get()
    info = {**metadata, 'source_invocation_id': invocation.invocation_id if invocation else '',
            'body_ref': ref, 'body_path': str(folder / 'body.txt'),
            'body_sha256': hashlib.sha256(content).hexdigest(),
            'saved_body_bytes': len(content), 'body_chars': len(text)}
    # 正文为 HTTP 内容解压后的字节；另存 UTF-8 阅读副本，保留原响应编码信息。
    for name, data in [('body.bin', content), ('body.txt', text.encode('utf-8')),
                       ('metadata.json', json.dumps(info, ensure_ascii=False).encode())]:
        path = folder / name
        with path.open('xb') as stream:
            path.chmod(0o600)
            stream.write(data)
    if request is not None:
        headers = request['headers']
        raw = request['line'] + b'\r\n' + b'\r\n'.join(k+b': '+v for k,v in headers) + b'\r\n\r\n' + request['body']
        argv = ['curl', '--path-as-is', '--request', request['method'], request['url']]
        for key, value in headers:
            argv.extend(['--header', key.decode('latin-1')+': '+value.decode('latin-1')])
        if request['body']:
            argv.extend(['--data-binary', '@request-body.bin'])
        for name, data in [('request.http',raw), ('request-body.bin',request['body']),
                           ('poc.sh',shlex.join(argv).encode())]:
            with (folder/name).open('xb') as output:
                (folder/name).chmod(0o600)
                output.write(data)
        info['evidence_files'] = {key:f'http-responses/{ref}/{name}' for key,name in
            (('request','request.http'),('response_body','body.bin'),('response_metadata','metadata.json'),
             ('poc','poc.sh'),('request_body','request-body.bin'))}
        info['evidence_note'] = '最终 HTTP 请求与已保存的解压后响应正文、原响应头分开交付；不是原始网络帧。PoC 从实际请求重建，尚未另行执行。'
    return info


def _read(*, workspace_root: str, body_ref: str, mode: str = 'read', offset: int = 0,
          limit: int = 3500, query: str = '', start_line: int | None = None) -> dict[str, Any]:
    if not workspace_root or not re.fullmatch(r'[a-f0-9]{32}', body_ref):
        raise ValueError('需要本会话 HTTP 工具返回的 body_ref')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 4000:
        raise ValueError('offset 为非负字符偏移，limit 为 1–4000')
    root = (Path(workspace_root) / 'http-responses').resolve()
    folder = root / body_ref
    if folder.is_symlink() or folder.resolve().parent != root:
        raise ValueError('响应引用越界')
    for name in ('body.txt', 'metadata.json'):
        if (folder / name).is_symlink():
            raise ValueError('响应文件不能是符号链接')
    info = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
    with (folder / 'body.txt').open(encoding='utf-8', newline='') as stream:
        text = stream.read()
    result = {key: info[key] for key in ('body_ref', 'body_sha256', 'body_chars', 'download_complete', 'source_invocation_id')}
    result['text_decode_lossy'] = bool(info.get('text_decode_lossy'))
    if start_line is not None:
        if mode != 'read' or offset or type(start_line) is not int or start_line < 1:
            raise ValueError('start_line 仅用于 read，必须≥1且不能同时指定 offset')
        offset = 0
        for _ in range(start_line - 1):
            pos = text.find('\n', offset)
            if pos < 0:
                offset = len(text)
                break
            offset = pos + 1
    if offset > len(text):
        raise ValueError('offset 超出已保存正文长度')
    if mode == 'read':
        end = min(len(text), offset + limit)
        return {**result, 'text': text[offset:end], 'offset': offset,
                'start_line': text.count('\n', 0, offset) + 1,
                'next_offset': end, 'has_more': end < len(text)}
    if mode != 'search' or not isinstance(query, str) or not 1 <= len(query) <= 500:
        raise ValueError('search 需要 1–500 字符的原文字面量 query')
    matches, cursor, used = [], offset, 0
    while len(matches) < 8:
        pos = text.find(query, cursor)
        if pos < 0:
            cursor = len(text)
            break
        begin, end = max(0, pos - 100), min(len(text), pos + len(query) + 100)
        if used + end - begin > limit:
            if not matches:
                end = min(end, begin + limit)
            else:
                break
        matches.append({'offset': pos, 'line': text.count('\n', 0, pos) + 1,
                        'context_offset': begin, 'text': text[begin:end]})
        used += end - begin
        cursor = pos + 1
    return {**result, 'matches': matches, 'next_offset': cursor,
            'has_more': text.find(query, cursor) >= 0}


async def http_response_read(*, workspace_root: str = '', **kwargs: Any) -> ToolResult:
    args = {k: v for k, v in kwargs.items() if k in {'body_ref', 'mode', 'offset', 'limit', 'query', 'start_line'}}
    try:
        if args.get('mode', 'read') not in ('read', 'search'):
            raise ValueError('mode 只支持 read 或 search')
        result = await asyncio.to_thread(_read, workspace_root=workspace_root, **args)
        return ToolResult(tool='http_response_read', ok=True,
                          stdout=json.dumps(result, ensure_ascii=False, separators=(',', ':')),
                          parsed={key: value for key, value in result.items() if key not in {'text', 'matches'}})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return ToolResult(tool='http_response_read', ok=False, returncode=1, error=str(exc),
                          parsed={'retryable': False})


SPEC = ToolSpec(name='http_response_read', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
                description='本地读取或搜索 HTTP 响应快照，不发送网络请求')
SCHEMA = {'type': 'function', 'function': {'name': 'http_response_read',
    'description': '按 body_ref 读取完整源码或搜索原文字面量，不重新请求网页、不做 HTML 摘要。offset/limit 按字符，返回续读偏移和行号；单行 JS 也可分段读取。',
    'parameters': {'type': 'object', 'properties': {
        'body_ref': {'type': 'string'}, 'mode': {'type': 'string', 'enum': ['read', 'search']},
        'offset': {'type': 'integer', 'minimum': 0},
        'start_line': {'type': 'integer', 'minimum': 1, 'description': 'read 可选，从该行开始；与 offset 互斥'},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 4000},
        'query': {'type': 'string', 'description': 'search 时的原文字面量'}}, 'required': ['body_ref']}}}
