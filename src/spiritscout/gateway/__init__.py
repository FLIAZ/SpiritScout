"""多模型网关（设计书 4.9）：统一接入、注册表、路由、主备切换、预算治理。

模型接入方式（硬约束 C9）：OpenAI/Anthropic 兼容格式 API，
配置 base_url + api_key 即启用云端模型；LiteLLM 为统一调用层。
原则：模型是建议者、规则引擎是裁决者（4.9.5）。
六协作模式（4.9.3）：路由/主备（gateway）+ 投票/辩论/级联/异构角色（collab）。
"""

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
)
from spiritscout.gateway.registry import ModelRegistry

__all__ = [
    "AgentBoundGateway", "CascadeResult", "ChatResponse", "CollabGateway",
    "DebateResult", "JudgeResult", "ModelGateway", "ModelRegistry",
    "VoteResult", "extract_json", "parse_judgment",
]
