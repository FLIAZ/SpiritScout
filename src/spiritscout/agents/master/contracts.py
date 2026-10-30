"""主调度器共享的状态、上下文与角色映射。"""
from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from spiritscout.agents.sub.base import SubAgent
from spiritscout.blackboard import ConvergenceEngine
from spiritscout.blackboard.store import Blackboard
from spiritscout.bus.base import EventBus
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.models import (
    AgentType,
)
from spiritscout.gateway import ModelGateway
from spiritscout.policy.engine import PolicyEngine

# 战术 → 子 Agent 映射；模式装配时可覆盖或扩展。
TACTIC_AGENT_MAP: dict[str, tuple[AgentType, str]] = {
    "BUSINESS-CONTEXT": (AgentType.BUSINESS_CONTEXT, "business_context"),
    "TA0043": (AgentType.RECON, "recon"),          # 侦察
    "TA0001": (AgentType.WEB_VULN, "web_probe"),   # 初始访问（T1190 Web 打点）
    "TA0007": (AgentType.PORT_SCAN, "port_scan"),  # 发现（端口与服务测绘）
    "HOST-VULN": (AgentType.HOST_VULN, "host_probe"),  # 主机与服务审计
    "TA0006": (AgentType.POST_EXPLOIT, "post_exploit"),  # 凭据线索与横向测绘
}


class MasterState(TypedDict):
    target: str
    round_no: int
    dag: dict[str, Any]
    tasks_dispatched: Annotated[list[dict], operator.add]
    round_views: Annotated[list[dict], operator.add]
    continue_iteration: bool
    stop_reason: str
    plan_source: str


@dataclass
class MasterContext:
    bus: EventBus
    blackboard: Blackboard
    convergence: ConvergenceEngine
    gateway: ModelGateway
    policy: PolicyEngine
    agents: dict[AgentType, SubAgent]
    default_routes: int = 3
    audit: AuditLogger | None = None
    enable_llm_planning: bool = True
    # 模式装配器注入：CTF/渗透赛覆盖战术映射与规划来源
    tactic_map: dict[str, tuple[AgentType, str]] | None = None
    plan_override: Any = None   # Callable[[MasterState], dict] 返回 {"dag_nodes": [...]}
    extra: dict[str, Any] = field(default_factory=dict)
    knowledge: Any = None       # KnowledgeStore（参考事实检索，None=不接）
    ring: Any = None            # RingLedger 环形台账（与子 Agent 共持；None=不督导）
    pause_gate: Any = None      # 暂停门（与子 Agent 共持；None=无暂停能力）
    questions: Any = None       # QuestionBroker（复盘向用户提问；None=不可用）
    journal: Any = None         # 执行台账与加密检查点

    def __post_init__(self) -> None:
        if self.audit is None:
            self.audit = NullAuditLogger()


