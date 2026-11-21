"""项目级资产收集任务接口。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from spiritscout.assets.campaigns import AssetCampaignManager
from spiritscout.config.settings import ModelEntry
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.resources.store import ResourceStore


class AssetCampaignCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proxy_plan_id: str = Field(default="direct", pattern=r"^(direct|proxy-[a-f0-9]{12})$")
    direct_fallback: StrictBool = False
    model: str = Field(default="", max_length=1000)
    targets: list[str] = Field(min_length=1, max_length=200)
    oneforall_path: str = Field(default="", max_length=500)
    python: str = Field(default="", max_length=300)
    timeout_seconds: int = Field(default=600, ge=30, le=7200)
    instruction: str = Field(default="", max_length=20000)
    brute: bool = False
    excluded_assets: list[str] = Field(default_factory=list, max_length=500)


class AssetCampaignModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=1000)


class AssetApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: bool = Field(strict=True)


def mount_asset_campaigns(app: FastAPI, cfg: Any, store: ResourceStore, models: list[Any] | None = None,
                          models_source: Callable[[], list[ModelEntry]] | None = None) -> AssetCampaignManager:
    manager = AssetCampaignManager(store, cfg, models, models_source)
    app.state.asset_campaigns = manager

    @app.get("/api/projects/{project_id}/asset-campaigns")
    def list_campaigns(project_id: str) -> dict[str, Any]:
        if store.project(project_id) is None:
            raise HTTPException(404, f"项目不存在: {project_id}")
        return {"campaigns": [manager.view(c["campaign_id"]) for c in store.asset_campaigns(project_id)]}

    @app.get("/api/projects/{project_id}/asset-targets")
    def asset_targets(project_id: str) -> dict[str, Any]:
        result = store.project_asset_targets(project_id)
        if result is None:
            raise HTTPException(404, f"项目不存在: {project_id}")
        return result

    @app.post("/api/projects/{project_id}/asset-campaigns", status_code=201)
    def create_campaign(project_id: str, payload: AssetCampaignCreate) -> dict[str, Any]:
        config = payload.model_dump(exclude={"targets"})
        try:
            if payload.proxy_plan_id != "direct":
                from spiritscout.resources.proxy_pool import ProxyPool

                ProxyPool(store).snapshot(payload.proxy_plan_id)
            if payload.model:
                config["model"] = manager.resolve_model(payload.model).key
            return store.create_asset_campaign(project_id, payload.targets, config)
        except (ValueError, SpiritScoutError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/asset-campaigns/{campaign_id}")
    def get_campaign(campaign_id: str) -> dict[str, Any]:
        campaign = manager.view(campaign_id)
        if campaign is None:
            raise HTTPException(404, "资产收集任务不存在")
        return campaign

    @app.get("/api/asset-campaigns/{campaign_id}/targets")
    def campaign_targets(campaign_id: str) -> dict[str, Any]:
        result = store.asset_campaign_targets(campaign_id)
        if result is None:
            raise HTTPException(404, "资产收集任务不存在")
        return result

    @app.get("/api/asset-campaigns/{campaign_id}/export.csv")
    def export_campaign(campaign_id: str) -> Response:
        from spiritscout.assets.export import asset_csv

        batch = store.asset_campaign_targets(campaign_id)
        if batch is None:
            raise HTTPException(404, "资产收集批次不存在")
        return Response("\ufeff" + asset_csv(batch["targets"]), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{campaign_id}.csv"'})

    @app.get("/api/asset-campaigns/{campaign_id}/events")
    async def campaign_events(campaign_id: str, request: Request,
                              after: int = Query(default=0, ge=0)) -> StreamingResponse:
        if store.asset_campaign(campaign_id) is None:
            raise HTTPException(404, "资产收集任务不存在")
        try:
            cursor = max(after, int(request.headers.get("last-event-id") or 0))
        except ValueError as exc:
            raise HTTPException(422, "无效的输出游标") from exc

        async def events():
            nonlocal cursor
            previous = None
            idle = 0
            while not await request.is_disconnected():
                rows = store.asset_campaign_events(campaign_id, cursor)
                for row in rows:
                    cursor = row["seq"]
                    yield f"id: {cursor}\nevent: output\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                # 终态也先排空持久化日志，避免最后一批输出在连接关闭时丢失。
                if len(rows) == 200:
                    continue
                campaign = manager.view(campaign_id)
                if campaign is None:
                    yield 'event: deleted\ndata: {}\n\n'
                    return
                snapshot = json.dumps(campaign, ensure_ascii=False)
                if snapshot != previous:
                    yield f"event: state\ndata: {snapshot}\n\n"
                    previous = snapshot
                if campaign["status"] not in {"queued", "running", "paused"}:
                    # 状态读取前可能刚写入终态，复核游标后再结束。
                    if store.asset_campaign_events(campaign_id, cursor, limit=1):
                        continue
                    yield 'event: done\ndata: {}\n\n'
                    return
                idle += 1
                if idle >= 60:
                    yield ': keepalive\n\n'
                    idle = 0
                await asyncio.sleep(0.25)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/asset-campaigns/{campaign_id}/start")
    async def start_campaign(campaign_id: str, payload: AssetCampaignModel | None = Body(default=None)) -> dict[str, Any]:
        try:
            return manager.start(campaign_id, payload.model if payload else None)
        except KeyError as exc:
            raise HTTPException(404, "资产收集任务不存在") from exc
        except SpiritScoutError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/asset-campaigns/{campaign_id}/stop")
    async def stop_campaign(campaign_id: str) -> dict[str, Any]:
        if store.asset_campaign(campaign_id) is None:
            raise HTTPException(404, "资产收集任务不存在")
        return await manager.stop(campaign_id) or {}

    @app.post("/api/asset-campaigns/{campaign_id}/pause")
    async def pause_campaign(campaign_id: str) -> dict[str, Any]:
        if store.asset_campaign(campaign_id) is None:
            raise HTTPException(404, "资产收集任务不存在")
        return manager.pause(campaign_id) or {}

    @app.post("/api/asset-campaigns/{campaign_id}/resume")
    async def resume_campaign(campaign_id: str, payload: AssetCampaignModel | None = Body(default=None)) -> dict[str, Any]:
        try:
            return manager.resume(campaign_id, payload.model if payload else None)
        except KeyError as exc:
            raise HTTPException(404, "资产收集任务不存在") from exc
        except SpiritScoutError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/asset-campaigns/{campaign_id}/approvals/{approval_id}")
    async def decide_approval(campaign_id: str, approval_id: str,
                              payload: AssetApprovalDecision) -> dict[str, Any]:
        running = manager._running.get(campaign_id)
        if running is None or not running.approvals.decide(approval_id, payload.allow):
            raise HTTPException(404, "审批已结束或任务不存在")
        return manager.view(campaign_id) or {}

    @app.delete("/api/asset-campaigns/{campaign_id}")
    async def delete_campaign(campaign_id: str) -> dict[str, Any]:
        if not await manager.delete(campaign_id):
            raise HTTPException(404, "资产收集任务不存在")
        return {"deleted": campaign_id, "assets_preserved": True}

    return manager
