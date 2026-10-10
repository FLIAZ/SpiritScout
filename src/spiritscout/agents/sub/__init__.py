"""探测执行类子 Agent 基座（设计书 4.3）。

通用能力：本地路线生成器（默认 3 条差异化路线）、执行引擎、本地共享缓存、
分支健康度管理器（自动剪枝/合并）、局部反思与纠错（每 3 步复盘）。
"""

from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.agents.sub.cache import LocalSharedCache
from spiritscout.agents.sub.health import BranchHealthManager

__all__ = ["BranchHealthManager", "LocalSharedCache", "RoutePlan", "StepResult", "SubAgent"]
