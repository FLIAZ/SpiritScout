"""模型提问的送达与答复队列。

问题和选项由模型提供，自由输入优先；同文问题合并，停止会话时唤醒等待项。
用户选择的授权答复或模型确认的明确授权目标才加入当前会话批准记录。"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.ids import new_id

_DIRECT_INPUT_QUESTION = re.compile(
    r"账号|用户名|密码|凭据|验证码|手机号|邮箱|token|cookie|api[ _-]?key|密钥|请求记录|har",
    re.IGNORECASE,
)
_DEFERRED_INPUT_OPTION = re.compile(
    r"稍后|之后|后续|待会|一会儿|过会|我(?:可以|能|会)提供|(?:可以|能够)提供|"
    r"(?:注册|准备|整理|拿到|获取).{0,12}(?:后|好).{0,12}(?:给|提供|发送|补充)|"
    r"再.{0,12}(?:给|提供|发送|补充)",
    re.IGNORECASE,
)
_NEGATIVE_INPUT_OPTION = re.compile(r"没有|无账号|无法|不能|不提供|暂不|不便|拒绝|只做|跳过")


def normalize_options(question: str, options: Any) -> list[str]:
    """具体输入直接走当前卡片文本框，不保留“以后再给”的承诺选项。"""
    cleaned: list[str] = []
    for value in (options or [])[:6]:
        option = str(value).strip()[:200]
        if option and option not in cleaned:
            cleaned.append(option)
    if not _DIRECT_INPUT_QUESTION.search(question):
        return cleaned
    return [option for option in cleaned
            if not (_DEFERRED_INPUT_OPTION.search(option)
                    and not _NEGATIVE_INPUT_OPTION.search(option))]


def normalize_targets(targets: Any) -> list[dict[str, Any]]:
    """扩围目标归一化：接受 "host" 或 {"target": ..., "why": ...}。

    approved_by 是模型声明的授权选项原文；使用原文关联，避免选项清理后下标错位。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (targets or [])[:6]:
        if isinstance(item, str):
            target, why = item.strip()[:300], ""
        elif isinstance(item, dict):
            target = str(item.get("target") or "").strip()[:300]
            why = str(item.get("why") or "").strip()[:200]
        else:
            continue
        if target and target not in seen:
            seen.add(target)
            entry: dict[str, Any] = {"target": target, "why": why}
            if isinstance(item, dict) and isinstance(item.get("approved_by"), list):
                entry["approved_by"] = [str(value).strip()[:200]
                                        for value in item["approved_by"][:6] if isinstance(value, str)]
            out.append(entry)
    return out


