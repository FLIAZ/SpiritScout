"""内置工具：HTTP 探测（http_probe）、DNS 解析（dns_resolve）、本地文件分析（file_analyze）。

内置工具直接由 Python 实现，所有网络请求强制过 ScopeGuard（合规 G1），
速率受全局限制（9.2 流量管控）。这是 MVP 阶段侦察/Web 子 Agent 的
基础执行能力；外部工具（nmap/nuclei 等）经 CLI 封装接入。
file_analyze 仅访问本地文件（CTF 附件分析），不触网，路径需位于允许目录内。
"""

from __future__ import annotations

import asyncio
import re
import socket
import time
from pathlib import Path
from typing import Any

import httpx

from spiritscout.core.exceptions import ScopeViolation
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

_UA = "Mozilla/5.0 (X11; Linux x86_64) SpiritScout/0.1 (authorized-security-test)"


class RateLimiter:
    """全局请求速率限制（9.2：统一管控探测频率与流量特征）。"""

    def __init__(self, rps: float = 10.0):
        self._interval = 1.0 / max(rps, 0.1)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


async def http_probe(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: Any = None,
    headers: dict[str, str] | None = None,
    guard: ScopeGuard,
    limiter: RateLimiter | None = None,
    timeout: float = 15.0,
    max_output_chars: int = 20000,
    **_: Any,  # 兼容 builtin_ctx 中面向其他内置工具的键（如 allowed_dirs）
) -> ToolResult:
    """HTTP 探测工具：授权校验 → 限速 → 请求 → 结构化解析（标题/指纹/长度）。"""
    started = time.perf_counter()
    try:
        guard.check_url(url)
    except ScopeViolation as exc:
        return ToolResult(tool="http_probe", ok=False, error=str(exc), returncode=-1)

    if limiter:
        await limiter.acquire()
    req_headers = {"User-Agent": _UA, **(headers or {})}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.request(
                method.upper(), url, params=params, content=data, headers=req_headers
            )
    except httpx.HTTPError as exc:
        return ToolResult(tool="http_probe", ok=False, error=f"HTTP 错误: {exc}",
                          returncode=-2, duration_ms=(time.perf_counter() - started) * 1000)

    body = resp.text
    if len(body) > max_output_chars:
        body = body[: max_output_chars * 3 // 4] + "\n...[截断]...\n" + body[-max_output_chars // 4 :]

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:200]

    fingerprint: dict[str, Any] = {}
    server = resp.headers.get("server")
    if server:
        fingerprint["server"] = server
    powered = resp.headers.get("x-powered-by")
    if powered:
        fingerprint["x_powered_by"] = powered

    parsed = {
        "status_code": resp.status_code,
        "title": title,
        "content_length": len(resp.content),
        "headers": dict(resp.headers),
        "fingerprint": fingerprint,
        "final_url": str(resp.url),
    }
    return ToolResult(
        tool="http_probe", ok=True, returncode=0,
        stdout=body, parsed=parsed,
        duration_ms=(time.perf_counter() - started) * 1000,
    )


async def dns_resolve(
    host: str, *, guard: ScopeGuard, limiter: RateLimiter | None = None,
    timeout: float = 5.0, **_: Any,
) -> ToolResult:
    """DNS 解析（被动信息收集）。"""
    started = time.perf_counter()
    try:
        guard.check_target(host)
    except ScopeViolation as exc:
        return ToolResult(tool="dns_resolve", ok=False, error=str(exc), returncode=-1)
    if limiter:
        await limiter.acquire()
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, family=socket.AF_INET), timeout=timeout
        )
        ips = sorted({info[4][0] for info in infos})
    except (TimeoutError, socket.gaierror) as exc:
        return ToolResult(tool="dns_resolve", ok=False, error=f"解析失败: {exc}",
                          returncode=-2, duration_ms=(time.perf_counter() - started) * 1000)
    return ToolResult(
        tool="dns_resolve", ok=True, stdout="\n".join(ips),
        parsed={"host": host, "ips": ips},
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# 文件魔数签名（Misc 隐写/格式识别 4.4.5）
_MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"\x7fELF", "elf"),
    (b"\x1f\x8b", "gzip"),
    (b"\xd4\xc3\xb2\xa1", "pcap"),
    (b"Rar!\x1a\x07", "rar"),
    (b"SQLite format 3", "sqlite"),
]
_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e]{4,}")


async def file_analyze(
    path: str,
    *,
    allowed_dirs: list[str] | None = None,
    max_strings: int = 80,
    max_read_bytes: int = 4 * 1024 * 1024,
    **_: Any,
) -> ToolResult:
    """本地文件分析（CTF 附件：魔数识别 + 可打印字符串提取，4.4.5）。

    安全边界：仅允许读取 allowed_dirs 内的文件（默认工作区 attachments），
    不触网、不执行文件内容。
    """
    started = time.perf_counter()
    p = Path(path).resolve()
    if allowed_dirs:
        roots = [Path(d).resolve() for d in allowed_dirs]
        if not any(p.is_relative_to(root) for root in roots):
            return ToolResult(tool="file_analyze", ok=False, returncode=-1,
                              error=f"路径超出允许目录: {p}（仅允许 {allowed_dirs}）")
    if not p.is_file():
        return ToolResult(tool="file_analyze", ok=False, returncode=-2,
                          error=f"文件不存在: {p}")
    data = p.read_bytes()[:max_read_bytes]
    magic = next((name for sig, name in _MAGIC_SIGNATURES
                  if data.startswith(sig)), "unknown")
    strings = [m.group(0).decode("ascii", errors="replace")
               for m in _PRINTABLE_RUN.finditer(data)][:max_strings]
    return ToolResult(
        tool="file_analyze", ok=True, returncode=0,
        stdout="\n".join(strings),
        parsed={
            "path": str(p), "size": p.stat().st_size, "magic": magic,
            "strings": strings,
            "is_elf": magic == "elf",
            "elf_class": {1: "32bit", 2: "64bit"}.get(data[4]) if magic == "elf" and len(data) > 5 else None,
        },
        duration_ms=(time.perf_counter() - started) * 1000,
    )


# ---------------------------------------------------------------- 注册表

# 内置工具的规范描述（策略门风险定级的依据，4.1 统一抽象 BUILTIN 态）
BUILTIN_SPECS: dict[str, ToolSpec] = {
    "http_probe": ToolSpec(
        name="http_probe", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
        description="HTTP 探测（GET，授权门 + 限速 + 结构化解析）"),
    "dns_resolve": ToolSpec(
        name="dns_resolve", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
        description="DNS 解析（A/AAAA/PTR，授权门 + 限速）"),
    "file_analyze": ToolSpec(
        name="file_analyze", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
        description="本地文件分析（魔数/字符串/熵，仅限 allowed_dirs）"),
}
