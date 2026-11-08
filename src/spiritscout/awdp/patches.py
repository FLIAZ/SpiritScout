"""校验 AWDP shell 脚本或 tar.gz 提交件，并安全发布产物快照。"""
from __future__ import annotations

import hashlib
import io
import os
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterator, TypeVar

from spiritscout.tools.sandbox_paths import (
    sandbox_directory,
    sandbox_scope,
    sandbox_tmp_directory,
)

_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_CONTENT_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 4096
_T = TypeVar("_T")


@dataclass(frozen=True)
class ValidatedPatch:
    """证明提交件结构；脚本目标清单由模型声明，不宣称漏洞已被修复。"""

    path: Path
    files: tuple[str, ...]
    sha256: str
    format: str = "tar_gzip"
    entrypoint: str | None = None


def _validate_script(source: BinaryIO) -> None:
    """只解析 shell 语法，不执行补丁；不推断脚本真实修改了哪些文件。"""
    content = source.read(4 * 1024 * 1024 + 1)
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("补丁脚本超过 4 MiB 校验上限")
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError("补丁脚本必须是 UTF-8 文本") from exc
    if '\x00' in text or '\r' in text or not text.startswith('#!'):
        raise ValueError("补丁脚本需要 shell shebang 和 LF 换行，且不能含 NUL 字节")
    header = shlex.split(text.splitlines()[0][2:])
    if header[:1] == ['/usr/bin/env']:
        header = header[1:]
        if header[:1] == ['-S']:
            header = header[1:]
    if (not header or header[0] not in ('sh', 'bash', '/bin/sh', '/bin/bash', '/usr/bin/sh', '/usr/bin/bash')
            or any(not flag.startswith('-') or len(flag) < 2 or set(flag[1:]) - set('efuvxC') for flag in header[1:])):
        raise ValueError("补丁脚本 shebang 需指定 sh 或 bash（可用 /usr/bin/env）")
    interpreter = shutil.which(Path(header[0]).name)
    if not interpreter:
        raise ValueError("本机缺少脚本语法检查所需的 shell")
    environment = {key: value for key, value in os.environ.items() if key not in {'BASH_ENV', 'ENV', 'SHELLOPTS', 'BASHOPTS'}}
    try:
        result = subprocess.run([interpreter, '-n'], input=content, capture_output=True,
                                timeout=10, env=environment, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("补丁脚本语法检查超时") from exc
    if result.returncode:
        raise ValueError("补丁脚本语法检查失败：" + result.stderr.decode('utf-8', errors='replace')[:500])


def _member_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ValueError("补丁文件名必须是原始附件中的 POSIX 相对路径")
    if raw.startswith("/") or ".." in raw.split("/"):
        raise ValueError(f"归档路径不能包含绝对路径或目录穿越：{raw}")
    path = PurePosixPath(raw)
    if not path.parts or path == PurePosixPath("."):
        raise ValueError("归档路径不能为空")
    return path.as_posix()


@contextmanager
def _open_regular(root: Path, relative: Path) -> Iterator[BinaryIO]:
    """逐层通过目录描述符读取，拒绝链接与特殊文件，包括检查后的替换。"""
    parts = relative.parts
    if relative.is_absolute() or not parts or any(p in {".", ".."} for p in parts):
        raise ValueError("文件必须位于本题工作目录或本会话产物目录")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("提交件必须是普通文件，不能是链接或特殊文件")
        with os.fdopen(fd, "rb") as stream:
            yield stream
    finally:
        os.close(directory)


@contextmanager
def _patch_snapshot(workspace: Path, relative: Path) -> Iterator[BinaryIO]:
    # 沙箱仍可能改写提交件；校验与发布必须使用同一份私有快照。
    with _open_regular(workspace, relative) as original, tempfile.TemporaryFile() as snapshot:
        if os.fstat(original.fileno()).st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("补丁压缩包超过 128 MiB 校验上限")
        total = 0
        while chunk := original.read(65536):
            total += len(chunk)
            if total > _MAX_ARCHIVE_BYTES:
                raise ValueError("补丁压缩包超过 128 MiB 校验上限")
            snapshot.write(chunk)
        snapshot.seek(0)
        yield snapshot


def _digest(stream: BinaryIO, limit: int = _MAX_CONTENT_BYTES) -> str:
    digest = hashlib.sha256()
    total = 0
    while chunk := stream.read(65536):
        total += len(chunk)
        if total > limit:
            raise ValueError("归档解压内容超过校验上限")
        digest.update(chunk)
    return digest.hexdigest()


def _original_values(workspace: Path, attachments: list[str], wanted: set[str],
                     read: Callable[[BinaryIO], _T]) -> dict[str, _T]:
    """原始路径只从用户提供的附件读取，不把沙箱生成文件当作原件。"""
    found: dict[str, _T] = {}
    available: list[str] = []

    def remember(name: str) -> None:
        # 缺少原件时给小模型真实路径线索，不猜测或自动改写其部署映射。
        if len(available) < 12:
            available.append(name)

    def add(name: str, stream: BinaryIO) -> None:
        if name in found:
            raise ValueError(f"多个原始附件包含同名文件，无法确定补丁来源：{name}")
        found[name] = read(stream)

    for raw in attachments:
        path = Path(raw)
        if path.is_absolute():
            if not path.is_relative_to(workspace):
                raise ValueError("原始附件不在本会话附件目录")
            relative = path.relative_to(workspace)
        else:
            relative = path if path.parts[:1] == ("attachments",) else Path("attachments") / path
        if relative.parts[:1] != ("attachments",):
            raise ValueError("原始附件不在本会话附件目录")
        with _open_regular(workspace, relative) as source:
            if zipfile.is_zipfile(source):
                source.seek(0)
                with zipfile.ZipFile(source) as archive:
                    for item in archive.infolist():
                        if item.is_dir():
                            continue
                        name = _member_path(item.filename)
                        remember(name)
                        if name not in wanted:
                            continue
                        mode = item.external_attr >> 16
                        if stat.S_ISLNK(mode):
                            raise ValueError(f"不接受原始附件中的符号链接：{name}")
                        if item.file_size > _MAX_CONTENT_BYTES:
                            raise ValueError("原始附件文件超过校验上限")
                        with archive.open(item) as content:
                            add(name, content)
            elif path.name.lower().endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
                source.seek(0)
                with tarfile.open(fileobj=source, mode="r:*") as archive:
                    for item in archive:
                        if item.issym() or item.islnk():
                            if _member_path(item.name) in wanted:
                                raise ValueError(f"不接受原始附件中的链接：{item.name}")
                        if not item.isfile():
                            continue
                        name = _member_path(item.name)
                        remember(name)
                        if name in wanted:
                            content = archive.extractfile(item)
                            if content is None:
                                raise ValueError(f"无法读取原始文件：{name}")
                            with content:
                                add(name, content)
            else:
                name = _member_path(relative.relative_to("attachments").as_posix())
                remember(name)
                if name in wanted:
                    source.seek(0)
                    add(name, source)
    missing = wanted - found.keys()
    if missing:
        raise ValueError("补丁路径与原始附件不一致：" + ", ".join(sorted(missing))
            + "；原附件路径示例（前 12 项，非自动映射）：" + ", ".join(available)[:1200]
            + "；source 使用原件路径，patched 保留修改副本完整路径。")
    return found


def _original_digests(workspace: Path, attachments: list[str],
                      wanted: set[str]) -> dict[str, str]:
    return _original_values(workspace, attachments, wanted, _digest)


def _validate_archive(source: BinaryIO, workspace: Path, challenge: str, digest: str,
                      names: tuple[str, ...], attachments: list[str]) -> str | None:
    from spiritscout.awdp.originals import load_record
    from spiritscout.awdp.package import entrypoint_of, read_package

    members = read_package(source)
    try:
        build = load_record(workspace, challenge, 'builds', digest)
    except FileNotFoundError:
        build = None
    entrypoint = entrypoint_of(members, build['entrypoint'] if build else None)
    if entrypoint:
        _validate_script(io.BytesIO(members[entrypoint][0]))
    if build:
        mapping = build['mapping']
        if build.get('format') != 'tar_gzip' or set(names) != {item['source'] for item in mapping}:
            raise ValueError('声明文件与构建记录清单不一致')
        payloads = {item['source']: item['payload'] for item in mapping}
        if set(members) != {entrypoint, *payloads.values()}:
            raise ValueError('补丁包与构建记录清单不一致')
    else:
        # 兼容自行制作的赛事包与旧的纯文件覆盖包；入口不计入原件修改清单。
        prefix = entrypoint.rpartition('/')[0] + '/' if entrypoint and '/' in entrypoint else ''
        payloads = {}
        allowed = {entrypoint}
        for name in names:
            allowed.update({name, prefix + name, prefix + 'files/' + name} if entrypoint else {name})
        if extra := set(members) - allowed:
            raise ValueError('补丁包含 files 未声明的文件：' + ', '.join(sorted(extra)))
        for name in names:
            choices = {name, prefix + name, prefix + 'files/' + name} if entrypoint else {name}
            found = choices.intersection(members) - {entrypoint}
            if len(found) != 1:
                raise ValueError('补丁实际内容与 files 清单不一致：' + name)
            payloads[name] = found.pop()
        extra = set(members) - set(payloads.values()) - {entrypoint}
        if extra:
            raise ValueError('补丁包含 files 未声明的文件：' + ', '.join(sorted(extra)))
    originals = _original_digests(workspace, attachments, set(names))
    for name, payload in payloads.items():
        if originals[name] == hashlib.sha256(members[payload][0]).hexdigest():
            raise ValueError('补丁包含未修改的原始文件：' + name)
    if build and any(originals[item['source']] != item['before_sha256'] for item in build['mapping']):
        raise ValueError('构建后原始附件已变化，请重新构建')
    return entrypoint


def validate_patch(workspace: Path, challenge_id: str, archive_name: str,
                   files: list[str], attachments: list[str]) -> ValidatedPatch:
    """接受本题相对路径、沙箱 /tmp 路径或受控绝对路径，校验后发布。

    赛事归档包含指定入口与修改文件，兼容裸脚本/纯文件覆盖包；目标清单仍指原件路径。
    不执行补丁脚本，不把语法检查当作漏洞修复有效性验证。
    """
    if (not isinstance(files, list) or not files
            or not all(isinstance(f, str) and f.strip() for f in files)):
        raise ValueError("files 必须是非空字符串数组，如 [\"attachment/app.rb\"]")
    names = tuple(_member_path(f.strip()) for f in files)
    if len(set(names)) != len(names):
        raise ValueError("files 清单含重复路径")
    workspace = workspace.resolve()
    safe = sandbox_scope(challenge_id)
    rundir = sandbox_directory(challenge_id)
    tmpdir = sandbox_tmp_directory(challenge_id)
    raw = Path(archive_name)
    if raw.is_absolute():
        if raw.is_relative_to(Path("/tmp")) and not raw.is_relative_to(workspace):
            candidates = [tmpdir / raw.relative_to("/tmp")]
        elif raw.is_relative_to(workspace):
            candidates = [raw.relative_to(workspace)]
        else:
            raise ValueError("补丁包只能来自本题沙箱或本会话 artifacts 目录")
    else:
        candidates = [rundir / raw, tmpdir / raw, raw]
    allowed = []
    for candidate in candidates:
        if (candidate.is_absolute() or ".." in candidate.parts
                or not candidate.parts):
            continue
        if (candidate.is_relative_to(rundir) or candidate.is_relative_to(tmpdir)
                or candidate.parts[:1] == ("artifacts",)):
            allowed.append(candidate)
    if not allowed:
        raise ValueError("补丁包只能来自本题沙箱或本会话 artifacts 目录")
    for relative in allowed:
        try:
            with _patch_snapshot(workspace, relative) as source:
                if os.fstat(source.fileno()).st_size > _MAX_ARCHIVE_BYTES:
                    raise ValueError("补丁压缩包超过 128 MiB 校验上限")
                sha256 = _digest(source, _MAX_ARCHIVE_BYTES)
                source.seek(0)
                script = relative.suffix.lower() == ".sh"
                entrypoint = None
                if script:
                    _validate_script(source)
                    _original_digests(workspace, attachments, set(names))
                    entrypoint = relative.name if relative.name in {'patch.sh', 'update.sh'} else 'patch.sh'
                else:
                    entrypoint = _validate_archive(source, workspace, challenge_id, sha256, names, attachments)
                source.seek(0)
                artifacts = workspace / "artifacts"
                artifacts.mkdir(exist_ok=True)
                if artifacts.is_symlink():
                    raise ValueError("产物目录不能是符号链接")
                directory = os.open(artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    # 用独立目录保留赛事文件名，历史产物不覆盖，写入全程持有 dirfd。
                    stem = f"fix-{safe or 'challenge'}-{os.urandom(6).hex()}"
                    if entrypoint:
                        os.mkdir(stem, mode=0o700, dir_fd=directory)
                        child = os.open(stem, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                        os.close(directory)
                        directory = child
                        artifacts = artifacts / stem
                    filename = (Path(entrypoint).name if script else
                                Path(entrypoint).stem + '.tar.gz' if entrypoint else stem + '.tar.gz')
                    fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
                    try:
                        with os.fdopen(fd, "wb") as target:
                            shutil.copyfileobj(source, target, length=65536)
                    except BaseException:
                        os.unlink(filename, dir_fd=directory)
                        raise
                finally:
                    os.close(directory)
                return ValidatedPatch(artifacts / filename, names, sha256,
                                      "shell_script" if script else "tar_gzip", entrypoint)
        except FileNotFoundError:
            continue
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise ValueError(f"无法读取或校验补丁归档：{exc}") from exc
    raise ValueError("补丁包不存在；相对路径以本题工作目录为准，也可使用实际绝对路径")
