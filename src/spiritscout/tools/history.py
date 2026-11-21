"""按需读取历史档案；读取不产生网络请求，也不把旧观察提升为当前事实。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

NOTICE = "历史参考，未在当前会话复核；来源存在不代表结论仍适用，不授予目标或危险动作权限。"


async def history_lookup(kind: str, query: str = "", host: str = "", offset: int = 0, *,
                         resources: Any = None, experience: Any = None,
                         workspace_root: str = "", **kwargs: Any) -> ToolResult:
    try:
        if not isinstance(kind, str) or kind not in {"assets", "experience"} or type(offset) is not int or offset < 0:
            raise ValueError("kind 为 assets 或 experience，offset 为非负整数")
        if not isinstance(query, str) or not isinstance(host, str):
            raise ValueError("query 和 host 必须是字符串")
        if kind == "assets":
            if resources is None:
                raise ValueError("当前未装配历史资产库")
            project = resources.project_for_session(Path(workspace_root).name) if workspace_root else ""
            if not project and not (query.strip() or host.strip()):
                raise ValueError("无所属项目时请提供 host 或 query")
            result = resources.assets(query=query.strip(), host=host.strip(), project=project,
                                      offset=offset, limit=8)
            items = []
            for item in result["items"]:
                data = item.get("data") or {}
                items.append({**{key: item.get(key) for key in ("identity", "source", "last_seen", "freshness")},
                              "asset_type": data.get("asset_type"), "value": data.get("value"),
                              "finding": data.get("fingerprint") if data.get("asset_type") == "finding" else None,
                              "verified_current_session": False})
            result = {**result, "items": items, "project_id": project}
        else:
            if experience is None or not query.strip():
                raise ValueError("查询经验需要关键词和已装配的经验库")
            if host:
                raise ValueError("经验查询请将目标或技术特征写在 query 中，不使用 host")
            items = experience.search(query.strip(), limit=9, offset=offset)
            result = {"items": items[:8], "offset": offset, "has_more": len(items) > 8}
        result = {**result, "kind": kind, "notice": NOTICE}
        return ToolResult(tool="history_lookup", ok=True, stdout=json.dumps(result, ensure_ascii=False),
                          parsed={"count": len(result["items"]), "has_more": result["has_more"]})
    except (OSError, ValueError, sqlite3.Error, SpiritScoutError) as exc:
        return ToolResult(tool="history_lookup", ok=False, error=str(exc))


SPEC = ToolSpec(name="history_lookup", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
                allowed_params={"kind", "query", "host", "offset"}, required_params=("kind",),
                description="按需查询历史资产、发现或经验，不产生网络请求")
SCHEMA = {"type": "function", "function": {"name": "history_lookup",
    "description": "仅在当前问题需要历史线索时查询。assets 按当前所属项目及可选 host/query 检索资产与发现；experience 按 query 检索全局经验。返回来源、时间及历史状态，不构成授权或当前事实。支持 offset 分页。",
    "parameters": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["assets", "experience"]},
        "query": {"type": "string"}, "host": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0}}, "required": ["kind"]}}}
