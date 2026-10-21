"""项目分区路由：用户创建的共享记忆分区。

项目不是授权分区：不携带、不扩大任何授权；测试边界仍由模型结合本次用户上下文维护。
删除项目只解绑——资产、观测与发现全部保留并转为未分类。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.resources.store import ProjectNameConflict, ResourceStore


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=80)
    # 说明可承载完整测试规则，不限长度（由用户按项目维护）
    description: str = Field(default="")


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=80)
    description: str | None = None


def mount_projects(app: FastAPI, store: ResourceStore, manager: Any) -> None:
    def catalog() -> dict[str, Any]:
        data = store.projects()
        sessions = manager.list()
        known = {item["project_id"] for item in data["items"]}
        counts = dict.fromkeys(known, 0)
        unclassified = 0
        for session in sessions:
            project_id = str(session.project_id or "")
            if project_id in counts:
                counts[project_id] += 1
            else:
                # 空串或指向已删项目：都算未分类（与观测作用域同口径）
                unclassified += 1
        for item in data["items"]:
            item["session_count"] = counts.get(item["project_id"], 0)
        data["unclassified"]["session_count"] = unclassified
        data["all"]["session_count"] = len(sessions)
        return {
            "projects": data["items"],
            "unclassified": data["unclassified"],
            "all": data["all"],
        }

    @app.get("/api/projects")
    def list_projects() -> dict[str, Any]:
        return catalog()

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate) -> dict[str, Any]:
        try:
            return store.create_project(payload.name, payload.description)
        except ProjectNameConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: str,
                       payload: ProjectPatch) -> dict[str, Any]:
        fields = payload.model_fields_set
        if not fields:
            raise HTTPException(422, "未提供要修改的字段")
        try:
            project = store.update_project(
                project_id,
                name=payload.name if "name" in fields else None,
                description=payload.description
                if "description" in fields else None)
        except ProjectNameConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if project is None:
            raise HTTPException(404, f"项目不存在: {project_id}")
        return project

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str,
                             confirm: bool = Query(False)) -> dict[str, Any]:
        if store.project(project_id) is None:
            raise HTTPException(404, f"项目不存在: {project_id}")
        if not confirm:
            raise HTTPException(
                409, "删除项目需显式 confirm=true（资产与发现保留并转为未分类）")
        failed: list[dict[str, str]] = []
        for session in manager.list():
            if str(session.project_id or "") != project_id:
                continue
            try:
                manager.set_project(session.session_id, "")
            except (SpiritScoutError, OSError, ValueError, sqlite3.Error) as exc:
                failed.append({"session_id": session.session_id,
                               "error": str(exc)[:200]})
        result = store.delete_project(project_id)
        if result is None:              # 并发删除：按已不存在处理
            raise HTTPException(404, f"项目不存在: {project_id}")
        return {
            "deleted": project_id,
            "detached_sessions": result["detached_sessions"],
            "detached_observations": result["detached_observations"],
            "failed_sessions": failed,
        }
