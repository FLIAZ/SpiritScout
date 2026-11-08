"""附件原件引用：私有快照与公开工作副本分离，不猜测部署路径。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
import zipfile
from pathlib import Path

from spiritscout.awdp.patches import _member_path, _open_regular
from spiritscout.tools.sandbox_paths import (
    open_directory,
    sandbox_directory,
    sandbox_scope,
)

SCHEMA = {'type': 'function', 'function': {'name': 'source_manifest',
    'description': 'AWDP 原件目录。list 列出附件真实成员和 source_ref；read 按引用分页读原文；checkout 将全部原件复制到本题工作目录供扫描/运行。source_ref 可直接给 edit_source、build_patch，无需手抄哈希或猜归档外层目录。引用绑定本题附件内容，附件变化后旧引用失效。',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'action': {'type': 'string', 'enum': ['list', 'read', 'checkout']},
        'source_ref': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}},
        'required': ['action']}}}

MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024


def private_dir(challenge: str) -> Path:
    # 普通工具的 bubblewrap 会遮蔽 workspace；仅工作目录/附件/产物回挂。
    return Path('.awdp') / sandbox_scope(challenge)


def write_private(root: Path, relative: Path, payload: bytes) -> None:
    with open_directory(root, relative.parent, create=True) as directory:
        try:
            fd = os.open(relative.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
        except FileExistsError:
            with _open_regular(root, relative) as stream:
                if stream.read() != payload:
                    raise ValueError('私有快照内容冲突') from None
            return
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)


def save_record(root: Path, challenge: str, kind: str, key: str, data: dict) -> None:
    write_private(root, private_dir(challenge) / kind / (key + '.json'),
                  json.dumps(data, ensure_ascii=False, sort_keys=True).encode())


def load_record(root: Path, challenge: str, kind: str, key: str) -> dict:
    if not isinstance(key, str) or not re.fullmatch('[a-z0-9-]{1,80}', key):
        raise ValueError('无效的原件/验证引用')
    with _open_regular(root, private_dir(challenge) / kind / (key + '.json')) as stream:
        return json.load(stream)


def catalog(root: Path, challenge: str, attachments: list[str]) -> dict:
    try:
        return _catalog(root, challenge, attachments)
    except (tarfile.TarError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError(f'无法建立原件快照：{exc}') from exc


def _catalog(root: Path, challenge: str, attachments: list[str]) -> dict:
    """只读取声明的附件，不扫描模型生成文件；所有归档成员均有明确覆盖结果。"""
    root = root.resolve()
    entries, seen, total = [], set(), 0

    def add(name, source, mode, origin):
        nonlocal total
        name = _member_path(name)
        if name in seen:
            raise ValueError(f'原件成员重名，不能确定部署来源：{name}')
        data = source.read(MAX_FILE + 1)
        total += len(data)
        if len(data) > MAX_FILE or total > MAX_TOTAL or len(entries) >= 4096:
            raise ValueError('原件快照上限：单文件 2 MiB、总计 32 MiB、4096 文件；未生成不完整目录')
        digest = hashlib.sha256(data).hexdigest()
        write_private(root, private_dir(challenge) / 'blobs' / digest, data)
        entries.append({'source': name, 'sha256': digest, 'size': len(data),
                        'mode': mode & 0o777, 'attachment': origin})
        seen.add(name)

    for raw in attachments:
        path = Path(raw)
        relative = path.relative_to(root) if path.is_absolute() else path
        if relative.parts[:1] != ('attachments',):
            relative = Path('attachments') / relative
        with _open_regular(root, relative) as stream:
            # 固定归档字节，避免在建目录期间同一附件被宿主替换/改写。
            data = stream.read(128 * 1024 * 1024 + 1)
            mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
        if len(data) > 128 * 1024 * 1024:
            raise ValueError('原始附件超过 128 MiB')
        source, origin = io.BytesIO(data), relative.as_posix()
        if zipfile.is_zipfile(source):
            source.seek(0)
            with zipfile.ZipFile(source) as archive:
                for member in archive.infolist():
                    _member_path(member.filename)
                    permissions = member.external_attr >> 16
                    if stat.S_ISLNK(permissions):
                        raise ValueError(f'原件快照不支持归档链接：{member.filename}')
                    if member.is_dir():
                        continue
                    if stat.S_IFMT(permissions) not in (0, stat.S_IFREG):
                        raise ValueError('原件快照不支持特殊文件')
                    with archive.open(member) as content:
                        add(member.filename, content, permissions or 0o644, origin)
        elif path.name.lower().endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')):
            source.seek(0)
            with tarfile.open(fileobj=source) as archive:
                for member in archive:
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise ValueError(f'原件快照不支持归档链接或特殊文件：{member.name}')
                    with archive.extractfile(member) as content:
                        add(member.name, content, member.mode, origin)
        else:
            source.seek(0)
            add(relative.relative_to('attachments').as_posix(), source, mode, origin)
    if not entries:
        raise ValueError('没有可用的原始附件文件')
    entries.sort(key=lambda item: item['source'])
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    key = 'src-' + digest[:24]
    result = {'catalog_ref': key, 'sha256': digest, 'files': [
        dict(item, source_ref=f'{key}:{i}') for i, item in enumerate(entries)]}
    save_record(root, challenge, 'catalogs', key, result)
    return result


def resolve_ref(root: Path, challenge: str, reference: str, current: dict | None = None) -> tuple[dict, bytes]:
    if not isinstance(reference, str) or not re.fullmatch(r'src-[a-f0-9]{24}:\d{1,4}', reference):
        raise ValueError('source_ref 必须来自 source_manifest')
    key, index = reference.split(':')
    if current is not None and current['catalog_ref'] != key:
        raise ValueError('原始附件已变化，请重新 source_manifest 获取引用')
    manifest = current or load_record(root, challenge, 'catalogs', key)
    try:
        entry = manifest['files'][int(index)]
    except IndexError as exc:
        raise ValueError('原件引用不存在') from exc
    with _open_regular(root, private_dir(challenge) / 'blobs' / entry['sha256']) as stream:
        content = stream.read(MAX_FILE + 1)
    if hashlib.sha256(content).hexdigest() != entry['sha256']:
        raise ValueError('原件私有快照哈希不一致')
    return entry, content


def source_manifest(root: Path, challenge: str, attachments: list[str], args: dict) -> dict:
    if set(args) - {'action', 'source_ref', 'offset'}:
        raise ValueError('source_manifest 参数不符合契约')
    manifest = catalog(root, challenge, attachments)
    action, offset = args.get('action'), args.get('offset', 0)
    if not isinstance(offset, int) or offset < 0:
        raise ValueError('offset 必须是非负整数')
    result = {'ok': True, 'catalog_ref': manifest['catalog_ref'], 'total': len(manifest['files'])}
    if action == 'list':
        result['files'] = [dict(entry, read_call={'name': 'source_manifest', 'arguments': {
            'action': 'read', 'source_ref': entry['source_ref']}}) for entry in manifest['files'][offset:offset + 8]]
        if offset + 8 < len(manifest['files']):
            result['next_call'] = {'name': 'source_manifest', 'arguments': {'action': 'list', 'offset': offset + 8}}
    elif action == 'read':
        entry, data = resolve_ref(root, challenge, args.get('source_ref'), manifest)
        content = data.decode('utf-8')
        result.update(entry, content=content[offset:offset + 4000], offset=offset)
        if offset + 4000 < len(content):
            result['next_call'] = {'name': 'source_manifest', 'arguments': {
                'action': 'read', 'source_ref': entry['source_ref'], 'offset': offset + 4000}}
    elif action == 'checkout':
        directory = sandbox_directory(challenge) / ('source-' + os.urandom(6).hex())
        for entry in manifest['files']:
            _, data = resolve_ref(root, challenge, entry['source_ref'], manifest)
            path = directory / entry['source']
            write_private(root, path, data)
            with open_directory(root, path.parent) as fd:
                os.chmod(path.name, entry['mode'], dir_fd=fd, follow_symlinks=False)
        result['path'] = str(root / directory)
        result['note'] = '工作副本保留原件层级；部署映射由题目规则决定。原件引用见 list/read。'
    else:
        raise ValueError('action 应为 list/read/checkout')
    return result


def initial_context(root: Path, challenge: str, attachments: list[str]) -> dict:
    """启动感知只提供附件事实及可复现副本，不选择漏洞、修改或验证步骤。"""
    checked = source_manifest(root, challenge, attachments, {'action': 'checkout'})
    manifest = catalog(root, challenge, attachments)
    remaining, items = 5000, []
    for entry in manifest['files'][:8]:
        _, data = resolve_ref(root, challenge, entry['source_ref'], manifest)
        item = dict(entry, read_call={'name': 'source_manifest', 'arguments': {
            'action': 'read', 'source_ref': entry['source_ref']}})
        try:
            content = data.decode('utf-8')
        except UnicodeDecodeError:
            content = None
        if content is not None and '\0' not in content and remaining > 0:
            item['content'] = content[:min(2000, remaining)]
            item['partial'] = len(item['content']) != len(content)
            if item['partial']:
                item['next_call'] = {'name': 'source_manifest', 'arguments': {
                    'action': 'read', 'source_ref': entry['source_ref'], 'offset': len(item['content'])}}
            remaining -= len(item['content'])
        items.append(item)
    return dict(checked, files=items, partial=len(manifest['files']) > len(items),
                note='系统已从真实附件检出此工作副本并读取以下原文；尚未审计、修补或验证。'
                     '续读附件用对应 next_call/read_call，不能用 history_read(output) 读取附件。'
                     'source_ref 指向只读原件；patched 使用 edit_source 返回值。目录本身不推断部署根。')
