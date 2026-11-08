"""真实数据包索引：保留 tshark 解析来源，不把应用层交互伪装成网络帧。"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from spiritscout.traffic.recorder import _connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
 capture_id TEXT PRIMARY KEY, created_at REAL NOT NULL, source TEXT NOT NULL,
 name TEXT NOT NULL, interface TEXT NOT NULL, capture_filter TEXT NOT NULL,
 status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', packet_count INTEGER NOT NULL DEFAULT 0,
 file_bytes INTEGER NOT NULL DEFAULT 0, limited INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS packets (
 id INTEGER PRIMARY KEY AUTOINCREMENT, capture_id TEXT NOT NULL, number INTEGER NOT NULL,
 time REAL NOT NULL, relative_time REAL NOT NULL, source TEXT NOT NULL, destination TEXT NOT NULL,
 source_port INTEGER, destination_port INTEGER, protocol TEXT NOT NULL, length INTEGER NOT NULL,
 captured_length INTEGER NOT NULL, info TEXT NOT NULL, tcp_stream INTEGER, udp_stream INTEGER,
 detail TEXT NOT NULL, UNIQUE(capture_id, number)
);
CREATE INDEX IF NOT EXISTS packet_capture ON packets(capture_id, id DESC);
CREATE INDEX IF NOT EXISTS packet_tcp_stream ON packets(capture_id, tcp_stream);
"""
COLUMNS = ("id,capture_id,number,time,relative_time,source,destination,source_port,"
           "destination_port,protocol,length,captured_length,info,tcp_stream,udp_stream")


def _schema(conn: Any) -> None:
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def _exists(conn: Any) -> bool:
    return conn is not None and conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='packets'").fetchone() is not None


def capture_path(workspace: Path, capture_id: str, name: str = "") -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", capture_id) or name not in ("", "source.pcap", "source.pcapng"):
        raise ValueError("抓包引用无效")
    root = workspace / "traffic"
    folder = root / "captures" / capture_id
    for path in (root, root / "captures", folder, folder / name if name else folder):
        if path.is_symlink():
            raise ValueError("抓包路径不能为符号链接")
    return folder / name if name else folder


def create_capture(workspace: Path, capture_id: str, source: str, *, name: str = "",
                   interface: str = "", capture_filter: str = "") -> None:
    with _connect(workspace, write=True) as conn:
        _schema(conn)
        conn.execute("""INSERT INTO captures
          (capture_id,created_at,source,name,interface,capture_filter,status) VALUES (?,?,?,?,?,?,'starting')""",
          (capture_id, time.time(), source, name[:200], interface, capture_filter))


def update_capture(workspace: Path, capture_id: str, *, status: str, error: str = "",
                   file_bytes: int = 0, limited: bool = False) -> None:
    with _connect(workspace, write=True) as conn:
        conn.execute("UPDATE captures SET status=?,error=?,file_bytes=?,limited=? WHERE capture_id=?",
                     (status, error[:2000], file_bytes, int(limited), capture_id))


def captures(workspace: Path) -> list[dict[str, Any]]:
    with _connect(workspace) as conn:
        return [] if not _exists(conn) else [dict(row) for row in conn.execute(
            "SELECT * FROM captures ORDER BY created_at DESC LIMIT 200")]


def get_capture(workspace: Path, capture_id: str) -> dict[str, Any] | None:
    with _connect(workspace) as conn:
        row = None if not _exists(conn) else conn.execute(
            "SELECT * FROM captures WHERE capture_id=?", (capture_id,)).fetchone()
    return dict(row) if row is not None else None


def packet_summary(workspace: Path) -> dict[str, int]:
    with _connect(workspace) as conn:
        if not _exists(conn):
            return {"packet_count": 0, "packet_bytes": 0}
        return dict(conn.execute("SELECT count(*) packet_count,coalesce(sum(length),0) packet_bytes FROM packets").fetchone())


def append_packets(workspace: Path, capture_id: str, rows: list[dict[str, Any]]) -> None:
    with _connect(workspace, write=True) as conn:
        conn.executemany(f"INSERT INTO packets ({COLUMNS.split(',', 1)[1]},detail) "
                         f"VALUES ({','.join('?' for _ in COLUMNS.split(','))})",
                         [(*[row[key] for key in COLUMNS.split(',')[1:]], json.dumps(row['detail'], ensure_ascii=False))
                          for row in rows])
        conn.execute("UPDATE captures SET packet_count=packet_count+? WHERE capture_id=?", (len(rows), capture_id))


def packet_page(workspace: Path, capture_id: str, *, before: int | None = None,
                limit: int = 100, numbers: list[int] | None = None, order: str = "desc") -> dict[str, Any]:
    if order not in {"asc", "desc"}:
        raise ValueError("无效的包顺序")
    clauses, args = ["capture_id=?"], [capture_id]
    if before is not None:
        clauses.append("id > ?" if order == "asc" else "id < ?")
        args.append(before)
    if numbers is not None:
        if not numbers:
            return {"items": [], "has_more": False, "next_before": None}
        clauses.append(f"number IN ({','.join('?' for _ in numbers)})")
        args.extend(numbers)
    with _connect(workspace) as conn:
        rows = [] if not _exists(conn) else conn.execute(
            f"SELECT {COLUMNS} FROM packets WHERE {' AND '.join(clauses)} ORDER BY id {order.upper()} LIMIT ?",
            (*args, limit + 1)).fetchall()
    items = [dict(row) for row in rows[:limit]]
    return {"items": items, "has_more": len(rows) > limit,
            "next_before": items[-1]["id"] if items else None}


