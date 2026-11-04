"""校验 AWDP tar.gz 提交件，并安全复制到会话 artifacts 目录。"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from spiritscout.tools.sandbox_paths import (
    sandbox_directory,
    sandbox_scope,
    sandbox_tmp_directory,
)

_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_CONTENT_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 4096


@dataclass(frozen=True)
class ValidatedPatch:
    """只证明归档格式、原始路径与内容变化；不宣称漏洞已被修复。"""

    path: Path
    files: tuple[str, ...]
    sha256: str


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


def _original_digests(workspace: Path, attachments: list[str],
                      wanted: set[str]) -> dict[str, str]:
    """原始路径只从用户提供的附件读取，不把沙箱生成文件当作原件。"""
    found: dict[str, str] = {}

    def add(name: str, stream: BinaryIO) -> None:
        if name in found:
            raise ValueError(f"多个原始附件包含同名文件，无法确定补丁来源：{name}")
        found[name] = _digest(stream)

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
                        if name not in wanted:
                            continue
                        mode = item.external_attr >> 16
                        if stat.S_ISLNK(mode):
                            raise ValueError(f"不接受原始附件中的符号链接：{name}")
                        if item.file_size > _MAX_CONTENT_BYTES:
                            raise ValueError("原始附件文件超过校验上限")
                        with archive.open(item) as content:
                            add(name, content)
            elif path.name.lower().endswith((".tar", ".tar.gz", ".tgz")):
                source.seek(0)
                with tarfile.open(fileobj=source, mode="r:*") as archive:
                    for item in archive:
                        if not item.isfile():
                            continue
                        name = _member_path(item.name)
                        if name in wanted:
                            content = archive.extractfile(item)
                            if content is None:
                                raise ValueError(f"无法读取原始文件：{name}")
                            with content:
                                add(name, content)
            elif path.name in wanted:
                source.seek(0)
                add(path.name, source)
    missing = wanted - found.keys()
    if missing:
        raise ValueError("补丁路径与原始附件不一致：" + ", ".join(sorted(missing)))
    return found


def validate_patch(workspace: Path, challenge_id: str, archive_name: str,
                   files: list[str], attachments: list[str]) -> ValidatedPatch:
    """接受本题相对路径、沙箱 /tmp 路径或受控绝对路径，校验后发布。

    提交件仅含修改文件，内部相对路径必须与原始附件一致。不执行验证脚本，
    不把语法/漏洞修复有效性当作归档校验已覆盖的内容。
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
                if source.read(2) != b"\x1f\x8b":
                    raise ValueError("补丁包必须是真实的 gzip 压缩 tar 归档")
                source.seek(0)
                digests: dict[str, str] = {}
                total = count = 0
                with tarfile.open(fileobj=source, mode="r:gz") as archive:
                    for item in archive:
                        count += 1
                        if count > _MAX_MEMBERS:
                            raise ValueError("补丁归档条目过多")
                        name = _member_path(item.name)
                        if item.isdir():
                            if not any(n.startswith(name + "/") for n in names):
                                raise ValueError(f"补丁包含无关目录：{name}")
                            continue
                        if not item.isfile():
                            raise ValueError(f"补丁不允许链接或特殊文件：{name}")
                        if name in digests:
                            raise ValueError(f"补丁包含重复归档路径：{name}")
                        if name not in names:
                            raise ValueError(f"补丁包含 files 未声明的文件：{name}")
                        total += item.size
                        if total > _MAX_CONTENT_BYTES:
                            raise ValueError("补丁解压内容超过 512 MiB 校验上限")
                        content = archive.extractfile(item)
                        if content is None:
                            raise ValueError(f"无法读取补丁文件：{name}")
                        with content:
                            digests[name] = _digest(content, item.size)
                if set(digests) != set(names):
                    raise ValueError("补丁实际内容与 files 清单不一致")
                originals = _original_digests(workspace, attachments, set(names))
                unchanged = [name for name in names if originals[name] == digests[name]]
                if unchanged:
                    raise ValueError("补丁包含未修改的原始文件：" + ", ".join(unchanged))
                source.seek(0)
                sha256 = _digest(source, _MAX_ARCHIVE_BYTES)
                source.seek(0)
                artifacts = workspace / "artifacts"
                artifacts.mkdir(exist_ok=True)
                if artifacts.is_symlink():
                    raise ValueError("产物目录不能是符号链接")
                directory = os.open(artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    # 随机文件名且 O_EXCL，不覆盖已有产物；通过 dirfd 抵御路径替换。
                    filename = f"fix-{safe or 'challenge'}-{os.urandom(6).hex()}.tar.gz"
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
                return ValidatedPatch(artifacts / filename, names, sha256)
        except FileNotFoundError:
            continue
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise ValueError(f"无法读取或校验补丁归档：{exc}") from exc
    raise ValueError("补丁包不存在；相对路径以本题工作目录为准，也可使用实际绝对路径")
