"""高风险动作即时确认（G5 的交互形态）：发现高风险入口时暂停该分支，
向操作者询问是否放行——授权决策发生在触发时刻，而非开工前盲勾。

语义：
- 同一动作并发请求合并为一条待办（多条路线同时撞上同一入口只问一次）；
- 允许 = 本次会话内该动作放行（写入 ScopeGuard 会话级批准集，不再重复询问）；
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


class ApprovalBroker:
    """会话内待确认队列：ask() 挂起等待操作者裁决，decide() 唤醒。"""

    def __init__(self, audit: AuditLogger | None = None):
        self._audit = audit or NullAuditLogger()
        self._pending: dict[str, dict] = {}          # approval_id → 待办记录
        self._waiters: dict[str, asyncio.Future[bool]] = {}
        self._by_action: dict[str, str] = {}          # action → approval_id（合并并发请求）

    # ------------------------------------------------------------ 询问侧（agent 分支调用）

    async def ask(self, action: str, detail: str) -> bool:
        """请求放行一个高风险动作；挂起直到操作者裁决或会话停止。"""
        existing = self._by_action.get(action)
        if existing and existing in self._waiters:
            return await self._waiters[existing]      # 同动作已在询问：附等同一裁决
        aid = new_id("apv")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._waiters[aid] = fut
        self._by_action[action] = aid
        self._pending[aid] = {
            "approval_id": aid, "action": action, "detail": detail,
            "requested_at": time.time(),
        }
        self._audit.log("approval.requested", "approval_broker",
                        {"approval_id": aid, "action": action, "detail": detail})
        try:
            return await fut
        finally:
            self._pending.pop(aid, None)
            self._waiters.pop(aid, None)
            self._by_action.pop(action, None)

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
        fut.set_result(allow)
        return True

    def cancel_all(self, *, reason: str = "会话停止") -> None:
        """会话停止/关停：全部待办按拒绝唤醒（等待中的分支随即按违规留痕退出）。"""
        for aid, fut in list(self._waiters.items()):
            if not fut.done():
                rec = self._pending.get(aid, {})
                self._audit.log("approval.decided", "approval_broker", {
                    "approval_id": aid, "action": rec.get("action", ""),
                    "allow": False, "by": "system", "reason": reason})
                fut.set_result(False)

    # ------------------------------------------------------------ 展示侧

    def view(self) -> list[dict]:
        """当前待办列表（/state 折入，前端渲染确认条）。"""
        return [dict(rec) for rec in self._pending.values()]


# 审批门回调签名：policy 层只依赖此形状，不反向依赖 Web
ApprovalGate = Callable[[str, str], Awaitable[bool]]
