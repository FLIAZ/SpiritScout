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
from spiritscout.policy.questions import QuestionBroker
from spiritscout.web import agentcfg, vault


async def _llm_route_mode(cfg: SystemConfig, models: list[ModelEntry],
                          mission: str, heuristic: RunMode,
                          audit: AuditLogger) -> tuple[RunMode | None, str]:
    """LLM 语义路由（兼任开工预检）：一次调用裁决「模式 + 意图」。

    意图四分类（判断完全在提示词内，后端不写死规则）：
    - task：明确的可执行任务（有具体目标/题目/清晰的行动指令）→ 开工派工；
    - task_with_rules：任务与规则同时下发 → 规则入库后开工；
    - rules：规则/流程/手册说明（含占位凭证），尚无题目或目标 →
      灵枢确认规则并等待，绝不派工（手册里的示例地址不是目标）；
    - dialogue：普通对话/提问 → 灵枢回复，不开工。

    - 网关调用失败 → 抛出：等同模型预检失败，会话不开工并明示原因；
    - 应答解析失败 → 返回 (None, "task")：按装配层结构化兜底走。
    低思考档提示：分类是小判断，不值得重推理（模型未声明低档时忽略）。
    """
    from spiritscout.gateway import ModelGateway, ModelRegistry
    from spiritscout.gateway.backends import extract_json

    gateway = ModelGateway(ModelRegistry(models), config=cfg.gateway, audit=audit)
    try:
        resp = await gateway.chat(
            [{"role": "system", "content": (
                "你是灵枢的任务路由与意图判读。通读用户输入，判断两件事："
                "属于哪类安全测试分支、用户此刻的意图类型，只输出 JSON。"
                "ROLE: mode_router")},
             {"role": "user", "content": json.dumps({
                 "mission": mission[:3000],
                 "branches": {
                     "src": "授权安全测试/渗透：对站点、系统或业务做侦察→漏洞"
                            "探测→核验→报告（含 SRC 漏洞挖掘场景）",
                     "ctf": "CTF 解题：目标是具体赛题、以夺取 flag 为目的",
                     "competition": "渗透赛/攻防对抗：有赛制、排名、AWD 等规则"},
                 "intent_types": {
                     "task": "明确的可执行任务：有具体目标/题目/清晰的行动指令",
                     "task_with_rules": "任务与规则同时下发：既有目标或行动指令，"
                                        "又附带流程/约束/API 约定等执行规则",
                     "rules": "规则/流程/手册/约定：讲的是「应该怎么做」——接口"
                              "清单、步骤、约束、终止条件，或凭证为占位符；"
                              "没有可直接执行的具体目标或题目",
                     "dialogue": "普通对话/提问/闲聊：无需派工，灵枢正常回复"},
                 "fallback_guess": heuristic.value,
                 "answer": '{"mode": "src|ctf|competition", '
                           '"intent": "task|task_with_rules|rules|dialogue", '
                           '"rationale": "一句话"}',
             }, ensure_ascii=False)}],
            task_type="classification", response_format="json",
            reasoning_effort="low",
            timeout_seconds=min(45, cfg.gateway.request_timeout_seconds))
    except Exception as exc:
        audit.log("system.lifecycle", "mode_router", {
            "action": "model_preflight_failed", "error": str(exc)[:300]})
        raise SpiritScoutError(
            f"模型不可用，会话未开工：{exc}。请在「模型」页检查接入配置"
            f"与密钥后重新发起") from exc
    try:
        data = extract_json(resp.content)
        mode = RunMode(str((data or {}).get("mode", "")).strip().lower())
    except (ValueError, TypeError) as exc:
        audit.log("decision.change", "mode_router", {
            "action": "llm_mode_route_fallback",
            "heuristic": heuristic.value, "reason": f"应答不可解析: {exc}"[:160]})
        return None, "task"
    intent = str((data or {}).get("intent", "task")).strip().lower()
    if intent not in ("task", "task_with_rules", "rules", "dialogue"):
        intent = "task"
    audit.log("decision.change", "mode_router", {
        "action": "llm_mode_route", "llm_mode": mode.value,
        "intent": intent, "heuristic": heuristic.value,
        "rationale": str((data or {}).get("rationale", ""))[:200]})
    return mode, intent

TERMINAL_STATES = frozenset({"completed", "failed", "stopped", "interrupted"})


