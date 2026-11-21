"""项目级 AI 资产收集任务的启动、暂停、审批与回收。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from spiritscout.assets.agent import run_collection
from spiritscout.config.model_identity import resolve_model
from spiritscout.config.settings import ModelEntry
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.pause import PauseGate
from spiritscout.gateway.session_model import bind_session_model
from spiritscout.policy.approvals import ApprovalBroker
from spiritscout.resources.store import ResourceStore

logger = logging.getLogger(__name__)


@dataclass
class _RunningCampaign:
    task: asyncio.Task[None] | None
    pause_gate: PauseGate
    approvals: ApprovalBroker
    stopping: bool = False
    model: ModelEntry | None = None


class AssetCampaignManager:
    """批次与输出落盘；AI、工具和审批等待者由当前运行句柄持有。"""

    def __init__(self, store: ResourceStore, cfg: Any | None = None, models: list[Any] | None = None,
                 models_source: Callable[[], list[ModelEntry]] | None = None) -> None:
        self.store, self.cfg, self.models = store, cfg, models or []
        self.models_source = models_source
        self._running: dict[str, _RunningCampaign] = {}

    def view(self, campaign_id: str) -> dict[str, Any] | None:
        campaign = self.store.asset_campaign(campaign_id)
        if campaign:
            running = self._running.get(campaign_id)
            campaign["approvals"] = running.approvals.view() if running else []
        return campaign

    def resolve_model(self, reference: str) -> ModelEntry:
        if not reference.strip():
            raise ConfigurationError("请先在资产收集页选择 LLM 模型")
        entries = self.models_source() if self.models_source else self.models
        return resolve_model(entries, reference.strip())

    def start(self, campaign_id: str, model: str | None = None) -> dict[str, Any]:
        campaign = self.store.asset_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        reference = model if model is not None else str(campaign.get("config", {}).get("model") or "")
        current = self._running.get(campaign_id)
        if current and current.task and not current.task.done():
            if model is not None and reference != str(campaign.get("config", {}).get("model") or ""):
                raise ConfigurationError("任务正在运行，停止后才能更换 LLM 模型")
            return self.view(campaign_id) or campaign
        selected = self.resolve_model(reference) if reference else None
        if selected:
            campaign = self.store.update_asset_campaign(
                campaign_id, config={**campaign.get("config", {}), "model": selected.key}) or campaign
        saved = self.store.asset_campaign_targets(campaign_id) or {}
        self.store.update_asset_campaign(
            campaign_id, status="running", started_at=time.time(), finished_at=0, error="",
            stats={"targets": len(campaign["targets"]), "completed": 0, "candidates": len(saved.get("targets", [])),
                   "failed": 0, "skipped": 0, "commands": 0},
        )
        self.store.append_asset_campaign_event(campaign_id, "开始 AI 资产收集；关联资产不自动扩大测试授权。")
        running = _RunningCampaign(None, PauseGate(), ApprovalBroker(), model=selected)
        self._running[campaign_id] = running
        running.task = asyncio.create_task(self._run(campaign, running), name=f"asset-campaign:{campaign_id}")
        return self.view(campaign_id) or campaign

    async def stop(self, campaign_id: str) -> dict[str, Any] | None:
        running = self._running.get(campaign_id)
        if running and running.task and not running.task.done():
            if not running.stopping:
                running.stopping = True
                running.approvals.cancel_all(reason="收集任务停止")
                running.pause_gate.resume()
                running.task.cancel()
            # 不把 API 请求取消传播到正在回收进程/工具的后台任务。
            try:
                await asyncio.shield(running.task)
            except asyncio.CancelledError:
                # 任务尚未调度就被停止时，协程的 finally 不会运行；API 自身取消仍应传播。
                if not running.task.cancelled():
                    raise
            if self._running.get(campaign_id) is running:
                self._running.pop(campaign_id, None)
        campaign = self.store.asset_campaign(campaign_id)
        if campaign and campaign["status"] in {"running", "paused", "queued"}:
            self.store.append_asset_campaign_event(campaign_id, "任务已停止，已收集资产保留。")
            self.store.update_asset_campaign(campaign_id, status="stopped", finished_at=time.time())
        return self.view(campaign_id)

    async def delete(self, campaign_id: str) -> bool:
        if self.store.asset_campaign(campaign_id) is None:
            return False
        await self.stop(campaign_id)
        return self.store.delete_asset_campaign(campaign_id)

    def pause(self, campaign_id: str) -> dict[str, Any] | None:
        running = self._running.get(campaign_id)
        if running and not running.stopping and running.task and not running.task.done():
            running.pause_gate.pause()
            self.store.append_asset_campaign_event(campaign_id, "已暂停下一步 AI/工具调用；当前命令会继续完成。")
            self.store.update_asset_campaign(campaign_id, status="paused")
        return self.view(campaign_id)

    def resume(self, campaign_id: str, model: str | None = None) -> dict[str, Any]:
        campaign = self.store.asset_campaign(campaign_id)
        if campaign is None:
            raise KeyError(campaign_id)
        running = self._running.get(campaign_id)
        if not running or not running.task or running.task.done():
            return self.start(campaign_id, model)
        if model is not None and model != str(campaign.get("config", {}).get("model") or ""):
            raise ConfigurationError("任务暂停中，停止后才能更换 LLM 模型")
        if not running.stopping:
            running.pause_gate.resume()
            self.store.append_asset_campaign_event(campaign_id, "继续 AI 资产收集。")
            self.store.update_asset_campaign(campaign_id, status="running")
        return self.view(campaign_id) or campaign

    async def aclose(self) -> None:
        await asyncio.gather(*(self.stop(key) for key in list(self._running)))

    async def _run(self, campaign: dict[str, Any], running: _RunningCampaign) -> None:
        campaign_id = campaign["campaign_id"]
        try:
            # 批次选模独立于会话 ContextVar；单模型注册表也不继承其他供应商的主备链。
            entries = [running.model] if running.model else []
            with bind_session_model(running.model.key if running.model else "", lambda: entries):
                result = await run_collection(self, campaign, running)
            # AI 结束判断与实际工具回收都完成后才对前端发布终态。
            self.store.update_asset_campaign(campaign_id, **result, finished_at=time.time())
        except asyncio.CancelledError:
            self.store.append_asset_campaign_event(campaign_id, "任务已停止，工具资源已回收。")
            self.store.update_asset_campaign(campaign_id, status="stopped", finished_at=time.time())
        except Exception as exc:
            logger.exception("资产收集任务失败: %s", campaign_id)
            error = f"{type(exc).__name__}: {exc}"
            self.store.append_asset_campaign_event(campaign_id, error, "error")
            self.store.update_asset_campaign(campaign_id, status="failed", error=error, finished_at=time.time())
        finally:
            running.approvals.cancel_all(reason="收集任务收尾")
            if self._running.get(campaign_id) is running:
                self._running.pop(campaign_id, None)
