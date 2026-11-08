"""按会话复用已采集证据；三个工具均为本地只读，不重新请求目标。"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec
from spiritscout.traffic import recorder
from spiritscout.traffic.proxy import raw_evidence

NAMES = frozenset({"traffic_search", "traffic_get", "traffic_body_read"})


def _read(name: str, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
    if not workspace:
        raise ValueError("缺少当前会话工作区")
    root = Path(workspace)
    if name == "traffic_search":
        limit = args.get("limit", 3)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit 需要为 1–10")
        before = args.get("before")
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("before 需要为正整数")
        filters = {key: str(args.get(key, "")) for key in ("query", "body", "method", "status")}
        if any(len(v) > 200 for v in filters.values()):
            raise ValueError("筛选项最多 200 字符")
        return recorder.page(root, limit=limit, before=before, **filters)
    record_id = args.get("record_id")
    if type(record_id) is not int or record_id < 1:
        raise ValueError("record_id 需要为当前会话的正整数编号")
    if name == "traffic_get":
        data = recorder.detail(root, record_id)
        if data is None:
            raise ValueError("当前会话没有该流量记录")
        for key in ("request_headers", "response_headers"):
            data[key] = [[k, v[:256]] for k, v in data.get(key, [])[:20]]
        for key in ("request_body", "response_body"):
            body = data.get(key, {})
            body["truncated"] = body.get("truncated", False) or len(body.get("text", "")) > 3500
            body["text"] = body.get("text", "")[:3500]
        return data
    direction, offset, limit = args.get("direction", "response"), args.get("offset", 0), args.get("limit", 4000)
    if direction not in {"request", "response"} or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
        raise ValueError("direction 为 request/response，offset ≥ 0，limit 为 1–8000 字节")
    path, metadata = raw_evidence(root, record_id, direction)
    size = path.stat().st_size
    if offset > size:
        raise ValueError("offset 超过已保存原文大小")
    with path.open("rb") as source:
        source.seek(offset)
        data = source.read(limit)
    try:
        text, encoding = data.decode("utf-8"), "utf-8"
        if "\x00" in text:
            raise ValueError("binary")
    except (UnicodeDecodeError, ValueError):
        text, encoding = base64.b64encode(data).decode("ascii"), "base64"
    return {"record_id": record_id, "direction": direction, "offset": offset, "next_offset": offset + len(data),
            "has_more": offset + len(data) < size, "saved_bytes": size, "encoding": encoding, "data": text,
            "body_info": metadata[direction],
            "content_encoding": next((v for k, v in metadata.get(direction + "_headers", []) if k.lower() == "content-encoding"), "identity"),
            "note": "此为未脱敏原文，保留内容编码；偏移按字节，保存完整性以 body_info 为准。"}


async def _run(name: str, workspace_root: str = "", **args: Any) -> ToolResult:
    import sqlite3

    try:
        data = await asyncio.to_thread(_read, name, workspace_root, args)
        return ToolResult(tool=name, ok=True, stdout=json.dumps(data, ensure_ascii=False))
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        return ToolResult(tool=name, ok=False, returncode=1, error=str(exc), parsed={"retryable": False})


async def traffic_search(*, workspace_root: str = "", **args: Any) -> ToolResult:
    return await _run("traffic_search", workspace_root, **args)


async def traffic_get(*, workspace_root: str = "", **args: Any) -> ToolResult:
    return await _run("traffic_get", workspace_root, **args)


async def traffic_body_read(*, workspace_root: str = "", **args: Any) -> ToolResult:
    return await _run("traffic_body_read", workspace_root, **args)


FUNCTIONS = {fn.__name__: fn for fn in (traffic_search, traffic_get, traffic_body_read)}
_PARAMS = {"traffic_search": {"query", "body", "method", "status", "before", "limit"},
           "traffic_get": {"record_id"}, "traffic_body_read": {"record_id", "direction", "offset", "limit"}}
SPECS = {name: ToolSpec(name=name, kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
    allowed_params=_PARAMS[name], description="只读检索/读取当前会话已采集流量，不发送目标网络请求") for name in NAMES}


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}


SCHEMAS = [
    _schema("traffic_search", "检索当前会话已采集流量，返回轻量索引供按编号读取；本地只读，不重发目标请求。正文检索范围为已保存的脱敏预览。", {
        "query": {"type": "string"}, "body": {"type": "string"}, "method": {"type": "string"},
        "status": {"type": "string", "description": "如 200、4xx"}, "before": {"type": "integer"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, []),
    _schema("traffic_get", "按当前会话编号读取流量头与脱敏正文预览；代理原文可通过 traffic_body_read 分页读取。", {"record_id": {"type": "integer"}}, ["record_id"]),
    _schema("traffic_body_read", "分页读取代理已保存的原始正文，不请求目标；原文未脱敏且保留内容编码，offset/limit 按字节。", {
        "record_id": {"type": "integer"}, "direction": {"type": "string", "enum": ["request", "response"]},
        "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 8000}}, ["record_id"]),
]