async def _master_reply(cfg: SystemConfig, models: list[ModelEntry],
                        user_text: str, audit, *, kind: str) -> str:
    """灵枢对非派工输入的应答（dialogue 正常对话 / rules 规则确认）。

    LLM 不可用时回退固定话术——用户至少能看到确定的反馈。
    """
    from spiritscout.gateway import ModelGateway, ModelRegistry

    system = ("你是灵枢。用中文写一段简短、自然的回复。"
              if kind == "dialogue" else
              "你是灵枢。用户刚下发了执行规则/流程手册（不是具体题目或目标）。"
              "写一段简短确认：记住了哪些关键约定（3~5 条要点），正在等待"
              "用户提供什么（题目/目标/真实凭证）。")
    try:
        gateway = ModelGateway(ModelRegistry(models), config=cfg.gateway, audit=audit)
        resp = await gateway.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user_text[:4000]}],
            task_type="meta_planning", max_tokens=1024,
            timeout_seconds=min(60, cfg.gateway.request_timeout_seconds))
        text = (resp.content or "").strip()
        if text:
            return text[:2500]
    except (SpiritScoutError, OSError, ValueError, TypeError) as exc:
        # 应答失败回退固定话术，输入已持久化不丢
        audit.log("master.reply_failed", "master", {"error": str(exc)[:200]})
    if kind == "rules":
        return ("已收到执行规则并完整记录：灵枢将严格按该约定行事。"
                "请下发具体题目或目标（以及真实凭证），我再开工。")
    return ("已收到你的消息。当前没有可执行的任务指令——下发目标或题目"
            "后我立即安排。")


