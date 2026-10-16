"""HTTP 连接固定到已检查的地址，域名仍用于 Host、TLS SNI 和证书验证。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable

import httpx

from spiritscout.policy.scope import ScopeGuard


class ScopedHTTPTransport(httpx.AsyncBaseTransport):
    """每跳解析一次，按逻辑 origin 隔离连接池，禁止环境代理绕过连接门。"""

    def __init__(self, guard: ScopeGuard,
                 transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None):
        self.guard = guard
        self._factory = transport_factory or (lambda: httpx.AsyncHTTPTransport(trust_env=False))
        self._transports: dict[tuple[str, str, int, str], httpx.AsyncBaseTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.guard.ensure_not_denied(str(request.url))
        host = request.url.raw_host.decode("ascii")
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            timeout = request.extensions.get("timeout", {}).get("connect", 5.0)
            try:
                infos = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(
                        host, port, type=socket.SOCK_STREAM), timeout=timeout)
            except (OSError, TimeoutError) as exc:
                raise httpx.ConnectError(f"目标解析失败: {host}", request=request) from exc
            addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
        if not addresses:
            raise httpx.ConnectError(f"目标没有连接地址: {host}", request=request)
        # 多地址全部检查；实际连接仅使用本次检查后的 IP，不再解析域名。
        self.guard.ensure_addrs_not_denied(addresses)
        for index, address in enumerate(addresses):
            key = (request.url.scheme, host, port, address)
            transport = self._transports.get(key)
            if transport is None:
                transport = self._factory()
                self._transports[key] = transport
            pinned = httpx.Request(
                request.method, request.url.copy_with(host=address),
                headers=request.headers, stream=request.stream,
                extensions={**request.extensions, "sni_hostname": host},
            )
            try:
                return await transport.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # 仅建连失败可换地址；写出请求后的错误不能在此重放。
                if index == len(addresses) - 1:
                    raise
        raise AssertionError("连接地址为空")

    async def aclose(self) -> None:
        await asyncio.gather(*(t.aclose() for t in self._transports.values()),
                             return_exceptions=True)
