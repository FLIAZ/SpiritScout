"""Agent 的中文名称、职责和角色列表。"""

from __future__ import annotations

from spiritscout.agents.profiles import public_roster

ROSTER: list[dict[str, object]] = public_roster()

_BY_KEY: dict[str, dict[str, object]] = {str(r["key"]): r for r in ROSTER}


def roster_view() -> list[dict[str, object]]:
    """Web 编队页数据：完整名册（中文别名/职责/工具边界）。"""
    return [dict(r) for r in ROSTER]


def display_name(key: str) -> str:
    """Agent 显示名：中文别名（未知 key 原样返回，不静默吞掉新类型）。"""
    entry = _BY_KEY.get(key)
    return str(entry["name"]) if entry else key