def _preflight_models(cfg: SystemConfig, models: list[ModelEntry],
                      audit: AuditLogger) -> None:
    """开工前预检（静态，毫秒级）：模型注册表非空。

    连通性不再用 LLM 调用证明——重推理端点上一次「就绪检查」曾花 10s+，
    多题并发时是纯浪费；语义路由分支由路由调用兼任预检，静态路由分支的
    连通性由首个真实模型调用证明（失败在时间线上即时可见）。
    """
    if not models:
        audit.log("system.lifecycle", "preflight", {
            "action": "model_preflight_failed", "error": "模型注册表为空"})
        raise SpiritScoutError(
            "模型注册表为空，会话未开工。请在「模型」页配置至少一个模型后重试")


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
    phase: str = "queued"        # 细粒度启动/执行阶段，避免把启动等待误报成业务覆盖
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    # 高风险动作即时确认队列（G5 交互形态；进程内状态，会话终态即失效）
    approvals: ApprovalBroker = field(
        default_factory=ApprovalBroker, repr=False, compare=False)
    # 模型发起的用户询问队列（ask_user 工具后端；问题与选项由模型撰写）
    questions: QuestionBroker = field(
        default_factory=QuestionBroker, repr=False, compare=False)
    # 环形协同台账：子 Agent 已尝试登记 + 灵枢督导建议（进程内，随会话消亡）
    ring: RingLedger = field(
        default_factory=RingLedger, repr=False, compare=False)
    # 用户会话中发来的意见与附件记录（进程内收件箱；灵枢在最近的决策点
    # 取走消费——派发期督导或轮末复盘，消费动作落审计事件）
    user_inbox: list[dict[str, Any]] = field(
        default_factory=list, repr=False, compare=False)
    # 启动计划（进程内持有，续传复用同一授权/赛题载荷）
    plan: Any = field(default=None, repr=False, compare=False)
    # 原始启动参数（含赛题/赛制 YAML，落 meta.json）——控制台重启后据此
    # 确定性重建启动计划，续传不因重启被拦
    launch_request: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False)
    # 用户历史意见（最新在后，封顶保留；拓扑图与状态面展示用）
    opinions: list[dict[str, Any]] = field(
        default_factory=list, repr=False, compare=False)
    # 续传前的终态（续传失败时恢复，原成果不丢）
    resume_prev_status: str | None = field(
        default=None, repr=False, compare=False)

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
            "phase": self.phase,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "error": self.error,
        }
        # 仪表盘总览的轻量指标（从运行摘要提取，不走完整 summary）
        run = (self.summary or {}).get("run", {}) if isinstance(self.summary, dict) else {}
        usage = run.get("model_usage") or {}
        out["tokens"] = usage.get("used_tokens", 0)
        out["cost_usd"] = usage.get("used_usd", 0.0)
        out["rounds"] = run.get("rounds_executed", 0)
        out["flag_stats"] = run.get("flag_stats")
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
            tuple[Session, SystemConfig, LaunchPlan, bool, list[ModelEntry], bool]] = \
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
                launch_request=data.get("launch_request", {}),
                opinions=data.get("opinions", []),
                status=data.get("status", "interrupted"),
                phase=data.get("phase", "interrupted"),
                created_at=data.get("created_at", 0.0),
                started_at=data.get("started_at"),
                finished_at=data.get("finished_at"),
                summary=data.get("summary", {}), error=data.get("error", ""),
            )
            if session.status not in TERMINAL_STATES:
                session.status = "interrupted"
                session.phase = "interrupted"
                session.finished_at = session.finished_at or time.time()
                self._persist(session)
            self._sessions[session.session_id] = session

    def _persist(self, session: Session) -> None:
        session.meta_path.write_text(json.dumps({
            "session_id": session.session_id, "mode": session.mode,
            "target": session.target, "title": session.title,
            "request": session.request,
            "launch_request": session.launch_request,
            "opinions": session.opinions[-8:],
            "status": session.status, "phase": session.phase,
            "created_at": session.created_at,
            "started_at": session.started_at, "finished_at": session.finished_at,
            "summary": session.summary, "error": session.error,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self) -> list[Session]:
        return sorted(self._sessions.values(),
                      key=lambda s: s.created_at, reverse=True)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def _set_phase(self, session: Session, phase: str,
                   audit: AuditLogger | None = None) -> None:
        """持久化用户可见阶段；审计存在时同步留痕。"""
        if session.phase == phase:
            return
        session.phase = phase
        self._persist(session)
        if audit is not None:
            audit.log("system.lifecycle", "runtime", {
                "action": "phase_changed", "phase": phase})

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
            session, cfg, plan, no_llm, models, resume = await self._queue.get()
            try:
                if resume:
                    if session.status != "running":
                        continue        # 续传排队期间状态被并发改动（如再次停止）
                elif session.status != "created":
                    continue            # 排队期间已被停止/删除
                session.status = "running"
                session.phase = "starting"
                session.started_at = session.started_at or time.time()
                self._persist(session)
                session.task = asyncio.create_task(
                    self._run(session, cfg, plan, no_llm, models, resume),
                    name=f"spiritscout-{session.session_id}",
                )
                session.task.add_done_callback(
                    lambda task, current=session: self._settle_task(current, task))
                if session.status != "running":
                    session.task.cancel()   # spawn 瞬间被并发 stop：立即取消
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 监督器不能因单会话退出
                session.status = "failed"
                session.phase = "failed"
                session.error = f"启动监督失败：{type(exc).__name__}: {exc}"[:500]
                session.finished_at = time.time()
                self._persist(session)
            finally:
                self._queue.task_done()

    def _settle_task(self, session: Session, task: asyncio.Task) -> None:
        """兜住执行协程 try 块之外的异常，禁止会话永久留在 running。"""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            exc = None
        if session.status in TERMINAL_STATES:
            return
        session.status = "failed" if exc is not None else "stopped"
        session.phase = session.status
        if exc is not None:
            session.error = f"执行任务异常退出：{type(exc).__name__}: {exc}"[:500]
        session.finished_at = time.time()
        self._persist(session)

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

    async def create(self, payload: dict[str, Any], plan: LaunchPlan, *,
                     staged_files: list[Path] | None = None) -> Session:
        """创建会话并入队启动（payload 已经 app 层合规校验；plan 由 launch 装配）。

        staged_files：用户任务下达前暂存的附件——在此移入会话工作区并注入
        赛题载荷（必须在入队前完成，会话任务可能即刻开工）。
        """
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
                     if k not in ("challenges_yaml", "rules_yaml", "upload_ids")},
            created_at=time.time(),
        )
        session.workspace.mkdir(parents=True, exist_ok=False)
        session.plan = plan
        session.launch_request = {k: v for k, v in payload.items()
                                  if k != "upload_ids"}
        self._sessions[session.session_id] = session
        self._persist(session)

        cfg = self._session_cfg(session, payload.get("budget_tokens"))
        session.approvals = ApprovalBroker(AuditLogger(cfg.audit_log_path))
        session.questions = QuestionBroker(AuditLogger(cfg.audit_log_path))
        session.ring = RingLedger(AuditLogger(cfg.audit_log_path))
        no_llm = bool(payload.get("no_llm", False))
        # 暂存附件材料化：移入工作区 attachments/ + 注入无附件的赛题载荷
        # （方向判类/附件分析工具/题面视图直接可用）+ 种入灵枢收件箱
        if staged_files:
            dest_dir = session.workspace / "attachments"
            dest_dir.mkdir(parents=True, exist_ok=True)
            audit = AuditLogger(cfg.audit_log_path)
            records: list[dict[str, Any]] = []
            for src in staged_files:
                dest = dest_dir / src.name
                n = 2
                while dest.exists():                # 同名不覆盖
                    dest = dest_dir / f"{src.stem}-{n}{src.suffix}"
                    n += 1
                shutil.move(str(src), str(dest))
                records.append({"kind": "attachment", "name": dest.name,
                                "path": str(dest), "size": dest.stat().st_size})
                audit.log("user.attachment", "user", {
                    "name": dest.name, "size": records[-1]["size"],
                    "path": str(dest)})
            session.user_inbox.extend(records)
            for ch in plan.challenges:
                if not ch.get("attachments"):
                    ch["attachments"] = [r["path"] for r in records]
            for updir in {p.parent for p in staged_files}:
                shutil.rmtree(updir, ignore_errors=True)
        self.start()            # 直接使用（非 Web lifespan）时懒启动监督协程
        await self._queue.put((session, cfg, plan, no_llm, models, False))
        return session

    def _rebuild_plan(self, session: Session) -> LaunchPlan | None:
        """控制台重启后从持久化的启动参数重建计划（同一输入 → 同一装配）。"""
        req = session.launch_request or {}
        if not req:
            return None
        try:
            # 自动模式且尚未创建黑板，说明旧进程在真正开工前中断；应按原始
            # 用户意图重新判定，而不是把当时的临时兜底模式永久钉死。
            untouched_auto = bool(req.get("mode_auto")) and not session.bb_path.exists()
            plan = prepare_launch(LaunchRequest(
                mode=None if untouched_auto else RunMode(session.mode),
                target=req.get("target") or None,
                extra_scope=list(req.get("scope") or []),
                platform=req.get("platform") or None,
                allow_sensitive=bool(req.get("allow_sensitive", False)),
                rps=float(req.get("rps", 10.0)),
                flag_format=req.get("flag_format") or None,
                challenges_yaml=req.get("challenges_yaml") or None,
                rules_yaml=req.get("rules_yaml") or None,
                accept_rule_conflicts=bool(req.get("accept_rule_conflicts", False)),
                mission=req.get("mission") or None,
                has_uploads=(session.workspace / "attachments").is_dir(),
            ))
        except (SpiritScoutError, TypeError, ValueError):
            return None
        # 工作区里用户上传过的附件重新挂到无附件的赛题上（与创建时同规则）
        att = session.workspace / "attachments"
        if att.is_dir() and plan.challenges:
            files = sorted(str(p) for p in att.iterdir() if p.is_file())
            if files:
                for ch in plan.challenges:
                    if not ch.get("attachments"):
                        ch["attachments"] = files
        return plan

    def _cancel_stale_tasks(self, session: Session) -> int:
        """续传前归档上一次被中断的黑板任务。

        进程停止时子 Agent 已不存在，黑板中仍为 pending/running 的任务不可能
        继续执行。将其原位更新为 cancelled，避免状态页和后续报告把历史残留
        当成当前活跃任务。
        """
        if not session.bb_path.exists():
            return 0
        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import AgentType, BlackboardLayer

        active = {"pending", "dispatched", "running"}
        changed = 0
        bb = Blackboard(session.bb_path, bus=None)
        try:
            for rec in bb.query(BlackboardLayer.TASK_STATE, limit=10000):
                data = rec["data"]
                if "dag_snapshot" in data:
                    dirty = False
                    for node in data.get("dag_snapshot", []):
                        if node.get("status") in active:
                            node["status"] = "cancelled"
                            dirty = True
                    if dirty:
                        bb.write(
                            BlackboardLayer.TASK_STATE, rec["record_id"], data,
                            agent_type=AgentType.MASTER,
                            updated_by="session-resume",
                            expected_version=rec["version"],
                        )
                    continue
                task = data.get("task") or {}
                if task.get("status") not in active:
                    continue
                task["status"] = "cancelled"
                for route in data.get("routes", []):
                    if route.get("status") in active:
                        route["status"] = "cancelled"
                summary = data.setdefault("summary", {})
                summary["interrupted"] = True
                summary["reason"] = "上一次会话在任务终态写入前停止"
                bb.write(
                    BlackboardLayer.TASK_STATE, rec["record_id"], data,
                    agent_type=AgentType.MASTER,
                    updated_by="session-resume",
                    dedup_key=f"task|{rec['record_id']}",
                    expected_version=rec["version"],
                )
                changed += 1
        finally:
            bb.close()
        if changed:
            AuditLogger(session.audit_path).log(
                "system.lifecycle", "session-resume", {
                    "action": "stale_tasks_cancelled", "count": changed})
        return changed

    async def resume(self, session_id: str) -> Session:
        """续传：终态会话带用户新来的意见重新开工（同一工作区/审计/Flag 库）。

        复用启动计划——进程内持有则直接用；控制台重启过则从 meta.json 的
        原始启动参数确定性重建。CTF 已破题的赛题不再重解，未破题的与用户
        意见一起进入新一轮决策环。
        """
        from spiritscout.config import load_model_config
        session = self._sessions.get(session_id)
        if session is None:
            raise SpiritScoutError(f"会话不存在: {session_id}")
        if session.status not in TERMINAL_STATES:
            raise SpiritScoutError("会话仍在运行中，意见将直接进入当前决策环")
        if session.plan is None:
            session.plan = self._rebuild_plan(session)
        if session.plan is None:
            raise SpiritScoutError(
                "该会话缺少启动信息（由不支持续传的旧版本创建），无法续传——"
                "请基于现有报告与审计新开任务")
        # 开工前中断的自动模式会在 _rebuild_plan 中重新确定装配模式。
        if not session.bb_path.exists():
            session.mode = session.plan.authorization.mode.value
            session.target = session.plan.target
        self._cancel_stale_tasks(session)
        models = vault.apply_keys(load_model_config(self._cfg.models_config_path))
        if not models:
            raise SpiritScoutError(
                f"模型注册表为空: {self._cfg.models_config_path}（请在「模型」页检查）")
        session.resume_prev_status = session.status
        session.status = "running"
        session.phase = "queued"
        session.finished_at = None
        session.error = ""
        self._persist(session)
        cfg = self._session_cfg(session, session.request.get("budget_tokens"))
        session.approvals = ApprovalBroker(AuditLogger(cfg.audit_log_path))
        session.questions = QuestionBroker(AuditLogger(cfg.audit_log_path))
        session.ring = RingLedger(AuditLogger(cfg.audit_log_path))
        no_llm = bool(session.request.get("no_llm", False))
        self.start()
        await self._queue.put((session, cfg, session.plan, no_llm, models, True))
        return session

    async def _run(self, session: Session, cfg: SystemConfig,
                   plan: LaunchPlan, no_llm: bool,
                   models: list[ModelEntry], resume: bool = False) -> None:
        """会话执行体：按模式分发 runtime 入口，落终态与摘要。

        resume=True 为续传运行：同一工作区/审计/Flag 库重开一轮，
        模式不再改判（会话模式已定），失败时恢复原终态（原成果不丢）。
        """
        try:
            async with self._sem:
                # 导入、模式解析和信号量等待都必须在故障隔离范围内；任何一步
                # 出错都落终态，不能留下只有 meta.json 的假 running 会话。
                from spiritscout.runtime import (
                    run_competition_engagement,
                    run_ctf_engagement,
                    run_engagement,
                )

                mode = RunMode(session.mode)
                # 开工即有声：先落一条事件让用户立刻看到灵枢动起来了
                # （随后的模型调用期间时间线不再静默）
                audit = AuditLogger(cfg.audit_log_path)
                audit.log("system.lifecycle", "runtime", {
                    "action": "session_kickoff", "resume": resume})
                routed = None
                intent = "task"
                if not no_llm and models:
                    if not resume and plan.needs_mode_routing and plan.mission:
                        # 语义路由兼任开工预检（开工前唯一模型调用，调用成功
                        # 即模型全链可用），不为预检单付一次串行等待
                        self._set_phase(session, "mode_routing", audit)
                        routed, intent = await _llm_route_mode(
                            cfg, models, plan.mission, mode, audit)
                    else:
                        self._set_phase(session, "model_preflight", audit)
                        _preflight_models(cfg, models, audit)
                # 意图分流（LLM 裁决，后端只搬运结果）：
                # dialogue/rules 不派工——灵枢回复后收尾，规则持久化，
                # 等用户下发真正的任务（发送消息即带现场续传）
                if intent in ("rules", "dialogue", "task_with_rules") \
                        and plan.mission:
                    if intent != "dialogue":
                        session.standing_rules = plan.mission
                        if isinstance(session.launch_request, dict) \
                                and session.launch_request.get("mission"):
                            session.launch_request["mission"] = ""
                    self._persist(session)
                    self._set_phase(session, "master_reply", audit)
                    reply = await _master_reply(
                        cfg, models, plan.mission, audit,
                        kind="rules" if intent == "rules" else "dialogue")
                    audit.log("master.reply", "master", {"text": reply[:4000]})
                    session.status = "completed"
                    session.phase = "completed"
                    session.finished_at = time.time()
                    session.summary = {
                        "note": ("已记录执行规则，等待题目/目标下发"
                                 if intent == "rules" else
                                 "已回复用户；未识别到可执行任务"),
                        "intent": intent}
                    self._persist(session)
                    return
                if routed is not None and routed != mode and plan.source_request:
                    try:
                        plan = prepare_launch(LaunchRequest(**{
                            **plan.source_request.__dict__, "mode": routed}))
                        mode = routed
                        session.mode = routed.value
                        session.target = plan.target
                        cfg.mode = routed
                    except SpiritScoutError as exc:
                        # 改判缺必需输入（如判渗透赛但无赛制 YAML）：
                        # 不静默跟随——留痕并按兜底执行
                        audit.log("decision.change", "mode_router", {
                            "action": "llm_mode_route_rejected",
                            "llm_mode": routed.value, "kept": mode.value,
                            "reason": str(exc)[:300]})
                self._set_phase(session, "runtime_starting", audit)
                agent_models = agentcfg.read_assignments(cfg.agent_models_path)
                self._set_phase(session, "executing", audit)
                if mode == RunMode.CTF:
                    result = await run_ctf_engagement(
                        cfg=cfg, models=models,
                        authorization=plan.authorization,
                        challenges=plan.challenges,
                        flag_patterns=plan.flag_patterns,
                        enable_llm_planning=not no_llm,
                        agent_models=agent_models,
                        approval_gate=session.approvals.ask,
                        ring=session.ring,
                        user_inbox=session.user_inbox,
                        question_broker=session.questions,
                        resume=resume)
                elif mode == RunMode.COMPETITION:
                    result = await run_competition_engagement(
                        cfg=cfg, models=models,
                        authorization=plan.authorization,
                        rules=plan.rules, challenges=plan.challenges,
                        enable_llm_planning=not no_llm,
                        agent_models=agent_models,
                        approval_gate=session.approvals.ask,
                        ring=session.ring,
                        user_inbox=session.user_inbox,
                        question_broker=session.questions)
                else:
                    result = await run_engagement(
                        cfg=cfg, models=models,
                        authorization=plan.authorization,
                        target=plan.target,
                        enable_llm_planning=not no_llm,
                        mission=plan.mission,
                        agent_models=agent_models,
                        approval_gate=session.approvals.ask,
                        ring=session.ring,
                        user_inbox=session.user_inbox,
                        question_broker=session.questions)
                session.status = "completed"
                session.phase = "completed"
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
            session.phase = "stopped"
        except Exception as exc:  # noqa: BLE001 —— 会话故障隔离：单会话失败不影响其他会话
            if resume and session.resume_prev_status:
                # 续传未开工/中途失败：恢复原终态，原成果与摘要不丢
                session.status = session.resume_prev_status
                session.phase = session.status
                session.error = f"续传未完成：{type(exc).__name__}: {exc}"[:500]
            else:
                session.status = "failed"
                session.phase = "failed"
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
        session.questions.cancel_all()
        task = session.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if session.status not in TERMINAL_STATES:
            session.status = "stopped"
            session.phase = "stopped"
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
