"""智能渗透赛专项引擎（设计书 4.5，渗透赛模式专属）。

赛制规则解析器（4.5.1）+ 赛制自适应调度器（4.5.2）+ 赛况监控（4.5.3）。
"""

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