class QuestionBroker:
    """会话内待答问题队列：ask() 挂起等待用户作答，answer() 唤醒。

    并发语义与 ApprovalBroker 一致：同一问题的多个等待者共用一个裁决
    Future，经 shield 附等——单个分支被取消不影响他人等待；待办生命
    周期由 Broker 统一管理（作答落地即移除；等待者全部退出按「无答复」
    回收），创建者退出不再误删仍有人等待的问题。
    """

    def __init__(self, audit: AuditLogger | None = None, *,
                 context_path: str | Path | None = None,
                 interactive: bool = True):
        self._audit = audit or NullAuditLogger()
        self.interactive = interactive
        self._blackboard: Any = None
        self._context_path = context_path
        self._guard: Any = None                       # ScopeGuard（扩围写入）
        self._pending: dict[str, dict] = {}           # question_id → 待答记录
        self._waiters: dict[str, asyncio.Future[dict]] = {}
        self._answer_locks: dict[str, asyncio.Lock] = {}
        self._by_question: dict[str, str] = {}        # 问题指纹 → question_id
        self._fingerprints: dict[str, str] = {}       # question_id → 问题指纹
        self._consumers: dict[str, int] = {}          # question_id → 等待者引用数

    def bind_guard(self, guard: Any) -> None:
        """绑定会话守卫：用户确认扩围时写入其会话批准集。"""
        self._guard = guard

    def bind_context(self, blackboard: Any) -> None:
        """绑定会话事实库，所有用户答复完整回灌各 Agent 的上下文。"""
        self._blackboard = blackboard
        if blackboard is not None:
            from spiritscout.core.models import BlackboardLayer

            for row in blackboard.query(BlackboardLayer.TOPOLOGY, limit=10000,
                                        data_filter=("kind", "team_wait")):
                data = row["data"]
                if data.get("status") == "waiting" and data["question_id"] not in self._waiters:
                    self._publish_dependencies(data["question_id"], data, "interrupted")

    def bind_context_path(self, path: str | Path) -> None:
        """预检阶段也可通过短连接落盘，运行态关闭后仍保留答复原文。"""
        self._context_path = path

    # ------------------------------------------------------------ 询问侧（agent 调用）

    async def ask(self, agent: str, question: str, options: list[str],
                  targets: list[Any] | None = None, *,
                  multiple: bool = False, secret: bool = False,
                  allow_new_targets: bool = False,
                  blocking_task_ids: list[str] | None = None, block_all_tasks: bool = False) -> dict[str, Any]:
        """提交一个用户问题；挂起直到作答/停止。返回 {"answer", "scope_added"}。

        multiple=True 表示选项可多选（前端勾选后统一提交），默认单选。
        secret=True 用于受控工具的一次性验证，不合并挑战，不把答复写入共享历史。
        allow_new_targets 仅在开工范围确认中允许模型解释用户自行声明的新目标。
        """
        question = str(question or "").strip()[:2000]
        if not self.interactive:
            # 无人值守模式不代填用户选择，不创建待办；明确交回模型自主决策。
            self._audit.log("user.question_skipped", agent, {
                "question": question[:500], "reason": "智能体模式禁止用户交互"})
            return {"answer": "智能体模式为全自动模式，没有用户答复。请根据任务原文、"
                    "比赛规则、已有授权与现场证据自主决策并继续；缺少必要条件时"
                    "记录阻塞并推进其他任务，无法继续则如实报告结果，不等待用户。",
                    "scope_added": [], "autonomous": True}
        from spiritscout.tools.invocation import current_execution_scope

        scope = current_execution_scope.get()
        owner = scope.task_id if scope else ""
        if (not isinstance(block_all_tasks, bool) or blocking_task_ids is not None
                and (not isinstance(blocking_task_ids, list)
                     or not all(isinstance(task, str) and task.strip() for task in blocking_task_ids))):
            raise ValueError("问题依赖需要任务 ID 列表和布尔值 block_all_tasks")
        # 验证码属于单次挑战；即使两个账号同名，也不能合并后共用一次答复。
        fingerprint = f"{agent}|{question}" + ('|' + new_id('verification') if secret else '')
        existing = self._by_question.get(fingerprint)
        if existing and existing in self._waiters:
            qid = existing                            # 同一问题已在询问：共用裁决
        else:
            qid = self._create(agent, question, fingerprint, options, targets,
                               multiple=multiple, secret=secret, allow_new_targets=allow_new_targets)
        fut = self._waiters[qid]
        pending = self._pending[qid]
        pending["blocking_task_ids"] = sorted(set(pending.get("blocking_task_ids", []))
                                              | set(blocking_task_ids or []) | ({owner} if owner else set()))
        pending["block_all_tasks"] = pending.get("block_all_tasks", False) or block_all_tasks
        self._consumers[qid] = self._consumers.get(qid, 0) + 1
        try:
            self._publish_dependencies(qid, pending, "waiting")
            # shield：单个等待者被取消不连带取消共享的作答
            return await asyncio.shield(fut)
        finally:
            self._detach(qid, fingerprint)

    async def wait_for_task(self, task_id: str) -> None:
        """只阻塞依赖待答问题的任务；在途操作可以收尾。"""
        if not task_id:
            return
        while True:
            futures = [self._waiters[qid] for qid, item in list(self._pending.items())
                       if qid in self._waiters and (item.get("block_all_tasks")
                           or task_id in item.get("blocking_task_ids", []))]
            if not futures:
                return
            await asyncio.gather(*(asyncio.shield(future) for future in futures))

    def _publish_dependencies(self, qid: str, item: dict, status: str) -> None:
        from spiritscout.core.models import BlackboardLayer

        data = {"kind": "team_wait", "question_id": qid, "status": status,
                "blocking_task_ids": item.get("blocking_task_ids", []),
                "block_all_tasks": item.get("block_all_tasks", False)}
        if self._blackboard is not None:
            self._blackboard.write(BlackboardLayer.TOPOLOGY, qid, data, updated_by="questions")
        self._audit.log("user.question_dependencies", item.get("agent", "system"), data)

    def _create(self, agent: str, question: str, fingerprint: str,
                options: list[str], targets: list[Any] | None,
                *, multiple: bool = False, secret: bool = False,
                allow_new_targets: bool = False) -> str:
        qid = new_id("ask")
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[qid] = fut
        self._answer_locks[qid] = asyncio.Lock()
        self._by_question[fingerprint] = qid
        self._fingerprints[qid] = fingerprint
        self._pending[qid] = {
            "question_id": qid, "agent": agent, "question": question,
            "options": normalize_options(question, options),
            "multiple": bool(multiple),
            "secret": bool(secret),
            "allow_new_targets": bool(allow_new_targets),
            "targets": normalize_targets(targets),
            "requested_at": time.time(),
        }
        self._audit.log("user.question", agent, {
            "question_id": qid, "question": question,
            "options": self._pending[qid]["options"],
            "multiple": bool(multiple),
            "targets": self._pending[qid]["targets"]})
        return qid

    def _detach(self, qid: str, fingerprint: str) -> None:
        """等待者退出：引用归零且尚未作答 → 无人等待的问题按「无答复」回收。"""
        if qid not in self._waiters:
            return
        remaining = self._consumers.get(qid, 1) - 1
        if remaining > 0:
            self._consumers[qid] = remaining
            return
        self._consumers.pop(qid, None)
        fut = self._waiters.pop(qid, None)
        pending = self._pending.pop(qid, None)
        self._answer_locks.pop(qid, None)
        self._fingerprints.pop(qid, None)
        if self._by_question.get(fingerprint) == qid:
            self._by_question.pop(fingerprint, None)
        if fut is not None and not fut.done():
            fut.set_result({"answer": "", "scope_added": [],
                            "cancelled": "等待者全部退出"})
        if pending is not None:
            self._publish_dependencies(qid, pending, "cancelled")

    def _resolve(self, qid: str, payload: dict[str, Any]) -> None:
        """作答落地：唤醒全部等待者并移除待办（生命周期收口在 Broker）。"""
        fut = self._waiters.pop(qid, None)
        pending = self._pending.pop(qid, None)
        self._answer_locks.pop(qid, None)
        self._consumers.pop(qid, None)
        fingerprint = self._fingerprints.pop(qid, None)
        if fingerprint and self._by_question.get(fingerprint) == qid:
            self._by_question.pop(fingerprint, None)
        if fut is not None and not fut.done():
            fut.set_result(payload)
        if pending is not None:
            self._publish_dependencies(qid, pending, "cancelled" if payload.get("cancelled") else "answered")

    # ------------------------------------------------------------ 作答侧（Web 端点调用）

    def answer_lock(self, question_id: str) -> asyncio.Lock | None:
        """解释与提交共用同一问题锁；锁随待答问题取消或完成一起回收。"""
        return self._answer_locks.get(question_id)

    def prepare_answer(self, question_id: str, *, choice: int | None = None,
                       choices: list[int] | None = None, text: str = "",
                       allow_targets: bool = False) -> tuple[dict, str, list[int]] | None:
        """校验且组合答复，不唤醒任务、不改变授权，允许异步解释前先检查输入。"""
        rec = self._pending.get(question_id)
        fut = self._waiters.get(question_id)
        if rec is None or fut is None or fut.done():
            return None
        if choice is not None and choices:
            raise ValueError("choice 与 choices 只能二选一")
        multiple = bool(rec.get("multiple"))
        raw = [choice] if choice is not None else list(choices or [])
        picked: list[int] = []
        if raw:
            if not multiple and len(raw) > 1:
                raise ValueError("这条问题只能选择一个选项")
            for index in raw:
                if type(index) is not int or not 0 <= index < len(rec["options"]):
                    raise ValueError("请选择有效选项")
                if index not in picked:
                    picked.append(index)
        answer_text = str(text or "").strip()
        if multiple:
            parts: list[str] = []
            if picked:
                parts.append("、".join(rec["options"][i] for i in picked))
            if answer_text:
                parts.append(answer_text)
            answer = "；".join(parts)
        elif answer_text:
            answer = answer_text
        elif picked:
            answer = rec["options"][picked[0]]
        else:
            answer = ""
        if not answer and not allow_targets:
            raise ValueError("请选择有效选项或填写答复")
        return dict(rec), answer, picked

    def answer(self, question_id: str, *, choice: int | None = None,
               choices: list[int] | None = None, text: str = "",
               add_targets: list[str] | None = None,
               resolved_targets: list[str] | None = None, scope_resolved: bool = False) -> bool:
        """作答一条待答问题。choice=单选下标；choices=多选下标（仅多选问题）；
        text=自由输入（多选时作为补充说明与所选选项一并回传）；
        add_targets=显式确认的扩围目标（兼容旧调用，必须是 targets 子集）。"""
        rec = self._pending.get(question_id)
        fut = self._waiters.get(question_id)
        if rec is None or fut is None or fut.done():
            return False
        prepared = self.prepare_answer(question_id, choice=choice, choices=choices,
                                       text=text, allow_targets=bool(add_targets))
        if prepared is None:
            return False
        rec, answer, picked = prepared
        if rec.get('secret'):
            if add_targets or len(answer) > 256:
                raise ValueError('验证答复最多 256 字符，不能附加授权目标')
            # 一次性验证材料只交回等待的浏览器工具，不进入黑板、模型或审计原文。
            self._audit.log('user.answered', 'web', {'question_id': question_id,
                'question': rec['question'], 'agent': rec['agent'],
                'answer': '已提交验证材料', 'secret': True, 'scope_added': []})
            self._resolve(question_id, {'answer': answer, 'scope_added': []})
            return True
        selected_options = {rec["options"][i] for i in picked} if not str(text or "").strip() else set()
        mapped = [item["target"] for item in rec["targets"]
                  if selected_options.intersection(item.get("approved_by", []))]
        resolved = list(resolved_targets or [])
        add_targets = list(dict.fromkeys([*(add_targets or []), *mapped, *resolved]))
        added: list[str] = []
        if add_targets and self._guard is not None:
            valid = {t["target"] for t in rec["targets"] if isinstance(t, dict)}
            if rec.get("allow_new_targets"):
                valid.update(resolved)
            for target in add_targets[:6]:
                if str(target) in valid:
                    try:
                        added.append(self._guard.approve_target(str(target)))
                    except (ScopeViolation, ValueError, TypeError):
                        continue    # 单条扩围失败不影响作答
        if not answer and added:
            # 只提交显式扩围目标也是明确作答（如「这些目标本次要测」）
            answer = "已确认目标：" + "、".join(added)
        if not answer:
            raise ValueError("请选择有效选项或填写答复")
        from spiritscout.gateway.world_state import (
            record_user_statement,
            record_user_statement_at_path,
        )

        statement = {"kind": "answer", "question": rec["question"], "answer": answer,
                     "scope_added": added, "statement_id": question_id}
        if self._blackboard is not None:
            record_user_statement(self._blackboard, statement)
        elif self._context_path is not None:
            record_user_statement_at_path(self._context_path, statement)
        choice_mapped = bool(picked and not str(text or "").strip()
                             and all("approved_by" in item for item in rec["targets"]))
        payload = {"answer": answer, "choices": picked, "scope_added": added,
                   "scope_resolved": bool(scope_resolved or mapped or choice_mapped)}
        self._audit.log("user.answered", "web", {
            "question_id": question_id, "question": rec["question"],
            "agent": rec["agent"], "answer": answer,
            "choices": picked, "scope_added": added})
        self._resolve(question_id, payload)
        return True

    def cancel_all(self, *, reason: str = "会话停止") -> None:
        """会话停止/关停：全部挂起问题按「无答复」唤醒。"""
        for qid in list(self._pending):
            self._audit.log("user.answered", "system", {
                "question_id": qid, "answer": "", "scope_added": [],
                "reason": reason})
            self._resolve(qid, {"answer": "", "scope_added": [],
                                "cancelled": reason})

    # ------------------------------------------------------------ 展示侧

    def view(self) -> list[dict]:
        """当前待答列表（/state 折入，前端渲染提问卡）。

        先复制 pending 字典再遍历：/state 在线程池执行，与本事件循环的
        登记/作答并发时，直接迭代活字典会撞「changed size」。
        """
        return [dict(rec) for rec in self._pending.copy().values()]
