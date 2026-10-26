"""逐笔用量持久化和按文件版本缓存的读取视图；旧 usage.json 保持可读。"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from spiritscout.gateway.cache import cache_summary


class UsageCall(BaseModel):
    ts: float = Field(allow_inf_nan=False)
    tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0, allow_inf_nan=False)
    model: str = ""
    mode: str = ""
    call_id: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    first_delta_ms: float | None = Field(default=None, ge=0)
    task_type: str = ""
    actor: str = ""
    request_bytes: int | None = Field(default=None, ge=0)
    reused_prefix_bytes: int | None = Field(default=None, ge=0)
    new_suffix_bytes: int | None = Field(default=None, ge=0)
    context_parts: dict[str, int] = Field(default_factory=dict)
    cache_scope: str = ""
    task_id: str = ""
    route_id: str = ""
    estimated_cost_cny_min: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    estimated_cost_cny_max: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    pricing_version: str = ""
    pricing_period: str = ""


class UsageSnapshot(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    used_tokens: int = Field(ge=0)
    used_usd: float = Field(default=0, ge=0)
    cache: dict[str, Any] = Field(default_factory=dict)
    per_model_tokens: dict[str, int] = Field(default_factory=dict)
    per_mode_tokens: dict[str, int] = Field(default_factory=dict)
    call_series: list[UsageCall] = Field(default_factory=list)
    calls: int = Field(default=0, ge=0)
    mode: str = ""


def file_version(path: Path) -> tuple[int, ...]:
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return ()


class UsageLedger:
    """一笔调用一个事务；崩溃恢复不依赖摘要刷新，重复调用 ID 不重复计量。"""

    def __init__(self, path: Path, legacy: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("CREATE TABLE IF NOT EXISTS baseline(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS calls(seq INTEGER PRIMARY KEY,call_id TEXT NOT NULL UNIQUE,data TEXT NOT NULL)")
            if not conn.execute("SELECT 1 FROM baseline WHERE id=1").fetchone():
                baseline: dict[str, Any] = {"used_tokens": 0}
                if legacy and legacy.is_file():
                    try:
                        baseline = UsageSnapshot.model_validate_json(legacy.read_bytes()).model_dump()
                    except (OSError, ValueError):
                        pass
                conn.execute("INSERT OR IGNORE INTO baseline VALUES(1,?)", (json.dumps(baseline),))
            conn.commit()
        os.chmod(self.path, 0o600)

    def append(self, entry: dict[str, Any]) -> None:
        row = UsageCall.model_validate({**entry, "call_id": entry.get("call_id") or uuid.uuid4().hex}).model_dump()
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("INSERT OR IGNORE INTO calls(call_id,data) VALUES(?,?)",
                         (row["call_id"], json.dumps(row, ensure_ascii=False, separators=(",", ":"))))

    def read(self) -> dict[str, Any]:
        return read_usage(self.path.with_suffix(".json"), ledger=self.path) or {"used_tokens": 0}


def _load_ledger(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        base = UsageSnapshot.model_validate_json(conn.execute("SELECT data FROM baseline WHERE id=1").fetchone()[0]).model_dump()
        rows = [UsageCall.model_validate_json(row[0]).model_dump()
                for row in conn.execute("SELECT data FROM calls ORDER BY seq")]
    for row in rows:
        base["used_tokens"] += row["tokens"]
        base["used_usd"] += row["cost_usd"]
        model, mode = row["model"], row["mode"]
        base["per_model_tokens"][model] = base["per_model_tokens"].get(model, 0) + row["tokens"]
        if mode:
            base["per_mode_tokens"][mode] = base["per_mode_tokens"].get(mode, 0) + row["tokens"]
        base["mode"] = mode
    base["call_series"].extend(rows)
    base["calls"] = max(base["calls"], len(base["call_series"]) - len(rows)) + len(rows)
    return base


class _FileCache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.version: tuple[Any, ...] | None = None
        self.snapshot: dict[str, Any] | None = None


@lru_cache(maxsize=256)
def _file_cache(path: str, ledger: bool) -> _FileCache:
    # 只为每个文件保留最新快照，不能按每次调用的版本积累整份历史。
    return _FileCache()


def _cached_read(path: str, ledger: bool, version: tuple[Any, ...]) -> dict[str, Any]:
    cache = _file_cache(path, ledger)
    with cache.lock:
        if cache.version != version or cache.snapshot is None:
            snapshot = (_load_ledger(Path(path)) if ledger else
                        UsageSnapshot.model_validate_json(Path(path).read_bytes()).model_dump())
            snapshot["cache"] = cache_summary(snapshot["call_series"])
            cache.snapshot, cache.version = snapshot, version
        return cache.snapshot


def read_usage(legacy: Path, *, ledger: Path | None = None) -> dict[str, Any] | None:
    """原子替换、WAL 追加和同长度改写都会失效；不让调用方修改缓存本体。"""
    ledger = ledger or legacy.with_suffix(".db")
    if ledger.is_file():
        path = ledger
        version = (file_version(path), file_version(Path(str(path) + "-wal")))
        is_ledger = True
    elif legacy.is_file():
        path, version, is_ledger = legacy, (file_version(legacy),), False
    else:
        return None
    try:
        return copy.deepcopy(_cached_read(str(path.resolve()), is_ledger, version))
    except (OSError, ValueError, sqlite3.Error, TypeError):
        return None


def bind_usage(gateway: Any, legacy: Path) -> UsageLedger:
    ledger = UsageLedger(legacy.with_suffix(".db"), legacy)
    gateway.budget.restore(ledger.read())
    def record(entry: dict[str, Any]) -> None:
        try:
            ledger.append(entry)
        except (OSError, ValueError, sqlite3.Error) as exc:
            gateway.audit.log("usage.persist_failed", "gateway", {"error": str(exc)[:300]})
    gateway.budget.on_call = record
    return ledger
