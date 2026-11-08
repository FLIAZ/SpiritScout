"""AWDP gzip/tar 文件规范与安全解包；仅依赖标准库，可冻结到验证沙箱。"""
from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path, PurePosixPath


def member_path(raw: str) -> str:
    if (not isinstance(raw, str) or not raw or raw.startswith('/') or '\\' in raw
            or '..' in raw.split('/') or any(ord(c) < 32 or ord(c) == 127 for c in raw)):
        raise ValueError('归档路径不能包含绝对路径、目录穿越或控制字符')
    value = PurePosixPath(raw).as_posix()
    if value == '.':
        raise ValueError('归档路径不能为空')
    return value


def pack(files: dict[str, tuple[bytes, int]]) -> bytes:
    """固定时间戳、属主和顺序；包内仅包含入口与实际修复文件。"""
    result = io.BytesIO()
    with gzip.GzipFile(fileobj=result, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
            for name, (data, mode) in sorted(files.items()):
                item = tarfile.TarInfo(member_path(name))
                item.size, item.mode = len(data), mode & 0o777
                archive.addfile(item, io.BytesIO(data))
    return result.getvalue()


def read_package(source) -> dict[str, tuple[bytes, int]]:
    """先校验整个归档，拒绝链接、重复/冲突路径、特殊权限和无关目录。"""
    if source.read(2) != b'\x1f\x8b':
        raise ValueError('补丁包必须是真实的 gzip 压缩 tar 归档')
    source.seek(0)
    files, directories, seen = {}, set(), set()
    total = count = 0
    with tarfile.open(fileobj=source, mode='r:gz') as archive:
        for item in archive:
            count += 1
            if count > 4096:
                raise ValueError('补丁归档条目过多')
            # 常见 tar -C directory . 带一个根目录占位；不允许别的别名覆盖。
            if item.isdir() and item.name in ('.', './'):
                continue
            name = member_path(item.name)
            if name in seen:
                raise ValueError('补丁包含重复归档路径：' + name)
            seen.add(name)
            if item.mode & 0o7000:
                raise ValueError('补丁不允许 setuid/setgid/sticky 权限：' + name)
            if item.isdir():
                directories.add(name)
                continue
            if not item.isfile():
                raise ValueError('补丁不允许链接或特殊文件：' + name)
            total += item.size
            if total > 512 * 1024 * 1024:
                raise ValueError('补丁解压内容超过 512 MiB 校验上限')
            content = archive.extractfile(item)
            if content is None:
                raise ValueError('无法读取补丁文件：' + name)
            with content:
                data = content.read(item.size + 1)
            if len(data) != item.size:
                raise ValueError('补丁文件长度不符：' + name)
            files[name] = (data, item.mode & 0o777)
    for name in files:
        if any(parent.as_posix() in files for parent in PurePosixPath(name).parents if str(parent) != '.'):
            raise ValueError('补丁包含文件/目录冲突：' + name)
    if not files or any(not any(name.startswith(d + '/') for name in files) for d in directories):
        raise ValueError('补丁为空或包含无关目录')
    return files


def entrypoint_of(files: dict, expected: str | None = None) -> str | None:
    if expected is not None:
        candidates = [expected] if expected in files else []
    else:
        candidates = [name for name in files if PurePosixPath(name).name in {'patch.sh', 'update.sh'}
                      and len(PurePosixPath(name).parts) <= 2]
    if not candidates and expected is None:
        return None  # 兼容明确要求文件覆盖包的既有调用。
    if len(candidates) != 1:
        raise ValueError('补丁必须包含唯一的指定入口 patch.sh 或 update.sh')
    name = candidates[0]
    data, mode = files[name]
    if '\r' in data.decode('utf-8') or not mode & 0o111:
        raise ValueError('补丁入口必须使用 UTF-8/LF 换行并具有可执行权限')
    return name


def extract_package(source, destination: Path, entrypoint: str | None = None) -> Path:
    """只向新建空目录写入；不用 tar.extractall，避免归档链接与路径别名。"""
    files = read_package(source)
    entrypoint = entrypoint_of(files, entrypoint)
    if entrypoint is None:
        raise ValueError('需要可执行的补丁包入口')
    destination.mkdir(parents=True, exist_ok=False)
    for name, (data, mode) in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(mode)
    return destination / entrypoint
