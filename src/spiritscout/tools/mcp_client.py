"""MCP 客户端（官方 ``mcp`` SDK）：把外部安全工具服务器接入智能体工具环。

三类传输（协议规范 transports）：
- **stdio**：子进程 + 标准输入输出 JSON-RPC（本机 CLI 型服务器，
  如 ``idalib-mcp --stdio``、``yak mcp``）；
- **http**：Streamable HTTP——POST 单端点，应答为 JSON 或 SSE 流
  （Yakit 引擎 ``--transport streamable_http``，端点为 ``/mcp``）；
- **sse**：旧版 SSE 传输——GET 长连接 + POST 消息端点（Burp 官方扩展等）。

接入方式完全配置驱动（configs/system.yaml 的 ``mcp_servers`` 段）：
每台服务器在会话基础设施建立时握手并拉取工具清单，工具以
``mcp__<服务器名>__<工具名>`` 前缀并入智能体工具目录。服务器不可达
不阻断开工（留痕后跳过）——MCP 是增益能力而非主链依赖。

生命周期：官方 SDK 的会话是异步上下文管理器（进入=连接+协商，
退出=断开），而灵枢的 MCP 连接随一次会话存续、跨多次调用——因此每台
服务器由一个常驻任务持有会话，调用请求经队列投递、结果回填 Future；
任务退出（服务器断开）时唤醒全部等待者并拒绝后续调用。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Implementation

from spiritscout.config.settings import McpServerConfig
from spiritscout.tools.spec import RiskLevel

_HANDSHAKE_TIMEOUT = 60.0        # 握手：子进程冷启动 + initialize + tools/list
_SESSION_READ_TIMEOUT = 3600.0   # 会话级默认读超时（单次调用超时另行覆盖）
_CLIENT_INFO = Implementation(name="spiritscout", version="0.1.0")


def resolve_mcp_risk(server: McpServerConfig, tool: dict[str, Any]) -> RiskLevel:
    """区分 MCP 的环境变更提示与宿主明令禁止的破坏性工具。

    destructiveHint 不等于 DoS。可能修改状态的工具解析为 INTRUSIVE——
    注册层据此自动配发行为声明字段（调用时声明本次语义，仅危险语义逐次
    请示），能力级别本身不再整体审批；只有宿主可信配置明确指定
    destructive 才硬拒绝，不能由远端提示猜测。
    普通浏览器导航等特例由精确工具配置声明，不放行整个服务器。
    """
    override = server.tool_risk_overrides.get(str(tool.get("name", "")))
    if override is not None:
        return RiskLevel(override)
    if server.default_risk == RiskLevel.DESTRUCTIVE.value:
        return RiskLevel.DESTRUCTIVE
    if server.risk_by_action:
        return RiskLevel.READONLY
    annotations = tool.get("annotations") or {}
    readonly = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    destructive = annotations.get("destructiveHint", annotations.get("destructive_hint"))
    # MCP 规范中 readOnlyHint=true 时 destructiveHint 没有意义。
    if readonly is True:
        return RiskLevel.READONLY
    if readonly is False or destructive is True:
        return RiskLevel.INTRUSIVE
    return RiskLevel(server.default_risk)


class McpError(RuntimeError):
    """MCP 调用失败（握手/请求/协议不符）。"""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_command(command: str) -> str:
    """项目内相对路径命令（如 ``.venv/bin/idalib-mcp``、``.tools/yak/yak``）
    解析为项目根下绝对路径；其余原样交给 PATH（npx、docker 等）。"""
    if not command or Path(command).is_absolute():
        return command
    candidate = _project_root() / command
    return str(candidate) if candidate.exists() else command


def _content_texts(result: Any) -> list[str]:
    texts = [str(getattr(block, "text", "")) for block in (result.content or [])
             if getattr(block, "type", "") == "text"]
    return texts or ["（无文本内容）"]


class McpClient:
    """单台 MCP 服务器的常驻客户端：start → call → close。"""

    def __init__(self, name: str, *, timeout: float = 180.0):
        self.name = name
        self.timeout = timeout
        self.tools: list[dict[str, Any]] = []
        self._requests: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._fatal: BaseException | None = None

    # ---- 传输（子类实现）：返回官方 SDK 的异步上下文管理器

    def _connect(self) -> Any:
        raise NotImplementedError

    # ---- 对外接口

    async def start(self) -> list[dict[str, Any]]:
        """连接 + 握手 + 拉取工具清单；失败抛 McpError（调用方留痕跳过）。"""
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._task = loop.create_task(self._serve(), name=f"mcp[{self.name}]")
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), _HANDSHAKE_TIMEOUT)
        except TimeoutError as exc:
            await self.close()
            raise McpError(
                f"MCP[{self.name}] 握手超时（{_HANDSHAKE_TIMEOUT:.0f}s）") from exc
        if self._fatal is not None:
            raise McpError(f"MCP[{self.name}] 连接失败: {str(self._fatal)[:300]}")
        return self.tools

    async def call(self, tool: str, arguments: dict[str, Any] | None = None,
                   *, timeout: float | None = None) -> dict[str, Any]:
        """调用远程工具，归一为 {"text": str, "is_error": bool}。"""
        if self._task is None or self._task.done():
            raise McpError(f"MCP[{self.name}] 会话未建立或已断开")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        await self._requests.put(
            (future, tool, dict(arguments or {}), float(timeout or self.timeout)))
        return await future

    async def close(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            await self._requests.put(None)
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 10)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    # ---- 常驻会话任务

    async def _serve(self) -> None:
        try:
            async with self._connect() as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(
                        read, write,
                        read_timeout_seconds=timedelta(
                            seconds=_SESSION_READ_TIMEOUT),
                        client_info=_CLIENT_INFO) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.tools = [
                        {"name": t.name,
                         "description": t.description or "",
                         "inputSchema": getattr(t, "inputSchema", None) or {},
                         "annotations": (
                             t.annotations.model_dump(mode="json", exclude_none=True)
                             if getattr(t, "annotations", None) is not None else {})}
                        for t in listed.tools
                    ]
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(None)
                    await self._pump(session)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 —— 连接/会话故障：唤醒等待者
            self._fatal = exc
        finally:
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(None)   # start() 检查 _fatal
            while not self._requests.empty():
                item = self._requests.get_nowait()
                if item is None or item[0].done():
                    continue
                item[0].set_exception(
                    McpError(f"MCP[{self.name}] 会话已断开"))

    async def _pump(self, session: ClientSession) -> None:
        while True:
            item = await self._requests.get()
            if item is None:
                return
            future, tool, arguments, timeout = item
            if future.done():
                continue
            try:
                result = await session.call_tool(
                    tool, arguments,
                    read_timeout_seconds=timedelta(seconds=timeout))
            except Exception as exc:  # noqa: BLE001 —— 单次调用失败不拆会话
                # 等待方可能在调用途中取消（future 已 done）——回填前须确认，
                # 否则 InvalidStateError 会把整条常驻会话拆掉
                if not future.done():
                    future.set_exception(McpError(
                        f"MCP[{self.name}].{tool} 调用失败: {str(exc)[:300]}"))
                continue
            if not future.done():
                future.set_result({
                    "text": "\n".join(_content_texts(result))[:16000],
                    "is_error": bool(result.isError),
                })


class McpStdioClient(McpClient):
    """stdio 传输：服务器作为子进程，走标准输入输出 JSON-RPC。"""

    def __init__(self, name: str, *, command: str, args: list[str],
                 env: dict[str, str] | None = None, timeout: float = 180.0):
        super().__init__(name, timeout=timeout)
        self._params = StdioServerParameters(
            command=_resolve_command(command), args=list(args),
            env=dict(env) if env else None)

    def _connect(self) -> Any:
        return stdio_client(self._params)


class McpHttpClient(McpClient):
    """Streamable HTTP 传输：POST 到单一端点，应答 JSON 或 SSE 流。"""

    def __init__(self, name: str, *, url: str,
                 headers: dict[str, str] | None = None, timeout: float = 180.0):
        super().__init__(name, timeout=timeout)
        self._url = url
        self._headers = dict(headers or {})

    def _connect(self) -> Any:
        return streamablehttp_client(
            self._url, headers=self._headers or None,
            timeout=max(self.timeout, 30.0),
            sse_read_timeout=max(self.timeout, 300.0))


class McpSseClient(McpClient):
    """旧版 SSE 传输：GET 长连接收事件，POST 到消息端点发请求。"""

    def __init__(self, name: str, *, url: str,
                 headers: dict[str, str] | None = None, timeout: float = 180.0):
        super().__init__(name, timeout=timeout)
        self._url = url
        self._headers = dict(headers or {})

    def _connect(self) -> Any:
        return sse_client(
            self._url, headers=self._headers or None,
            timeout=max(self.timeout, 30.0),
            sse_read_timeout=max(self.timeout, 300.0))


def build_client(spec: Any) -> McpClient:
    """按配置构造客户端（spec: config.settings.McpServerConfig）。"""
    transport = str(spec.transport or "stdio").lower()
    timeout = float(spec.timeout or 180.0)
    if transport in ("http", "streamable_http", "streamable-http"):
        if not spec.url:
            raise McpError(f"MCP[{spec.name}] http 传输缺少 url")
        return McpHttpClient(spec.name, url=spec.url,
                             headers=dict(spec.headers or {}), timeout=timeout)
    if transport in ("sse", "sse-legacy"):
        if not spec.url:
            raise McpError(f"MCP[{spec.name}] sse 传输缺少 url")
        return McpSseClient(spec.name, url=spec.url,
                            headers=dict(spec.headers or {}), timeout=timeout)
    if not spec.command:
        raise McpError(f"MCP[{spec.name}] stdio 传输缺少 command")
    return McpStdioClient(spec.name, command=spec.command,
                          args=list(spec.args or []),
                          env=dict(spec.env or {}), timeout=timeout)
