"""可独立挂载的控制台通知与用量模块；主应用负责认证和静态资源接入。"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, Query

from spiritscout.web.notify import NotificationReader
from spiritscout.web.usage_stats import usage_stats


def mount_console_insights(app: FastAPI, cfg: Any, manager: Any) -> None:
    """挂载两个只读 GET 路由；沿用 app 的现有认证边界。"""
    reader = NotificationReader()

    @app.get("/api/notifications")
    def notifications() -> dict[str, Any]:
        return reader.read(manager.list())

    @app.get("/api/usage/stats")
    def stats(period: Literal["hour", "today", "7d", "30d", "all"] = Query(default="today")) -> dict[str, Any]:
        return usage_stats(cfg, manager, period)
