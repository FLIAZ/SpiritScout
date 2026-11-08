"""按原文和哈希生成最小修改副本；不做模糊匹配或跨文件猜测。"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
from pathlib import Path

from spiritscout.awdp.patches import _open_regular
from spiritscout.awdp.scanner import _source_path
from spiritscout.tools.sandbox_paths import (
    open_directory,
    sandbox_directory,
    sandbox_tmp_directory,
)

SCHEMA = {'type': 'function', 'function': {'name': 'edit_source',
    'description': 'AWDP 精确编辑：source_ref 或 path+sha256 定位原件；edits 用 search+replace。匹配失败可核对候选后改用 search_ref+replace。生成副本和 diff，patched 交 build_patch；不自动验证修法。',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'source_ref': {'type': 'string', 'description': '优先填启动材料/source_manifest 的原件引用，不另传 path/sha256。继续修改工作副本时填空字符串，并传 path+sha256'},
        'path': {'type': 'string'},
        'sha256': {'type': 'string', 'description': 'source_context/read 或实际读取原文件得到的 SHA-256；不匹配拒绝编辑'},
        'edits': {'type': 'array', 'minItems': 1, 'maxItems': 32, 'items': {'type': 'object',
            'additionalProperties': False, 'properties': {
                'search': {'type': 'string', 'description': '唯一精确原文；改用 search_ref 时填空字符串'},
                'search_ref': {'type': 'string', 'description': '匹配失败返回的候选范围引用；先核对位置和原文'},
                'replace': {'type': 'string', 'description': '修后原文，可为空以删除；不支持省略号占位或模糊匹配'}},
            'required': ['search', 'replace']}}}, 'required': ['source_ref', 'edits']}}}


class EditMatchError(ValueError):
    def __init__(self, number: int, content: str, search: str, digest: str):
        super().__init__(f'第 {number} 项原文未唯一匹配，未编辑；核对 candidates 后用 search_ref 重试该项')
        ranges = []
        start = content.find(search)
        while start >= 0 and len(ranges) < 4:
            ranges.append((start, start + len(search)))
            start = content.find(search, start + 1)
        if not ranges:
            # 仅给出逐行内容相同、首尾空白不同的候选；绝不自动选择或改写。
            lines, wanted = content.splitlines(keepends=True), search.splitlines(keepends=True)
            wanted = [line.strip(' \t\r\n') for line in wanted]
            if wanted and any(wanted):
                normalized = [line.strip(' \t\r\n') for line in lines]
                offsets, starts, offset, position = [], {}, 0, 1
                for index, line in enumerate(lines):
                    offsets.append(offset)
                    starts[position] = index
                    offset += len(line)
                    position += len(normalized[index]) + 1
                offsets.append(offset)
                haystack, needle = '\0' + '\0'.join(normalized) + '\0', '\0' + '\0'.join(wanted) + '\0'
                position = haystack.find(needle)
                while position >= 0 and len(ranges) < 4:
                    index = starts[position + 1]
                    end = offsets[index + len(wanted)]
                    if not search.endswith(('\n', '\r')):
                        last = lines[index + len(wanted) - 1]
                        end -= len(last) - len(last.rstrip('\r\n'))
                    ranges.append((offsets[index], end))
                    position = haystack.find(needle, position + 1)
        candidates = []
        for start, end in ranges[:3]:
            text = content[start:end]
            candidates.append({'search_ref': f'span-{digest}:{start}:{end}',
                'start_line': content.count('\n', 0, start) + 1,
                'end_line': content.count('\n', 0, end - 1) + 1,
                'content': text[:1500], 'content_partial': len(text) > 1500})
        self.result = {'ok': False, 'executed': False, 'status': 'edit_mismatch',
            'edit_index': number, 'sha256': digest, 'candidates': candidates,
            'candidates_partial': len(ranges) > 3, 'error': str(self),
            'note': '未写文件。候选不是自动修复；核对范围，保持其余 edits，失败项填写 search="" 和候选 search_ref。'
                    '无候选或展示不完整时按行回读原文，不要为缩进差异重写整份文件。'}


def edit_source(root: Path, challenge: str, args: dict, attachments: list[str] | None = None) -> dict:
    if not isinstance(args, dict) or set(args) - {'path', 'sha256', 'edits', 'source_ref'} or 'edits' not in args:
        raise ValueError('edit_source 需要 source_ref 或 path+sha256，以及 edits')
    root = root.resolve()
    reference = args.get('source_ref')
    if reference:
        from spiritscout.awdp.originals import catalog, resolve_ref

        if 'path' in args or 'sha256' in args:
            raise ValueError('source_ref 与 path+sha256 只能选择一种')
        manifest = catalog(root, challenge, attachments) if attachments is not None else None
        entry, data = resolve_ref(root, challenge, reference, manifest)
        path, mode, expected = Path(entry['source']), entry['mode'], entry['sha256']
    else:
        expected = args.get('sha256')
        path = Path(_source_path(root, challenge, args.get('path')))
        host = path if path.is_relative_to(root) else root / sandbox_tmp_directory(challenge) / path.relative_to('/tmp')
        with _open_regular(root, host.relative_to(root)) as stream:
            data = stream.read(1024 * 1024 + 1)
            mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & 0o777
    edits = args['edits']
    if not isinstance(expected, str) or not re.fullmatch('[a-fA-F0-9]{64}', expected):
        raise ValueError('需要实际读取原文件得到的 SHA-256')
    if not isinstance(edits, list) or not 1 <= len(edits) <= 32:
        raise ValueError('edits 必须包含 1–32 项')
    if len(data) > 1024 * 1024:
        raise ValueError('精确编辑支持单文件最多 1 MiB，请选择实际需要修复的文件')
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected.lower():
        raise ValueError(f'原文哈希已变化，未编辑。当前 sha256={actual}；请重新读取后核对修法')
    content = data.decode('utf-8')
    if '\0' in content:
        raise ValueError('edit_source 只支持 UTF-8 文本源码')
    spans = []
    for number, entry in enumerate(edits, 1):
        # 本地结构化解码常填齐必填 search；空串明确表示改用范围引用，不猜测非空双输入。
        if isinstance(entry, dict) and entry.get('search') == '' and 'search_ref' in entry:
            entry = {key: value for key, value in entry.items() if key != 'search'}
        if (not isinstance(entry, dict) or set(entry) not in ({'search', 'replace'}, {'search_ref', 'replace'})
                or not isinstance(entry['replace'], str)):
            raise ValueError(f'第 {number} 项需要 search 或 search_ref（二选一），以及字符串 replace')
        replacement = entry['replace']
        if 'search_ref' in entry:
            ref = entry['search_ref']
            match = re.fullmatch(r'span-([a-f0-9]{64}):(\d{1,7}):(\d{1,7})', ref) if isinstance(ref, str) else None
            if not match or match[1] != actual:
                raise ValueError(f'第 {number} 项 search_ref 无效或原文哈希已变化，请重新定位')
            start, end = int(match[2]), int(match[3])
            if not 0 <= start < end <= len(content):
                raise ValueError(f'第 {number} 项 search_ref 范围无效')
            search = content[start:end]
        else:
            search = entry['search']
            if not isinstance(search, str) or not search:
                raise ValueError(f'第 {number} 项 search 必须是非空原文')
            start = content.find(search)
            if start < 0 or content.find(search, start + 1) >= 0:
                raise EditMatchError(number, content, search, actual)
            end = start + len(search)
        if search == replacement:
            raise ValueError(f'第 {number} 项没有实际修改')
        spans.append((start, end, replacement))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError('编辑范围重叠，未编辑；所有 search 都须针对同一原件')
    modified = content
    for start, end, replacement in reversed(spans):
        modified = modified[:start] + replacement + modified[end:]
    after = modified.encode('utf-8')
    if len(after) > 1024 * 1024 or b'\0' in after or after == data:
        raise ValueError('修后源码过大、包含 NUL 或与原件相同，未编辑')
    directory = sandbox_directory(challenge) / ('edit-' + os.urandom(8).hex())
    filename = path.name
    # diff 文件采用固定且不同的名字，避免源码本身名为 changes.diff 时冲突。
    diff_name = '_changes.diff' if filename != '_changes.diff' else '__changes.diff'
    diff = ''.join(difflib.unified_diff(content.splitlines(keepends=True), modified.splitlines(keepends=True),
                                      fromfile=str(path), tofile=str(root / directory / filename)))
    with open_directory(root, directory, create=True) as descriptor:
        for name, payload, permissions in ((filename, after, mode), (diff_name, diff.encode(), 0o600)):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                os.fchmod(stream.fileno(), permissions)
    return {'ok': True, 'original_path': str(path), 'patched': str(root / directory / filename),
        **({'source_ref': reference} if reference else {}),
        'before_sha256': actual, 'sha256': hashlib.sha256(after).hexdigest(), 'edits': len(edits),
        'diff': diff[:5000], 'diff_partial': len(diff) > 5000, 'diff_file': str(root / directory / diff_name),
        'original_unchanged': True, 'syntax_verified': False, 'vulnerability_verified': False,
        'note': '已生成修改副本；后续编辑/验证/build_patch 的 patched 使用返回的完整绝对路径，不要再剥除目录。'
                'build_patch 的 source 使用原始附件成员相对路径，不能填写 original_path。多文件修复分别编辑；'
                '需核对最小差异并在干净副本应用最终 patch.sh 验证正常功能和漏洞。'}
