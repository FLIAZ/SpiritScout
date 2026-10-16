"""共享资产观测、技能覆盖和加密 MCP 配置；不保存测试授权。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet

from spiritscout.config.settings import McpServerConfig
from spiritscout.core.audit import sanitize_record
from spiritscout.core.exceptions import SpiritScoutError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,kind TEXT,identity TEXT,host TEXT,first_seen REAL,last_seen REAL);
CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,asset_id TEXT,source TEXT,observed_at REAL,digest TEXT,data TEXT,UNIQUE(asset_id,source));
CREATE INDEX IF NOT EXISTS obs_asset_time ON observations(asset_id,observed_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS settings(kind TEXT,id TEXT,version INTEGER,data BLOB,updated_at REAL,PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,kind TEXT,created_at REAL,title TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_resource_thread ON threads(kind);
CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY,thread_id TEXT,role TEXT,content TEXT,created_at REAL);
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,thread_id TEXT,state TEXT,data BLOB,result TEXT,created_at REAL);
CREATE TABLE IF NOT EXISTS resource_imports(source TEXT PRIMARY KEY,identity TEXT,high_water INTEGER,legacy_version TEXT);
"""


def resource_root(cfg: Any) -> Path:
    return Path(cfg.resources.root or Path(cfg.workspace.root) / "resources").resolve()


@lru_cache(maxsize=1)
def builtin_skills() -> dict[str, dict[str, Any]]:
    path = Path(__file__).parents[1] / "knowledge/data/security_skills.json"
    return {item["skill_id"]: item for item in json.loads(path.read_text())}


