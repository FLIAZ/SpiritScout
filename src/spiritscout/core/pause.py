"""暂停门与执行间隙检测（跨平台设计）。

协作式暂停：不设杀开关——检查点（灵枢图节点边界、子 Agent 步骤边界）
await 门放行，在途的一次工具调用/模型请求跑完才停。暂停期间灵枢↔用户
直达对话与审批通道照常，用时按区间扣账。

执行间隙检测（合盖待机/Ctrl+Z/VM 快照）：只看墙钟心跳缺口，全平台一致。
不碰 monotonic 时钟语义——Linux/macOS 挂起时 monotonic 停走而 Windows
（QPC）照走，按时钟分叉判休眠在 Windows 上永远失效；而「每秒 tick 是否
按时发生」在任何平台行为相同，且语义更准：它标记的是「控制台刚刚没在跑」，
与用户感知一致。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from spiritscout.core.audit import AuditLogger, NullAuditLogger

#: 心跳缺口超过该阈值判为执行间隙（秒）。事件循环被同步重活短暂饿死
#: 不会误报；真误报的代价也只是一条时间线备注与一段扣账，无害。
GAP_THRESHOLD_SECONDS = 60.0


class PauseGate:
    """暂停门（set=放行 / clear=暂停）+ 用时扣账 + 间隙检测。

    一个会话一份，主/子 Agent 上下文共持同一引用（dataclasses.replace
    共享）；Web 层 pause()/resume() 直接调用，幂等。
    """

    def __init__(self, audit: AuditLogger | None = None) -> None:
        self._gate = asyncio.Event()
        self._gate.set()
        self._audit: AuditLogger | NullAuditLogger = audit or NullAuditLogger()
        self.excluded_seconds = 0.0        # 暂停 + 执行间隙累计扣账（用时展示扣除）
        self._paused_at: float | None = None
        self._last_tick = time.time()
        self._gap_task: asyncio.Task | None = None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    async def wait(self) -> None:
        """检查点挂起：放行态立即通过；暂停态等到手动继续。"""
        await self._gate.wait()

    def pause(self) -> None:
        if self._paused_at is not None:
            return
        self._paused_at = time.time()
        self._gate.clear()
        self._audit.log("engagement.paused", "runtime", {})

    def resume(self) -> None:
        if self._paused_at is None:
            return
        span = time.time() - self._paused_at
        self.excluded_seconds += span
        self._paused_at = None
        self._gate.set()
        self._audit.log("engagement.resumed", "runtime",
                        {"paused_seconds": round(span, 1)})

    def restore(self, paused: bool, excluded_seconds: float = 0.0) -> None:
        """从持久化状态静默恢复（控制台重启/续传重建）：不记审计、不补扣账。"""
        self.excluded_seconds = max(0.0, float(excluded_seconds))
        if paused and self._paused_at is None:
            self._paused_at = time.time()
            self._gate.clear()

    # ------------------------------------------------------------ 间隙检测

    def start_gap_watch(self) -> None:
        """启动心跳间隙检测（幂等）。"""
        if self._gap_task is not None and not self._gap_task.done():
            return
        self._last_tick = time.time()
        self._gap_task = asyncio.create_task(
            self._gap_watch(), name="spiritscout-gap-watch")

    async def stop_gap_watch(self) -> None:
        task, self._gap_task = self._gap_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _gap_watch(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            gap = now - self._last_tick
            self._last_tick = now
            if gap <= GAP_THRESHOLD_SECONDS:
                continue
            # 暂停态下睡着的区间由暂停扣账覆盖，不重复计
            if not self.paused:
                self.excluded_seconds += gap
            self._audit.log("system.gap", "runtime",
                            {"gap_seconds": round(gap, 1)})

    def snapshot(self) -> dict[str, Any]:
        return {"paused": self.paused,
                "excluded_seconds": round(self.excluded_seconds, 1)}
