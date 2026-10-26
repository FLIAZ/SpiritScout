"""智能体竞赛的规则解析、任务调度与赛况监控。"""

from spiritscout.competition.engine import (
    ChallengeCard,
    CompetitionEngine,
    PriorityWeights,
)
from spiritscout.competition.rules import CompetitionRules, ContestType
from spiritscout.competition.strategy import (
    competition_solver_prompt,
    competition_strategy_prompt,
)

__all__ = [
    "ChallengeCard", "CompetitionEngine", "CompetitionRules",
    "ContestType", "PriorityWeights", "competition_solver_prompt",
    "competition_strategy_prompt",
]
