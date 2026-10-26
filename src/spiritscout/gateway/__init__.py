"""模型网关：端点接入、路由、故障切换、用量统计与协作调用。"""

from spiritscout.gateway.backends import extract_json
from spiritscout.gateway.collab import (
    CascadeResult,
    CollabGateway,
    DebateResult,
    JudgeResult,
    VoteResult,
    parse_judgment,
)
from spiritscout.gateway.gateway import (
    AgentBoundGateway,
    ChatResponse,
    ModelGateway,
    chat_decision,
)
from spiritscout.gateway.registry import ModelRegistry

__all__ = [
    "AgentBoundGateway", "CascadeResult", "ChatResponse", "CollabGateway",
    "DebateResult", "JudgeResult", "ModelGateway", "ModelRegistry",
    "VoteResult", "chat_decision", "extract_json", "parse_judgment",
]
