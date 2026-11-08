"""可选 tshark 采集/离线解析：不提权、不改网络，进程生命周期归会话管理。"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from spiritscout.platform_support import kill_process_tree, process_options
from spiritscout.traffic import packets
from spiritscout.traffic.capture_io import wait_io as _io
from spiritscout.traffic.proxy import ProxyManager

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PACKETS = 10000
_MAGIC = {b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d", b"\x0a\x0d\x0d\x0a"}


class CaptureError(ValueError):
    pass


def _new_folder(workspace: Path, capture_id: str) -> Path:
    folder = packets.capture_path(workspace, capture_id)
    for directory in (workspace / "traffic", workspace / "traffic" / "captures", folder):
        directory.mkdir(mode=0o700, exist_ok=True)
    return folder


async def _tail(stream: asyncio.StreamReader) -> bytes:
    data = b""
    while chunk := await stream.read(4096):
        data = (data + chunk)[-4096:]
    return data


async def _command(args: list[str], *, timeout: float = 15, cap: int = 262144,
                   drain_overflow: bool = False) -> tuple[int, bytes, str]:
    """所有外部命令使用 argv，输出有界，超时/取消杀进程组并回收。"""
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **process_options())
    err = asyncio.create_task(_tail(proc.stderr))
    try:
        async def read() -> bytes:
            data = bytearray()
            while chunk := await proc.stdout.read(65536):
                data.extend(chunk[:max(0, cap - len(data))] if drain_overflow else chunk)
                if not drain_overflow and len(data) > cap:
                    raise CaptureError("解析输出超过上限，请缩小过滤范围")
            await proc.wait()
            return bytes(data)
        data = await asyncio.wait_for(read(), timeout)
        diagnostic = (await err).decode(errors="replace")
        return proc.returncode, data, diagnostic
    finally:
        kill_process_tree(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 3)
        if not err.done():
            err.cancel()
        await asyncio.gather(err, return_exceptions=True)


async def capabilities() -> dict[str, Any]:
    executable = shutil.which("tshark")
    result = {"available": bool(executable), "live_available": False, "interfaces": [],
              "error": "未安装 tshark；可继续查看应用层交互记录", "max_file_bytes": MAX_FILE_BYTES,
              "max_packets": MAX_PACKETS}
    if not executable:
        return result
    try:
        dumpcap = shutil.which("dumpcap") or str(Path(executable).with_name("dumpcap.exe" if os.name == "nt" else "dumpcap"))
        code, data, diagnostic = await _command([dumpcap, "-D"], timeout=5, cap=32768)
        interfaces = []
        for line in data.decode(errors="replace").splitlines():
            prefix, sep, label = line.partition(". ")
            if sep and prefix.isdigit():
                # 使用索引，避免将标签或 extcap 描述当成命令行参数。
                interfaces.append({"id": prefix, "name": label[:200]})
        result.update(live_available=code == 0 and bool(interfaces), interfaces=interfaces,
                      error="" if code == 0 and interfaces else diagnostic[:1000] or "无可用抓包网卡")
    except (OSError, TimeoutError, CaptureError):
        result["error"] = "无法列出抓包网卡；请检查 tshark / dumpcap 执行权限。离线解析仍可使用。"
    return result


class CaptureManager:
    def __init__(self, manager: Any):
        self.manager = manager
        self.proxies = ProxyManager(manager)
        self.jobs: dict[str, asyncio.Future] = {}
        self.active_ids: dict[str, tuple[Path, str]] = {}
        self.deleted: set[str] = set()
        self.closing = False
        self.queue: asyncio.Queue = asyncio.Queue()
        self.supervisor: asyncio.Task | None = None

    def start_supervisor(self) -> None:
        if self.supervisor is None:
            self.supervisor = asyncio.create_task(self._supervise())
            self.proxies.start_supervisor()

    async def _supervise(self) -> None:
        while True:
            sid, placeholder, args = await self.queue.get()
            if not placeholder.cancelled():
                self.jobs[sid] = asyncio.create_task(self._run(*args))
                placeholder.set_result(None)
            self.queue.task_done()

    def _launch(self, sid: str, *args: Any) -> None:
        if self.supervisor is None or self.supervisor.done() or self.closing:
            raise CaptureError("流量采集服务尚未启动，请重启控制台")
        placeholder = asyncio.get_running_loop().create_future()
        self.jobs[sid] = placeholder
        self.queue.put_nowait((sid, placeholder, args))

    def _session(self, session_id: str) -> Any:
        session = self.manager.get(session_id)
        if self.closing or session_id in self.deleted or session is None or not session.workspace.is_dir():
            raise CaptureError("会话已删除或服务正在关闭")
        return session

    def _available(self, session_id: str) -> Any:
        session = self._session(session_id)
        if session_id in self.jobs and not self.jobs[session_id].done():
            raise CaptureError("本会话已有正在采集或解析的任务")
        if self.supervisor is None or self.supervisor.done():
            raise CaptureError("流量采集服务尚未启动，请重启控制台")
        return session

    async def start(self, session_id: str, interface: str, capture_filter: str, duration: int) -> dict[str, Any]:
        if not capture_filter.strip() or len(capture_filter) > 1024 or any(ord(c) < 32 for c in capture_filter):
            raise CaptureError("请填写非空 BPF 采集过滤器，例如 host 127.0.0.1 and tcp port 8080")
        if not 5 <= duration <= 600:
            raise CaptureError("采集时长须为 5–600 秒")
        self._available(session_id)
        caps = await capabilities()
        if not caps["live_available"]:
            raise CaptureError(caps["error"])
        if interface not in {item["id"] for item in caps["interfaces"]}:
            raise CaptureError("请选择可用网卡")
        session = self._available(session_id)
        capture_id = uuid.uuid4().hex
        folder = _new_folder(session.workspace, capture_id)
        path = folder / "source.pcapng"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        packets.create_capture(session.workspace, capture_id, "live", name="实时抓包",
                               interface=interface, capture_filter=capture_filter)
        self.active_ids[session_id] = (session.workspace, capture_id)
        args = [shutil.which("tshark"), "-n", "-l", "-p", "-i", interface, "-f", capture_filter,
                "-a", f"duration:{duration}", "-a", f"filesize:{MAX_FILE_BYTES // 1024}",
                "-c", str(MAX_PACKETS), "-s", "65535", "-w", str(path), "-P", "-T", "ek", "-x"]
        self._launch(session_id, session.workspace, capture_id, args, path, duration + 30)
        return {"capture_id": capture_id, "status": "starting"}

    async def import_file(self, session_id: str, chunks: AsyncIterator[bytes], name: str) -> dict[str, Any]:
        if not shutil.which("tshark"):
            raise CaptureError("离线解析需要已安装的 tshark")
        session = self._available(session_id)
        # 上传本身也占用会话任务槽；删除/关闭可取消，避免复活已删除的目录。
        task = asyncio.create_task(self._import(session_id, session.workspace, chunks, name))
        self.jobs[session_id] = task
        return await asyncio.shield(task)

    async def import_attachment(self, session_id: str, data: bytes, name: str) -> dict[str, Any]:
        """附件按内容复用解析结果，不覆盖原附件，也不重复创建同一索引。"""
        session = self._session(session_id)
        capture_id = hashlib.sha256(data).hexdigest()[:32]
        existing = packets.get_capture(session.workspace, capture_id)
        if existing:
            return existing
        if not shutil.which("tshark"):
            raise CaptureError("离线附件预览需要已安装的 tshark")
        self._available(session_id)
        async def chunks():
            for offset in range(0, len(data), 65536):
                yield data[offset:offset + 65536]
        task = asyncio.create_task(self._import(session_id, session.workspace, chunks(), name, capture_id))
        self.jobs[session_id] = task
        return await asyncio.shield(task)

    async def _import(self, session_id: str, workspace: Path, chunks: AsyncIterator[bytes], name: str,
                      capture_id: str | None = None) -> dict[str, Any]:
        capture_id = capture_id or uuid.uuid4().hex
        folder = _new_folder(workspace, capture_id)
        path = folder / "source.pcap"
        size, header = 0, bytearray()
        try:
            with path.open("xb") as output:
                path.chmod(0o600)
                async for chunk in chunks:
                    self._session(session_id)
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise CaptureError("抓包文件超过 20 MiB 上限")
                    header.extend(chunk[:max(0, 24 - len(header))])
                    if len(header) >= 4 and bytes(header[:4]) not in _MAGIC:
                        raise CaptureError("仅支持未压缩的 PCAP / PCAPNG 文件")
                    await _io(output.write, chunk)
            if size < 24 or bytes(header[:4]) not in _MAGIC:
                raise CaptureError("抓包文件为空或文件头不完整")
            self._session(session_id)
            packets.create_capture(workspace, capture_id, "import", name=Path(name).name)
            self.active_ids[session_id] = (workspace, capture_id)
            args = [shutil.which("tshark"), "-n", "-l", "-r", str(path), "-c", str(MAX_PACKETS), "-T", "ek", "-x"]
            # 替换上传任务后不再操作工作区；后台解析纳入相同取消入口。
            self._launch(session_id, workspace, capture_id, args, path, 60)
            return {"capture_id": capture_id, "status": "starting"}
        except BaseException:
            path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                folder.rmdir()
            raise

    async def _run(self, workspace: Path, capture_id: str, args: list[str], path: Path, timeout: int) -> None:
        proc, stderr, batch = None, None, []
        count, status, error, last_flush = 0, "completed", "", 0.0
        try:
            packets.update_capture(workspace, capture_id, status="running")
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=2 * 1024 * 1024, **process_options())
            stderr = asyncio.create_task(_tail(proc.stderr))

            async def consume() -> None:
                nonlocal count, last_flush
                while line := await proc.stdout.readline():
                    row = packets.decode_packet(json.loads(line), capture_id)
                    if row is None:
                        continue
                    batch.append(row)
                    count += 1
                    if len(batch) >= 16 or time.monotonic() - last_flush > 0.25:
                        flushing = list(batch)
                        batch.clear()
                        await _io(packets.append_packets, workspace, capture_id, flushing)
                        last_flush = time.monotonic()
                await proc.wait()
            await asyncio.wait_for(consume(), timeout)
            diagnostic = (await stderr).decode(errors="replace")
            if proc.returncode:
                status, error = "failed", diagnostic[-2000:] or f"tshark 退出码 {proc.returncode}"
        except asyncio.CancelledError:
            status = "stopped"
        except TimeoutError:
            status, error = "failed", "解析或采集超时，已停止进程"
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            status, error = "failed", f"采集或解析失败：{type(exc).__name__}"
        finally:
            if proc is not None:
                if proc.returncode is None and os.name != "nt":
                    with contextlib.suppress(OSError):
                        os.killpg(proc.pid, signal.SIGINT)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), 2)
                kill_process_tree(proc)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 3)
            if stderr is not None:
                if not stderr.done():
                    stderr.cancel()
                await asyncio.gather(stderr, return_exceptions=True)
            try:
                if batch:
                    await _io(packets.append_packets, workspace, capture_id, batch)
                size = path.stat().st_size if path.exists() else 0
                packets.update_capture(workspace, capture_id, status=status, error=error,
                    file_bytes=size, limited=count >= MAX_PACKETS or size >= MAX_FILE_BYTES)
            except (OSError, ValueError, sqlite3.Error):
                pass

    async def stop(self, session_id: str, *, deleting: bool = False) -> None:
        if deleting:
            self.deleted.add(session_id)
            await self.proxies.stop(session_id)
        task = self.jobs.get(session_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # 上传收尾可能已把任务槽换成解析协程。
            if self.jobs.get(session_id) is not task:
                await self.stop(session_id)
        if session_id in self.active_ids:
            workspace, capture_id = self.active_ids[session_id]
            try:
                row = packets.get_capture(workspace, capture_id)
                if row and row["status"] in {"starting", "running"}:
                    packets.update_capture(workspace, capture_id, status="stopped")
            except (OSError, ValueError, sqlite3.Error):
                pass

    async def aclose(self) -> None:
        self.closing = True
        await self.proxies.aclose()
        await asyncio.gather(*(self.stop(sid) for sid in list(self.jobs)))
        if self.supervisor is not None:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)

    def catalog(self, session_id: str) -> list[dict[str, Any]]:
        session = self._session(session_id)
        rows = packets.captures(session.workspace)
        task = self.jobs.get(session_id)
        if task is None or task.done():
            for row in rows:
                if row["status"] in {"running", "starting"}:
                    packets.update_capture(session.workspace, row["capture_id"], status="interrupted",
                                           error="采集进程已结束或服务已重启", file_bytes=row["file_bytes"])
                    row.update(status="interrupted", error="采集进程已结束或服务已重启")
        return rows


def source_path(workspace: Path, capture_id: str) -> tuple[Path, dict[str, Any]]:
    row = packets.get_capture(workspace, capture_id)
    if row is None:
        raise CaptureError("抓包记录不存在")
    path = packets.capture_path(workspace, capture_id, "source.pcapng" if row["source"] == "live" else "source.pcap")
    if not path.is_file():
        raise CaptureError("抓包文件不可用")
    return path, row


async def display_numbers(workspace: Path, capture_id: str, expression: str) -> list[int]:
    path, _ = source_path(workspace, capture_id)
    code, data, error = await _command([shutil.which("tshark") or "tshark", "-n", "-r", str(path),
        "-c", str(MAX_PACKETS), "-Y", expression, "-T", "fields", "-e", "frame.number"], cap=100000)
    if code:
        raise CaptureError(error[-1000:] or "显示过滤器无效")
    return [int(line) for line in data.splitlines() if line.isdigit()]


async def follow_stream(workspace: Path, capture_id: str, stream: int, protocol: str) -> dict[str, Any]:
    path, row = source_path(workspace, capture_id)
    if row["status"] in {"starting", "running"}:
        raise CaptureError("请先停止抓包，再追踪重组后的流")
    code, data, error = await _command([shutil.which("tshark") or "tshark", "-n", "-r", str(path),
        "-q", "-z", f"follow,{protocol},raw,{stream}"], cap=1024 * 1024, drain_overflow=True)
    if code:
        raise CaptureError(error[-1000:] or "无法追踪该流")
    segments, nodes = [], []
    used = 0
    for line in data.decode(errors="replace").splitlines():
        if line.startswith("Node "):
            nodes.append(line[:300])
        hexdata = line.strip()
        if not hexdata or any(c not in "0123456789abcdefABCDEF" for c in hexdata) or len(hexdata) % 2:
            continue
        raw = bytes.fromhex(hexdata)
        if used + len(raw) > 131072 or len(segments) >= 500:
            return {"segments": segments, "nodes": nodes, "truncated": True, "protocol": protocol, "stream": stream}
        used += len(raw)
        segments.append({"direction": 1 if line.startswith("\t") else 0,
                         "text": raw.decode("utf-8", errors="replace"), "hex": raw.hex()})
    return {"segments": segments, "nodes": nodes, "truncated": len(data) >= 1024 * 1024,
            "protocol": protocol, "stream": stream}
