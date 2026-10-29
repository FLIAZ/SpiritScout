"""会话存储预览与后台清理；只清已投递通知、旧 DAG/检查点和已知下载缓存。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.tools.run_lock import ExecutionRunLock

if TYPE_CHECKING:
    from spiritscout.web.sessions import Session, SessionManager

_DAG = ("task_state", "dag-snapshot")


def _regular(path: Path, root: Path) -> bool:
    """拒绝沙箱中的符号链接及越界路径，扫描和删除使用相同边界。"""
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return False
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def tree_bytes(root: Path) -> int:
    """按实际分配块统计；不跟随符号链接，不重复统计硬链接。"""
    total = 0
    seen: set[tuple[int, int]] = set()
    if root.is_symlink() or not root.exists():
        return 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            info = (Path(directory) / name).lstat()
            identity = (info.st_dev, info.st_ino)
            if stat.S_ISREG(info.st_mode) and identity not in seen:
                seen.add(identity)
                total += getattr(info, "st_blocks", 0) * 512 if hasattr(info, "st_blocks") else info.st_size
    return total


def cache_paths(workspace: Path) -> list[Path]:
    """只识别包下载缓存，保留安装目录、脚本、浏览器登录态和所有输出。"""
    sandbox = workspace / "sandbox"
    if not _regular(sandbox, workspace) or not sandbox.is_dir():
        return []
    out = []
    for actor in sandbox.iterdir():
        if not _regular(actor, workspace) or not actor.is_dir():
            continue
        for name in (".cache/pip", ".cache/uv", ".npm/_cacache"):
            path = actor / name
            if _regular(path, workspace) and path.is_dir():
                out.append(path)
    return out


def _connect(workspace: Path, *, readonly: bool = True) -> sqlite3.Connection:
    db = workspace / "bb.db"
    for name in ("bb.db", "bb.db-wal", "bb.db-shm", "execution.key"):
        if not _regular(workspace / name, workspace):
            raise SpiritScoutError("会话数据库或密钥路径存在符号链接，未执行清理")
    conn = sqlite3.connect(db.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                           uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def database_preview(workspace: Path) -> dict[str, Any]:
    if not (workspace / "bb.db").exists():
        return {}
    conn = _connect(workspace)
    try:
        tables = _tables(conn)
        result: dict[str, Any] = {"bytes": (workspace / "bb.db").stat().st_size,
                                  "reusable_bytes": conn.execute("PRAGMA freelist_count").fetchone()[0]
                                  * conn.execute("PRAGMA page_size").fetchone()[0]}
        if "bb_event_outbox" in tables:
            result["delivered_events"] = conn.execute(
                "SELECT COUNT(*) FROM bb_event_outbox WHERE delivered=1").fetchone()[0]
        if "bb_record_history" in tables:
            n = conn.execute("SELECT COUNT(*) FROM bb_record_history WHERE layer=? AND record_id=?", _DAG).fetchone()[0]
            result["old_dag_versions"] = max(0, n - 64)
        if "runtime_checkpoints" in tables:
            n, keys = conn.execute("SELECT COUNT(*),COUNT(DISTINCT checkpoint_key || char(0) || scope) "
                                   "FROM runtime_checkpoints").fetchone()
            result["checkpoints"] = n
            result["checkpoint_keys"] = keys
        return result
    finally:
        conn.close()


def _prune_checkpoints(conn: sqlite3.Connection, workspace: Path) -> int:
    """保留每个恢复键的最新检查点和传递依赖；校验完成前不删除任何检查点。"""
    rows = conn.execute("SELECT checkpoint_id,scope,checkpoint_key,schema_version "
                        "FROM runtime_checkpoints ORDER BY checkpoint_id DESC").fetchall()
    by_id = {row["checkpoint_id"]: row for row in rows}
    latest: dict[tuple[str, str], int] = {}
    for row in rows:
        if row["schema_version"] not in (1, 2):
            raise SpiritScoutError("存在未知检查点格式，保留全部检查点")
        latest.setdefault((row["scope"], row["checkpoint_key"]), row["checkpoint_id"])
    keep: set[int] = set()
    cipher = Fernet((workspace / "execution.key").read_bytes()) if rows else None
    for key, head in latest.items():
        current = head
        depth = 0
        while True:
            row = by_id.get(current)
            if row is None or (row["scope"], row["checkpoint_key"]) != key or depth > 31:
                raise SpiritScoutError("检查点依赖链不完整，保留全部检查点")
            if current in keep:
                break
            keep.add(current)
            blob = conn.execute("SELECT state_cipher FROM runtime_checkpoints WHERE checkpoint_id=?",
                                (current,)).fetchone()[0]
            payload = json.loads(cipher.decrypt(blob))
            if not isinstance(payload, dict):
                raise SpiritScoutError("检查点内容无效，保留全部检查点")
            if row["schema_version"] == 1:
                break
            base = payload.get("base_checkpoint")
            if type(base) is not int or base >= current or not isinstance(payload.get("tail"), list):
                raise SpiritScoutError("检查点依赖链无效，保留全部检查点")
            current = base
            depth += 1
    # 临时表避开 SQLite 参数数量上限，不写到会话以外的持久库。
    conn.execute("CREATE TEMP TABLE retained_checkpoints(id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO retained_checkpoints VALUES(?)", ((n,) for n in keep))
    return conn.execute("DELETE FROM runtime_checkpoints WHERE checkpoint_id NOT IN "
                        "(SELECT id FROM retained_checkpoints)").rowcount


def clean_database(workspace: Path, progress: Any) -> dict[str, Any]:
    """事务内精简；VACUUM 失败时已释放页仍可复用，如实返回收缩未完成。"""
    if not (workspace / "bb.db").exists():
        return {}
    conn = _connect(workspace, readonly=False)
    result: dict[str, Any] = {"warnings": []}
    try:
        tables = _tables(conn)
        # 大量 DELETE 也会生成回滚日志/WAL，不能等到 VACUUM 才检查磁盘。
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        unused = conn.execute("PRAGMA freelist_count").fetchone()[0]
        required = (pages - unused) * conn.execute("PRAGMA page_size").fetchone()[0]
        if shutil.disk_usage(workspace).free < required + 64 * 1024 * 1024:
            result["warnings"].append("数据库处理临时空间不足，未修改数据库；请先释放空间后重试")
            return result
        progress("正在精简数据库历史")
        conn.execute("BEGIN IMMEDIATE")
        if "bb_event_outbox" in tables:
            result["events_removed"] = conn.execute("DELETE FROM bb_event_outbox WHERE delivered=1").rowcount
        if "bb_record_history" in tables:
            result["dag_versions_removed"] = conn.execute(
                "DELETE FROM bb_record_history WHERE layer=? AND record_id=? AND version NOT IN "
                "(SELECT version FROM bb_record_history WHERE layer=? AND record_id=? "
                "ORDER BY version DESC LIMIT 64)", (*_DAG, *_DAG)).rowcount
        if "runtime_checkpoints" in tables:
            try:
                result["checkpoints_removed"] = _prune_checkpoints(conn, workspace)
            except (SpiritScoutError, OSError, ValueError, TypeError, KeyError) as exc:
                result["warnings"].append(f"检查点未清理：{type(exc).__name__}")
            except Exception as exc:
                # 密钥缺失、密文校验失败等不能导致恢复链被部分删除。
                from cryptography.fernet import InvalidToken
                if not isinstance(exc, InvalidToken):
                    raise
                result["warnings"].append("检查点未清理：密文校验失败")
        conn.commit()
        progress("正在收缩数据库文件")
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint[0]:
            raise SpiritScoutError("数据库仍有读写连接，收缩未完成，可稍后重试")
        size = conn.execute("PRAGMA page_size").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        used = (conn.execute("PRAGMA page_count").fetchone()[0] - free) * size
        if shutil.disk_usage(workspace).free < used * 2 + 64 * 1024 * 1024:
            result["warnings"].append("磁盘剩余空间不足，已精简记录，空闲页可复用；暂未收缩文件")
        else:
            conn.execute("VACUUM")
            if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                result["warnings"].append("数据库已收缩，但 WAL 仍被读取，稍后重试可释放剩余空间")
            result["compacted"] = True
        result["reusable_bytes"] = conn.execute("PRAGMA freelist_count").fetchone()[0] * size
        return result
    finally:
        conn.close()


class StorageCleaner:
    """单个磁盘清理任务串行执行；标记在启动线程前设置，恢复与删除共享此互斥。"""

    def __init__(self, manager: SessionManager):
        self.manager = manager
        self.jobs: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None

    def _session(self, session_id: str) -> Session:
        session = self.manager.get(session_id)
        if session is None:
            raise SpiritScoutError("会话不存在")
        if session.workspace.is_symlink() or session.workspace.resolve().parent != self.manager.root.resolve():
            raise SpiritScoutError("会话工作区路径不合法")
        return session

    def blocked_reason(self, session: Session) -> str:
        from spiritscout.ctf.report_jobs import status
        from spiritscout.runtime import pending_distill_snapshots
        from spiritscout.web.sessions import TERMINAL_STATES
        if session.session_id in self.manager.storage_busy:
            return "正在清理存储"
        if session.status not in TERMINAL_STATES or (session.task and not session.task.done()):
            return "会话仍在执行，请先停止"
        if status(session.workspace).get("status") in {"queued", "running"} or pending_distill_snapshots(session.workspace):
            return "报告或经验提炼尚未完成"
        return ""

    def overview(self, sessions: list[dict[str, Any]]) -> dict[str, Any]:
        for row in sessions:
            workspace = self._session(row["session_id"]).workspace
            row["bytes"] = tree_bytes(workspace)
        sessions.sort(key=lambda row: row["bytes"], reverse=True)
        return {"sessions": sessions, "total_bytes": sum(row["bytes"] for row in sessions)}

    def preview(self, session_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        paths = cache_paths(session.workspace)
        return {"session_id": session_id, "bytes": tree_bytes(session.workspace),
                "cache_bytes": sum(tree_bytes(path) for path in paths),
                "database": database_preview(session.workspace),
                "blocked_reason": self.blocked_reason(session)}

    def start(self, session_id: str, *, database: bool, caches: bool) -> dict[str, Any]:
        if self._task and not self._task.done():
            raise SpiritScoutError("已有清理任务正在执行，请等待完成")
        session = self._session(session_id)
        reason = self.blocked_reason(session)
        if reason:
            raise SpiritScoutError(reason)
        if not database and not caches:
            raise SpiritScoutError("请至少选择一项清理内容")
        self.manager.storage_busy.add(session_id)
        job = {"session_id": session_id, "status": "running", "phase": "正在准备清理", "started_at": time.time()}
        self.jobs[session_id] = job
        self._task = asyncio.create_task(self._run(session, job, database, caches))
        return dict(job)

    async def _run(self, session: Session, job: dict[str, Any], database: bool, caches: bool) -> None:
        loop = asyncio.get_running_loop()
        def progress(phase: str) -> None:
            loop.call_soon_threadsafe(job.update, {"phase": phase})
        def work() -> dict[str, Any]:
            for name in ("bb.db", "bb.db-wal", "bb.db-shm", "execution.key"):
                if not _regular(session.workspace / name, session.workspace):
                    raise SpiritScoutError("会话数据库或密钥路径存在符号链接，未执行清理")
            lock = ExecutionRunLock(session.workspace,
                                    session.bb_path if session.bb_path.exists() else None)
            lock.acquire()
            try:
                before = tree_bytes(session.workspace)
                warnings = []
                if caches:
                    progress("正在清理依赖下载缓存")
                    for path in cache_paths(session.workspace):
                        try:
                            shutil.rmtree(path)
                        except OSError:
                            warnings.append(f"下载缓存清理失败：{path.relative_to(session.workspace)}")
                result = clean_database(session.workspace, progress) if database else {}
                warnings.extend(result.pop("warnings", []))
                after = tree_bytes(session.workspace)
                return {**result, "before_bytes": before, "after_bytes": after,
                        "released_bytes": max(0, before - after), "warnings": warnings}
            finally:
                lock.close()
        try:
            result = await asyncio.to_thread(work)
            job.update(result)
            job.update(status="partial" if result["warnings"] else "completed", phase="清理结束")
        except Exception as exc:
            job.update(status="failed", phase="清理未完成", error=f"{type(exc).__name__}: {exc}")
        finally:
            job["finished_at"] = time.time()
            self.manager.storage_busy.discard(session.session_id)

    async def close(self) -> None:
        if self._task:
            await asyncio.shield(self._task)
