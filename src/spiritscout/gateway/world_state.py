"""Codex 式世界状态：运行时事实只发送一次，变化时补一条完整字段片段。

系统提示是稳定前缀，不随授权扩围、任务或环境变化；授权范围、用户批准的
目标、禁打边界与沙箱环境放在 user 角色的 ``<world_state>`` 片段里。语义
与 Codex 的 ``environment_context`` 一致：最新片段覆盖同名字段，未出现的
字段沿用上一片段；变化按顶层字段整体重发，旧的中间片段被折叠也不丢事实。
"""

from __future__ import annotations

import json
import platform as _platform
from datetime import datetime
from typing import Any


def authorization(guard: Any) -> dict[str, Any]:
    """授权事实投影：初始白名单 + 用户会话批准集 + 禁打硬边界。"""
    scope = getattr(guard, "scope", None) if guard is not None else None
    if scope is None:
        return {}
    approved = getattr(guard, "approved_targets", None)
    return {
        "allowed_domains": list(getattr(scope, "allowed_domains", []) or []),
        "allowed_cidrs": list(getattr(scope, "allowed_cidrs", []) or []),
        "allowed_urls": list(getattr(scope, "allowed_urls", []) or []),
        "approved_by_user": sorted(approved()) if callable(approved) else [],
        "denied": list(getattr(scope, "denied", []) or []),
        "local_analysis": bool(getattr(scope, "local_analysis", False)),
    }


def environment(*, workspace: str = "", artifacts: str = "") -> dict[str, Any]:
    """沙箱运行态事实：只陈述位置与平台，不枚举可用的命令或库。"""
    facts: dict[str, Any] = {
        "platform": _platform.system().lower() or "unknown",
        "date": datetime.now().date().isoformat(),
    }
    if workspace:
        facts["workspace"] = workspace
    if artifacts:
        facts["artifacts"] = artifacts
    return facts


def snapshot(*, guard: Any = None, env: dict[str, Any] | None = None) -> dict[str, Any]:
    """当前世界状态：空字段不占位，片段只承载运行时事实。"""
    out: dict[str, Any] = {}
    auth = authorization(guard)
    if auth:
        out["authorization"] = auth
    if env:
        out["environment"] = env
    return out


def render(values: dict[str, Any], *, update: bool = False) -> str:
    attribute = ' update="true"' if update else ""
    body = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return f"<world_state{attribute}>\n{body}\n</world_state>"


def update(previous: dict[str, Any] | None, current: dict[str, Any]) -> str | None:
    """返回与上次不同的顶层字段（整值重发）；无变化返回 None。

    不发送 JSON Patch：旧片段可能被上下文压缩折叠，后续片段必须能独立
    读懂。整值重发让最新片段始终包含该字段的完整现状（与 Codex 的
    environment_context 按区块重绘同语义）。
    """
    if not current:
        return None
    if not previous:
        return render(current)
    changed: dict[str, Any] = {}
    for key, value in current.items():
        if previous.get(key) != value:
            changed[key] = value
    for key in previous.keys() - current.keys():
        changed[key] = None
    if not changed:
        return None
    return render(changed, update=True)
