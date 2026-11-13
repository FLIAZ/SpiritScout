"""主 Agent 的用户直达与实时督导通道。"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from typing import Any

from spiritscout.agents.master.contracts import MasterState
from spiritscout.agents.master.evidence import (
    check_basis,
    execution_digest,
    platform_ref,
    record_ref,
)
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
        self._chat_lock = asyncio.Lock()
        delay = self._CHAT_INTERVAL
        while True:
            await asyncio.sleep(delay)
            try:
                ok = await self._respond_user_feed()
                delay = self._CHAT_INTERVAL if ok else min(30, delay * 2)
            except Exception as exc:  # noqa: BLE001
                self.ctx.audit.log("master.reply_failed", self.agent_id, {"error": str(exc)[:200]})
                delay = min(30, delay * 2)

    async def _respond_user_feed(self) -> bool:
        lock = getattr(self, "_chat_lock", None)
        if lock is None:
            lock = self._chat_lock = asyncio.Lock()
        async with lock:
            feed = self._drain_user_feed()
            if not feed:
                return True
            control = self.ctx.extra.get('scan_controller')
            if control is not None:
                batch = ':'.join(str(item.get('statement_id') or id(item)) for item in feed)
                await control.hold_for_user(batch)
            seed = self.ctx.extra.get("dialogue_history")
            history = seed if isinstance(seed, list) else getattr(self, "_chat_history", [])
            self._chat_history = history
            messages = [str(item.get("text") or "").strip() for item in feed if item.get("text")]
            attachments = [str(path) for item in feed for attachment in item.get("attachments", [])
                           if (path := attachment.get("path") if isinstance(attachment, dict) else attachment)]
            attachments.extend(str(item["path"]) for item in feed if item.get("path"))
            result = await self._llm_user_chat(messages, attachments, history)
            if result is None:
                batch = tuple(item.get("statement_id", str(id(item))) for item in feed)
                if getattr(self, "_chat_failure_batch", None) != batch:
                    self._chat_failure_batch = batch
                    self.ctx.audit.log("master.reply", self.agent_id, {
                        "text": "消息及附件已保存，当前回复通道未完成处理；尚未确认转达，会继续重试。"})
                return False
            reply, steering = result
            for index, item in enumerate(steering):
                source = ":".join(str(record.get("statement_id", id(record))) for record in feed)
                self.ctx.ring.steer(item["agent"], item["hint"], rationale=item.get("rationale", ""),
                    task_id=item.get("task_id", ""), supersedes=item.get("supersedes"),
                    source_id=f"user:{source}:{index}")
            if not isinstance(seed, list):
                history.extend({"role": "user", "content": message} for message in messages)
                if attachments:
                    history.append({"role": "user", "content": "上传附件：" + json.dumps(attachments, ensure_ascii=False)})
            history.append({"role": "assistant", "content": reply})
            self.ctx.audit.log("master.reply", self.agent_id, {"text": reply})
            with contextlib.suppress(Exception):
                self.ctx.blackboard.write(BlackboardLayer.TOPOLOGY, new_id("reply"),
                    {"kind": "reply", "text": reply, "ts": time.time()},
                    agent_type=AgentType.MASTER, updated_by=self.agent_id)
            callback = self.ctx.extra.get("on_user_feed")
            if callback is not None:
                callback(feed)
            else:
                inbox = self.ctx.extra.get("user_inbox") or []
                inbox[:] = [item for item in inbox if item not in feed]
            self._chat_failure_batch = None
            if steering:
                self.ctx.audit.log("decision.change", self.agent_id, {
                    "action": "user_chat_steering", "count": len(steering)})
            return True

    async def _llm_user_chat(self, messages: list[str], attachments: list[str],
                             history: list[dict[str, str]]
                             ) -> tuple[str, list[dict[str, Any]]] | None:
        """灵枢回答用户（独立上下文）：直接答复 + 可选的子 Agent 方向建议。"""
        roster = "；".join(
            f"{get_profile(agent_type).name}({agent_type.value})"
            for agent_type in self.ctx.agents)
        paused = bool(self.ctx.pause_gate is not None and self.ctx.pause_gate.paused)
        situation = self._engagement_status()
        payload = {
            "mission": self._mission()[:1000],
            "scanner": self._scanner_context(),
            "world_state": self._world_state(),
            "agents": [a.value for a in self.ctx.agents],
            "roster": roster,
            "engagement_paused": paused,
            "situation": situation,
            "active_tasks": situation.get("active_tasks", []),
            "chat_history": history[-self._CHAT_HISTORY_MAX:],
            "new_messages": messages,
            "steering_status": {task["task_id"]: self.ctx.ring.steerings_for(task["agent"], task_id=task["task_id"])
                                for task in situation.get("active_tasks", [])} if self.ctx.ring else {},
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
                "通知该类型所有任务时才填写 broadcast=true。新指令不会自动取消旧指令；"
                "确需替代时用 supersedes 指定 steering_status 中的旧指令 ID。"
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
            if not isinstance(data, dict) or not isinstance(data.get("reply"), str):
                raise TypeError("用户对话必须返回 JSON 对象和文字回复")
            reply = data["reply"].strip()
            raw = data.get("steering") or []
            if not isinstance(raw, list) or not reply:
                raise ValueError("用户对话应答或指令格式无效")
            current = self._engagement_status().get("active_tasks", [])
            steering = self._target_steering(raw, current, allow_broadcast=True)
            if len(steering) != len(raw):
                raise ValueError("指令收件任务尚未就绪或无效，消息保留待重试")
            control_result = await self._apply_scanner_control(data)
            if control_result is not None and not control_result['ok']:
                raise ValueError(control_result['error'])
            return reply, steering
        except (SpiritScoutError, OSError, ValueError, TypeError) as exc:
            # 对话通道失败不影响主链路，但留痕可见
            self.ctx.audit.log("master.reply_failed", self.agent_id,
                               {"error": str(exc)[:200]})
            return None

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
        return items

    def _light_supervision(self) -> bool:
        from spiritscout.core.models import RunMode

        return (bool((getattr(self.ctx, "extra", None) or {}).get("lightweight_local"))
                and getattr(getattr(self.ctx, "policy", None), "mode", None) in (RunMode.CTF, RunMode.AWDP))

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
            facts = {**digest, "tasks": [
                {**{k: v for k, v in task.items() if k not in {'steering', 'blackboard_ref', 'routes'}},
                 'routes': [{k: v for k, v in route.items() if k != 'progress'} for route in task.get('routes', [])]}
                for task in digest['tasks']]}
            if self._light_supervision():
                for task in facts["tasks"]:
                    executions = task.get("executions")
                    if executions:
                        task["routes"] = [{k: v for k, v in route.items() if k not in {"notes", "notes_view"}}
                                          for route in task.get("routes", [])]
                        signatures = {json.dumps({k: row.get(k) for k in
                            ("tool_name", "params_digest", "execution_status", "exit_code", "result_digest")},
                            sort_keys=True) for row in executions}
                        task["executions"] = sorted(signatures)
            fingerprint = json.dumps(facts, ensure_ascii=False, sort_keys=True)
            if fingerprint == last_fingerprint:
                continue
            last_fingerprint = fingerprint
            steering = await self._llm_steer(digest, decisions)
            for item in steering:
                self.ctx.ring.steer(item["agent"], item["hint"],
                                    rationale=item.get("rationale", ""),
                                    task_id=item.get("task_id", ""), supersedes=item.get("supersedes"))

    def _progress_digest(self, task_ids: list[str]) -> dict[str, Any]:
        """本轮任务的实时进度摘要（子 Agent 随步上报的路线笔记）。"""
        bb = getattr(self.ctx, "blackboard", None)
        tasks: list[dict[str, Any]] = []
        for tid in task_ids:
            read_versioned = getattr(bb, 'read_versioned', None)
            versioned = read_versioned(BlackboardLayer.TASK_STATE, tid) if callable(read_versioned) else None
            data, version = versioned if isinstance(versioned, tuple) and len(versioned) == 2 else (
                bb.read(BlackboardLayer.TASK_STATE, tid) if bb is not None else None, None)
            if data is None:
                continue
            task = data.get("task", {})
            if task.get("status") not in {
                    TaskStatus.PENDING.value, TaskStatus.DISPATCHED.value,
                    TaskStatus.RUNNING.value}:
                continue
            routes = [
                {"route": r.get("name"), "status": r.get("status"),
                 "route_id": r.get('route_id', ''),
                 "progress": r.get("progress"),
                 "notes": [str(n)[:500] for n in (r.get("notes") or [])[-3:]],
                 "notes_view": {'kind': 'agent_summary', 'partial': True,
                                'digest': hashlib.sha256(json.dumps((r.get('notes') or [])[-3:],
                                                                   ensure_ascii=False).encode()).hexdigest()[:16]}}
                for r in data.get("routes", [])
            ]
            challenge = (task.get("payload") or {}).get("challenge") or {}
            tasks.append({"agent": task.get("agent_type"),
                          "task_id": tid,
                          **({'blackboard_ref': {'layer': 'task_state', 'record_id': tid, 'version': version}}
                             if version is not None else {}),
                          "challenge": {k: challenge[k] for k in ("id", "title")
                                        if k in challenge},
                          "steering": self.ctx.ring.steerings_for(
                              task.get("agent_type"), task_id=tid) if self.ctx.ring else [],
                          "title": str(task.get("title", ""))[:80],
                          "routes": routes})
        if self._light_supervision():
            for task in tasks:
                task["executions"] = execution_digest(getattr(self.ctx, "journal", None), task["task_id"])
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
                "scanner": self._scanner_context(),
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
                            "把 investigation_frontier 当作共享调查前沿：优先推动能解决"
                            "remaining_unknowns、改变后续决策的最小实验；同一 subject 的重复"
                            "版本持续增加但 evidence_state/status 未变化时，要求换验证方法、"
                            "交给更合适角色或按 stop_condition 收口。"
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
            if self._light_supervision():
                payload["constraints"] += (
                    "executions 是本任务实际工具执行记录，不是仅有计划；即使没有漏洞发现也不能说没有执行。"
                    "exit_code、stderr 和 error 必须一起看，execution_ok 不等于假设成立；预览不代表完整源码。"
                    "attachments 只读，shell/python 默认 workdir 与 artifacts 可写。某个目录写入失败"
                    "不证明整个沙箱只读，不因此改用在线服务或放弃本地分析。"
                    "对已执行且结果明确的动作不要重复下指令；按实际证据选择后续方法或要求补充缺口。")
            for task in payload['active_tasks']:
                original = next(t for t in digest['tasks'] if t['task_id'] == task['task_id'])
                if original.get('blackboard_ref'):
                    task['basis_ref'] = record_ref(original['blackboard_ref'])
            platform_progress = digest.get('platform_progress') or {}
            platform_basis = platform_ref(platform_progress) if platform_progress else ''
            if platform_basis:
                payload['platform_basis_ref'] = platform_basis
            payload['basis_contract'] = (
                'defer_challenges 每项必须提供 basis_refs 数组，引用 active_tasks 的 basis_ref。'
                'cancel_running/release_instance 还须引用同题 world_state.shared_facts 的版本化 '
                'layer:record_id@version，或 platform_basis_ref；摘要和进度分数不能单独作为取消/释放依据。')
            resp = await chat_decision(
                self.ctx.gateway,
                decision_messages(self._system_prompt() + "\nROLE: collab_judge", payload, 16000),
                audit=self.ctx.audit, actor=self.agent_id,
                memory=decision_memory(self, "steering"),
                task_type="meta_planning", response_format="json",
                reasoning_effort=self._master_effort,
            )
            data = extract_json(resp.content) or {}
            active_ids = {str((t.get("challenge") or {}).get("id") or "")
                          for t in digest["tasks"]}
            raw_defer = data.get("defer_challenges") or []
            proposed = [i for i in (raw_defer if isinstance(raw_defer, list) else [])
                        if isinstance(i, dict) and str(i.get("id") or "") in active_ids
                        and i.get("id") and str(i.get("reason") or "").strip()]
            visible_refs = {task['basis_ref'] for task in payload['active_tasks'] if task.get('basis_ref')}
            for fact in (payload.get('world_state') or {}).get('shared_facts', []):
                if isinstance(fact, dict) and isinstance(fact.get('blackboard_ref'), dict):
                    visible_refs.add(record_ref(fact['blackboard_ref']))
            if platform_basis:
                visible_refs.add(platform_basis)
            deferred = []
            for item in proposed:
                ids = [t['task_id'] for t in digest['tasks']
                       if str((t.get('challenge') or {}).get('id') or '') == str(item['id'])]
                refs = item.get('basis_refs')
                if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in visible_refs for ref in refs)
                        or not check_basis(self.ctx.blackboard, item, ids, current_platform=platform_basis)):
                    self.ctx.audit.log('decision.change', self.agent_id, {
                        'action': 'steering_basis_rejected', 'challenge_id': str(item['id']),
                        'reason': '依据缺失、未展示、版本已变化、跨题或摘要不足以取消现场'})
                    continue
                deferred.append({**item, '_steering_task_ids': ids})
            if deferred and decisions is not None:
                decisions.put_nowait({"defer_challenges": deferred})
            items = data.get("steering")
            if not isinstance(items, list):
                raise TypeError("steering 应答不是列表")
            steering = self._target_steering(items, digest["tasks"])
            await self._apply_scanner_control(data)
            return steering
        except Exception:  # noqa: BLE001 —— 督导失败不拖垮派发
            return []

    def _steering_decision_current(self, item: dict[str, Any]) -> bool:
        """入队之后现场还可能变化，实际取消／释放前再做一次本地校验。"""
        if '_steering_task_ids' not in item:
            return True  # 主规划／用户指令沿用已有决策通道。
        platform = self.ctx.extra.get('platform')
        progress = platform.progress() if platform is not None and hasattr(platform, 'progress') else {}
        valid = check_basis(self.ctx.blackboard, item, item['_steering_task_ids'],
                            current_platform=platform_ref(progress) if progress else '')
        if not valid:
            self.ctx.audit.log('decision.change', self.agent_id, {
                'action': 'steering_basis_rejected', 'challenge_id': str(item.get('id') or ''),
                'reason': '排队期间依据已变化，保留当前现场'})
        return valid

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
            supersedes = item.get("supersedes") or []
            if not isinstance(supersedes, list) or not all(isinstance(identifier, str) for identifier in supersedes):
                continue
            if self._light_supervision() and not allow_broadcast and not supersedes and self.ctx.ring:
                previous = self.ctx.ring.steerings_for(item["agent"], task_id=tid)
                if any(record.get("hint", "").strip() == item["hint"].strip()
                       and not (record.get("feedback") and all(
                           feedback.get("status") == "completed" for feedback in record["feedback"].values()))
                       for record in previous):
                    self.ctx.audit.log("decision.change", self.agent_id, {
                        "action": "steering_duplicate", "task_id": tid,
                        "reason": "同一任务已有相同且未完成的指令，保留原编号供反馈"})
                    continue
            out.append({"agent": item["agent"], "task_id": tid,
                        "hint": item["hint"], "rationale": str(item.get("rationale", "")),
                        "supersedes": supersedes})
        return out

    # ------------------------------------------------------------ 节点：汇聚

