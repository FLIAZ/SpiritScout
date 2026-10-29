"""调用上下文：只在当前异步调用链传播，不把审批扩大到整个会话。"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class InvocationContext:
    invocation_id: str
    params_digest: str
    record_approval: Callable[[str, bool], None] | None = None


current_invocation: ContextVar[InvocationContext | None] = ContextVar(
    "current_invocation", default=None)


@dataclass(frozen=True)
class ExecutionScope:
    task_id: str = ""
    route_id: str = ""
    agent_id: str = ""
    identity_id: str = ""
    environment_id: str = ""
    step_id: str = ""
    turn: int = 0
    tool_call_id: str = ""
    tool_name: str = ""


current_execution_scope: ContextVar[ExecutionScope | None] = ContextVar(
    "current_execution_scope", default=None)
