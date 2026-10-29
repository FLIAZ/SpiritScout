"""主 Agent 的用户直达与实时督导通道。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from spiritscout.agents.master.contracts import MasterState
from spiritscout.agents.profiles import get_profile
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    Task,
    TaskStatus,
)
from spiritscout.gateway import chat_decision
from spiritscout.gateway.backends import extract_json
from spiritscout.gateway.context import (
    decision_memory,
    decision_messages,
    shared_context,
)


class MasterInteraction:
    _CHAT_INTERVAL = 1.0      # 消息轮询节拍（秒）——用户消息 1s 内直达灵枢
    _CHAT_HISTORY_MAX = 12    # 单次应答携带的对话窗口（上限；有会话对话史时取史末尾）

    async def _user_chat_watch(self) -> None:
        """用户 ↔ 灵枢的即时对话通道（与子 Agent 的派发/督导完全隔离）。

        用户会话中发来的消息 1 秒内直达灵枢，不等待子 Agent 的回合边界：
        灵枢在自己的独立对话里直接回答用户（reply 经审计流实时可见），
        需要子 Agent 调整方向或分析新附件时经环形台账下发建议——子 Agent
        在其下一回合消费（执行中的模型调用无法被打断，这是两条通道之间
        唯一的交接点）。通道失败不影响主链路。
        """
        assert self.ctx.audit is not None
        # 对话上下文：会话对话史（跨续传持久化，用户消息已由 /messages
        # 随发随落）优先——灵枢应答原地追加、封顶由会话层负责；无对话史
        # 的装配（测试/离线）退回进程内独立窗口，行为同旧
        seed = self.ctx.extra.get("dialogue_history")
        own_history = not isinstance(seed, list)
        history: list[dict[str, str]] = [] if own_history else seed
        while True:
            await asyncio.sleep(self._CHAT_INTERVAL)
            feed = self._drain_user_feed()
            if not feed:
                continue
            messages = [str(item.get("text") or "").strip()
                        for item in feed if isinstance(item, dict)]
            messages = [m for m in messages if m]
            attachments = [str(item.get("path"))
                           for item in feed if isinstance(item, dict)
                           and item.get("path")]
            if not messages and not attachments:
                continue
            reply, steering = await self._llm_user_chat(
                messages, attachments, history)
            if own_history:
                for message in messages:
                    history.append({"role": "user", "content": message[:1000]})
            if not reply:
                # 应答失败也要给用户可见的回执（意见内容已留在意见流不丢失）
                reply = ("已收到你的消息并转达给执行中的智能体；灵枢应答通道"
                         "暂时不可用，稍后会继续跟进。")
            history.append({"role": "assistant", "content": reply[:2000]})
            if own_history:
                del history[:-self._CHAT_HISTORY_MAX]
            self.ctx.audit.log("master.reply", self.agent_id, {
                "text": reply[:4000]})
            # 应答落黑板：拓扑图「你 → 灵枢」节点的回执数据源
            with contextlib.suppress(Exception):
                self.ctx.blackboard.write(
                    BlackboardLayer.TOPOLOGY, new_id("reply"),
                    {"kind": "reply", "text": reply[:1000],
                     "about": messages[0][:300] if messages else "",
                     "ts": time.time()},
                    agent_type=AgentType.MASTER, updated_by=self.agent_id,
                    dedup_key=f"reply|{time.time()}")
            for item in steering:
                self.ctx.ring.steer(item["agent"], item["hint"],
                                    rationale=item.get("rationale", ""),
                                    task_id=item.get("task_id", ""))
            if steering:
                self.ctx.audit.log("decision.change", self.agent_id, {
                    "action": "user_chat_steering", "count": len(steering)})

    async def _llm_user_chat(self, messages: list[str], attachments: list[str],
                             history: list[dict[str, str]]
                             ) -> tuple[str, list[dict[str, str]]]:
        """灵枢回答用户（独立上下文）：直接答复 + 可选的子 Agent 方向建议。"""
        roster = "；".join(
            f"{get_profile(agent_type).name}({agent_type.value})"
            for agent_type in self.ctx.agents)
        paused = bool(self.ctx.pause_gate is not None and self.ctx.pause_gate.paused)
        situation = self._engagement_status()
        payload = {
            "mission": self._mission()[:1000],
            "world_state": self._world_state(),
            "agents": [a.value for a in self.ctx.agents],
            "roster": roster,
            "engagement_paused": paused,
            "situation": situation,
            "active_tasks": situation.get("active_tasks", []),
            "chat_history": history[-self._CHAT_HISTORY_MAX:],
            "new_messages": messages,
            "attachments": attachments,
            "constraints": (
                "你是灵枢，安全测试负责人。用户在会话进行中直接向你发消息，"
                "这条对话与子 Agent 的执行互不干扰。engagement_paused 为真表示"
                "用户手动暂停了任务推进——你照常回答问题、聊方向，但不要在回复里"
                "承诺立即推进执行（恢复由用户操作）。situation 是从黑板读到的"
                "实时态势（进行到第几轮、各节点状态、已有成果计数与在途任务）——"
                "回答进度类问题以它为准，不要凭任务书或臆测说「刚开始」之类的话。"
                "reply 用简洁的中文直接"
                "回答用户：确认收到指导意见、回答疑问、简述当前态势与下一步"
                "打算。需要子 Agent 调整方向或分析新上传的附件时，同时在 "
                "steering 里给出建议（agent 必须取自 agents 列表，hint 一句话，"
                "涉及附件写明文件路径，子 Agent 可直接读取；hint 会随会话页"
                "展示给用户——用平实中文，不用 HAR、PoC 这类行话缩写）；"
                "无需调整则 steering 给空列表。"
                "针对具体任务的指令须填写 active_tasks 中的 task_id；仅当明确需要"
                "通知该类型所有任务时才填写 broadcast=true。"
                "返回 JSON：{\"reply\": str, "
                "\"steering\": [{\"agent\": str, \"task_id\": str, \"broadcast\": bool, \"hint\": str, "
                "\"rationale\": str}]}"),
        }
        try:
            resp = await chat_decision(
                self.ctx.gateway,
                decision_messages(self._system_prompt() + "\nROLE: user_chat", payload, 16000),
                memory=decision_memory(self, "user_chat"), audit=self.ctx.audit, actor=self.agent_id,
                task_type="meta_planning", response_format="json",
                reasoning_effort="low",   # 对话应答要快，不与重推理抢档
            )
            data = extract_json(resp.content) or {}
            reply = str(data.get("reply") or "").strip()[:4000]
            steering = self._target_steering(
                data.get("steering") or [], situation.get("active_tasks", []),
                allow_broadcast=True)
            return reply, steering
        except (SpiritScoutError, OSError, ValueError, TypeError) as exc:
            # 对话通道失败不影响主链路，但留痕可见
            self.ctx.audit.log("master.reply_failed", self.agent_id,
                               {"error": str(exc)[:200]})
            return "", []

    # ------------------------------------------------------------ 环形督导

    _STEER_INTERVAL = 2.0   # 观察节拍（秒）；无进展变化不调用 LLM

    def _drain_user_feed(self) -> list[dict[str, Any]]:
        """取走用户会话中发来的消息与附件记录（用户直达通道的消费点）。"""
        inbox = self.ctx.extra.get("user_inbox")
        if not inbox:
            return []
        from spiritscout.gateway.world_state import record_user_statement

        items = list(inbox)
        for item in items:
            if isinstance(item, dict) and item.get("text"):
                statement = {"kind": "message", "text": str(item["text"])}
                if item.get("statement_id"):
                    statement["statement_id"] = item["statement_id"]
                record_user_statement(self.ctx.blackboard, statement)
        inbox.clear()
        return items

    async def _steering_watch(
            self, task_ids: list[str],
            decisions: asyncio.Queue[dict[str, Any]] | None = None) -> None:
        """派发期并发督导：读各任务实时路线笔记 → LLM 判读全局 → 写方向建议。

        新补位任务通过共享名单进入观察；方向建议交子 Agent 权衡，竞赛暂缓
        裁定经队列交调度器；暂停派工、取消在途与释放实例由模型分别明确选择。
        用户消息不在这里消费——它们走独立的直达通道（_user_chat_watch），
        两条通道并行互不阻塞。
        """
        assert self.ctx.audit is not None
        last_fingerprint = ""
        while True:
            await asyncio.sleep(self._STEER_INTERVAL)
            digest = self._progress_digest(task_ids)
            if not digest["tasks"]:
                continue
            # 投递新指令本身不是现场进展，避免“发指令→摘要变化→再发指令”自激循环。
            facts = {**digest, "tasks": [{k: v for k, v in t.items() if k != "steering"}
                                          for t in digest["tasks"]]}
            fingerprint = json.dumps(facts, ensure_ascii=False, sort_keys=True)
            if fingerprint == last_fingerprint:
                continue
            last_fingerprint = fingerprint
            steering = await self._llm_steer(digest, decisions)
            for item in steering:
                self.ctx.ring.steer(item["agent"], item["hint"],
                                    rationale=item.get("rationale", ""),
                                    task_id=item.get("task_id", ""))

    def _progress_digest(self, task_ids: list[str]) -> dict[str, Any]:
        """本轮任务的实时进度摘要（子 Agent 随步上报的路线笔记）。"""
        bb = getattr(self.ctx, "blackboard", None)
        tasks: list[dict[str, Any]] = []
        for tid in task_ids:
            data = bb.read(BlackboardLayer.TASK_STATE, tid) if bb is not None else None
            if data is None:
                continue
            task = data.get("task", {})
            if task.get("status") not in {
                    TaskStatus.PENDING.value, TaskStatus.DISPATCHED.value,
                    TaskStatus.RUNNING.value}:
                continue
            routes = [
                {"route": r.get("name"), "status": r.get("status"),
                 "progress": r.get("progress"),
                 "notes": [str(n)[:500] for n in (r.get("notes") or [])[-3:]]}
                for r in data.get("routes", [])
            ]
            challenge = (task.get("payload") or {}).get("challenge") or {}
            tasks.append({"agent": task.get("agent_type"),
                          "task_id": tid,
                          "challenge": {k: challenge[k] for k in ("id", "title")
                                        if k in challenge},
                          "steering": self.ctx.ring.steerings_for(
                              task.get("agent_type"), task_id=tid) if self.ctx.ring else [],
                          "title": str(task.get("title", ""))[:80],
                          "routes": routes})
        platform = self.ctx.extra.get("platform")
        return {"tasks": tasks,
                "platform_progress": {k: v for k, v in platform.progress().items() if k != "updated_at"} if platform is not None
                and hasattr(platform, "progress") else {}}

    def _route_digest(self, state: MasterState) -> list[dict[str, Any]]:
        """本轮各子 Agent 路线的执行笔记摘要（任务结果流向复盘）。

        路线观察/结论是复盘判断「还有哪些方向没试过」的第一手依据；
        笔记显示「LLM 不可用/调查停摆」属基础设施问题，不是无解题空间。
        """
        bb = self.ctx.blackboard
        routes: list[dict[str, Any]] = []
        for spec in state["tasks_dispatched"]:
            if len(routes) >= 20:
                break
            try:
                task = Task.model_validate(spec["task"])
            except Exception:  # noqa: BLE001, S112 —— 脏数据跳过，不影响复盘
                continue
            if task.round_no != state["round_no"]:
                continue
            data = bb.read(BlackboardLayer.TASK_STATE, task.task_id)
            if data is None:
                continue
            for r in data.get("routes", []):
                if len(routes) >= 20:
                    break
                routes.append({
                    "agent": task.agent_type.value,
                    "node": str(task.payload.get("node_title", ""))[:60],
                    "route": r.get("name"), "status": r.get("status"),
                    "notes": [str(n)[:160] for n in (r.get("notes") or [])[-2:]],
                })
        return routes

    async def _llm_steer(
            self, digest: dict[str, Any],
            decisions: asyncio.Queue[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """LLM 判读全局进度，产出需调整方向的子 Agent 建议（白名单校验）。"""
        valid_agents = sorted({t["agent"] for t in digest["tasks"]
                               if t.get("agent")})
        if not valid_agents:
            return []
        try:
            payload: dict[str, Any] = {
                "action": "steer",
                "mission": self._mission()[:1000],
                "world_state": self._world_state(),
                "business_context": shared_context(self.ctx.blackboard),
                # 按任务 ID 编址，某条路线变化只追加该任务的补丁，不重发整批。
                "progress": {**digest, "tasks": {
                    t["task_id"]: {k: v for k, v in t.items() if k != "steering"}
                    for t in digest["tasks"] if t.get("task_id")}},
                "active_tasks": [{k: t.get(k) for k in ("task_id", "agent", "challenge")}
                                 for t in digest["tasks"]],
                "steering_status": {t["task_id"]: t.get("steering", [])
                                    for t in digest["tasks"] if t.get("task_id")},
                "competition": {
                    "challenges": [{k: ch[k] for k in
                                    ("id", "title", "difficulty", "score", "solved_count")
                                    if k in ch}
                                   for ch in (self.ctx.extra.get("competition_context") or {})
                                   .get("challenges", []) if isinstance(ch, dict)]},
                "agents": valid_agents,
                "constraints": (
                            "你在实时督导并行作业的子 Agent。依据各路线进度与最新"
                            "笔记判断：谁走进了死胡同（建议换方向）、谁发现了高价值"
                            "线索（建议深挖或提醒同伴接力）、谁在同质重复做功"
                            "（建议避开同伴已覆盖的入口）。竞赛中某题反复探测而无新增"
                            "有效信息时先建议换方法、补信息或专项接力。未得分与耗时较长"
                            "不等于无进展；有新证据或正在验证新假设时保留现场推进。"
                            "只对确有必要的 Agent 调整，一切正常返回空列表。"
                            "defer_challenges 默认只暂停新增派工，cancel_running=true 才取消在途工作；"
                            "确需腾名额或重置现场时明确 release_instance=true，系统先退场再关闭。"
                            "释放需比较当前进展、重建代价与其他题实际机会，不能为轮换而轮换。"
                            "reason 简短保留已排除方向、有效线索和回访条件。"
                            "不要按固定时长或回合数机械跳题。有实质突破可继续。返回 JSON："
                            "{\"steering\": [{\"agent\": 仅 agents 列表之一, "
                            "\"task_id\": active_tasks 中对应任务的 task_id, "
                            "\"hint\": 下一步指令, \"rationale\": 判断依据}], "
                            "\"defer_challenges\": [{\"id\": 进度中在途赛题ID, \"reason\": str, \"cancel_running\": bool, \"release_instance\": bool}]}"),
            }
            payload["constraints"] += (
                "每条指令必须指定 task_id，不能把某题指令发给所有同类型任务。"
                "steering.feedback 中 delivered 仅表示已投递，accepted 仅表示计划执行；"
                "completed/blocked 是子 Agent 自报，结合后续工具结果判断实际效果。"
                "若前次指令尚未奏效，结合反馈与新证据纠偏或暂缓，不要重复改写同一建议。"
                "只引用对应任务的可见事实，不把截断摘要当完整源码，不臆测已经具备的能力；"
                "以 platform_progress 的已完成题目为准，不能把已完成题当未尝试题。")
            resp = await chat_decision(
                self.ctx.gateway,
                decision_messages(self._system_prompt() + "\nROLE: collab_judge", payload, 16000),
                audit=self.ctx.audit, actor=self.agent_id,
                memory=decision_memory(self, "steering"),
                task_type="meta_planning", response_format="json",
                reasoning_effort=self._MASTER_EFFORT,
            )
            data = extract_json(resp.content) or {}
            active_ids = {str((t.get("challenge") or {}).get("id") or "")
                          for t in digest["tasks"]}
            raw_defer = data.get("defer_challenges") or []
            deferred = [i for i in (raw_defer if isinstance(raw_defer, list) else [])
                        if isinstance(i, dict) and str(i.get("id") or "") in active_ids
                        and i.get("id") and str(i.get("reason") or "").strip()]
            if deferred and decisions is not None:
                decisions.put_nowait({"defer_challenges": deferred})
            items = data.get("steering")
            if not isinstance(items, list):
                raise TypeError("steering 应答不是列表")
            return self._target_steering(items, digest["tasks"])
        except Exception:  # noqa: BLE001 —— 督导失败不拖垮派发
            return []

    def _target_steering(self, items: list[Any], tasks: list[dict[str, Any]], *,
                         allow_broadcast: bool = False) -> list[dict[str, str]]:
        """校验模型选定的收件任务；不把歧义指令静默广播给全部同类型任务。"""
        out = []
        valid_agents = {a.value for a in self.ctx.agents} if allow_broadcast else {
            t.get("agent") for t in tasks}
        for item in items:
            if (not isinstance(item, dict) or item.get("agent") not in valid_agents
                    or not isinstance(item.get("hint"), str) or not item["hint"].strip()):
                continue
            candidates = [t for t in tasks if t.get("agent") == item["agent"]]
            tid = str(item.get("task_id") or "")
            broadcast = allow_broadcast and item.get("broadcast") is True and not tid
            if not tid and not broadcast and len(candidates) == 1:
                tid = str(candidates[0].get("task_id") or "")
            if not broadcast and (not tid or tid not in {t.get("task_id") for t in candidates}):
                self.ctx.audit.log("decision.change", self.agent_id, {
                    "action": "steering_rejected", "agent": item["agent"],
                    "task_id": tid, "reason": "收件任务缺失、歧义或不属于该 Agent"})
                continue
            out.append({"agent": item["agent"], "task_id": tid,
                        "hint": item["hint"][:2000],
                        "rationale": str(item.get("rationale", ""))[:600]})
        return out

    # ------------------------------------------------------------ 节点：汇聚

