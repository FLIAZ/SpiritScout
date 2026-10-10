"""SSE 事件流：会话审计日志跟随 + 心跳保活。

数据源是 append-only 的 audit.jsonl（同进程写入）。游标 = (行号, 字节偏移)：
按偏移增量读取，未写完的尾部行暂存下轮重读；行号即 SSE 事件 ID，
客户端断线重连以 Last-Event-ID 续传（重新扫描跳过已收行，一次性成本）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

_HEARTBEAT = ": hb\n\n"
_DONE = "event: done\ndata: {}\n\n"


def _frame(event_id: int, line: str) -> str:
    return f"id: {event_id}\nevent: audit\ndata: {line}\n\n"


async def stream_audit(path: Path, last_event_id: int = 0, *,
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
            fh = path.open("r", encoding="utf-8")  # 生命周期随生成器（finally 关闭）
        except FileNotFoundError:
            fh = None
            return
        skipped = 0
        while skipped < last_event_id:
            if not fh.readline():
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
                lines = data.split("\n")
                tail = "" if data.endswith("\n") else lines.pop()
                for line in lines:
                    cursor += 1
                    text = line.strip()
                    if text:
                        yield _frame(cursor, text)
                        emitted += 1
                        if max_frames is not None and emitted >= max_frames:
                            return
                offset = fh.tell() - len(tail.encode("utf-8"))
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
