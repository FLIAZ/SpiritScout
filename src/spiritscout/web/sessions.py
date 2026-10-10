"""Web 控制台会话管理器（会话隔离的任务操作台）。

隔离模型：每会话独立工作区 ``<workspace>/sessions/<id>/`` 与独立
SystemConfig 副本（黑板 DB / 知识库 / 审计 / 报告路径全部重定向，互不共享）；
同进程 ``asyncio.Task`` 驱动三模式 engagement（runtime 无模块级单例，
天然支持并发，D4）。

``meta.json`` 持久化会话元数据 → 进程重启后扫描 sessions/ 目录重建注册表，
历史会话离线可查；重启时发现 running 残留标记 interrupted（显式而非猜测）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spiritscout.agents.ring import RingLedger
from spiritscout.config.settings import ModelEntry, SystemConfig
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import RunMode
from spiritscout.launch import LaunchPlan, LaunchRequest, prepare_launch
from spiritscout.policy.approvals import ApprovalBroker
from spiritscout.web import agentcfg, vault


async def _llm_route_mode(cfg: SystemConfig, models: list[ModelEntry],
                          mission: str, heuristic: RunMode,
                          audit: AuditLogger) -> RunMode | None:
    """智能模式的 LLM 语义路由：任务描述交给模型判定归属分支
    （CTF/SRC/渗透赛），不写关键词脚本；不可用/解析失败 → None（按兜底走）。

    护栏：只返回三枚举之一，模型自由裁决「走哪条分支」，但分支内部的
    范围/预算/核验护栏不变。
    """
    from spiritscout.gateway import ModelGateway, ModelRegistry
    from spiritscout.gateway.backends import extract_json

    gateway = ModelGateway(ModelRegistry(models), config=cfg.gateway, audit=audit)
    try:
        resp = await gateway.chat(
            [{"role": "system", "content": (
                "你是灵探的任务路由。通读用户的任务描述，判断它属于哪一类安全"
                "测试分支，只输出 JSON。ROLE: mode_router")},
             {"role": "user", "content": json.dumps({
                 "mission": mission[:2000],
                 "branches": {
                     "src": "授权安全测试/渗透：对站点、系统或业务做侦察→漏洞"
                            "探测→核验→报告（含 SRC 漏洞挖掘场景）",
                     "ctf": "CTF 解题：目标是具体赛题、以夺取 flag 为目的",
                     "competition": "渗透赛/攻防对抗：有赛制、排名、AWD 等规则"},
                 "fallback_guess": heuristic.value,
                 "answer": '{"mode": "src|ctf|competition", "rationale": "一句话"}',
             }, ensure_ascii=False)}],
            task_type="classification", response_format="json")
        data = extract_json(resp.content)
        mode = RunMode(str((data or {}).get("mode", "")).strip().lower())
        audit.log("decision.change", "mode_router", {
            "action": "llm_mode_route", "llm_mode": mode.value,
            "heuristic": heuristic.value,
            "rationale": str((data or {}).get("rationale", ""))[:200]})
        return mode
    except Exception as exc:  # noqa: BLE001 —— 路由失败按兜底模式走（留痕）
        audit.log("decision.change", "mode_router", {
            "action": "llm_mode_route_fallback",
            "heuristic": heuristic.value, "error": str(exc)[:200]})
        return None

TERMINAL_STATES = frozenset({"completed", "failed", "stopped", "interrupted"})


async def _preflight_models(cfg: SystemConfig, models: list[ModelEntry],
                            audit: AuditLogger) -> None:
    """开工前模型预检：LLM 是系统的脑——一次最小调用全链失败即熔断，
    会话落 failed 并明示原因（不静默降级为模板跑批交付垃圾报告）。

    仅一次 ~8 token 的调用，成本可忽略；mock 后端照常应答（离线测试无感）。
    """
    from spiritscout.gateway import ModelGateway, ModelRegistry

    gateway = ModelGateway(ModelRegistry(models), config=cfg.gateway, audit=audit)
    try:
        await gateway.chat(
            [{"role": "user", "content": "就绪检查：回复 ok 即可"}],
            task_type="preflight", max_tokens=8)
    except Exception as exc:
        audit.log("system.lifecycle", "preflight", {
            "action": "model_preflight_failed", "error": str(exc)[:300]})
        raise SpiritScoutError(
            f"模型不可用，会话未开工：{exc}。请在「模型」页检查接入配置"
            f"与密钥后重新发起") from exc


@dataclass
class Session:
    """一次渗透任务会话（会话隔离单元）。"""

    session_id: str
    mode: str
    target: str
    workspace: Path
    title: str = ""                                          # 会话名（可重命名；空 = 自动摘要）
    request: dict[str, Any] = field(default_factory=dict)   # 启动参数（展示用）
    status: str = "created"      # created/running/completed/failed/stopped/interrupted
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    # 高风险动作即时确认队列（G5 交互形态；进程内状态，会话终态即失效）
    approvals: ApprovalBroker = field(
        default_factory=ApprovalBroker, repr=False, compare=False)
    # 环形协同台账：子 Agent 已尝试登记 + 灵枢督导建议（进程内，随会话消亡）
    ring: RingLedger = field(
        default_factory=RingLedger, repr=False, compare=False)

    # ---- 路径（隔离边界）
    @property
    def bb_path(self) -> Path:
        return self.workspace / "bb.db"

    @property
    def audit_path(self) -> Path:
        return self.workspace / "audit.jsonl"

    @property
    def flags_path(self) -> Path:
        return self.workspace / "flags.json"

    @property
    def meta_path(self) -> Path:
        return self.workspace / "meta.json"

    def report_path(self) -> Path | None:
        """最新报告文件（未生成 → None）。"""
        reports = self.workspace / "reports"
        if not reports.is_dir():
            return None
        candidates = sorted(reports.glob("*.md"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0] if candidates else None

    def display_title(self) -> str:
        """会话名：用户重命名优先；否则取任务描述首行（无则退回目标）。"""
        if self.title:
            return self.title
        mission = str(self.request.get("mission") or "").strip()
        if mission:
            first = mission.splitlines()[0].strip()
            return first[:42] + ("…" if len(first) > 42 else "")
        return self.target or self.session_id

    def brief(self, *, include_summary: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": self.session_id, "mode": self.mode,
            "title": self.display_title(), "renamed": bool(self.title),
            "target": self.target, "status": self.status,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "error": self.error,
        }
        if include_summary:
            out["summary"] = self.summary
        return out


class SessionManager:
    """会话注册表与生命周期（创建/启动/停止/删除/重启重建）。

    任务创建走「创建队列 + 监督协程」：HTTP 端点只入队，监督协程（在应用
    生命周期内启动、不隶属任何请求作用域）负责 spawn 会话任务。原因：
    starlette TestClient/ASGI 栈会在请求返回时取消请求任务组内的 asyncio
    任务——若会话任务在端点内直接 create_task，响应一发出就会被误取消
    （实测 t≈响应完成时刻，CancelledError 经 langgraph 传播）。
    """

    def __init__(self, cfg: SystemConfig, models: list[ModelEntry],
                 *, max_concurrent: int = 4):
        self._cfg = cfg
        self._models = models
        self._sem = asyncio.Semaphore(max_concurrent)
        self._sessions: dict[str, Session] = {}
        self._queue: asyncio.Queue[
            tuple[Session, SystemConfig, LaunchPlan, bool, list[ModelEntry]]] = \
            asyncio.Queue()
        self._supervisor: asyncio.Task | None = None
        self.root = Path(cfg.workspace.root) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._scan()

    # ------------------------------------------------------------ 注册表

    def _scan(self) -> None:
        """启动重建：加载历史会话；running 残留 → interrupted（进程已死）。"""
        for meta in sorted(self.root.glob("*/meta.json")):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            session = Session(
                session_id=data["session_id"], mode=data["mode"],
                target=data["target"], workspace=meta.parent,
                title=data.get("title", ""),
                request=data.get("request", {}),
                status=data.get("status", "interrupted"),
                created_at=data.get("created_at", 0.0),
                started_at=data.get("started_at"),
                finished_at=data.get("finished_at"),
                summary=data.get("summary", {}), error=data.get("error", ""),
            )
            if session.status not in TERMINAL_STATES:
                session.status = "interrupted"
                session.finished_at = session.finished_at or time.time()
                self._persist(session)
            self._sessions[session.session_id] = session

    def _persist(self, session: Session) -> None:
        session.meta_path.write_text(json.dumps({
            "session_id": session.session_id, "mode": session.mode,
            "target": session.target, "title": session.title,
            "request": session.request,
            "status": session.status, "created_at": session.created_at,
            "started_at": session.started_at, "finished_at": session.finished_at,
            "summary": session.summary, "error": session.error,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self) -> list[Session]:
        return sorted(self._sessions.values(),
                      key=lambda s: s.created_at, reverse=True)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    # ------------------------------------------------------------ 生命周期

    def _session_cfg(self, session: Session, budget_tokens: int | None) -> SystemConfig:
        """会话级配置副本：全部状态路径重定向到会话工作区（隔离核心）。"""
        cfg = self._cfg.model_copy(deep=True)
        ws = session.workspace
        cfg.mode = RunMode(session.mode)
        cfg.workspace.root = str(ws)
        cfg.blackboard.sqlite_path = str(session.bb_path)
        cfg.knowledge.sqlite_path = str(ws / "kb.db")
        cfg.knowledge.chroma_path = str(ws / "chroma")
        cfg.audit_log_path = str(session.audit_path)
        cfg.report.output_dir = str(ws / "reports")
        if budget_tokens:
            cfg.gateway.budget_tokens = budget_tokens
        return cfg

    # ------------------------------------------------------------ 监督协程

    def start(self) -> None:
        """启动监督协程（幂等；应用 lifespan startup 或直接使用时懒启动）。"""
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(
                self._supervise(), name="spiritscout-supervisor")

    async def _supervise(self) -> None:
        """从创建队列取会话并 spawn 执行任务（不隶属任何请求作用域）。"""
        while True:
            session, cfg, plan, no_llm, models = await self._queue.get()
            if session.status != "created":
                continue            # 排队期间已被停止/删除
            session.status = "running"
            session.started_at = time.time()
            self._persist(session)
            session.task = asyncio.create_task(
                self._run(session, cfg, plan, no_llm, models),
                name=f"spiritscout-{session.session_id}",
            )
            if session.status != "running":
                session.task.cancel()   # spawn 瞬间被并发 stop：立即取消

    async def aclose(self) -> None:
        """应用关停：停监督协程，再停所有未终态会话（幂等）。"""
        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
        for session in list(self._sessions.values()):
            if session.status not in TERMINAL_STATES:
                await self.stop(session.session_id)

    async def create(self, payload: dict[str, Any], plan: LaunchPlan) -> Session:
        """创建会话并入队启动（payload 已经 app 层合规校验；plan 由 launch 装配）。"""
        # 热生效：Web 端改模型注册表后，新会话立即用新配置；
        # 加载失败显式报错（不静默退回旧配置）。先于建目录，失败不留痕。
        from spiritscout.config import load_model_config
        models = vault.apply_keys(
            load_model_config(self._cfg.models_config_path))
        if not models:
            raise SpiritScoutError(
                f"模型注册表为空: {self._cfg.models_config_path}（"
                "请在 Web「模型」面板或配置文件中至少注册一个云端模型）")
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        session = Session(
            session_id=session_id,
            mode=str(payload["mode"]), target=plan.target,
            workspace=self.root / session_id,
            title=str(payload.get("title") or "").strip()[:80],
            request={k: v for k, v in payload.items()
                     if k not in ("challenges_yaml", "rules_yaml")},
            created_at=time.time(),
        )
        session.workspace.mkdir(parents=True, exist_ok=False)
        self._sessions[session.session_id] = session
        self._persist(session)

        cfg = self._session_cfg(session, payload.get("budget_tokens"))
        session.approvals = ApprovalBroker(AuditLogger(cfg.audit_log_path))
        session.ring = RingLedger(AuditLogger(cfg.audit_log_path))
        no_llm = bool(payload.get("no_llm", False))
        self.start()            # 直接使用（非 Web lifespan）时懒启动监督协程
        await self._queue.put((session, cfg, plan, no_llm, models))
        return session

    async def _run(self, session: Session, cfg: SystemConfig,
                   plan: LaunchPlan, no_llm: bool,
                   models: list[ModelEntry]) -> None:
        """会话执行体：按模式分发 runtime 入口，落终态与摘要。"""
        from spiritscout.runtime import (
            run_competition_engagement,
            run_ctf_engagement,
            run_engagement,
        )

        mode = RunMode(session.mode)
        async with self._sem:
            try:
                # 开工预检：模型全链不可用即熔断（LLM-first：无脑不开工）
                if not no_llm and models:
                    await _preflight_models(
                        cfg, models, AuditLogger(cfg.audit_log_path))
                # 智能模式：LLM 语义路由复核装配层的结构化兜底判定
                if plan.mode_auto and plan.mission and not no_llm and models:
                    audit = AuditLogger(cfg.audit_log_path)
                    routed = await _llm_route_mode(
                        cfg, models, plan.mission, mode, audit)
                    if routed is not None and routed != mode and plan.source_request:
                        try:
                            plan = prepare_launch(LaunchRequest(**{
                                **plan.source_request.__dict__, "mode": routed}))
                            mode = routed
                            session.mode = routed.value
                            session.target = plan.target
                        except SpiritScoutError as exc:
                            # 改判缺必需输入（如判渗透赛但无赛制 YAML）：
                            # 不静默跟随——留痕并按兜底执行
                            audit.log("decision.change", "mode_router", {
                                "action": "llm_mode_route_rejected",
                                "llm_mode": routed.value, "kept": mode.value,
                                "reason": str(exc)[:300]})
                agent_models = agentcfg.read_assignments(cfg.agent_models_path)
                if mode == RunMode.CTF:
                    result = await run_ctf_engagement(
                        cfg=cfg, models=models,
                        authorization=plan.authorization,
                        challenges=plan.challenges,
                        flag_patterns=plan.flag_patterns,
                        enable_llm_planning=not no_llm,
                        agent_models=agent_models,
                        approval_gate=session.approvals.ask,
                        ring=session.ring)
                elif mode == RunMode.COMPETITION:
                    result = await run_competition_engagement(
                        cfg=cfg, models=models,
                        authorization=plan.authorization,
                        rules=plan.rules, challenges=plan.challenges,
                        enable_llm_planning=not no_llm,
                        agent_models=agent_models,
                        approval_gate=session.approvals.ask,
                        ring=session.ring)
                else:
                    result = await run_engagement(
                        cfg=cfg, models=models,
                        authorization=plan.authorization,
                        target=plan.target,
                        enable_llm_planning=not no_llm,
                        mission=plan.mission,
                        agent_models=agent_models,
                        approval_gate=session.approvals.ask,
                        ring=session.ring)
                session.status = "completed"
                session.summary = {
                    "run": result.run_summary,
                    "verify": result.verify_stats,
                    "flags": result.flag_stats,
                    "report": result.report_path,
                }
            except asyncio.CancelledError:
                # stop() 主动取消：runtime finally 已关闭 infra；落 stopped 终态后
                # 正常结束（不向上 re-raise——任务目的即被终止）
                session.status = "stopped"
            except Exception as exc:  # noqa: BLE001 —— 会话故障隔离：单会话失败不影响其他会话
                session.status = "failed"
                session.error = f"{type(exc).__name__}: {exc}"[:500]
            finally:
                session.finished_at = time.time()
                self._persist(session)

    async def stop(self, session_id: str) -> Session | None:
        """幂等停止：取消执行任务（数据保留）；已终态直接返回。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.approvals.cancel_all()
        task = session.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if session.status not in TERMINAL_STATES:
            session.status = "stopped"
            session.finished_at = time.time()
            self._persist(session)
        return session

    def rename(self, session_id: str, title: str) -> Session | None:
        """重命名会话（任意状态可改；空串 = 恢复自动摘要名）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.title = title.strip()[:80]
        self._persist(session)
        return session

    async def delete(self, session_id: str, *, confirm: bool = False) -> Session | None:
        """删除会话：仅终态可删且需 confirm；连工作区目录一并移除。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.status not in TERMINAL_STATES:
            raise SpiritScoutError(
                f"会话 {session_id} 仍在 {session.status}，请先停止再删除")
        if not confirm:
            raise SpiritScoutError("删除会话需显式 confirm=true（连工作区一并移除）")
        self._sessions.pop(session_id, None)
        shutil.rmtree(session.workspace, ignore_errors=True)
        return session
