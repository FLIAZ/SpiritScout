"""单机工作区运行锁；不把可被沙箱修改的锁文件放进会话目录。"""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

from spiritscout.core.exceptions import ConfigurationError


def _lock(fd: int) -> None:
    """锁同一个文件身份；Windows 锁区避开 SQLite 自身的保留锁字节。"""
    if os.name == "nt":
        import msvcrt

        # 字节范围锁可以位于 EOF 之后，不写入、不扩大数据库文件。
        os.lseek(fd, (1 << 63) - 2, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError from exc
            raise
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _open(path: Path) -> int:
    return os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
                   | getattr(os, "O_BINARY", 0), 0o600)


class ExecutionRunLock:
    def __init__(self, workspace: Path, database: Path | None = None):
        self.workspace = workspace.resolve()
        self._fd: int | None = None
        self.database = database.resolve() if database is not None else None
        self._database_fd: int | None = None

    @property
    def owned(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self.owned:
            return
        directory = self.workspace.parent / ".execution-locks"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = hashlib.sha256(str(self.workspace).encode()).hexdigest() + ".lock"
        fd = _open(directory / name)
        try:
            _lock(fd)
        except BlockingIOError:
            os.close(fd)
            raise ConfigurationError("该会话工作区已有执行进程，请先停止原运行") from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        try:
            if self.database is not None:
                self.acquire_database(self.database)
        except BaseException:
            self.close()
            raise

    def acquire_database(self, database: Path) -> None:
        """直接锁数据库 inode，路径别名/硬链接也不能取得第二份执行权。

        Unix flock / Windows 保留区锁与 SQLite 自身锁独立；Web 查询不申请执行锁。
        """
        database = database.resolve()
        if not self.owned:
            raise ConfigurationError("取得数据库执行锁前必须持有工作区锁")
        if self._database_fd is not None:
            if not os.path.samestat(os.fstat(self._database_fd), database.stat()):
                raise ConfigurationError("运行锁不能切换到其他执行数据库")
            return
        database.parent.mkdir(parents=True, exist_ok=True)
        fd = _open(database)
        try:
            _lock(fd)
        except BlockingIOError:
            os.close(fd)
            raise ConfigurationError("该执行数据库已有运行进程，请先停止原运行") from None
        except BaseException:
            os.close(fd)
            raise
        self.database = database
        self._database_fd = fd

    def close(self) -> None:
        if self._database_fd is not None:
            os.close(self._database_fd)
            self._database_fd = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
