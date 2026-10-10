"""合规与策略层：授权边界（ScopeGuard）、分模式策略、人工确认门。

原则（合规自查 G1/G5）：默认 deny；任何出网行为先过 ScopeGuard；
敏感操作需显式开启 + 人工确认。
"""

from spiritscout.policy.approvals import ApprovalBroker
from spiritscout.policy.engine import PlatformRule, PolicyEngine
from spiritscout.policy.scope import ScopeGuard

__all__ = ["ApprovalBroker", "PlatformRule", "PolicyEngine", "ScopeGuard"]
