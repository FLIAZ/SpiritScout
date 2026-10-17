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
    # 侵入性能力（可能改写环境/触碰状态）：调用时按本次行为语义声明，
    # 仅危险语义（G5）逐次即时请示
    INTRUSIVE = "intrusive"
    DESTRUCTIVE = "destructive"  # 破坏性（系统默认拒绝封装，合规 G3）


class ActionCategory(str, enum.Enum):
    """模型声明的行为类别；未知值必须补充说明，不能隐式降为只读。"""

    ANALYSIS = "analysis"
    REVERSE_SHELL = "reverse_shell"
    WEBSHELL_UPLOAD = "webshell_upload"
    CONTAINER_ESCAPE = "container_escape"
    BRUTE_FORCE = "brute_force"
    DELETE_FILES = "delete_files"
    INTRUSIVE_WRITE = "intrusive_write"


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
    # 目标抽取：调用前由 ToolRegistry 对这些参数统一做授权校验。
    # 空元组表示纯本地工具；支持多个字段以适配 MCP 工具的不同 schema。
    target_params: tuple[str, ...] = ("target",)
    # True 时，调用中必须至少提供一个可验证目标；适合远程网络工具。
    # 无法从参数证明目标在范围内时 fail closed。
    scope_required: bool = False
    # 某些 MCP 工具把目标嵌在原始 HTTP 报文等复合参数中，
    # 无法用 target_params 直接取值。提取器只返回待 ScopeGuard
    # 校验的目标，不改写真实工具入参。
    scope_target_extractor: Callable[[dict[str, Any]], list[str]] | None = None
    # 结果解析：stdout → 结构化 dict（None 则返回原文摘要）
    parser: Callable[[str, str, int], dict[str, Any]] | None = None
    # 降级替代工具名（失败时自动切换，4.3.1）
    fallback_tool: str | None = None
    # 放在末尾保留既有位置参数兼容性；所有调用方共用 Registry 校验。
    action_required: bool = False
    action_param: str = "action"  # MCP 原生 action 与安全行为声明使用独立字段
    automatic_replay: bool = True
    # 仅宿主可信配置赋值，远端工具描述不能授予角色权限。
    allowed_agents: tuple[str, ...] = ()


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
    invocation_id: str = ""
    execution_status: str = ""
    effect_status: str = "none"

    def summary(self, max_chars: int = 500) -> str:
        text = self.stdout or self.error or self.stderr
        return text[:max_chars] + ("..." if len(text) > max_chars else "")

    def failure_reason(self, max_chars: int = 6000) -> str:
        """错误可能由外部协议放在正文或 stderr，不能把已有诊断变成未知错误。"""
        text = self.error or self.stderr or self.stdout
        if text.strip():
            return text.strip()[:max_chars]
        if self.timed_out:
            return f"工具 {self.tool} 执行超时（退出码 {self.returncode}）"
        return f"工具 {self.tool} 执行未成功（退出码 {self.returncode}，服务未返回诊断正文）"
