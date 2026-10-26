"""按需查询当前 Agent 可见的 MCP 工具；不复制全量 schema 到每次模型请求。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def search_mcp_tools(catalog: Mapping[str, dict[str, Any]], query: str = "", *,
                     offset: int = 0, limit: int = 3) -> dict[str, Any]:
    """只查询调用方传入的权限视图；分页返回完整 schema，支持后就绪的工具。"""
    words = query.casefold().split()
    found = []
    for name, entry in catalog.items():
        schema = entry.get("schema") or {}
        text = " ".join((name, str(entry.get("server", "")),
                         str(schema.get("description") or ""))).casefold()
        score = sum(word in text for word in words)
        if words and not score:
            continue
        found.append((score, name, entry))
    found.sort(key=lambda row: (-row[0], row[1]))
    offset, limit = max(0, offset), max(1, min(10, limit))
    items = []
    for _, name, entry in found[offset:offset + limit]:
        schema = entry.get("schema") or {}
        items.append({"name": name, "server": entry.get("server", ""),
                      "description": schema.get("description") or entry.get("tool", ""),
                      "inputSchema": schema.get("inputSchema") or {"type": "object"},
                      "annotations": schema.get("annotations") or {}})
    return {"tools": items, "total": len(found),
            "next_offset": offset + len(items) if offset + len(items) < len(found) else None,
            "servers": sorted({str(e.get("server", "")) for e in catalog.values()}),
            "notice": "使用 mcp_call(name, arguments) 调用；目录实时更新，未命中可换词或翻页。"}


MCP_PROXY_SCHEMAS = [
    {"type": "function", "function": {
        "name": "mcp_search", "description": "按关键词查询外部 MCP 工具的完整参数说明；浏览器、逆向等能力先搜索再调用。query 为空浏览目录，支持分页和后来就绪的工具。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "offset": {"type": "integer"},
            "limit": {"type": "integer", "description": "默认 3，最多 10"}}}}},
    {"type": "function", "function": {
        "name": "mcp_call", "description": "调用 mcp_search 查到的 MCP 工具，按 inputSchema 填写 arguments；实际操作仍经过原工具权限、审批和审计。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "arguments": {"type": "object"}},
            "required": ["name", "arguments"]}}},
]
