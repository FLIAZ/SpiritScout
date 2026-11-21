"""会话内后台命令档案：独立于模型尾窗，分块无损压缩，不按时间删除。"""
from __future__ import annotations

import json
import os
import sqlite3
import zlib
from pathlib import Path

from spiritscout.core.storage_codec import pack_json, unpack_json

CHUNK_BYTES = 65536
DEFAULT_LIMIT = 512 * 1024 * 1024


class BackgroundArchive:
    def __init__(self, workspace: str | Path, *, writable: bool = False, limit: int = DEFAULT_LIMIT):
        folder = Path(workspace) / "background-tasks"
        path = folder / "archive.db"
        for candidate in (folder, path, path.with_name("archive.db-journal")):
            if candidate.is_symlink():
                raise OSError("后台任务存档路径存在符号链接")
        self.path = path
        self.limit = limit
        self.conn = None
        if not writable and not path.exists():
            return
        if writable:
            folder.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        self.conn = sqlite3.connect(path.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=3, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            if writable:
                self.conn.executescript("""
                    PRAGMA auto_vacuum=INCREMENTAL;
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY, created REAL NOT NULL, data BLOB NOT NULL,
                        state TEXT NOT NULL DEFAULT 'complete', status TEXT NOT NULL DEFAULT 'running',
                        stdout_bytes INTEGER NOT NULL DEFAULT 0, stderr_bytes INTEGER NOT NULL DEFAULT 0,
                        stored_bytes INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS chunks (
                        job TEXT NOT NULL, stream TEXT NOT NULL, offset INTEGER NOT NULL,
                        size INTEGER NOT NULL, data BLOB NOT NULL,
                        PRIMARY KEY(job, stream, offset)
                    );
                    CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC, id);
                """)
                # 旧索引只有预览；迁移身份和状态，绝不声称恢复了未保存的原文。
                legacy = folder / "jobs.json"
                if legacy.is_file() and not legacy.is_symlink() and not self.conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
                    with legacy.open("rb") as source:
                        raw = source.read(1048577)
                    if len(raw) > 1048576:
                        raise ValueError("旧后台任务索引过大")
                    for row in json.loads(raw):
                        self.conn.execute("INSERT OR IGNORE INTO jobs(id,created,data,state,status) VALUES(?,?,?,'legacy',?)",
                                          (row["job_id"], row.get("created_at", 0), pack_json(row), row.get("status", "unknown")))
                self.conn.commit()
            self.used = self.conn.execute("SELECT coalesce(sum(stored_bytes),0) FROM jobs").fetchone()[0]
            self.version = self.conn.execute("PRAGMA data_version").fetchone()[0]
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def save(self, row: dict, *, new: bool = False):
        with self.conn:
            self.conn.execute("""INSERT INTO jobs(id,created,data,status) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET data=excluded.data,status=excluded.status WHERE jobs.state!='cleared'""",
                              (row["job_id"], row["created_at"], pack_json(row), row["status"]))
            if new:
                self.conn.execute("UPDATE jobs SET state='complete' WHERE id=?", (row["job_id"],))

    def append(self, job: str, stream: str, data: bytes):
        if not data:
            return
        row = self.conn.execute(f"SELECT state,{stream}_bytes AS size FROM jobs WHERE id=?", (job,)).fetchone()
        if row is None or row["state"] != "complete":
            return
        version = self.conn.execute("PRAGMA data_version").fetchone()[0]
        if version != self.version:
            self.used = self.conn.execute("SELECT coalesce(sum(stored_bytes),0) FROM jobs").fetchone()[0]
            self.version = version
        offset = row["size"]
        with self.conn:
            for start in range(0, len(data), CHUNK_BYTES):
                raw = data[start:start + CHUNK_BYTES]
                packed = zlib.compress(raw, level=3)
                # 配额包含每块的索引开销；限制存档，不停止命令或清除旧证据。
                charged = len(packed) + 256
                if self.used + charged > self.limit:
                    self.conn.execute("UPDATE jobs SET state='limited' WHERE id=?", (job,))
                    break
                self.conn.execute("INSERT INTO chunks VALUES(?,?,?,?,?)", (job, stream, offset, len(raw), packed))
                self.conn.execute(f"UPDATE jobs SET {stream}_bytes={stream}_bytes+?, stored_bytes=stored_bytes+? WHERE id=?",
                                  (len(raw), charged, job))
                offset += len(raw)
                self.used += charged

    def fail(self, job: str, state="error"):
        with self.conn:
            self.conn.execute("UPDATE jobs SET state=? WHERE id=? AND state='complete'", (state, job))

    def get(self, job: str) -> dict | None:
        if self.conn is None:
            return None
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row) -> dict:
        result = unpack_json(row["data"])
        return {**result, "archive_state": row["state"], "stdout_bytes": row["stdout_bytes"],
                "stderr_bytes": row["stderr_bytes"], "stored_bytes": row["stored_bytes"]}

    def listing(self, offset=0, limit=50) -> dict:
        if self.conn is None:
            return {"items": [], "total": 0, "counts": {}, "stored_bytes": 0, "disk_bytes": 0, "limit_bytes": self.limit}
        counts = {}
        for record in self.conn.execute("SELECT status,count(*) FROM jobs GROUP BY status"):
            counts[record[0]] = record[1]
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY created DESC,id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"items": [self._row(row) for row in rows], "total": sum(counts.values()), "counts": counts,
                "stored_bytes": self.used, "disk_bytes": self.path.stat().st_size, "limit_bytes": self.limit}

    @staticmethod
    def _decode(row) -> bytes:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(row["data"], CHUNK_BYTES + 1)
        if len(raw) != row["size"] or len(raw) > CHUNK_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("后台输出存档块损坏")
        return raw

    def chunks(self, job: str, stream: str, *, start=0, end: int):
        # 每次只读一个有界块；下载不把全文载入内存，也不跨网络 yield 持有读事务。
        position = start
        while position < end:
            row = self.conn.execute("SELECT offset,size,data FROM chunks WHERE job=? AND stream=? AND offset<=? ORDER BY offset DESC LIMIT 1",
                                    (job, stream, position)).fetchone()
            if row is None or row["offset"] + row["size"] <= position:
                raise ValueError("后台输出存档不连续")
            raw = self._decode(row)
            yield raw[max(0, position - row["offset"]):min(len(raw), end - row["offset"])]
            position = row["offset"] + row["size"]

    def output(self, job: str, stream: str, *, before: int | None = None, limit=32768, query="", after=0) -> dict:
        item = self.get(job)
        if item is None:
            raise KeyError(job)
        total = item[f"{stream}_bytes"]
        match = None
        if query:
            needle = query.encode("utf-8")
            carry = b""
            position = after
            for raw in self.chunks(job, stream, start=after, end=total):
                joined = carry + raw
                found = joined.find(needle)
                if found >= 0:
                    match = position - len(carry) + found
                    break
                carry = joined[-(len(needle) - 1):] if len(needle) > 1 else b""
                position += len(raw)
            before = min(total, match + limit // 2) if match is not None else min(total, before if before is not None else total)
        end = min(total, total if before is None else before)
        start = max(0, end - limit)
        # 尾页可能从 UTF-8 字符中间开始，多读最多三字节并回到字符边界。
        padded = max(0, start - 3)
        raw = b"".join(self.chunks(job, stream, start=padded, end=min(total, end + 3)))
        while end < total and raw[end - padded] & 0xC0 == 0x80:
            end += 1
        raw = raw[:end - padded]
        index = start - padded
        while index > 0 and index < len(raw) and raw[index] & 0xC0 == 0x80:
            index -= 1
        start = padded + index
        return {"text": raw[index:].decode("utf-8", errors="replace"), "start": start, "end": end,
                "total_bytes": total, "has_earlier": start > 0, "match": match, "archive_state": item["archive_state"]}

    def clear(self, job: str):
        item = self.get(job)
        if item is None:
            raise KeyError(job)
        if item.get("status") == "running":
            raise ValueError("运行中的任务不能清理存档")
        data = {key: value for key, value in item.items() if key not in {"command", "stdout", "stderr", "output_file"}}
        with self.conn:
            self.conn.execute("DELETE FROM chunks WHERE job=?", (job,))
            self.conn.execute("UPDATE jobs SET data=?,state='cleared',stdout_bytes=0,stderr_bytes=0,stored_bytes=0 WHERE id=?", (pack_json(data), job))
        self.used -= item["stored_bytes"]
        self.conn.execute("PRAGMA incremental_vacuum")
