"""SSE 事件流：会话审计日志跟随 + 心跳保活。

数据源是 append-only 的 audit.jsonl。新页面使用完整行结尾的字节偏移
作为分页与 SSE 游标，断线后直接 seek；旧客户端保留行号续传兼容。
未写完的尾部行本轮不发，下轮从行首重读，中文等多字节文本不影响对齐。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from spiritscout.core.audit import sanitize_record

_HEARTBEAT = ": hb\n\n"
_DONE = "event: done\ndata: {}\n\n"


def read_audit_page(path: Path, *, before: int | None = None,
                    limit: int = 160) -> dict[str, Any]:
    """从字节游标向前读取一页，不为最近历史扫描整个长日志。"""
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return {"items": [], "older_cursor": 0, "cursor": 0, "has_more": False}
    with fh:
        size = fh.seek(0, 2)
        end = min(max(before, 0), size) if before is not None else size
        start, data = end, b""
        while start > 0 and data.count(b"\n") < limit + 2:
            block_start = max(0, start - 65536)
            fh.seek(block_start)
            data = fh.read(start - block_start) + data
            start = block_start
        if start:
            # 块首可能落在一行中间；该行留给上一页完整读取。
            first = data.find(b"\n") + 1
            start += first
            data = data[first:]
        rows = data.splitlines(keepends=True)
        complete = rows if not rows or rows[-1].endswith(b"\n") else rows[:-1]
        offset = start + sum(len(row) for row in complete[:-limit])
        older = offset
        items = []
        for row in complete[-limit:]:
            offset += len(row)
            if not row.strip():
                continue
            try:
                event = sanitize_record(json.loads(row))
                if not isinstance(event, dict):
                    raise ValueError("审计记录不是对象")
            except (ValueError, TypeError, AttributeError):
                event = {"type": "audit.invalid", "detail": {"error": "日志行无效"}}
            items.append({"id": offset, "event": event})
        return {"items": items, "older_cursor": older,
                "cursor": offset, "has_more": older > 0}


def read_activity_page(path: Path, *, before: int | None = None,
                       limit: int = 160) -> dict[str, Any]:
    """按完整活动计页，流式碎片不消耗工具记录的展示名额，游标仍指原始字节。"""
    items: list[dict[str, Any]] = []
    cursor = before
    newest = 0
    older = 0
    has_more = False
    count = 0
    while count < limit:
        page = read_audit_page(path, before=cursor, limit=limit)
        if not items:
            newest = page["cursor"]
        batch = page["items"]
        items = batch + items
        count += sum(item["event"].get("type") != "agent.delta" for item in batch)
        older, has_more = page["older_cursor"], page["has_more"]
        if not has_more or not batch:
            break
        cursor = older
    # 以第 limit 个完整活动的起点切页，前面的原始片段留给上一页。
    remaining = limit
    start = 0
    for index in range(len(items) - 1, -1, -1):
        if items[index]["event"].get("type") != "agent.delta":
            remaining -= 1
        if remaining == 0:
            start = index
            break
    if start:
        older, has_more = items[start - 1]["id"], True
        items = items[start:]
    merged: list[dict[str, Any]] = []
    for item in items:
        event = item["event"]
        detail = event.get("detail") or {}
        previous = merged[-1]["event"] if merged else {}
        prev_detail = previous.get("detail") or {}
        if (event.get("type") == previous.get("type") == "agent.delta"
                and event.get("actor") == previous.get("actor")
                and detail.get("kind") in {"reasoning", "text"}
                and all(detail.get(key) == prev_detail.get(key)
                        for key in ("kind", "call_id", "route", "turn"))):
            merged[-1] = {"id": item["id"], "event": {
                **event, "detail": {**detail,
                    "text": str(prev_detail.get("text") or "") + str(detail.get("text") or "")}}}
        else:
            merged.append(item)
    return {"items": merged, "older_cursor": older, "cursor": newest,
            "has_more": has_more, "activity_count": min(count, limit)}


def _frame(event_id: int, line: str) -> str:
    # 历史日志可能含旧版本写下的原始参数；读取时也不能直接转发。
    try:
        line = json.dumps(sanitize_record(json.loads(line)), ensure_ascii=False)
    except (ValueError, TypeError, AttributeError):
        line = json.dumps({"type": "audit.invalid", "detail": {"error": "日志行无效"}})
    return f"id: {event_id}\nevent: audit\ndata: {line}\n\n"


async def stream_audit(path: Path, last_event_id: int = 0, *,
                       byte_offset: int | None = None,
                       poll: float = 0.5, heartbeat: float = 15.0,
                       max_frames: int | None = None,
                       is_terminal: Callable[[], bool] | None = None,
                       ) -> AsyncIterator[str]:
    """跟随审计文件产出 SSE 帧。

    - max_frames：测试截断（生产为 None 不限）；
    - is_terminal：会话终态回调——已终态且审计流排空后产出 done 帧并结束
      （运行中会话保持长连接，心跳保活）。终态可收尾让短轮询客户端也能
      完整消费历史流，而不必永久挂连接。
    """
    cursor = 0
    offset = 0
    fh = None
    emitted = 0

    def _open() -> None:
        nonlocal fh, cursor, offset
        try:
            fh = path.open("rb")   # 二进制：字节偏移即真偏移，与跳行计量同源
        except FileNotFoundError:
            fh = None
            return
        skipped = 0
        if byte_offset is not None:
            fh.seek(max(0, byte_offset))
            offset = fh.tell()
            return
        while skipped < last_event_id:
            line = fh.readline()
            if not line:
                break
            skipped += 1
        cursor = skipped
        offset = fh.tell()

    def _done() -> bool:
        return is_terminal is not None and is_terminal()

    _open()
    idle = 0.0
    try:
        while True:
            if fh is None:                       # 审计文件尚未创建（会话刚启动）
                _open()
                if fh is None:
                    if _done():
                        yield _DONE
                        return
                    yield _HEARTBEAT
                    await asyncio.sleep(poll)
                    continue
            fh.seek(offset)
            data = fh.read()
            if data:
                lines = data.split(b"\n")
                if data.endswith(b"\n"):
                    lines.pop()                  # 完整换行结尾：末元素是空串，不是一行
                    tail = b""
                else:
                    tail = lines.pop()           # 半行：本轮不发，下轮从行首重读
                for line in lines:
                    cursor += 1
                    if byte_offset is not None:
                        offset += len(line) + 1
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        yield _frame(offset if byte_offset is not None else cursor, text)
                        emitted += 1
                        if max_frames is not None and emitted >= max_frames:
                            return
                        if emitted % 64 == 0:
                            await asyncio.sleep(0)  # 大量追帧时让出事件循环
                offset = fh.tell() - len(tail)   # 回退未消费的半行字节
                idle = 0.0
            else:
                if _done():                      # 终态且已排空：收尾
                    yield _DONE
                    return
                idle += poll
                if idle >= heartbeat:
                    idle = 0.0
                    yield _HEARTBEAT
            await asyncio.sleep(poll)
    finally:
        if fh is not None:
            fh.close()
