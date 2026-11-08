"""会话 HTTP 证据索引：旁路记录，不改变请求、下载上限和工具结果。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from spiritscout.tools.invocation import current_execution_scope, current_invocation

_LOG = logging.getLogger(__name__)
_WRITE_LOCKS: weakref.WeakValueDictionary[Path, Any] = weakref.WeakValueDictionary()
_WRITE_LOCKS_GUARD = threading.Lock()
BODY_LIMIT = 32768
_SECRET = re.compile(r"password|passwd|secret|token|authorization|cookie|api[-_]?key|session[-_]?id", re.I)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
 id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at REAL NOT NULL,
 method TEXT NOT NULL, url TEXT NOT NULL, host TEXT NOT NULL,
 status INTEGER, error TEXT NOT NULL, duration_ms REAL NOT NULL,
 request_bytes INTEGER NOT NULL, response_bytes INTEGER NOT NULL,
 agent_id TEXT NOT NULL, task_id TEXT NOT NULL, invocation_id TEXT NOT NULL,
 detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proxy_contexts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proxy_raw (exchange_id INTEGER PRIMARY KEY, metadata TEXT NOT NULL,
 request_blob TEXT NOT NULL, response_blob TEXT NOT NULL);
"""
_COLUMNS = ("id,captured_at,method,url,host,status,error,duration_ms,"
            "request_bytes,response_bytes,agent_id,task_id,invocation_id")


def safe_url(value: str) -> str:
    """展示副本去除用户口令、片段及常见认证参数；不参与真实请求。"""
    try:
        parts = urlsplit(value)
        host = parts.netloc.rsplit("@", 1)[-1]
        query = urlencode([(key, "[已隐藏]" if _SECRET.search(key) else val)
                           for key, val in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, host, parts.path, query, ""))[:8192]
    except ValueError:
        return "[地址无法解析]"


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 30:
        return "[嵌套过深，已省略]"
    if isinstance(value, dict):
        return {key: "[已隐藏]" if _SECRET.search(key) else _redact(val, depth + 1)
                for key, val in value.items()}
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value]
    return value


def _headers(headers: httpx.Headers) -> list[list[str]]:
    return [[key, "[已隐藏]" if _SECRET.search(key) else
             safe_url(value) if key.lower() in {"location", "referer"} else value[:8192]]
            for key, value in headers.multi_items()][:200]


def _body(data: bytes, content_type: str) -> dict[str, Any]:
    # 对结构化正文先脱敏再截断，避免截断破坏 JSON 后认证字段泄漏。
    content_type = content_type.lower()
    structured = "json" in content_type or "x-www-form-urlencoded" in content_type
    if structured:
        if len(data) > BODY_LIMIT:
            return {"text": "[结构化正文超出预览上限，已省略]", "encoding": "text", "truncated": True}
        try:
            text = data.decode("utf-8")
            if "json" in content_type:
                text = json.dumps(_redact(json.loads(text)), ensure_ascii=False, indent=2)
            else:
                text = urlencode([(key, "[已隐藏]" if _SECRET.search(key) else val)
                                  for key, val in parse_qsl(text, keep_blank_values=True)])
            encoded = text.encode("utf-8")
            return {"text": encoded[:BODY_LIMIT].decode("utf-8", errors="ignore"),
                    "encoding": "text", "truncated": len(encoded) > BODY_LIMIT}
        except (ValueError, RecursionError):
            return {"text": "[结构化正文无法安全解析，已省略]", "encoding": "text", "truncated": True}
    preview = data[:BODY_LIMIT]
    try:
        text = preview.decode("utf-8")
        if "\x00" in text:
            raise ValueError("binary")
        encoding = "utf-8"
    except (UnicodeDecodeError, ValueError):
        text, encoding = base64.b64encode(preview).decode("ascii"), "base64"
    return {"text": text, "encoding": encoding, "truncated": len(data) > BODY_LIMIT}


def _write_lock(path: Path) -> Any:
    # SQLite 首次切换 WAL 会直接报 BUSY；同库写事务串行，不阻塞其它会话。
    # 空闲锁随引用释放，避免历史会话不断增加常驻缓存。
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _WRITE_LOCKS[path] = lock
        return lock


