"""资源控制台路由：管理对话隔离于测试会话，所有变更显式版本校验。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.resources.assistant import ResourceAssistant, validate_skill
from spiritscout.resources.store import ResourceStore, redact_credentials, validate_mcp


class ResourceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict[str, Any]
    expected_version: int = Field(default=0, ge=0)


class ResourceMessage(BaseModel):
    message: str = Field(min_length=1, max_length=16000)


class ConfirmChange(BaseModel):
    confirm: StrictBool


def public_mcp(config: Any, version: int = 0, origin: str = "config") -> dict[str, Any]:
    data = config.model_dump()
    for key in ("headers", "env"):
        data[key + "_keys"] = list(data.pop(key))
    return {
        "id": config.name,
        "version": version,
        "origin": origin,
        "data": redact_credentials(data),
    }


def mount_resources(
    app: FastAPI, cfg: Any, store: ResourceStore, assistant: ResourceAssistant,
) -> None:
    def fail(exc: Exception) -> HTTPException:
        if isinstance(exc, ValidationError):
            errors = [
                {
                    "field": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"],
                }
                for item in exc.errors(include_input=False)
            ]
            return HTTPException(
                422,
                {
                    "message": "配置校验失败："
                    + "；".join(
                        item["field"] + " " + item["message"] for item in errors
                    ),
                    "errors": errors,
                },
            )
        return HTTPException(
            409 if isinstance(exc, SpiritScoutError) else 422,
            str(exc)[:600],
        )

    @app.get("/api/resources/mcp")
    def mcp_list() -> dict[str, Any]:
        overrides = {row["id"]: row for row in store.settings("mcp")}
        return {
            "items": [
                public_mcp(
                    item,
                    overrides.get(item.name, {}).get("version", 0),
                    "managed" if item.name in overrides else "config",
                )
                for item in store.effective_mcp(cfg.mcp_servers)
            ],
            "activation": "新启动会话生效；不会热替换正在执行的 MCP 连接",
        }

    @app.put("/api/resources/mcp/{name}")
    def mcp_save(name: str, payload: ResourceChange) -> dict[str, Any]:
        try:
            existing = next(
                (
                    item
                    for item in store.effective_mcp(cfg.mcp_servers)
                    if item.name == name
                ),
                None,
            )
            data = {
                **(existing.model_dump() if existing else {}),
                **payload.data,
                "name": name,
            }
            spec = validate_mcp(data)
            version = store.save(
                "mcp", name, spec.model_dump(), payload.expected_version
            )
            return public_mcp(spec, version, "managed")
        except (ValueError, SpiritScoutError) as exc:
            raise fail(exc) from exc

    @app.get("/api/resources/skills")
    def skill_list(
        q: str = Query("", max_length=200),
        offset: int = Query(0, ge=0),
        limit: int = Query(40, ge=1, le=100),
    ) -> dict[str, Any]:
        items = store.skills(q)
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "has_more": offset + limit < len(items),
        }

    @app.get("/api/resources/skills/{key}")
    def skill_detail(key: str) -> dict[str, Any]:
        item = next(
            (
                row
                for row in store.skills(key, include_body=True)
                if row["skill_id"] == key
            ),
            None,
        )
        if not item:
            raise HTTPException(404, "Skill 不存在")
        return item

    @app.put("/api/resources/skills/{key}")
    def skill_save(key: str, payload: ResourceChange) -> dict[str, Any]:
        try:
            original = next(
                (
                    row
                    for row in store.skills(key, include_body=True)
                    if row["skill_id"] == key
                ),
                {},
            )
            data = {
                k: v
                for k, v in {**original, **payload.data, "skill_id": key}.items()
                if k not in {"version", "origin"}
            }
            validate_skill(data)
            version = store.save("skill", key, data, payload.expected_version)
            return {"version": version, "skill_id": key}
        except (ValueError, SpiritScoutError) as exc:
            raise fail(exc) from exc

    @app.get("/api/assets")
    def assets(
        q: str = Query("", max_length=200),
        host: str = Query("", max_length=300),
        project: str = Query("", max_length=64),
        unclassified: int = Query(0, ge=0, le=1),
        kind: str = Query("", max_length=16),
        offset: int = Query(0, ge=0),
        limit: int = Query(40, ge=1, le=100),
    ) -> dict[str, Any]:
        if project and unclassified:
            raise HTTPException(422, "project 与 unclassified 互斥")
        if kind not in {"", "asset", "finding"}:
            raise HTTPException(422, "kind 只能是空、asset 或 finding")
        return store.assets(q, offset, limit, host=host, project=project,
                            unclassified=bool(unclassified), kind=kind)

    @app.get("/api/assets/{key}/history")
    def asset_history(key: str, offset: int = Query(0, ge=0)) -> dict[str, Any]:
        return store.history(key, offset)

    @app.post("/api/assets/sync")
    async def asset_sync() -> dict[str, Any]:
        """只导入历史会话的黑板资产；不触发命名/归类（归属由用户决定）。"""
        def scan() -> tuple[int, int]:
            count, errors = 0, 0
            for path in (Path(cfg.workspace.root) / "sessions").glob("sess-*"):
                try:
                    count += store.import_session(path, full=True)
                except (OSError, ValueError, sqlite3.Error):
                    errors += 1
            return count, errors

        count, errors = await run_in_threadpool(scan)
        return {"scanned_records": count, "failed_sessions": errors}

    @app.get("/api/resources/{kind}/chat")
    def chat_view(kind: Literal["mcp", "skill"]) -> dict[str, Any]:
        return assistant.view(kind)

    @app.post("/api/resources/{kind}/chat")
    async def chat_message(
        kind: Literal["mcp", "skill"], payload: ResourceMessage
    ) -> dict[str, Any]:
        try:
            return await assistant.message(kind, payload.message)
        except (ValueError, TypeError, SpiritScoutError, OSError) as exc:
            raise fail(exc) from exc

    @app.post("/api/resources/proposals/{key}/apply")
    async def apply_proposal(key: str, payload: ConfirmChange) -> dict[str, Any]:
        if not payload.confirm:
            raise HTTPException(422, "需要确认具体变更")
        try:
            assistant.apply(key)
        except ValueError as exc:
            raise fail(exc) from exc
        return {"state": "applying"}
