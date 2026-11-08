"""Linux 会话录制代理：独立 Go 进程、私有原文、可验证的调用归属。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import ssl
import uuid
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from spiritscout.platform_support import kill_process_tree
from spiritscout.tools.invocation import current_execution_scope, current_invocation
from spiritscout.traffic import recorder
from spiritscout.traffic.capture_io import wait_io

_ACTIVE: dict[str, SessionProxy] = {}
_HEX = re.compile(r"^[a-f0-9]{32}$")
_SHA = re.compile(r"^[a-f0-9]{64}$")
_SEARCH_LIMIT = 1024 * 1024


def executable() -> str | None:
    configured = os.environ.get("SPIRITSCOUT_TRAFFIC_PROXY", "")
    path = Path(configured) if configured else Path(__file__).parents[3] / ".tools" / "spiritscout-traffic-proxy"
    return str(path.resolve()) if path.is_file() and os.access(path, os.X_OK) else shutil.which("spiritscout-traffic-proxy")


def _private(root: Path) -> Path:
    if root.is_symlink():
        raise ValueError("代理证据目录不能是符号链接")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def proxy_root(workspace: Path, run: str = "") -> Path:
    if run and not _HEX.fullmatch(run):
        raise ValueError("代理运行引用无效")
    root = _private(workspace / "traffic")
    root = _private(root / "proxy")
    return _private(root / run) if run else root


def _context(workspace: Path, identifier: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
    with recorder._connect(workspace, write=value is not None) as conn:
        if value is not None:
            conn.execute("INSERT INTO proxy_contexts VALUES (?,?)", (identifier, json.dumps(value, ensure_ascii=False)))
            return value
        if conn is None:
            return {}
        row = conn.execute("SELECT data FROM proxy_contexts WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else {}


def _body_data(path: Path, headers: httpx.Headers) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(_SEARCH_LIMIT + 1)
    encoding = headers.get("content-encoding", "").lower()
    if encoding in {"gzip", "deflate"}:
        try:
            data = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS).decompress(data, _SEARCH_LIMIT + 1)
        except zlib.error:
            return b""  # 原文件仍可下载，不把压缩字节误当明文。
    elif encoding and encoding != "identity":
        return b""
    return data


def _ingest(workspace: Path, run: str, event: dict[str, Any]) -> None:
    key = str(event["key"])
    if not _HEX.fullmatch(key):
        raise ValueError("代理原文引用无效")
    root = proxy_root(workspace, run)
    blobs = _private(workspace / "traffic" / "blobs")
    bodies, search = {}, []
    for direction in ("request", "response"):
        info = event[direction]
        if info["file"] != f"{key}-{direction}.bin" or not _SHA.fullmatch(info["sha256"]):
            raise ValueError("代理正文引用无效")
        source = root / info["file"]
        if source.is_symlink():
            raise ValueError("代理正文不能是符号链接")
        destination = blobs / info["sha256"]
        if destination.is_symlink():
            raise ValueError("原文 blob 不能是符号链接")
        if source.exists():
            try:
                os.link(source, destination, follow_symlinks=False)
            except FileExistsError:
                pass
            source.unlink()
        elif info.get("error") and not destination.exists():
            with destination.open("xb") as output:
                destination.chmod(0o600)
                output.write(b"")
        info.pop("file")
        headers = httpx.Headers(event.get(direction + "_headers", []))
        data = _body_data(destination, headers)
        body = recorder._body(data, headers.get("content-type", ""))
        if not data and info["bytes"] and headers.get("content-encoding", "identity") != "identity":
            body = {"text": "[正文保持原始内容编码，暂不可预览；可下载原文]", "encoding": "encoded", "truncated": True}
        body["truncated"] = bool(body["truncated"] or info["truncated"] or len(data) > recorder.BODY_LIMIT)
        bodies[direction] = body
        # 索引使用脱敏后的可解码文本；大 JSON 预览被省略时不绕过脱敏建索引。
        search.append(body["text"] if body["encoding"] != "base64" else "")
    context = _context(workspace, str(event.get("context", "")))
    url = recorder.safe_url(event["url"])
    req_headers, res_headers = httpx.Headers(event["request_headers"]), httpx.Headers(event.get("response_headers", []))
    record = {"captured_at": event["started"], "method": event["method"], "url": url,
        "host": urlsplit(url).hostname or "", "status": event.get("status"), "error": event.get("error", ""),
        "duration_ms": event["duration_ms"], "request_bytes": event["request"]["total"],
        "response_bytes": event["response"]["total"], "agent_id": context.get("agent_id", ""),
        "task_id": context.get("task_id", ""), "invocation_id": context.get("invocation_id", ""),
        "detail": {"source": "session_proxy", "tool_call_id": context.get("tool_call_id", ""),
            "request_headers": recorder._headers(req_headers), "response_headers": recorder._headers(res_headers),
            "request_body": bodies["request"], "response_body": bodies["response"],
            "response_complete": event["response"]["complete"], "proxy_run": run,
            "original_available": True, "http_version": "", "transport": event["protocol"],
            "capture_error": event["request"].get("error", "") or event["response"].get("error", "")},
        "raw": event, "search_text": "\n".join(search)}
    recorder._append(workspace, record)


def raw_evidence(workspace: Path, record_id: int, direction: str) -> tuple[Path | None, dict[str, Any]]:
    with recorder._connect(workspace) as conn:
        exists = conn is not None and conn.execute("SELECT 1 FROM sqlite_master WHERE name='proxy_raw'").fetchone()
        row = conn.execute("SELECT * FROM proxy_raw WHERE exchange_id=?", (record_id,)).fetchone() if exists else None
    if row is None:
        raise ValueError("本会话没有该条代理原文")
    metadata = json.loads(row["metadata"])
    if direction == "metadata":
        return None, metadata
    digest = row[direction + "_blob"]
    if not _SHA.fullmatch(digest):
        raise ValueError("原文引用无效")
    root = workspace / "traffic"
    path = root / "blobs" / digest
    if any(p.is_symlink() for p in (root, root / "blobs", path)) or not path.is_file():
        raise ValueError("原文不可用")
    return path, metadata


class SessionProxy:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.run = uuid.uuid4().hex
        self.root = proxy_root(workspace, self.run)
        self.secret = secrets.token_urlsafe(32)
        self.address = ""
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self.ready.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        self.state = "starting"
        self.error = ""
        self.records = 0

    def status(self) -> dict[str, Any]:
        return {"available": bool(executable()), "status": self.state, "address": self.address,
                "records": self.records, "error": self.error, "run": self.run}

    async def environment(self) -> dict[str, str]:
        if self.state != "running" or self.proc is None or self.proc.returncode is not None:
            raise ValueError("会话代理已停止，请重新启用录制")
        scope, invocation = current_execution_scope.get(), current_invocation.get()
        identifier = uuid.uuid4().hex
        try:
            await wait_io(_context, self.workspace, identifier, {
                "invocation_id": invocation.invocation_id if invocation else "",
                "agent_id": scope.agent_id if scope else "", "task_id": scope.task_id if scope else "",
                "tool_call_id": scope.tool_call_id if scope else ""})
        except (OSError, sqlite3.Error):
            identifier = ""
            self.error = "调用归属保存失败，相关流量的发起者保持未知"
        address = self.address.replace("http://", f"http://{identifier}:{quote(self.secret, safe='')}@", 1)
        bundle = str(self.workspace / "traffic-ca" / "proxy-ca.pem")
        return {"HTTP_PROXY": address, "HTTPS_PROXY": address, "http_proxy": address, "https_proxy": address,
                "ALL_PROXY": address, "all_proxy": address, "NO_PROXY": "", "no_proxy": "",
                "SSL_CERT_FILE": bundle, "CURL_CA_BUNDLE": bundle, "REQUESTS_CA_BUNDLE": bundle,
                "NODE_EXTRA_CA_CERTS": bundle, "NODE_USE_ENV_PROXY": "1", "GIT_SSL_CAINFO": bundle}

    async def serve(self, upstream: str) -> None:
        err_task = None
        try:
            binary = executable()
            if binary is None:
                raise ValueError("未构建 Linux 录制代理，请运行 scripts/build-traffic-proxy.sh")
            env = {key: value for key, value in os.environ.items() if key in {"PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "LANG"}}
            env["HOME"] = str(self.root)
            self.proc = await asyncio.create_subprocess_exec(binary, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
                start_new_session=True, limit=4 * 1024 * 1024)
            async def discard() -> None:
                while await self.proc.stderr.read(4096):
                    pass  # 不把代理库日志中的目标信息写入普通审计。
            err_task = asyncio.create_task(discard())
            self.proc.stdin.write(json.dumps({"Root": str(self.root), "Secret": self.secret, "Upstream": upstream}).encode() + b"\n")
            await self.proc.stdin.drain()
            hello = json.loads(await asyncio.wait_for(self.proc.stdout.readline(), 15))
            if hello.get("type") != "ready" or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", hello.get("address", "")):
                raise ValueError("代理启动握手无效")
            self.address = hello["address"]
            ca_file = self.root / "ca-cert.pem"
            if ca_file.is_symlink():
                raise ValueError("代理 CA 不能是符号链接")
            ca = ca_file.read_bytes()
            public = _private(self.workspace / "traffic-ca")
            bundle = b"".join(ssl.DER_cert_to_PEM_cert(c).encode() for c in ssl.create_default_context().get_ca_certs(binary_form=True)) + ca
            if (public / "proxy-ca.pem").is_symlink():
                raise ValueError("代理证书不能是符号链接")
            temporary = public / (".ca-" + uuid.uuid4().hex)
            try:
                with temporary.open("xb") as output:
                    temporary.chmod(0o600)
                    output.write(bundle)
                temporary.replace(public / "proxy-ca.pem")
            finally:
                temporary.unlink(missing_ok=True)
            if self.state == "stopping":
                raise ValueError("代理启动已取消")
            self.state = "running"
            _ACTIVE[str(self.workspace)] = self
            self.ready.set_result(self.status())
            while line := await self.proc.stdout.readline():
                event = json.loads(line)
                if event.get("type") == "exchange":
                    try:
                        await wait_io(_ingest, self.workspace, self.run, event)
                        self.records += 1
                    except (OSError, ValueError, KeyError, sqlite3.Error):
                        self.error = "部分代理证据保存失败；目标流量未重试，原始文件保留在会话目录"
            await self.proc.wait()
            if self.state != "stopping" and self.proc.returncode:
                self.state, self.error = "failed", "代理进程异常退出"
            else:
                self.state = "stopped"
        except (OSError, ValueError, KeyError, TimeoutError, ssl.SSLError, sqlite3.Error) as exc:
            self.state, self.error = "failed", str(exc) if isinstance(exc, ValueError) else "录制代理启动或读取失败"
            if not self.ready.done():
                self.ready.set_exception(ValueError(self.error))
        finally:
            if _ACTIVE.get(str(self.workspace)) is self:
                _ACTIVE.pop(str(self.workspace), None)
            if self.proc is not None:
                self.proc.stdin.close()
                kill_process_tree(self.proc)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.proc.wait(), 3)
            if err_task:
                err_task.cancel()
                await asyncio.gather(err_task, return_exceptions=True)

    async def stop(self) -> None:
        if _ACTIVE.get(str(self.workspace)) is self:
            _ACTIVE.pop(str(self.workspace), None)
        self.state = "stopping" if self.task and not self.task.done() else "stopped"
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.send_signal(signal.SIGTERM)
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except TimeoutError:
                if self.proc:
                    kill_process_tree(self.proc)
                await self.task


async def tool_environment(workspace: Path) -> dict[str, str]:
    proxy = _ACTIVE.get(str(workspace.resolve()))
    return await proxy.environment() if proxy else {}


class ProxyManager:
    def __init__(self, manager: Any):
        self.manager = manager
        self.sessions: dict[str, SessionProxy] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.supervisor: asyncio.Task | None = None
        self.closing = False

    def start_supervisor(self) -> None:
        self.supervisor = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        while True:
            proxy, upstream = await self.queue.get()
            if proxy.state == "starting":
                proxy.task = asyncio.create_task(proxy.serve(upstream))
            self.queue.task_done()

    def status(self, session_id: str) -> dict[str, Any]:
        if session_id in self.sessions:
            return self.sessions[session_id].status()
        return {"available": bool(executable()), "status": "stopped", "address": "", "records": 0,
                "error": "" if executable() else "未构建代理：运行 scripts/build-traffic-proxy.sh"}

    async def start(self, session_id: str, upstream: str = "") -> dict[str, Any]:
        session = self.manager.get(session_id)
        if self.closing or self.supervisor is None or self.supervisor.done() or session is None:
            raise ValueError("会话不存在或代理服务未启动")
        if upstream:
            parsed = urlsplit(upstream)
            if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
                raise ValueError("上游代理需要 http://、https:// 或 socks5:// 地址")
        previous = self.sessions.get(session_id)
        if previous and previous.state in {"starting", "running", "stopping"}:
            raise ValueError("本会话代理已经启用或正在启动/停止")
        proxy = SessionProxy(session.workspace)
        self.sessions[session_id] = proxy
        self.queue.put_nowait((proxy, upstream))
        try:
            return await asyncio.wait_for(asyncio.shield(proxy.ready), 20)
        except TimeoutError as exc:
            await self.stop(session_id)
            raise ValueError("录制代理启动超时") from exc

    async def stop(self, session_id: str) -> None:
        proxy = self.sessions.get(session_id)
        if proxy:
            if proxy.task is None:
                proxy.state = "stopped"
                if not proxy.ready.done():
                    proxy.ready.set_exception(ValueError("代理启动已取消"))
            await proxy.stop()

    async def aclose(self) -> None:
        self.closing = True
        await asyncio.gather(*(self.stop(sid) for sid in list(self.sessions)))
        if self.supervisor:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)
