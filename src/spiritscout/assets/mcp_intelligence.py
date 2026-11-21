"""面向资产收集批次的通用情报 MCP Agent。

它只消费用户在 MCP 管理页配置的服务，不内置任何厂商 SDK 或密钥。
Agent 先读取工具目录，再按工具 schema 组装组织查询参数。
"""

from __future__ import annotations

import re
from contextlib import suppress
from typing import Any

from spiritscout.tools.mcp_client import McpError, build_client

_HOST = re.compile(r"(?<![a-z0-9-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?![a-z0-9-])", re.I)
_KEYWORDS = ("search", "query", "asset", "domain", "host", "org", "organization", "company")


class IntelligenceMcpAgent:
    def __init__(self, cfg: Any, *, timeout: float = 60.0) -> None:
        self.cfg = cfg
        self.timeout = timeout

    async def search_organization(self, organization: str) -> tuple[list[str], list[str]]:
        servers = [s for s in getattr(self.cfg, "mcp_servers", []) if s.enabled and self._is_intel_server(s)]
        domains: list[str] = []
        errors: list[str] = []
        for spec in servers:
            client = None
            try:
                client = build_client(spec)
                tools = await client.start()
                tool = self._select_tool(tools)
                if not tool:
                    continue
                result = await client.call(tool["name"], self._arguments(tool, organization), timeout=self.timeout)
                if result.get("is_error"):
                    errors.append(f"{spec.name}:mcp_error")
                for host in _HOST.findall(str(result.get("text") or "")):
                    host = host.lower().rstrip(".")
                    if host not in domains:
                        domains.append(host)
            except (McpError, OSError, ValueError, TypeError) as exc:
                errors.append(f"{getattr(spec, 'name', 'mcp')}:{type(exc).__name__}")
            finally:
                if client is not None:
                    with suppress(Exception):
                        await client.close()
        return domains, errors

    @staticmethod
    def _is_intel_server(spec: Any) -> bool:
        text = f"{spec.name} {spec.command} {spec.url}".lower()
        return any(word in text for word in ("fofa", "hunter", "quake", "zoomeye", "asset", "intel", "recon"))

    @staticmethod
    def _select_tool(tools: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = [tool for tool in tools if any(
            key in str(tool.get("name", "")).lower() for key in _KEYWORDS)]
        return sorted(candidates, key=lambda tool: ("search" not in str(tool.get("name", "")).lower(),
                                                     len(str(tool.get("name", "")))))[0] if candidates else None

    @staticmethod
    def _arguments(tool: dict[str, Any], organization: str) -> dict[str, Any]:
        schema = tool.get("inputSchema") or {}
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        arguments: dict[str, Any] = {}
        for name in properties or {}:
            lowered = str(name).lower()
            if any(token in lowered for token in ("query", "keyword", "search", "org", "company", "domain", "name")):
                arguments[name] = organization
        if not arguments and properties:
            first = next(iter(properties))
            arguments[first] = organization
        return arguments
