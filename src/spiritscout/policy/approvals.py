"""高风险动作即时确认（G5 的交互形态）：发现高风险入口时暂停该分支，
向操作者询问是否放行——授权决策发生在触发时刻，而非开工前盲勾。

语义：
- 有调用上下文时，仅相同调用 ID 与参数摘要合并；不同调用分别审批；
- 允许 = 当前调用放行，不写入会话级动作许可；
- 拒绝 = 本次调用按策略违规拒绝（路线留痕后继续其他分支）；
- 不设超时：合规决策不可由系统代答；会话停止即全部按拒绝唤醒。

审批记录进审计（approval.requested / approval.decided），经 SSE 实时可见。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.ids import new_id
from spiritscout.tools.invocation import current_invocation


class ApprovalBroker:
    """会话内待确认队列：ask() 挂起等待操作者裁决，decide() 唤醒。

    并发语义：同一调用的多个等待者共用一条待办与同一个裁决 Future；
    每个等待者经 shield 附等——任一分支被取消（路线收尾）不影响其他
    等待者，也不会连带取消裁决本身。待办生命周期由 Broker 统一管理：
    裁决落地即移除；等待者全部退出（引用归零）时按拒绝回收，创建者
    退出不再误删仍有人等待的待办。
    """

    def __init__(self, audit: AuditLogger | None = None):
        self._audit = audit or NullAuditLogger()
        self._pending: dict[str, dict] = {}          # approval_id → 待办记录
        self._waiters: dict[str, asyncio.Future[bool]] = {}
        self._by_action: dict[str, str] = {}          # 调用键 → approval_id；旧直接调用按 action 合并
        self._consumers: dict[str, int] = {}          # approval_id → 等待者引用数

    # ------------------------------------------------------------ 询问侧（agent 分支调用）

    async def ask(self, action: str, detail: str) -> bool:
        """请求放行一个高风险动作；挂起直到操作者裁决或会话停止。"""
        context = current_invocation.get()
        key = f"{context.invocation_id}:{context.params_digest}" if context else action
        existing = self._by_action.get(key)
        if existing and existing in self._waiters:
            aid = existing                            # 同调用已在询问：附等同一裁决
        else:
            aid = self._create(action, detail, key)
        fut = self._waiters[aid]
        self._consumers[aid] = self._consumers.get(aid, 0) + 1
        try:
            # shield：单个等待者被取消不连带取消共享的裁决
            return await asyncio.shield(fut)
        finally:
            self._detach(aid, key)

    def _create(self, action: str, detail: str, key: str) -> str:
        aid = new_id("apv")
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._waiters[aid] = fut
        self._by_action[key] = aid
        self._pending[aid] = {
            "approval_id": aid, "action": action, "detail": detail,
            "requested_at": time.time(),
            "dedup_key": key,
        }
        context = current_invocation.get()
        if context is not None:
            self._pending[aid].update(invocation_id=context.invocation_id,
                                      params_digest=context.params_digest)
        self._audit.log("approval.requested", "approval_broker",
                        {"approval_id": aid, "action": action, "detail": detail})
        return aid

    def _detach(self, aid: str, action: str) -> None:
        """等待者退出：引用归零且尚未裁决 → 无人等待的待办按拒绝回收。"""
        if aid not in self._waiters:
            return
        remaining = self._consumers.get(aid, 1) - 1
        if remaining > 0:
            self._consumers[aid] = remaining
            return
        self._consumers.pop(aid, None)
        fut = self._waiters.pop(aid, None)
        self._pending.pop(aid, None)
        if self._by_action.get(action) == aid:
            self._by_action.pop(action, None)
        if fut is not None and not fut.done():
            fut.set_result(False)

    def _resolve(self, aid: str, action: str, verdict: bool) -> None:
        """裁决落地：唤醒全部等待者并移除待办（生命周期收口在 Broker）。"""
        fut = self._waiters.pop(aid, None)
        self._pending.pop(aid, None)
        self._consumers.pop(aid, None)
        if self._by_action.get(action) == aid:
            self._by_action.pop(action, None)
        if fut is not None and not fut.done():
            fut.set_result(verdict)

    # ------------------------------------------------------------ 裁决侧（Web 端点调用）

    def decide(self, approval_id: str, allow: bool, *, by: str = "web") -> bool:
        """裁决一条待办。待办不存在（已裁决/已停止）→ False。"""
        rec = self._pending.get(approval_id)
        fut = self._waiters.get(approval_id)
        if rec is None or fut is None or fut.done():
            return False
        self._audit.log("approval.decided", "approval_broker", {
            "approval_id": approval_id, "action": rec["action"],
            "allow": allow, "by": by})
        self._resolve(approval_id, str(rec["dedup_key"]), allow)
        return True

    def cancel_all(self, *, reason: str = "会话停止") -> None:
        """会话停止/关停：全部待办按拒绝唤醒（等待中的分支随即按违规留痕退出）。"""
        for aid, rec in list(self._pending.items()):
            self._audit.log("approval.decided", "approval_broker", {
                "approval_id": aid, "action": rec.get("action", ""),
                "allow": False, "by": "system", "reason": reason})
            self._resolve(aid, str(rec["dedup_key"]), False)

    # ------------------------------------------------------------ 展示侧

    def view(self) -> list[dict]:
        """当前待办列表（/state 折入，前端渲染确认条）。"""
        return [dict(rec) for rec in self._pending.values()]


# 审批门回调签名：policy 层只依赖此形状，不反向依赖 Web
ApprovalGate = Callable[[str, str], Awaitable[bool]]
