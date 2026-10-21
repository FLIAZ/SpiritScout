"""合规与策略层：授权边界（ScopeGuard）、分模式策略、人工确认门。

测试边界由模型结合用户上下文维护，ScopeGuard 只记录授权事实与范围标注；
危险操作仍经 ApprovalBroker 对每次调用即时请示用户。
"""

from spiritscout.policy.approvals import ApprovalBroker
from spiritscout.policy.engine import PlatformRule, PolicyEngine
from spiritscout.policy.scope import ScopeGuard

__all__ = ["ApprovalBroker", "PlatformRule", "PolicyEngine", "ScopeGuard"]
