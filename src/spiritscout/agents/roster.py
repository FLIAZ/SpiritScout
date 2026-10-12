"""Agent 编队名册：全系统 Agent 的中文别名与职责，单一事实源。

Web 编队页、拓扑视图、任务列表的显示名一律取自此表，避免多处硬编码漂移。
name 为中文别名（用户面向），duty 为一句话职责；模型指派在编队页按模型 ID 显式完成。
"""

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
