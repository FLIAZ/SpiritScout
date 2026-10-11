"""MCP 客户端（Model Context Protocol）：把外部安全工具服务器接入智能体工具环。

两类传输（协议规范 model-context-protocol/specification）：
- **stdio**：子进程 + 换行分隔 JSON-RPC（本地服务器主流形态，
  如 ida-pro-mcp 的 ``idalib-mcp --stdio``）；
- **http**：Streamable HTTP——POST JSON-RPC 到单一端点，应答为 JSON 或
  SSE 流（Yakit 1.4.6+ 内置 MCP Server 即此协议；ida-pro-mcp 亦支持
  ``--transport http://...``）。

接入方式完全配置驱动（configs/system.yaml 的 ``mcp_servers`` 段）：
每台服务器在会话基础设施建立时握手并拉取工具清单，工具以
``mcp__<服务器名>__<工具名>`` 前缀并入智能体工具目录。服务器不可达
不阻断开工（留痕后跳过）——MCP 是增益能力而非主链依赖。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

_MCP_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "spiritscout", "version": "0.1.0"}
_HANDSHAKE_TIMEOUT = 30.0


class McpError(RuntimeError):
    """MCP 调用失败（握手/请求/协议不符）。"""


class McpClient:
    """单台 MCP 服务器的客户端基类：initialize → tools/list → tools/call。"""

    def __init__(self, name: str, *, timeout: float = 180.0):
        self.name = name
        self.timeout = timeout
        self.session_id: str = ""          # http 传输的会话头
        self._req_id = 0
        self.tools: list[dict[str, Any]] = []

    # ---- 传输接口（子类实现）

    async def _send(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    async def start(self) -> list[dict[str, Any]]:
        """握手 + 拉取工具清单，返回服务器暴露的工具定义列表。"""
        init = await self._rpc("initialize", {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {}, "clientInfo": _CLIENT_INFO,
        }, timeout=_HANDSHAKE_TIMEOUT)
        if not init:
            raise McpError(f"MCP[{self.name}] 握手无应答")
        await self._notify("notifications/initialized", {})
        listed = await self._rpc("tools/list", {}, timeout=_HANDSHAKE_TIMEOUT)
        tools = (listed or {}).get("tools") or []
        self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        return self.tools

    async def call(self, tool: str, arguments: dict[str, Any] | None = None,
                   *, timeout: float | None = None) -> dict[str, Any]:
        """调用远程工具，归一为 {"text": str, "is_error": bool}。"""
        result = await self._rpc("tools/call", {
            "name": tool, "arguments": arguments or {},
        }, timeout=timeout or self.timeout)
        if result is None:
            raise McpError(f"MCP[{self.name}].{tool} 无应答")
        if result.get("isError"):
            texts = _content_texts(result)
            return {"text": "\n".join(texts)[:8000] or "（工具报告错误）",
                    "is_error": True}
        return {"text": "\n".join(_content_texts(result))[:16000],
                "is_error": False}

    async def close(self) -> None:  # pragma: no cover - 子类按需覆盖
        return

    # ---- JSON-RPC 框架

    async def _rpc(self, method: str, params: dict[str, Any],
                   *, timeout: float) -> dict[str, Any] | None:
        self._req_id += 1
        payload = {"jsonrpc": "2.0", "id": self._req_id,
                   "method": method, "params": params}
        resp = await self._send(payload)
        if resp is None or resp.get("id") != self._req_id:
            return None
        if "error" in resp:
            err = resp["error"]
            raise McpError(f"MCP[{self.name}] {method} 错误: "
                           f"{err.get('message', err)}"[:300])
        return resp.get("result")

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """通知无 id、无应答——stdio 下只写不读（等应答会永远超时）。"""
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._send_no_wait(payload)

    async def _send_no_wait(self, payload: dict[str, Any]) -> None:
        await self._send(payload)


def _content_texts(result: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            out.append(str(item.get("text", "")))
    return out or ([str(result.get("result"))]
                   if result.get("result") is not None else [])


class McpStdioClient(McpClient):
    """stdio 传输：子进程 + 换行分隔 JSON-RPC。"""

    def __init__(self, name: str, *, command: str, args: list[str],
                 env: dict[str, str] | None = None, timeout: float = 180.0):
        super().__init__(name, timeout=timeout)
        self._command = command
        self._args = list(args)
        self._env = dict(env or {})
        self._proc: asyncio.subprocess.Process | None = None

    async def _send(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self._proc is None or self._proc.stdin is None:
            await self._ensure_proc()
        assert self._proc.stdin is not None and self._proc.stdout is not None
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        try:
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=10)
        except TimeoutError as exc:
            raise McpError(f"MCP[{self.name}] stdin 写入超时") from exc
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"MCP[{self.name}] 应答超时")
            try:
                raw = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=remaining)
            except TimeoutError as exc:
                raise McpError(f"MCP[{self.name}] 应答超时") from exc
            if not raw:
                raise McpError(f"MCP[{self.name}] 进程已退出")
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue                        # 非帧内容（横幅等）跳过
            if msg.get("id") != payload.get("id") and payload.get("id") is not None:
                continue                        # 服务器主动通知，等真正应答
            return msg if payload.get("id") is not None else None

    async def _send_no_wait(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            await self._ensure_proc()
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        try:
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=10)
        except TimeoutError as exc:
            raise McpError(f"MCP[{self.name}] stdin 写入超时") from exc

    async def _ensure_proc(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._command, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                pass


class McpHttpClient(McpClient):
    """Streamable HTTP 传输：POST 到单一端点，应答 JSON 或 SSE 流。"""

    def __init__(self, name: str, *, url: str, headers: dict[str, str] | None = None,
                 timeout: float = 180.0):
        super().__init__(name, timeout=timeout)
        self._url = url
        self._headers = dict(headers or {})
        self._client: httpx.AsyncClient | None = None

    async def _post(self, payload: dict[str, Any],
                    timeout: float) -> dict[str, Any] | None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=timeout)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        resp = await self._client.post(self._url, json=payload,
                                       headers=headers, timeout=timeout)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if resp.status_code == 202:
            return None                            # 已受理的通知
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            data = _sse_data(resp.text)            # 已整流到达的 SSE 文本
            return json.loads(data) if data else None
        if not resp.text.strip():
            return None
        return json.loads(resp.text)

    async def _send(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return await self._post(payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise McpError(f"MCP[{self.name}] HTTP 错误: {exc}"[:300]) from exc

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _sse_data(raw: str) -> str:
    """取 SSE 文本中最后一条 data 帧（JSON-RPC 应答在单帧内）。"""
    data: str = ""
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                data += line[5:].strip()
    return data


def build_client(spec: Any) -> McpClient:
    """按配置构造客户端（spec: config.settings.McpServerConfig）。"""
    transport = str(spec.transport or "stdio").lower()
    timeout = float(spec.timeout or 180.0)
    if transport == "http":
        if not spec.url:
            raise McpError(f"MCP[{spec.name}] http 传输缺少 url")
        return McpHttpClient(spec.name, url=spec.url,
                             headers=dict(spec.headers or {}), timeout=timeout)
    if not spec.command:
        raise McpError(f"MCP[{spec.name}] stdio 传输缺少 command")
    return McpStdioClient(spec.name, command=spec.command,
                          args=list(spec.args or []),
                          env=dict(spec.env or {}), timeout=timeout)