@contextmanager
def _connect(workspace: Path, *, write: bool = False) -> Iterator[sqlite3.Connection | None]:
    root = workspace / "traffic"
    if root.is_symlink():
        raise ValueError("流量目录不能是符号链接")
    if write:
        root.mkdir(mode=0o700, exist_ok=True)
    for name in ("records.db", "records.db-wal", "records.db-shm", "records.db-journal"):
        if (root / name).is_symlink():
            raise ValueError("流量数据库不能是符号链接")
    path = root / "records.db"
    if not write and not path.exists():
        yield None
        return
    with _write_lock(path.resolve()) if write else nullcontext():
        conn = sqlite3.connect(path.resolve().as_uri() + ("?mode=rwc" if write else "?mode=ro"),
                               uri=True, timeout=1)
        try:
            conn.row_factory = sqlite3.Row
            if write:
                path.chmod(0o600)
                conn.execute("PRAGMA journal_mode=WAL")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            else:
                conn.execute("PRAGMA query_only=ON")
            yield conn
            if write:
                conn.commit()
        finally:
            conn.close()


def _append(workspace: Path, record: dict[str, Any]) -> int:
    with _connect(workspace, write=True) as conn:
        cursor = conn.execute("""INSERT INTO exchanges
            (captured_at,method,url,host,status,error,duration_ms,request_bytes,response_bytes,
             agent_id,task_id,invocation_id,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (*[record[key] for key in _COLUMNS.split(",")[1:]],
             json.dumps(record["detail"], ensure_ascii=False)))
        record_id = cursor.lastrowid
        if record.get("raw"):
            raw = record["raw"]
            conn.execute("INSERT INTO proxy_raw VALUES (?,?,?,?)", (record_id,
                json.dumps(raw, ensure_ascii=False), raw["request"]["sha256"], raw["response"]["sha256"]))
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS exchange_fts USING fts5(content, tokenize='trigram')")
        except sqlite3.OperationalError:
            pass  # 缺少 FTS5 时继续记录，正文筛选退回预览扫描。
        else:
            content = record.get("search_text") or "\n".join(
                str(record["detail"].get(key, {}).get("text", "")) for key in ("request_body", "response_body"))
            conn.execute("INSERT INTO exchange_fts(rowid,content) VALUES (?,?)", (record_id, content[:2097152]))
        return record_id


async def record_http(workspace: str, request: httpx.Request, response: httpx.Response | None,
                      *, started: float, content: bytes = b"", complete: bool = False,
                      error: str = "") -> None:
    """失败只写本机日志；不将采集故障变成探测失败或重试目标请求。"""
    if not workspace:
        return
    scope, invocation = current_execution_scope.get(), current_invocation.get()
    try:
        request_body = request.content
        url = safe_url(str(request.url))
        record = {
            "captured_at": time.time(), "method": request.method, "url": url,
            "host": request.url.host, "status": response.status_code if response is not None else None,
            "error": error, "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "request_bytes": len(request_body), "response_bytes": len(content),
            "agent_id": scope.agent_id if scope else "", "task_id": scope.task_id if scope else "",
            "invocation_id": invocation.invocation_id if invocation else "",
            "detail": {"source": "http_probe", "tool_call_id": scope.tool_call_id if scope else "",
                       "request_headers": _headers(request.headers),
                       "request_body": _body(request_body, request.headers.get("content-type", "")),
                       "response_headers": _headers(response.headers) if response is not None else [],
                       "response_body": _body(content, response.headers.get("content-type", "")
                                              if response is not None else ""),
                       "response_complete": complete,
                       "http_version": response.http_version if response is not None else ""},
        }
        await asyncio.to_thread(_append, Path(workspace), record)
    except (OSError, ValueError, sqlite3.Error, httpx.RequestNotRead):
        # 不输出异常文本：底层异常可能携带目标地址或认证参数。
        _LOG.warning("会话 HTTP 流量记录失败，原工具结果不受影响")


async def record_network_event(workspace: str, *, protocol: str, target: str, source: str,
                               started: float, observations: dict[str, Any], received: bytes = b"",
                               error: str = "") -> None:
    """DNS API / TCP socket 观测不是原始帧；未获得的数据不补造。"""
    if not workspace:
        return
    scope, invocation = current_execution_scope.get(), current_invocation.get()
    try:
        record = {"captured_at": time.time(), "method": protocol, "url": target[:8192],
            "host": str(observations.get("host", ""))[:500], "status": None, "error": error,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "request_bytes": 0, "response_bytes": len(received),
            "agent_id": scope.agent_id if scope else "", "task_id": scope.task_id if scope else "",
            "invocation_id": invocation.invocation_id if invocation else "",
            "detail": {"source": source, "protocol": protocol,
                "observations": observations, "tool_call_id": scope.tool_call_id if scope else "",
                "request_headers": [], "response_headers": [], "request_body": _body(b"", ""),
                "response_body": _body(received, ""), "response_complete": False,
                "http_version": "", "note": "工具 API 观测；连接/解析记录不代表完整数据包，TCP 横幅仅为已读取片段。"}}
        await asyncio.to_thread(_append, Path(workspace), record)
    except (OSError, ValueError, sqlite3.Error):
        _LOG.warning("会话网络观测记录失败，原工具结果不受影响")


def summary(workspace: Path) -> dict[str, Any]:
    with _connect(workspace) as conn:
        if conn is None:
            return {"total": 0, "errors": 0, "request_bytes": 0, "response_bytes": 0, "last_at": None}
        return dict(conn.execute("""SELECT count(*) total,
            coalesce(sum(CASE WHEN error != '' OR status >= 400 THEN 1 ELSE 0 END),0) errors,
            coalesce(sum(request_bytes),0) request_bytes,
            coalesce(sum(response_bytes),0) response_bytes, max(captured_at) last_at FROM exchanges""").fetchone())


def page(workspace: Path, *, before: int | None = None, limit: int = 50,
         query: str = "", errors_only: bool = False, method: str = "", status: str = "",
         body: str = "") -> dict[str, Any]:
    clauses, params = [], []
    if before is not None:
        clauses.append("id < ?")
        params.append(before)
    if query:
        clauses.append("(instr(lower(url),lower(?)) > 0 OR instr(lower(method),lower(?)) > 0)")
        params.extend([query, query])
    if errors_only:
        clauses.append("(error != '' OR status >= 400)")
    if method:
        clauses.append("method = ?")
        params.append(method.upper())
    if status:
        if re.fullmatch(r"[1-5]xx", status.lower()):
            clauses.append("status >= ? AND status < ?")
            params.extend((int(status[0]) * 100, (int(status[0]) + 1) * 100))
        elif re.fullmatch(r"[1-5][0-9]{2}", status):
            clauses.append("status = ?")
            params.append(int(status))
        else:
            raise ValueError("状态码需要 100–599 或 2xx / 3xx / 4xx / 5xx")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect(workspace) as conn:
        if body and conn is not None:
            preview = "coalesce(json_extract(detail,'$.request_body.text'),'') || char(10) || coalesce(json_extract(detail,'$.response_body.text'),'')"
            has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='exchange_fts'").fetchone()
            if has_fts and len(body) >= 3:
                clauses.append(f"(id IN (SELECT rowid FROM exchange_fts WHERE content MATCH ?) OR "
                               f"(id NOT IN (SELECT rowid FROM exchange_fts) AND instr(lower({preview}),lower(?)) > 0))")
                params.extend(('"' + body.replace('"', '""') + '"', body))
            else:
                clauses.append(f"instr(lower({preview}),lower(?)) > 0")
                params.append(body)
            where = " WHERE " + " AND ".join(clauses)
        rows = [] if conn is None else conn.execute(
            f"SELECT {_COLUMNS} FROM exchanges{where} ORDER BY id DESC LIMIT ?",
            (*params, limit + 1)).fetchall()
    items = [dict(row) for row in rows[:limit]]
    return {"items": items, "has_more": len(rows) > limit,
            "next_before": items[-1]["id"] if items else None}


def detail(workspace: Path, record_id: int) -> dict[str, Any] | None:
    with _connect(workspace) as conn:
        row = None if conn is None else conn.execute(
            "SELECT * FROM exchanges WHERE id=?", (record_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result.update(json.loads(result.pop("detail")))
    return result