def packet_detail(workspace: Path, packet_id: int) -> dict[str, Any] | None:
    with _connect(workspace) as conn:
        row = None if not _exists(conn) else conn.execute("SELECT * FROM packets WHERE id=?", (packet_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result.update(json.loads(result.pop("detail")))
    return result


def decode_packet(document: dict[str, Any], capture_id: str) -> dict[str, Any] | None:
    """EK 每包一行；字段可能为数组或重复协议层，只取已解析事实，不猜应用协议。"""
    layers = document.get("layers")
    if not isinstance(layers, dict) or "frame" not in layers:
        return None

    def field(layer: str, name: str, fallback: str = "") -> str:
        tree = layers.get(layer, {})
        if isinstance(tree, list):
            tree = next((v for v in tree if isinstance(v, dict)), {})
        value = tree.get(layer + "_" + name.replace(".", "_"), fallback) if isinstance(tree, dict) else fallback
        if isinstance(value, list):
            value = value[0] if value else fallback
        return str(value)

    def integer(value: str) -> int | None:
        return int(value) if re.fullmatch(r"\d+", value) else None

    protocols = field("frame", "frame.protocols").split(":")
    protocol = next((p.upper() for p in ("websocket", "http2", "http", "tls", "dns", "quic", "tcp", "udp", "icmpv6", "icmp", "arp")
                     if p in layers), (protocols[-1] if protocols else "unknown").upper())
    source = field("ip", "ip.src") or field("ipv6", "ipv6.src") or field("eth", "eth.src")
    destination = field("ip", "ip.dst") or field("ipv6", "ipv6.dst") or field("eth", "eth.dst")
    info = ("HTTP " + field("http", "http.response.code") if field("http", "http.response.code") else
            (field("http", "http.request.method") + " " + field("http", "http.request.full_uri")).strip())
    if not info and "dns" in layers:
        direction = "Response" if field("dns", "dns.flags.response").lower() in {"true", "1"} else "Query"
        info = f"{direction} id={field('dns', 'dns.id')} {field('dns', 'dns.qry.name')}"
    info = info or ("opcode=" + field("websocket", "websocket.opcode") if "websocket" in layers else "")
    if not info and "tcp" in layers:
        flags = [name.upper() for name in ("syn", "fin", "reset", "push", "ack", "urg")
                 if field("tcp", "tcp.flags." + name).lower() in {"true", "1"}]
        retransmission = "[Retransmission] " if field("tcp", "tcp.analysis.retransmission").lower() in {"true", "1"} else ""
        info = f"{retransmission}[{', '.join(flags)}] Seq={field('tcp','tcp.seq')} Ack={field('tcp','tcp.ack')} Len={field('tcp','tcp.len')}"
    info = info or protocol
    raw = layers.get("frame_raw", "")
    if not isinstance(raw, str) or not re.fullmatch(r"[a-fA-F0-9]*", raw) or len(raw) % 2:
        raw = ""
    # 协议树不重复保存每个字段的 raw 副本；原始帧另存，字段/列表有界。
    trees, remaining, fields_limited = {}, 24000, False
    for name in dict.fromkeys(["frame", *protocols]):
        tree = layers.get(name)
        if not isinstance(tree, dict):
            continue
        kept = {}
        for key, value in tree.items():
            if key.endswith("_raw"):
                continue
            if remaining <= 0:
                fields_limited = True
                continue
            fields_limited |= (len(value) > 16 or any(len(str(v)) > 512 for v in value)) if isinstance(value, list) else len(str(value)) > 1024
            value = [str(v)[:512] for v in value[:16]] if isinstance(value, list) else str(value)[:1024]
            size = len(json.dumps(value).encode()) + len(key)
            if size > remaining:
                fields_limited = True
                continue
            kept[key] = value
            remaining -= size
        trees[name] = kept
    timestamp = field("frame", "frame.time_epoch")
    packet_time = (datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
                   if "T" in timestamp else float(timestamp or float(document["timestamp"]) / 1000))
    payload = field("tcp", "tcp.payload") or field("udp", "udp.payload") or field("data", "data.data")
    payload_hex = payload.replace(":", "")
    payload_bytes = bytes.fromhex(payload_hex[:16384]) if re.fullmatch(r"(?:[a-fA-F0-9]{2})*", payload_hex) else b""
    payload_text = payload_bytes.decode("utf-8", errors="replace")
    return {"capture_id": capture_id, "number": int(field("frame", "frame.number")),
            "time": packet_time,
            "relative_time": float(field("frame", "frame.time_relative", "0")),
            "source": source[:200], "destination": destination[:200],
            "source_port": integer(field("tcp", "tcp.srcport") or field("udp", "udp.srcport")),
            "destination_port": integer(field("tcp", "tcp.dstport") or field("udp", "udp.dstport")),
            "protocol": protocol, "length": int(field("frame", "frame.len", "0")),
            "captured_length": int(field("frame", "frame.cap_len", "0")), "info": info[:2000],
            "tcp_stream": integer(field("tcp", "tcp.stream")), "udp_stream": integer(field("udp", "udp.stream")),
            "detail": {"layers": trees, "raw_hex": raw[:131072], "raw_truncated": len(raw) > 131072,
                       "payload_text": payload_text, "payload_hex": payload_bytes.hex(),
                       "payload_truncated": len(payload_hex) > 16384,
                       "fields_limited": fields_limited, "protocols": protocols}}
