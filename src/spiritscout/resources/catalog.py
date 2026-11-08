"""资源目录的组合筛选；先筛选并计数，再按启用状态分页。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from spiritscout.resources.skill_search import TIERS, metadata, rank


def skill_catalog(
    items: list[dict[str, Any]], *, q: str = "", enabled: bool | None = None,
    domain: str = "", source: str = "", tier: str = "", added_from: date | None = None,
    added_to: date | None = None, date_state: str = "all", sort: str = "added_desc",
    offset: int = 0, limit: int = 40, semantic: bool = True,
) -> dict[str, Any]:
    """日期范围按北京时间自然日计算；未知时间不混入日期范围。"""
    if added_from and added_to and added_from > added_to:
        raise ValueError("开始日期不能晚于结束日期")
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime.combine(added_from, time.min, zone).timestamp() if added_from else None
    # 用次日零点的开区间覆盖结束日，保留浮点秒精度。
    end = None
    if added_to:
        midnight = datetime.combine(added_to, time.min, zone)
        end = (midnight.timestamp() + 86400 if added_to == date.max
               else (midnight + timedelta(days=1)).timestamp())
    if tier and tier not in TIERS:
        raise ValueError("未知技能分级")
    facets = {"domains": sorted({item.get("domain") or "custom" for item in items}),
              "tiers": TIERS}
    words = q.casefold().split()
    candidates = rank(items, q) if semantic else [item for item in items if not words or any(
        word in ' '.join(str(item.get(k, '')) for k in ('skill_id', 'description', 'domain')).casefold()
        for word in words)]
    rows = []
    for item in candidates:
        added = item.get("created_at")
        known = isinstance(added, (int, float)) and not isinstance(added, bool)
        if tier and metadata(item)['tier'] != tier:
            continue
        if domain and (item.get("domain") or "custom") != domain:
            continue
        if source and item["source_kind"] != source:
            continue
        if (date_state == "known" and not known) or (date_state == "unknown" and known):
            continue
        if start is not None and (not known or added < start):
            continue
        if end is not None and (not known or added >= end):
            continue
        rows.append(item)
    counts = {"enabled": sum(bool(row.get("enabled", True)) for row in rows),
              "disabled": sum(not row.get("enabled", True) for row in rows)}
    if enabled is not None:
        rows = [row for row in rows if bool(row.get("enabled", True)) == enabled]
    if sort != "relevance" or not q:
        rows.sort(key=lambda row: row["skill_id"].casefold())
    if sort not in {"name_asc", "relevance"}:
        field = "updated_at" if sort == "updated_desc" else "created_at"
        # 无时间的历史和内置记录始终排在已知记录后面。
        rows.sort(key=lambda row: (row.get(field) is None,
                  (row.get(field) or 0) * (1 if sort == "added_asc" else -1)))
    return {"items": rows[offset:offset + limit], "total": len(rows),
            "has_more": offset + limit < len(rows), "counts": counts, "facets": facets}


def mcp_catalog(
    items: list[dict[str, Any]], *, transport: str = "", risk: str = "",
    agent: str = "", **filters: Any,
) -> dict[str, Any]:
    """仅从已脱敏的配置生成目录，复用日期边界、分组计数和分页规则。"""
    facets = {
        "transports": sorted({row["data"]["transport"] for row in items}),
        "risks": sorted({row["data"]["default_risk"] for row in items}),
        "agents": sorted({role for row in items for role in row["data"]["allowed_agents"]}),
    }
    candidates = []
    for row in items:
        data = row["data"]
        roles = data["allowed_agents"]
        if transport and data["transport"] != transport:
            continue
        if risk and data["default_risk"] != risk:
            continue
        if agent and ((agent == "default" and bool(roles))
                      or (agent != "default" and agent not in roles)):
            continue
        candidates.append({**row, "skill_id": row["id"], "source_kind": row["origin"],
                           "enabled": data["enabled"], "domain": data["transport"],
                           "description": " ".join(str(data.get(key, "")) for key in
                               ("command", "args", "url", "default_risk", "allowed_agents"))})
    result = skill_catalog(candidates, semantic=False, **filters)
    by_id = {row["id"]: row for row in items}
    result["items"] = [by_id[row["id"]] for row in result["items"]]
    result["facets"] = facets
    return result
