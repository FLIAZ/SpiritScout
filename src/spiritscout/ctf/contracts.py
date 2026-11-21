"""CTF 工具结果契约；由执行器、专项能力和解题循环共用。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _ToolOutcome:
    """一次工具调用的结果：text 回灌模型，其余喂路线状态与前端轨迹。"""

    ok: bool
    text: str
    value: float = 0.0
    findings: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    observation: dict[str, Any] | None = None
    empty: bool = False        # 成功执行但无有效输出（喂空转感知，不做拦截）
    stop: bool = False         # 平台已确认该得分目标完成：不再请求模型写收尾
    metadata: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""       # stop=True 时的确定性短回执
