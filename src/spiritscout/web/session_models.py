"""会话数据与进程内交互状态；生命周期由 SessionManager 管理。"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from spiritscout.agents.ring import RingLedger
from spiritscout.config.settings import ModelEntry
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import BlackboardLayer
from spiritscout.core.pause import PauseGate
from spiritscout.gateway.usage import UsageSnapshot as _UsageSnapshot
from spiritscout.gateway.usage import read_usage
from spiritscout.policy.approvals import ApprovalBroker
from spiritscout.policy.questions import QuestionBroker

TERMINAL_STATES = frozenset({"completed", "failed", "stopped", "interrupted"})


DIALOGUE_HISTORY_MAX = 40


def _remember_reply(session: Session, reply: str) -> None:
    """灵枢应答落对话史（封顶保留）；用户消息由 /messages 端点随发随落。"""
    session.dialogue_history.append(
        {"role": "assistant", "content": reply or ""})
    del session.dialogue_history[:-DIALOGUE_HISTORY_MAX]


@dataclass
class Session:
    """一次渗透任务会话（会话隔离单元）。"""

    session_id: str
    mode: str
    target: str
    workspace: Path
    title: str = ""                                          # 会话名（可重命名；空 = 自动摘要）
    model: str = ""
    _model_entries: list[ModelEntry] | None = field(default=None, repr=False, compare=False)
    # 所属项目分区（'' = 未分类）；catalog 的成员行是事实源，meta 只做展示缓存
    project_id: str = ""
    # 小模型自动命名（创建后台生成；展示优先级：用户重命名 > auto_title > mission 首行）
    auto_title: str = ""
    request: dict[str, Any] = field(default_factory=dict)   # 启动参数（展示用）
    status: str = "created"      # created/running/completed/failed/stopped/interrupted
    phase: str = "queued"        # 细粒度启动/执行阶段，避免把启动等待误报成业务覆盖
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    # 用户下发的执行规则（rules/task_with_rules 意图入库）：随每次开工
    # 下发到灵枢与各智能体；跨轮次与续传保留
    standing_rules: str = ""
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
    execution_plan: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False)
    # 用户历史意见（最新在后，封顶保留；拓扑图与状态面展示用）
    opinions: list[dict[str, Any]] = field(
        default_factory=list, repr=False, compare=False)
    # 会话对话史（用户↔灵枢，最新在后）：首条即任务原文，续传轮以末尾
    # 为新输入，灵枢每次开口都带着它——大厂网页对话形态，会话任意时刻
    # 可从末尾接着聊。落 meta.json，跨重启保留；封顶见 DIALOGUE_HISTORY_MAX
    dialogue_history: list[dict[str, str]] = field(
        default_factory=list, repr=False, compare=False)
    # 续传前的终态（续传失败时恢复，原成果不丢）
    resume_prev_status: str | None = field(
        default=None, repr=False, compare=False)
    # 暂停门 + 用时扣账 + 执行间隙检测（进程内，随会话任务共持；
    # paused 态持久化到 meta.json，控制台重启后续传仍保持暂停）
    pause_gate: PauseGate = field(
        default_factory=PauseGate, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.questions.bind_context_path(self.bb_path)

    handled_user_messages: list[str] = field(default_factory=list, repr=False, compare=False)

    def remember_user_message(self, text: str, *, message_id: str = "",
                              attachments: list[dict[str, Any]] | None = None,
                              upload_ids: list[str] | None = None,
                              enqueue: bool = False) -> dict[str, Any]:
        """原话与附件共用稳定身份；待处理标记可从事实库恢复。"""
        from spiritscout.blackboard import Blackboard
        from spiritscout.gateway.world_state import (
            record_user_statement,
            sync_user_context,
        )

        statement_id = message_id or f"message-{uuid.uuid4().hex}"
        statement: dict[str, Any] = {"kind": "message", "text": text, "statement_id": statement_id}
        if attachments:
            statement["attachments"] = attachments
        if upload_ids:
            statement["upload_ids"] = upload_ids
        if enqueue:
            statement["delivery_pending"] = True
        blackboard = Blackboard(self.bb_path)
        try:
            sync_user_context(blackboard, dialogue_history=self.dialogue_history)
            context = blackboard.read(BlackboardLayer.TARGET_AUTH, "user-context") or {}
            prior = next((item for item in context.get("statements", [])
                          if item.get("statement_id") == statement_id), None)
            if prior is not None:
                if prior.get("text") != text or prior.get("upload_ids", []) != (upload_ids or []):
                    raise SpiritScoutError("消息标识已用于另一条消息")
                statement = dict(prior)
                extra = {**({"attachments": attachments} if attachments and not prior.get("attachments") else {}),
                         **({"delivery_pending": True} if enqueue else {})}
                if extra:
                    from spiritscout.core.exceptions import VersionConflict

                    while True:
                        value, version = blackboard.read_versioned(BlackboardLayer.TARGET_AUTH, "user-context")
                        entries = [{**item, **extra} if item.get("statement_id") == statement_id else item
                                   for item in value.get("statements", [])]
                        try:
                            blackboard.write(BlackboardLayer.TARGET_AUTH, "user-context", {**value, "statements": entries},
                                             expected_version=version, updated_by="user-context")
                            statement.update(extra)
                            break
                        except VersionConflict:
                            continue
            else:
                record_user_statement(blackboard, statement)
        finally:
            blackboard.close()
        if not any(item.get("statement_id") == statement_id for item in self.dialogue_history):
            entry: dict[str, Any] = {"role": "user", "content": text, "statement_id": statement_id}
            if attachments:
                entry["attachments"] = attachments
            self.dialogue_history.append(entry)
            del self.dialogue_history[:-DIALOGUE_HISTORY_MAX]
        if attachments:
            for entry in self.dialogue_history:
                if entry.get("statement_id") == statement_id:
                    entry["attachments"] = statement.get("attachments", attachments)
        if (enqueue and statement_id not in self.handled_user_messages
                and not any(item.get("statement_id") == statement_id for item in self.user_inbox)):
            self.user_inbox.append(statement)
        return statement

    def user_statement(self, message_id: str) -> dict[str, Any] | None:
        if not self.bb_path.exists():
            return None
        from spiritscout.blackboard import Blackboard

        blackboard = Blackboard(self.bb_path, readonly=True)
        try:
            context = blackboard.read(BlackboardLayer.TARGET_AUTH, "user-context") or {}
            return next((item for item in context.get("statements", [])
                         if item.get("statement_id") == message_id), None)
        finally:
            blackboard.close()

    def acknowledge_user_feed(self, items: list[dict[str, Any]]) -> None:
        ids = {item.get("statement_id") for item in items if item.get("statement_id")}
        self.user_inbox[:] = [item for item in self.user_inbox
                              if item.get("statement_id") not in ids and item not in items]
        self.handled_user_messages.extend(sorted(ids - set(self.handled_user_messages)))

    def restore_user_feed(self) -> None:
        if not self.bb_path.exists():
            return
        from spiritscout.blackboard import Blackboard

        blackboard = Blackboard(self.bb_path, readonly=True)
        try:
            mailbox = blackboard.read(BlackboardLayer.TARGET_AUTH, "user-mailbox") or {}
            self.handled_user_messages = list(mailbox.get("handled_ids", self.handled_user_messages))
            self.user_inbox[:] = [item for item in self.user_inbox
                                  if item.get("statement_id") not in self.handled_user_messages]
            context = blackboard.read(BlackboardLayer.TARGET_AUTH, "user-context") or {}
            pending = [item for item in context.get("statements", [])
                       if item.get("delivery_pending") and item.get("statement_id") not in self.handled_user_messages]
            known = {item.get("statement_id") for item in self.user_inbox}
            self.user_inbox.extend(item for item in pending if item.get("statement_id") not in known)
            history_ids = {item.get("statement_id") for item in self.dialogue_history}
            for item in pending:
                if item.get("statement_id") not in history_ids:
                    self.dialogue_history.append({"role": "user", "content": str(item.get("text") or ""),
                        "statement_id": item["statement_id"], **({"attachments": item["attachments"]}
                                                                if item.get("attachments") else {})})
            del self.dialogue_history[:-DIALOGUE_HISTORY_MAX]
        finally:
            blackboard.close()

    def has_runtime_state(self) -> bool:
        """用户原话可在开工前建库，不能再用文件存在推断业务已经启动。"""
        if not self.bb_path.exists():
            return False
        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import BlackboardLayer

        blackboard = Blackboard(self.bb_path, readonly=True)
        try:
            return any(row["record_id"] not in {"user-context", "user-mailbox"}
                       for row in blackboard.query(BlackboardLayer.TARGET_AUTH, limit=10000))
        finally:
            blackboard.close()

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

    def model_usage(self, *, include_calls: bool = True, call_fields: frozenset[str] | None = None) -> dict[str, Any]:
        """列表、状态页与仪表盘共用的用量视图；损坏快照回退到已保存汇总。"""
        live = read_usage(self.workspace / "usage.json", include_calls=include_calls, call_fields=call_fields)
        if live is not None:
            return live
        run = self.summary.get("run") if isinstance(self.summary, dict) else None
        stored = run.get("model_usage") if isinstance(run, dict) else None
        try:
            usage = _UsageSnapshot.model_validate(stored).model_dump()
        except ValidationError:
            usage = {}
        if usage.get("call_series"):
            from spiritscout.gateway.cache import cache_summary

            usage["cache"] = cache_summary(usage["call_series"])
        if not include_calls:
            usage.pop("call_series", None)
        elif call_fields is not None:
            usage["call_series"] = [{key: value for key, value in row.items() if key in call_fields}
                                    for row in usage.get("call_series", [])]
        return usage

    def report_path(self) -> Path | None:
        """最新报告文件；当前写作未完成或失败时不冒用旧报告。"""
        from spiritscout.ctf.report_jobs import status as report_status

        if report_status(self.workspace).get("status") in {"queued", "running", "failed"}:
            return None
        reports = self.workspace / "reports"
        if not reports.is_dir():
            return None
        candidates = sorted(reports.glob("*.md"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0] if candidates else None

    def display_title(self) -> str:
        """会话名：用户重命名优先，其次小模型自动命名，最后回到任务首行。"""
        if self.title:
            return self.title
        if self.auto_title:
            return self.auto_title
        mission = str(self.request.get("mission") or "").strip()
        if mission:
            first = mission.splitlines()[0].strip()
            return first[:42] + ("…" if len(first) > 42 else "")
        return self.target or self.session_id

    def brief(self, *, include_summary: bool = True,
              usage: dict[str, Any] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": self.session_id, "mode": self.mode,
            "title": self.display_title(), "renamed": bool(self.title),
            "model": self.model,
            "project_id": self.project_id,
            "target": self.target, "status": self.status,
            "phase": self.phase,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "error": self.error,
            "paused": self.pause_gate.paused,
            "excluded_seconds": round(self.pause_gate.excluded_seconds, 1),
        }
        binding = self.request.get("proxy_binding")
        if isinstance(binding, dict) and binding.get("id"):
            # 固定快照只投影展示字段，不回查会变化的代理池或暴露认证配置。
            out["proxy_route"] = {
                key: binding[key] for key in ("id", "name", "verification", "exit_ip")
                if key in binding
            }
            out["proxy_route"]["nodes"] = [
                {key: node[key] for key in ("name", "type", "server", "port") if key in node}
                for node in binding.get("nodes", []) if isinstance(node, dict)
            ]
        else:
            plan_id = self.request.get("proxy_plan_id") or self.launch_request.get("proxy_plan_id")
            out["proxy_route"] = {"id": "direct"} if plan_id == "direct" else None
        if self.request.get("side_agent"):
            source = self.request.get("side_context") or {}
            out["side_session"] = {"agent": self.request["side_agent"],
                "parent_session_id": source.get("parent_session_id"), "task_id": source.get("task_id"),
                "actor": source.get("actor")}
        # 仪表盘总览的轻量指标（从运行摘要提取，不走完整 summary）
        run = (self.summary or {}).get("run", {}) if isinstance(self.summary, dict) else {}
        usage = self.model_usage(include_calls=False) if usage is None else usage
        out["tokens"] = usage.get("used_tokens", 0)
        out["cost_usd"] = usage.get("used_usd", 0.0)
        # 运行中实时用量优先：网关逐笔持久化的 usage.db（兼容旧 usage.json）（列表/仪表盘随轮询跳动）
        out["rounds"] = run.get("rounds_executed", 0)
        # Flag 统计：CTF 结果在 summary["flags"]（运行摘要本身不带 flag_stats）；
        # 面板口径统一为 total/submitted/accepted/rejected
        flags = run.get("flag_stats") or (
            self.summary.get("flags") if isinstance(self.summary, dict) else None)
        if flags:
            flags = {**flags, "total": flags.get("total", flags.get("extracted", 0))}
        out["flag_stats"] = flags
        if include_summary:
            out["summary"] = self.summary
        return out
