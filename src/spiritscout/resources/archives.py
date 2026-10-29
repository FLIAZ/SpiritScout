"""仅在内存读取技能压缩包；不解压到磁盘，也不执行包内脚本。"""

from __future__ import annotations

import ctypes
import ctypes.util
import io
import lzma
import re
import stat
import tarfile
import zipfile
import zlib
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any

import yaml

from spiritscout.resources.assistant import validate_skill

MAX_UPLOAD = 20 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_FILE = 2 * 1024 * 1024


def _path(name: str) -> str:
    name = name.replace('\\', '/')
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or ':' in name
            or '\x00' in name or len(name) > 1024):
        raise ValueError('压缩包包含不安全路径')
    return str(path)


def _native_entries(content: bytes) -> Iterator[tuple[str, bytes]]:
    """RAR、7z 等交由可用的系统 libarchive 流式读取，仍不创建文件。"""
    library = ctypes.util.find_library('archive')
    if not library:
        raise ValueError('此格式需要系统 libarchive；可安装后重试，或转为 ZIP / TAR')
    lib = ctypes.CDLL(library)
    pointer = ctypes.c_void_p
    signatures = {
        'archive_read_new': (pointer, []),
        'archive_read_support_filter_all': (ctypes.c_int, [pointer]),
        'archive_read_support_format_all': (ctypes.c_int, [pointer]),
        'archive_read_open_memory': (ctypes.c_int, [pointer, pointer, ctypes.c_size_t]),
        'archive_read_next_header': (ctypes.c_int, [pointer, ctypes.POINTER(pointer)]),
        'archive_entry_pathname': (ctypes.c_char_p, [pointer]),
        'archive_entry_filetype': (ctypes.c_uint, [pointer]),
        'archive_entry_symlink': (ctypes.c_char_p, [pointer]),
        'archive_entry_hardlink': (ctypes.c_char_p, [pointer]),
        'archive_read_data': (ctypes.c_ssize_t, [pointer, pointer, ctypes.c_size_t]),
        'archive_read_free': (ctypes.c_int, [pointer]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, args
    reader = lib.archive_read_new()
    if not reader:
        raise ValueError('无法创建压缩包读取器')
    buffer = ctypes.create_string_buffer(content)
    try:
        lib.archive_read_support_filter_all(reader)
        lib.archive_read_support_format_all(reader)
        if lib.archive_read_open_memory(reader, buffer, len(content)) != 0:
            raise ValueError('压缩包损坏、加密或格式不受支持')
        entry = pointer()
        count = 0
        while True:
            status = lib.archive_read_next_header(reader, ctypes.byref(entry))
            if status == 1:
                break
            if status != 0:
                raise ValueError('压缩包损坏、加密或格式不受支持')
            count += 1
            if count > MAX_ENTRIES:
                raise ValueError('压缩包条目超过 1000 个')
            name = _path((lib.archive_entry_pathname(entry) or b'').decode('utf-8'))
            kind = lib.archive_entry_filetype(entry)
            if lib.archive_entry_symlink(entry) or lib.archive_entry_hardlink(entry):
                raise ValueError('压缩包不允许符号链接或硬链接')
            if kind == stat.S_IFDIR:
                continue
            if kind != stat.S_IFREG:
                raise ValueError('压缩包包含非普通文件')
            block = ctypes.create_string_buffer(65536)
            data = bytearray()
            while True:
                size = lib.archive_read_data(reader, block, len(block))
                if size < 0:
                    raise ValueError('压缩包损坏或加密')
                if size == 0:
                    break
                data.extend(block.raw[:size])
                if len(data) > MAX_FILE:
                    raise ValueError('包内单个文件超过 2 MiB')
            yield name, bytes(data)
    finally:
        lib.archive_read_free(reader)


def _entries(content: bytes) -> Iterator[tuple[str, bytes]]:
    source = io.BytesIO(content)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            if len(archive.infolist()) > MAX_ENTRIES:
                raise ValueError('压缩包条目超过 1000 个')
            for entry in archive.infolist():
                name = _path(entry.filename)
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError('压缩包包含链接或非普通文件')
                if entry.is_dir():
                    continue
                if entry.file_size > MAX_FILE:
                    raise ValueError('包内单个文件超过 2 MiB')
                with archive.open(entry) as stream:
                    yield name, stream.read(MAX_FILE + 1)
        return
    source.seek(0)
    try:
        archive = tarfile.open(fileobj=source, mode='r:*')
    except tarfile.ReadError:
        yield from _native_entries(content)
        return
    with archive:
        for count, entry in enumerate(archive, 1):
            if count > MAX_ENTRIES:
                raise ValueError('压缩包条目超过 1000 个')
            name = _path(entry.name)
            if entry.isdir():
                continue
            if not entry.isfile() or entry.issparse():
                raise ValueError('压缩包包含链接、稀疏文件或非普通文件')
            if entry.size > MAX_FILE:
                raise ValueError('包内单个文件超过 2 MiB')
            stream = archive.extractfile(entry)
            if stream is None:
                raise ValueError('无法读取包内文件')
            with stream:
                yield name, stream.read(MAX_FILE + 1)


def parse_skill_archive(content: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """从任意包装目录下的 SKILL.md 识别技能，并合并其 UTF-8 参考资料。"""
    if not content or len(content) > MAX_UPLOAD:
        raise ValueError('压缩包不能为空且不得超过 20 MiB')
    files: dict[str, bytes] = {}
    total = 0
    try:
        for name, data in _entries(content):
            total += len(data)
            if len(data) > MAX_FILE or total > MAX_EXPANDED:
                raise ValueError('压缩包展开内容超过限制（单文件 2 MiB，总计 32 MiB）')
            if name in files:
                raise ValueError('压缩包包含重复文件路径')
            files[name] = data
    except (OSError, RuntimeError, UnicodeError, zipfile.BadZipFile,
            tarfile.TarError, EOFError, NotImplementedError, zlib.error,
            lzma.LZMAError) as exc:
        raise ValueError('压缩包损坏、加密或内容无法读取') from exc
    documents = sorted(name for name in files if PurePosixPath(name).name == 'SKILL.md'
                       and '__MACOSX' not in PurePosixPath(name).parts)
    if not documents:
        raise ValueError('压缩包内未找到 SKILL.md')
    roots = {name: PurePosixPath(name).parent for name in documents}
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    ids: set[str] = set()
    for name in documents:
        root = roots[name]
        try:
            body = files[name].decode('utf-8-sig')
        except UnicodeError as exc:
            raise ValueError(f'{name} 必须使用 UTF-8 编码') from exc
        metadata: dict[str, Any] = {}
        if body.startswith('---\n') or body.startswith('---\r\n'):
            parts = re.split(r'^---\s*$', body, maxsplit=2, flags=re.MULTILINE)
            if len(parts) != 3:
                raise ValueError(f'{name} 的 YAML 元数据缺少结束标记')
            try:
                metadata = yaml.safe_load(parts[1]) or {}
            except (yaml.YAMLError, RecursionError) as exc:
                raise ValueError(f'{name} 的 YAML 元数据无效') from exc
            if not isinstance(metadata, dict):
                raise ValueError(f'{name} 的 YAML 元数据必须是对象')
        skill_id = metadata.get('name') or root.name
        if not isinstance(skill_id, str):
            raise ValueError(f'{name} 的 name 必须是文本')
        item = {'skill_id': skill_id, 'description': metadata.get('description', ''),
                'domain': metadata.get('domain', 'custom'), 'enabled': True, 'body': body, 'path': name,
                'source': 'archive', 'files': {'SKILL.md': body}}
        for path, raw in sorted(files.items()):
            candidate = PurePosixPath(path)
            owners = [parent for parent in roots.values() if candidate.is_relative_to(parent)]
            if (path == name or not candidate.is_relative_to(root)
                    or max(owners, key=lambda parent: len(parent.parts)) != root
                    or '__MACOSX' in candidate.parts or candidate.name == '.DS_Store'):
                continue
            try:
                text = raw.decode('utf-8-sig')
                if '\x00' in text:
                    raise UnicodeError()
            except UnicodeError:
                warnings.append(f'未导入非 UTF-8 文本文件：{path}')
                continue
            relative = str(candidate.relative_to(root))
            item['files'][relative] = text
        validate_skill(item)
        if skill_id in ids:
            raise ValueError(f'压缩包内 Skill ID 重复：{skill_id}')
        ids.add(skill_id)
        items.append(item)
    return items, warnings