def safe_asset(data: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    """身份不含凭据及查询值；请求头、请求体和账号会话不进入共享库。"""
    kind = str(data.get("asset_type") or "unknown")
    if kind not in {
        "url",
        "domain",
        "ip",
        "host",
        "service",
        "endpoint",
        "app",
        "finding",
    }:
        raise ValueError("共享库仅保存网络资产与发现摘要，不收录凭据类型")
    value = str(data.get("value") or "").strip()
    if not value:
        raise ValueError("资产标识为空")
    host = ""
    if "://" in value:
        url = urlsplit(value)
        host = (url.hostname or "").rstrip(".").encode("idna").decode().lower()
        if not host or url.scheme.lower() not in {"http", "https"}:
            raise ValueError("资产 URL 无效")
        port = url.port
        authority = f"[{host}]" if ":" in host else host
        if port and (url.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            authority += f":{port}"
        value = urlunsplit(
            (
                url.scheme.lower(),
                authority,
                url.path or "/",
                urlencode(
                    [
                        (key, "")
                        for key in sorted(
                            {
                                key
                                for key, _ in parse_qsl(
                                    url.query, keep_blank_values=True
                                )
                            }
                        )
                    ]
                ),
                "",
            )
        )
    elif kind == "ip":
        host = str(ipaddress.ip_address(value))
        value = host
    elif kind in {"domain", "host", "service", "finding"}:
        parsed = urlsplit("//" + value)
        host = (parsed.hostname or value).rstrip(".").encode("idna").decode().lower()
        if kind in {"domain", "ip", "host"}:
            value = host
        elif kind == "service":
            value = f"[{host}]" if ":" in host else host
            port = data.get("port") or parsed.port
            if port:
                value += f":{port}"
    attrs = data.get("attrs") or {}
    method = str(attrs.get("method") or "GET").upper() if kind == "endpoint" else ""
    identity_port = "" if "://" in value else str(data.get("port") or "")
    identity = f"{kind}|{method}|{value}|{identity_port}|{attrs.get('finding_type', '')}|{attrs.get('parameter', '')}"
    selected = {
        "asset_type": kind,
        "value": value,
        "port": data.get("port"),
        "service": data.get("service"),
        "fingerprint": data.get("fingerprint") or {},
        "attrs": {
            key: attrs[key]
            for key in (
                "method",
                "status_code",
                "kind",
                "source",
                "finding_type",
                "parameter",
            )
            if key in attrs
        },
    }
    selected = sanitize_record({"detail": selected})["detail"]
    return identity, host, kind, selected


class ResourceStore:
    def __init__(self, root: str | Path, freshness_hours: float = 24):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "catalog.db"
        self.freshness_hours = freshness_hours
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def cipher(self) -> Fernet:
        path = self.root / "catalog.key"
        if not path.exists():
            with self.connection() as conn:
                if (
                    conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM proposals LIMIT 1").fetchone()
                ):
                    raise SpiritScoutError(
                        "资源库密钥缺失，请恢复 catalog.key；未生成替换密钥"
                    )
            fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".catalog-key-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                Path(temporary).unlink(missing_ok=True)
        return Fernet(path.read_bytes())

    def settings(self, kind: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM settings WHERE kind=? ORDER BY id", (kind,)
            ).fetchall()
        cipher = self.cipher() if rows else None
        return [
            {
                "id": row["id"],
                "version": row["version"],
                "updated_at": row["updated_at"],
                "data": json.loads(cipher.decrypt(row["data"])),
            }
            for row in rows
        ]

    def save(self, kind: str, key: str, data: dict[str, Any], expected: int) -> int:
        payload = self.cipher().encrypt(json.dumps(data, ensure_ascii=False).encode())
        with self.connection() as conn:
            row = conn.execute(
                "SELECT version FROM settings WHERE kind=? AND id=?", (kind, key)
            ).fetchone()
            version = row[0] if row else 0
            if version != expected:
                raise SpiritScoutError("配置已被其他操作更新，请刷新后再保存")
            conn.execute(
                "INSERT INTO settings VALUES(?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET "
                "version=excluded.version,data=excluded.data,updated_at=excluded.updated_at",
                (kind, key, version + 1, payload, time.time()),
            )
        return version + 1

    def effective_mcp(self, base: list[McpServerConfig]) -> list[McpServerConfig]:
        result = {item.name: item for item in base}
        for row in self.settings("mcp"):
            result[row["id"]] = McpServerConfig.model_validate(row["data"])
        return list(result.values())

    def skills(
        self, query: str = "", *, include_body: bool = False
    ) -> list[dict[str, Any]]:
        result = {
            key: {**value, "enabled": True, "version": 0, "origin": "builtin"}
            for key, value in builtin_skills().items()
        }
        for row in self.settings("skill"):
            result[row["id"]] = {
                **row["data"],
                "version": row["version"],
                "origin": "custom",
            }
        words = query.casefold().split()
        rows = [
            row
            for row in result.values()
            if not words
            or any(
                word
                in (
                    row["skill_id"]
                    + " "
                    + row.get("description", "")
                    + " "
                    + row.get("domain", "")
                ).casefold()
                for word in words
            )
        ]
        rows.sort(key=lambda item: (item["origin"] != "custom", item["skill_id"]))
        return (
            rows
            if include_body
            else [{k: v for k, v in row.items() if k != "body"} for row in rows]
        )

    def observe(
        self, data: dict[str, Any], source: str, observed_at: float | None = None
    ) -> str:
        identity, host, kind, selected = safe_asset(data)
        key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        text = json.dumps(selected, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(text.encode()).hexdigest()
        timestamp = observed_at if observed_at is not None else time.time()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO assets VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "first_seen=MIN(first_seen,excluded.first_seen),last_seen=MAX(last_seen,excluded.last_seen)",
                (key, kind, identity, host, timestamp, timestamp),
            )
            conn.execute(
                "INSERT OR IGNORE INTO observations(asset_id,source,observed_at,digest,data) VALUES(?,?,?,?,?)",
                (key, source, timestamp, digest, text),
            )
        return key

    def assets(
        self, query: str = "", offset: int = 0, limit: int = 40, *, host: str = ""
    ) -> dict[str, Any]:
        with self.connection() as conn:
            where = ("WHERE identity LIKE ?" + (" AND host=?" if host else "") +
                " AND (kind!='finding' OR json_extract((SELECT data FROM observations o "
                "WHERE o.asset_id=assets.id ORDER BY observed_at DESC,id DESC LIMIT 1), "
                "'$.fingerprint.status')='accepted')")
            args = ["%" + query + "%", *([host.lower().rstrip(".")] if host else [])]
            total = conn.execute(
                "SELECT COUNT(*) FROM assets " + where, args
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM assets "
                + where
                + " ORDER BY last_seen DESC,id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                observations = conn.execute(
                    "SELECT * FROM observations WHERE asset_id=? ORDER BY observed_at DESC,id DESC LIMIT 2",
                    (row["id"],),
                ).fetchall()
                if not observations:
                    continue
                latest = observations[0]
                count = conn.execute(
                    "SELECT COUNT(DISTINCT source) FROM observations WHERE asset_id=?",
                    (row["id"],),
                ).fetchone()[0]
                items.append(
                    {
                        **dict(row),
                        "data": json.loads(latest["data"]),
                        "source": latest["source"],
                        "observations": count,
                        "changed": len(observations) > 1
                        and latest["digest"] != observations[1]["digest"],
                        "freshness": "recent"
                        if time.time() - row["last_seen"] < self.freshness_hours * 3600
                        else "stale",
                        "verified_current_session": False,
                    }
                )
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    def history(self, key: str, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT source,observed_at,digest,data FROM observations WHERE asset_id=? ORDER BY observed_at DESC,id DESC LIMIT ? OFFSET ?",
                (key, limit + 1, offset),
            ).fetchall()
        return {
            "items": [
                {**dict(row), "data": json.loads(row["data"])} for row in rows[:limit]
            ],
            "has_more": len(rows) > limit,
        }

    def import_session(self, workspace: Path, *, full: bool = False) -> int:
        """用历史表行号增量读取；旧库、数据库替换或游标回退时完整重扫。"""
        db = workspace / "bb.db"
        if not db.is_file():
            return 0
        source = str(db.resolve())
        stat = db.stat()
        identity = f"{stat.st_dev}:{stat.st_ino}"
        versions = []
        for path in (db, Path(str(db) + "-wal")):
            try:
                entry = path.stat()
                versions.append((entry.st_ino, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns))
            except FileNotFoundError:
                versions.append(())
        legacy_version = json.dumps(versions)
        with self.connection() as catalog:
            previous = catalog.execute("SELECT * FROM resource_imports WHERE source=?", (source,)).fetchone()
        cursor = previous["high_water"] if previous and previous["identity"] == identity and not full else -1
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("BEGIN")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            has_history = "bb_record_history" in tables
            upper = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM bb_record_history").fetchone()[0] if has_history else 0
            reset_cursor = upper < cursor
            if reset_cursor:
                cursor = -1
            rows = []
            if not has_history and previous and cursor >= 0 and previous["legacy_version"] == legacy_version:
                return 0
            if "bb_records" in tables and (cursor < 0 or not has_history):
                rows.extend(conn.execute("SELECT layer,record_id,version,data,updated_at FROM bb_records "
                                         "WHERE layer IN ('asset','vuln')").fetchall())
            if has_history:
                rows.extend(conn.execute("SELECT layer,record_id,version,data,updated_at FROM bb_record_history "
                    "WHERE rowid>? AND rowid<=? AND layer IN ('asset','vuln')", (cursor, upper)).fetchall())
        finally:
            conn.close()
        from datetime import datetime

        for layer, key, version, data, updated in rows:
            asset = record_asset(layer, json.loads(data))
            if asset:
                self.observe(asset, f"{workspace.name}:{layer}:{key}@{version}",
                             datetime.fromisoformat(updated).timestamp())
        # 观测全部提交之后才推进；中断重放仍由来源唯一键保持幂等。
        with self.connection() as catalog:
            catalog.execute("INSERT INTO resource_imports VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                "high_water=CASE WHEN ? THEN excluded.high_water WHEN identity=excluded.identity THEN MAX(high_water,excluded.high_water) "
                "ELSE excluded.high_water END,identity=excluded.identity,legacy_version=excluded.legacy_version",
                (source, identity, upper, legacy_version, reset_cursor))
        return len(rows)


def record_asset(layer: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """历史发现只共享分类摘要，不携带载荷、凭据或当前有效性承诺。"""
    if layer == "asset":
        return data
    if layer == "vuln":
        value = data.get("endpoint") or data.get("target")
        if not value:
            return None
        return {
            "asset_type": "finding",
            "value": value,
            "fingerprint": {
                key: data.get(key)
                for key in ("title", "vuln_type", "severity", "status", "confidence")
            },
            "attrs": {
                "finding_type": data.get("vuln_type", ""),
                "parameter": data.get("parameter", ""),
                "source": "historical_finding_requires_revalidation",
            },
        }
    return None


def validate_mcp(data: dict[str, Any]) -> McpServerConfig:
    entry = McpServerConfig.model_validate(data)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry.name):
        raise ValueError("MCP 名称仅支持字母、数字、下划线和短横线")
    if entry.enabled and entry.transport == "stdio" and not entry.command.strip():
        raise ValueError("stdio 接入需要填写可执行命令")
    if (
        entry.enabled
        and entry.transport != "stdio"
        and urlsplit(entry.url).scheme not in {"http", "https"}
    ):
        raise ValueError("远程 MCP 需要有效的 HTTP(S) 地址")
    return entry
