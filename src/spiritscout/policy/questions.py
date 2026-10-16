"""模型发起的用户询问（问题与选项由模型撰写，后端只做送达与回收）。

语义（对齐 Claude Code 式提问）：
- 模型遇到需要用户决策的事（授权范围外目标、路线分叉、取舍确认）时，
  用 ask_user 工具提交**自己总结润色的问题与选项**，后端不代写文案；
- ask() 挂起该分支直到用户作答或会话停止（不设超时，决策不可代答）；
- 用户可点选模型给出的选项，也可直接输入自己的意见（自由文本优先）；
- 问句附带的 scope targets 在控制台渲染为「加入本会话范围」勾选项——
  只有用户显式勾选，后端才把目标写入 ScopeGuard 会话批准集；
- 同一问题并发提交按问题文本合并（多条分支撞上同一个决策只问一次）；
- 会话停止 → 全部挂起问题按「无答复」唤醒，分支据此继续或收尾。

记录进审计（user.question / user.answered），经 /state 轮询实时可见。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.ids import new_id


class QuestionBroker:
    """会话内待答问题队列：ask() 挂起等待用户作答，answer() 唤醒。

    并发语义与 ApprovalBroker 一致：同一问题的多个等待者共用一个裁决
    Future，经 shield 附等——单个分支被取消不影响他人等待；待办生命
    周期由 Broker 统一管理（作答落地即移除；等待者全部退出按「无答复」
    回收），创建者退出不再误删仍有人等待的问题。
    """

    def __init__(self, audit: AuditLogger | None = None):
        self._audit = audit or NullAuditLogger()
        self._guard: Any = None                       # ScopeGuard（扩围写入）
        self._pending: dict[str, dict] = {}           # question_id → 待答记录
        self._waiters: dict[str, asyncio.Future[dict]] = {}
        self._by_question: dict[str, str] = {}        # 问题指纹 → question_id
        self._fingerprints: dict[str, str] = {}       # question_id → 问题指纹
        self._consumers: dict[str, int] = {}          # question_id → 等待者引用数

    def bind_guard(self, guard: Any) -> None:
        """绑定会话守卫：用户勾选扩围时写入其会话批准集。"""
        self._guard = guard

    # ------------------------------------------------------------ 询问侧（agent 调用）

    async def ask(self, agent: str, question: str, options: list[str],
                  targets: list[str] | None = None) -> dict[str, Any]:
        """提交一个用户问题；挂起直到作答/停止。返回 {"answer", "scope_added"}。"""
        question = str(question or "").strip()[:2000]
        fingerprint = f"{agent}|{question}"
        existing = self._by_question.get(fingerprint)
        if existing and existing in self._waiters:
            qid = existing                            # 同一问题已在询问：共用裁决
        else:
            qid = self._create(agent, question, fingerprint, options, targets)
        fut = self._waiters[qid]
        self._consumers[qid] = self._consumers.get(qid, 0) + 1
        try:
            # shield：单个等待者被取消不连带取消共享的作答
            return await asyncio.shield(fut)
        finally:
            self._detach(qid, fingerprint)

    def _create(self, agent: str, question: str, fingerprint: str,
                options: list[str], targets: list[str] | None) -> str:
        qid = new_id("ask")
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[qid] = fut
        self._by_question[fingerprint] = qid
        self._fingerprints[qid] = fingerprint
        self._pending[qid] = {
            "question_id": qid, "agent": agent, "question": question,
            "options": [str(o).strip()[:200] for o in (options or [])[:6]
                        if str(o).strip()],
            "targets": [str(t).strip()[:300] for t in (targets or [])[:6]
                        if str(t).strip()],
            "requested_at": time.time(),
        }
        self._audit.log("user.question", agent, {
            "question_id": qid, "question": question[:500],
            "options": self._pending[qid]["options"],
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
        self._pending.pop(qid, None)
        self._fingerprints.pop(qid, None)
        if self._by_question.get(fingerprint) == qid:
            self._by_question.pop(fingerprint, None)
        if fut is not None and not fut.done():
            fut.set_result({"answer": "", "scope_added": [],
                            "cancelled": "等待者全部退出"})

    def _resolve(self, qid: str, payload: dict[str, Any]) -> None:
        """作答落地：唤醒全部等待者并移除待办（生命周期收口在 Broker）。"""
        fut = self._waiters.pop(qid, None)
        self._pending.pop(qid, None)
        self._consumers.pop(qid, None)
        fingerprint = self._fingerprints.pop(qid, None)
        if fingerprint and self._by_question.get(fingerprint) == qid:
            self._by_question.pop(fingerprint, None)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    # ------------------------------------------------------------ 作答侧（Web 端点调用）

    def answer(self, question_id: str, *, choice: int | None = None,
               text: str = "", add_targets: list[str] | None = None) -> bool:
        """作答一条待答问题。choice=选项下标；text=自由输入（优先）；
        add_targets=用户勾选的扩围目标（必须是问句附带的 targets 子集）。"""
        rec = self._pending.get(question_id)
        fut = self._waiters.get(question_id)
        if rec is None or fut is None or fut.done():
            return False
        answer = str(text or "").strip()[:1000]
        if not answer:
            if type(choice) is not int or not 0 <= choice < len(rec["options"]):
                raise ValueError("请选择有效选项或填写答复")
            answer = rec["options"][choice]
        added: list[str] = []
        if add_targets and self._guard is not None:
            for target in add_targets[:6]:
                if str(target) in rec["targets"]:
                    try:
                        added.append(self._guard.approve_target(str(target)))
                    except (ScopeViolation, ValueError, TypeError):
                        continue    # 单条扩围失败不影响作答
        payload = {"answer": answer, "scope_added": added}
        self._audit.log("user.answered", "web", {
            "question_id": question_id, "answer": answer[:300],
            "scope_added": added})
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
        """当前待答列表（/state 折入，前端渲染提问卡）。"""
        return [dict(rec) for rec in self._pending.values()]
