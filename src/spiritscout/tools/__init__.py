"""工具抽象层（设计书 4.3.1 执行引擎与工具适配层 / 3.2.4-10）。

CLI / Docker / MCP 三态统一封装；命令生成、参数校验、超时控制、
结构化结果解析（输出窗口截断）；失败重试与降级替代。
"""

from spiritscout.tools.executor import (
    DockerExecutor,
    LocalExecutor,
    ToolExecutor,
    docker_available,
)
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

__all__ = [
    "DockerExecutor",
    "LocalExecutor",
    "RiskLevel",
    "ToolExecutor",
    "ToolKind",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "docker_available",
]
