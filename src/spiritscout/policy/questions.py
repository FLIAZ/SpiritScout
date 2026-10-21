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


def normalize_targets(targets: Any) -> list[dict[str, str]]:
    """扩围目标归一化：接受 "host" 或 {"target": ..., "why": ...}。

    why 是模型写给普通用户的一句话说明（它是什么、为什么需要）——
    控制台勾选项不再只有裸域名可看；缺说明时降级为纯目标，不拦住询问。
    """
    out: list[dict[str, str]] = []
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
            out.append({"target": target, "why": why})
    return out


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
                  targets: list[Any] | None = None, *,
                  multiple: bool = False) -> dict[str, Any]:
        """提交一个用户问题；挂起直到作答/停止。返回 {"answer", "scope_added"}。

        multiple=True 表示选项可多选（前端勾选后统一提交），默认单选。
        """
        question = str(question or "").strip()[:2000]
        fingerprint = f"{agent}|{question}"
        existing = self._by_question.get(fingerprint)
        if existing and existing in self._waiters:
            qid = existing                            # 同一问题已在询问：共用裁决
        else:
            qid = self._create(agent, question, fingerprint, options, targets,
                               multiple=multiple)
        fut = self._waiters[qid]
        self._consumers[qid] = self._consumers.get(qid, 0) + 1
        try:
            # shield：单个等待者被取消不连带取消共享的作答
            return await asyncio.shield(fut)
        finally:
            self._detach(qid, fingerprint)

    def _create(self, agent: str, question: str, fingerprint: str,
                options: list[str], targets: list[Any] | None,
                *, multiple: bool = False) -> str:
        qid = new_id("ask")
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[qid] = fut
        self._by_question[fingerprint] = qid
        self._fingerprints[qid] = fingerprint
        self._pending[qid] = {
            "question_id": qid, "agent": agent, "question": question,
            "options": [str(o).strip()[:200] for o in (options or [])[:6]
                        if str(o).strip()],
            "multiple": bool(multiple),
            "targets": normalize_targets(targets),
            "requested_at": time.time(),
        }
        self._audit.log("user.question", agent, {
            "question_id": qid, "question": question[:500],
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
               choices: list[int] | None = None, text: str = "",
               add_targets: list[str] | None = None) -> bool:
        """作答一条待答问题。choice=单选下标；choices=多选下标（仅多选问题）；
        text=自由输入（多选时作为补充说明与所选选项一并回传）；
        add_targets=用户勾选的扩围目标（必须是问句附带的 targets 子集）。"""
        rec = self._pending.get(question_id)
        fut = self._waiters.get(question_id)
        if rec is None or fut is None or fut.done():
            return False
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
        answer_text = str(text or "").strip()[:1000]
        if multiple:
            parts: list[str] = []
            if picked:
                parts.append("、".join(rec["options"][i] for i in picked))
            if answer_text:
                parts.append(answer_text)
            answer = "；".join(parts)[:1000]
        elif answer_text:
            answer = answer_text
        elif picked:
            answer = rec["options"][picked[0]]
        else:
            answer = ""
        if not answer and not add_targets:
            raise ValueError("请选择有效选项或填写答复")
        added: list[str] = []
        if add_targets and self._guard is not None:
            valid = {t["target"] for t in rec["targets"] if isinstance(t, dict)}
            for target in add_targets[:6]:
                if str(target) in valid:
                    try:
                        added.append(self._guard.approve_target(str(target)))
                    except (ScopeViolation, ValueError, TypeError):
                        continue    # 单条扩围失败不影响作答
        if not answer and added:
            # 只勾选扩围目标也是明确作答（如「这些目标本次要测」）
            answer = "已确认目标：" + "、".join(added)
        if not answer:
            raise ValueError("请选择有效选项或填写答复")
        payload = {"answer": answer, "choices": picked, "scope_added": added}
        self._audit.log("user.answered", "web", {
            "question_id": question_id, "answer": answer[:300],
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
