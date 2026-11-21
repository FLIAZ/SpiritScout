"""可信调用方订阅命令原始输出，不改变执行超时或模型结果窗口。"""

from collections.abc import Callable
from contextvars import ContextVar

command_output: ContextVar[Callable[[str, bytes], None] | None] = ContextVar(
    "command_output", default=None,
)
