"""赛制自适应调度器 + 赛况监控（设计书 4.5.2 / 4.5.3 / 6.7）。

- Jeopardy：按 6.7 公式动态排题，高分且可并行优先；
- AWD：攻击/防守双线任务队列 + 「得分速率 × 失分速率 × 剩余时间」动态换防；
- 靶场渗透赛：复用常规渗透链路，提交切换为赛方接口（装配层职责）；
- 赛况监控：可插拔榜单拉取器，输出排名/速率事件。

6.7 任务优先级公式：
    任务优先级 = 赛题分值 × 突破成功率预估 × 时间衰减系数 ÷ 资源消耗预估
（系数可配置）
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from spiritscout.competition.rules import CompetitionRules, ContestType
from spiritscout.core.audit import AuditLogger, NullAuditLogger

# 赛况拉取器签名：async fn() -> {"rank": int, "score": float, "solved": [...], ...}
ScoreboardFetcher = Callable[[], Awaitable[dict[str, Any]]]


@dataclass
class PriorityWeights:
    """6.7 公式系数（默认值即设计书权重，可调参）。"""

    success_history_default: float = 0.5   # 无历史数据时的突破成功率先验
    time_decay_grace: float = 0.1          # 时间衰减下限（避免末段归零）
    resource_cost_default: float = 1.0     # 资源消耗预估缺省值


@dataclass
class ChallengeCard:
    """调度视图中的赛题卡片。"""

    challenge_id: str
    title: str = ""
    direction: str = "misc"
    points: float = 100.0
    solved: bool = False
    estimated_cost: float = 1.0            # 资源消耗预估（token/时间）
    history_success: float | None = None   # 同类题历史命中率
    route_health: float = 0.5              # 当前路线健康度


@dataclass
class AWDState:
    """AWD 双线状态（动态换防的量化依据）。"""

    attack_score_rate: float = 0.0         # 得分速率（分/轮）
    defend_loss_rate: float = 0.0          # 失分速率（分/轮）
    defense_priority: float = 0.5          # 防守资源配比 0~1


class CompetitionEngine:
    """赛制自适应引擎（渗透赛模式专属， 骨架）。"""

    def __init__(
        self,
        rules: CompetitionRules,
        *,
        weights: PriorityWeights | None = None,
        total_duration_seconds: float = 8 * 3600,
        started_at: float | None = None,
        audit: AuditLogger | None = None,
        bus=None,
        max_parallel: int = 4,
    ):
        self.rules = rules
        self.w = weights or PriorityWeights()
        self.total_duration = total_duration_seconds
        self.started_at = started_at or time.monotonic()
        self.audit = audit or NullAuditLogger()
        self.bus = bus
        self.max_parallel = max_parallel
        self.awd = AWDState()

    # ------------------------------------------------------------ 6.7 优先级

    def time_decay(self, now: float | None = None) -> float:
        """时间衰减系数：剩余时间越少，对耗时长题目降权越狠。"""
        elapsed = (now or time.monotonic()) - self.started_at
        remaining_ratio = max(1.0 - elapsed / max(self.total_duration, 1), 0.0)
        return max(remaining_ratio, self.w.time_decay_grace)

    def success_estimate(self, card: ChallengeCard) -> float:
        """突破成功率预估 = 历史命中率（先验 0.5）× 当前路线健康度。"""
        history = card.history_success \
            if card.history_success is not None else self.w.success_history_default
        return history * max(card.route_health, 0.05)

    def priority(self, card: ChallengeCard, now: float | None = None) -> float:
        """任务优先级 = 分值 × 成功率 × 时间衰减 ÷ 资源消耗（6.7）。"""
        if card.solved:
            return 0.0
        cost = max(card.estimated_cost, 0.01)
        return (card.points * self.success_estimate(card)
                * self.time_decay(now) / cost)

    # ------------------------------------------------------------ Jeopardy 排题

    def schedule_jeopardy(
        self, cards: list[ChallengeCard], now: float | None = None
    ) -> list[ChallengeCard]:
        """按优先级排序选出本轮并行攻克的前 N 题（高分且可并行优先）。"""
        ranked = sorted((c for c in cards if not c.solved),
                        key=lambda c: self.priority(c, now), reverse=True)
        selected = ranked[: self.max_parallel]
        self.audit.log("competition.schedule", "competition_engine", {
            "contest_type": self.rules.contest_type.value,
            "selected": [{"id": c.challenge_id,
                          "priority": round(self.priority(c, now), 3)} for c in selected]})
        return selected

    def mark_solved(self, cards: list[ChallengeCard], challenge_id: str) -> None:
        """Flag 提交成功 → 标记完成，释放资源转向其他赛题（4.5.2）。"""
        for card in cards:
            if card.challenge_id == challenge_id:
                card.solved = True
                self.audit.log("competition.solved", "competition_engine",
                               {"challenge_id": challenge_id, "points": card.points})

    # ------------------------------------------------------------ AWD 动态换防

    def awd_rebalance(self, score_delta: float, loss_delta: float,
                      now: float | None = None) -> float:
        """按「得分速率 × 失分速率 × 剩余时间」调整防守资源配比（5.3.3）。

        失分速率上升 → 防守配比上调；得分停滞且失分为零 → 资源压向攻击线。
        """
        self.awd.attack_score_rate = max(score_delta, 0.0)
        self.awd.defend_loss_rate = max(loss_delta, 0.0)
        total = self.awd.attack_score_rate + self.awd.defend_loss_rate
        if total <= 0:
            self.awd.defense_priority = 0.5
        else:
            raw = self.awd.defend_loss_rate / total
            # 剩余时间越少，换防越果断（时间衰减放大）
            decay = self.time_decay(now)
            self.awd.defense_priority = min(0.9, max(0.1, raw / max(decay, 0.2)))
        self.audit.log("competition.awd_rebalance", "competition_engine", {
            "score_rate": score_delta, "loss_rate": loss_delta,
            "defense_priority": round(self.awd.defense_priority, 3)})
        return self.awd.defense_priority

    def awd_task_split(self, total_workers: int) -> tuple[int, int]:
        """按防守配比划分 (防守工位数, 攻击工位数)。"""
        defend = round(total_workers * self.awd.defense_priority)
        defend = min(max(defend, 1), max(total_workers - 1, 1)) if total_workers > 1 else 1
        return defend, max(total_workers - defend, 0)

    # ------------------------------------------------------------ 赛况监控

    async def poll_scoreboard(self, fetcher: ScoreboardFetcher) -> dict[str, Any]:
        """赛况实时监控骨架（4.5.3）：排名/分数/解题状态 → 事件。"""
        try:
            board = await fetcher()
        except Exception as exc:  # noqa: BLE001 —— 榜单故障不阻断比赛
            self.audit.log("competition.scoreboard_error", "competition_engine",
                           {"error": str(exc)[:200]})
            return {}
        board.setdefault("polled_at", time.time())
        self.audit.log("competition.scoreboard", "competition_engine",
                       {k: v for k, v in board.items() if k != "raw"})
        if self.bus is not None:
            from spiritscout.core.models import Event
            await self.bus.publish(Event(
                topic="competition.scoreboard", data_id="latest",
                source="competition_engine", payload=board))
        return board

    # ------------------------------------------------------------ 收敛判定

    def should_converge(self, cards: list[ChallengeCard],
                        now: float | None = None) -> tuple[bool, str]:
        """所有赛题完成或时间窗口结束 → 收敛（5.2.3-4）。"""
        if all(c.solved for c in cards):
            return True, "全部赛题已完成"
        if self.rules.contest_type != ContestType.AWD and self.time_decay(now) <= self.w.time_decay_grace + 1e-9:
            return True, "比赛时间窗口耗尽"
        return False, ""
