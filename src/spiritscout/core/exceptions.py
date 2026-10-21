"""系统异常体系。"""

from __future__ import annotations


class SpiritScoutError(Exception):
    """系统基础异常。"""


class ConfigurationError(SpiritScoutError):
    """配置错误（缺失、格式非法、校验失败）。"""


class ScopeViolation(SpiritScoutError):
    """授权声明无效（如范围为空或不在声明的测试时间窗口）。"""


class PolicyViolation(SpiritScoutError):
    """违反分模式策略或平台红线（如 CTF 模式调用端口扫描）。"""


class BudgetExceeded(SpiritScoutError):
    """模型/资源预算耗尽（成本硬顶）。"""


class VersionConflict(SpiritScoutError):
    """黑板写入版本冲突（乐观锁）。"""

    def __init__(self, layer: str, record_id: str, expected: int, actual: int):
        super().__init__(
            f"黑板版本冲突 layer={layer} id={record_id} expected={expected} actual={actual}"
        )
        self.layer = layer
        self.record_id = record_id
        self.expected = expected
        self.actual = actual


class ToolExecutionError(SpiritScoutError):
    """工具执行失败（超时、非零退出、沙箱错误）。"""


class HumanApprovalRequired(SpiritScoutError):
    """敏感操作需要人工确认（报备门/确认门）。"""


class RuleConflictError(SpiritScoutError):
    """赛制规则与合规基线冲突——携带冲突清单，供前端即时确认对话逐项呈现。"""

    def __init__(self, message: str, conflicts: list[str]):
        super().__init__(message)
        self.conflicts = list(conflicts)
