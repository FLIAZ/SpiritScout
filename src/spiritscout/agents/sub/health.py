"""分支健康度管理器（设计书 4.3.1 / 6.5）。

健康度得分 = 0.3×进展分 + 0.3×价值分 − 0.2×失败惩罚 − 0.2×风险分
- 失败惩罚：连续失败次数的指数惩罚；
- 风险分（6.5：探测频率、流量特征的暴露风险， 完整化）由 RiskAssessor
  综合三路信号：工具/步骤申报风险（risk_delta 累计）、滑动窗口探测频率、
  防护拦截信号（WAF/403/429 计数）；
- 自动终止低于阈值的路线，释放资源给高评分路线；
- 相似度高于阈值的冗余路线自动合并；
- 记录 token/工具调用成本，作为剪枝量化依据（AWE 资源治理，3.2.4-15）。

系数为可配置项，默认值即设计书公式系数。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from spiritscout.core.models import RouteState, TaskStatus


@dataclass
class HealthWeights:
    progress: float = 0.3
    value: float = 0.3
    failure: float = 0.2
    risk: float = 0.2


@dataclass
class RiskConfig:
    """暴露风险分参数（6.5 风险分 = 探测频率 + 流量特征；系数可调）。"""

    reference_steps_per_minute: float = 30.0   # 窗口内步骤速率达此值记满频率风险
    window_seconds: float = 60.0               # 探测频率滑动窗口
    rate_weight: float = 0.5                   # 频率分量权重
    signal_weight: float = 0.5                 # 拦截信号分量权重
    per_signal: float = 0.25                   # 每个拦截信号（WAF/403/429）的风险量


class RiskAssessor:
    """暴露风险评估（6.5 风险分完整化）。

    有效风险 = max(申报风险, 频率分量×w_r + 信号分量×w_s)，钳制 [0,1]。
    取 max 而非求和：申报风险（如敏感动作）是粘性下限，频率/信号在其之上
    补充「跑得太快/已被拦截」的动态暴露，避免双重计数导致误剪（原则）。
    """

    def __init__(self, config: RiskConfig | None = None):
        self.cfg = config or RiskConfig()

    def assess(self, route: RouteState, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        declared = min(max(route.risk_score, 0.0), 1.0)
        recent = [t for t in route.step_times if now - t <= self.cfg.window_seconds]
        rate = min(len(recent) / max(self.cfg.reference_steps_per_minute, 1e-9), 1.0)
        signals = min(route.detection_signals * self.cfg.per_signal, 1.0)
        dynamic = self.cfg.rate_weight * rate + self.cfg.signal_weight * signals
        return min(max(declared, dynamic), 1.0)


class BranchHealthManager:
    def __init__(
        self,
        *,
        prune_threshold: float = 0.2,
        weights: HealthWeights | None = None,
        risk: RiskConfig | None = None,
        max_routes: int = 8,
        min_steps_before_prune: int = 2,
    ):
        self.prune_threshold = prune_threshold
        self.w = weights or HealthWeights()
        self.risk_assessor = RiskAssessor(risk)
        self.max_routes = max_routes   # 分支数硬上限（防分支爆炸，9.1）
        self.min_steps_before_prune = min_steps_before_prune  # 宽限期：给路线起步机会

    # ------------------------------------------------------------ 评分

    def evaluate(self, route: RouteState, now: float | None = None) -> float:
        """计算并回写健康度得分（now 可注入以保证测试确定性）。"""
        failure_penalty = 1.0 - 0.5 ** route.consecutive_failures if route.consecutive_failures else 0.0
        progress = min(max(route.progress, 0.0), 1.0)
        value = min(max(route.value_score, 0.0), 1.0)
        risk = self.risk_assessor.assess(route, now)
        route.health = (
            self.w.progress * progress
            + self.w.value * value
            - self.w.failure * failure_penalty
            - self.w.risk * risk
        )
        return route.health

    # ------------------------------------------------------------ 剪枝/合并

    def should_prune(self, route: RouteState) -> bool:
        """低于阈值 → 剪枝（宽限期内不剪：起步路线进展/价值均为 0 属正常）。"""
        if route.status not in (TaskStatus.RUNNING, TaskStatus.PENDING):
            return False
        if route.steps_executed < self.min_steps_before_prune:
            return False
        return self.evaluate(route) < self.prune_threshold

    def prune(self, route: RouteState, reason: str) -> None:
        route.status = TaskStatus.PRUNED
        route.notes.append(f"pruned: {reason}")

    @staticmethod
    def signature(route_name: str) -> set[str]:
        """路线相似度签名（用于冗余合并）。"""
        return set(route_name.lower().replace("_", " ").split())

    def should_merge(self, a: RouteState, b: RouteState) -> bool:
        """Jaccard 相似度 > 0.6 → 冗余路线合并（4.3.1）。"""
        sa, sb = self.signature(a.name), self.signature(b.name)
        if not sa or not sb:
            return False
        jaccard = len(sa & sb) / len(sa | sb)
        return jaccard > 0.6

    def rebalance(self, routes: list[RouteState]) -> list[RouteState]:
        """一轮评估：剪枝低分路线、合并冗余路线，返回存活路线。"""
        alive: list[RouteState] = []
        for route in routes:
            if self.should_prune(route):
                self.prune(route, f"健康度 {route.health:.2f} 低于阈值 {self.prune_threshold}")
                continue
            alive.append(route)
        # 冗余合并：保留健康度更高者
        merged: list[RouteState] = []
        for route in alive:
            dup = next(
                (r for r in merged if r is not route and self.should_merge(r, route)), None
            )
            if dup is not None:
                loser, winner = (route, dup) if dup.health >= route.health else (dup, route)
                loser.status = TaskStatus.PRUNED
                loser.notes.append(f"merged_into: {winner.name}")
                if loser is dup:
                    merged.remove(dup)
                    merged.append(route)
            else:
                merged.append(route)
        return merged
