"""工具规格与结果模型。

每个工具声明：类型（CLI/Docker/MCP）、风险级别、参数 schema、超时、
目标抽取器（供 ScopeGuard 校验，合规 G1）。
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class ToolKind(str, enum.Enum):
    CLI = "cli"          # 本地命令行（subprocess）
    DOCKER = "docker"    # 容器隔离执行
    MCP = "mcp"          # MCP 工具服务（预留， 后扩展）
    BUILTIN = "builtin"  # 内置 Python 实现（http_probe 等）


class RiskLevel(str, enum.Enum):
    READONLY = "readonly"      # 只读探测（默认允许）
    INTRUSIVE = "intrusive"    # 侵入性（写入/利用，需敏感操作开关 + 人工确认 G5）
    DESTRUCTIVE = "destructive"  # 破坏性（系统默认拒绝封装，合规 G3）


@dataclass
class ToolSpec:
    name: str
    kind: ToolKind
    description: str = ""
    risk: RiskLevel = RiskLevel.READONLY
    timeout_seconds: int = 300
    # 参数模板：CLI 命令行模板（{param} 槽位）；BUILTIN 为函数名
    command_template: list[str] = field(default_factory=list)
    allowed_params: set[str] = field(default_factory=set)
    # 目标抽取：从参数中取授权校验对象（host/ip/url）
    target_param: str = "target"
    # 结果解析：stdout → 结构化 dict（None 则返回原文摘要）
    parser: Callable[[str, str, int], dict[str, Any]] | None = None
    # 降级替代工具名（失败时自动切换，4.3.1）
    fallback_tool: str | None = None


@dataclass
class ToolResult:
    tool: str
    ok: bool
    returncode: int = 0
    stdout: str = ""          # 已按 max_output_chars 截断（窗口截断防上下文污染）
    stderr: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    timed_out: bool = False
    error: str = ""
    attempts: int = 1
    rolled_back: bool = False  # 回滚保护触发标记

    def summary(self, max_chars: int = 500) -> str:
        text = self.stdout or self.error or self.stderr
        return text[:max_chars] + ("..." if len(text) > max_chars else "")
