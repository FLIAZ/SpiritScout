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
from urllib.parse import urljoin, urlparse

import httpx

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
    max_redirects: int = 5,
    max_output_chars: int = 20000,
    **_: Any,  # 兼容 builtin_ctx 中面向其他内置工具的键（如 allowed_dirs）
) -> ToolResult:
    """HTTP 探测工具：限速 → 请求（重定向逐跳跟随）→ 结构化解析。

    授权白名单不拦截：范围外目标照常请求，最终响应带 scope_note 标注，
    由模型决定是否向用户确认扩围；禁打列表在工具门（registry）拦截。
    """
    started = time.perf_counter()

    req_headers = {"User-Agent": _UA, **(headers or {})}
    try:
        current_url = url
        current_method = method.upper()
        current_data = data
        current_params = params
        redirect_chain: list[str] = []
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            for hop in range(max(0, max_redirects) + 1):
                if limiter:
                    await limiter.acquire()
                resp = await client.request(
                    current_method, current_url, params=current_params,
                    content=current_data, headers=req_headers)
                location = resp.headers.get("location")
                if resp.status_code not in {301, 302, 303, 307, 308} or not location:
                    break
                if hop >= max_redirects:
                    return ToolResult(
                        tool="http_probe", ok=False, returncode=-2,
                        error=f"重定向次数超过上限: {max_redirects}",
                        duration_ms=(time.perf_counter() - started) * 1000)
                next_url = urljoin(str(resp.url), location)
                redirect_chain.append(next_url)
                old = urlparse(current_url)
                new = urlparse(next_url)
                if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
                    # 跨 origin 时不转发调用方显式提供的认证信息。
                    req_headers = {k: v for k, v in req_headers.items()
                                   if k.lower() not in {"authorization", "proxy-authorization", "cookie"}}
                if resp.status_code == 303 or (
                        resp.status_code in {301, 302} and current_method == "POST"):
                    current_method, current_data = "GET", None
                current_params = None
                current_url = next_url
            else:  # pragma: no cover - 循环由 break/上限分支结束
                raise RuntimeError("redirect loop")
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
        "redirect_chain": redirect_chain,
    }
    # 范围标注（不拦截）：最终落点在授权白名单之外时告知模型——是否继续
    # 测试该目标由它向用户提问确认（ask_user），用户放行后标注消失
    if not guard.is_allowed(str(resp.url)):
        parsed["scope_note"] = (
            "最终响应来自当前授权范围之外（未拦截）。继续测试该目标前先用 "
            "ask_user 向用户确认，用户同意后系统会将其加入本会话范围。")
    return ToolResult(
        tool="http_probe", ok=True, returncode=0,
        stdout=body, parsed=parsed,
        duration_ms=(time.perf_counter() - started) * 1000,
    )


async def dns_resolve(
    host: str, *, guard: ScopeGuard, limiter: RateLimiter | None = None,
    timeout: float = 5.0, **_: Any,
) -> ToolResult:
    """DNS 解析（被动信息收集）。禁打列表在工具门拦截；白名单不拦。"""
    started = time.perf_counter()
    guard.ensure_not_denied(host)
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


# 常用 TCP 服务端口（测绘 MVP 集；自定义清单由路线步骤传入）
COMMON_TCP_PORTS = (21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 443, 445, 873,
                    1080, 1433, 1521, 2049, 2181, 2375, 2376, 3000, 3306, 3389,
                    3690, 5000, 5432, 5601, 5900, 6379, 7001, 7077, 8000, 8080,
                    8081, 8443, 8888, 9000, 9090, 9200, 9300, 11211, 27017, 6443)


async def tcp_scan(
    host: str,
    *,
    ports: list[int] | None = None,
    guard: ScopeGuard,
    limiter: RateLimiter | None = None,
    timeout: float = 1.2,
    max_ports: int = 256,
    **_: Any,
) -> ToolResult:
    """TCP 连接级端口扫描：授权主机上的存活端口与服务横幅（听风）。

    禁打列表在工具门（registry）拦截；连接为只读探测，并发受信号量约束、
    频率受全局 RPS 约束。开放端口抓取首包横幅（非 HTTP 服务给协议指纹），
    白名单外主机照常可达、结果带范围标注（扩围由用户决定）。
    """
    started = time.perf_counter()
    host = str(host or "").strip()
    if not host:
        return ToolResult(tool="tcp_scan", ok=False, returncode=-1, error="host 必填")
    guard.ensure_not_denied(host)
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, family=socket.AF_INET), timeout=5.0)
        ip = min({info[4][0] for info in infos})
    except (TimeoutError, socket.gaierror, OSError) as exc:
        return ToolResult(tool="tcp_scan", ok=False, returncode=-2,
                          error=f"主机解析失败: {exc}",
                          duration_ms=(time.perf_counter() - started) * 1000)
    raw = list(ports) if ports else list(COMMON_TCP_PORTS)
    port_list: list[int] = []
    for p in raw[:max_ports]:
        try:
            port_list.append(int(p))
        except (TypeError, ValueError):
            continue
    port_list = sorted({p for p in port_list if 1 <= p <= 65535})[:max_ports]
    sem = asyncio.Semaphore(64)

    async def probe(port: int) -> tuple[int, str] | None:
        async with sem:
            if limiter:
                await limiter.acquire()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), timeout=timeout)
            except (OSError, TimeoutError):
                return None
            banner = ""
            try:
                banner = (await asyncio.wait_for(reader.read(160), 0.6)) \
                    .decode("utf-8", errors="replace").strip()[:160]
            except (OSError, TimeoutError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            return port, banner

    results = await asyncio.gather(*(probe(p) for p in port_list))
    open_ports = [{"port": p, "banner": b} for r in results if r for (p, b) in [r]]
    parsed = {
        "host": host, "ip": ip, "scanned": len(port_list),
        "open": open_ports,
        "scope_note": ("" if guard.is_allowed(host) else
                       "主机在当前授权白名单之外（未拦截）。继续测试前先用 "
                       "ask_user 向用户确认，用户同意后系统会将其加入本会话范围。"),
    }
    return ToolResult(
        tool="tcp_scan", ok=True, returncode=0,
        stdout=f"开放 {len(open_ports)}/{len(port_list)} 端口",
        parsed=parsed,
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
        description="HTTP 探测（任意 HTTP 方法，授权门 + 限速 + 结构化解析）",
        target_params=("url",), scope_required=True),
    "dns_resolve": ToolSpec(
        name="dns_resolve", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
        description="DNS 解析（A/AAAA/PTR，授权门 + 限速）",
        target_params=("host",), scope_required=True),
    # 端口扫描：SRC/渗透赛测绘面（CTF 模式 C7 不装配该角色，模式层排除）
    "tcp_scan": ToolSpec(
        name="tcp_scan", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
        description="TCP 端口扫描（连接级探测 + 服务横幅，禁打门 + 限速 + 并发上限）",
        target_params=("host",), scope_required=True),
    "file_analyze": ToolSpec(
        name="file_analyze", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
        description="本地文件分析（魔数/字符串/熵，仅限 allowed_dirs）",
        target_params=()),
    # 沙箱执行：风险定级按「对目标的行为」判——它本身不侵入目标，脚本内容
    # 做什么由沙箱边界（整机只读 + 主目录遮蔽 + 工作区可写）与审计约束；
    # 工具只暴露给 CTF 方向 Agent 的目录，常规渗透模式不下发。
    "sandbox_exec": ToolSpec(
        name="sandbox_exec", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
        description="沙箱执行（bwrap：整机只读 + 主目录遮蔽 + 工作区可写）",
        target_params=()),
}
