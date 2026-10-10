"""策略引擎：分模式能力边界 + 工具风险门 + 平台红线规则（模型是建议者、规则是裁决者）。

合规自查映射：
- G2 CTF 模式禁端口扫描（硬约束 C7）；
- G3 禁 DoS（DESTRUCTIVE 级工具不注册/一律拒绝）；
- G5 敏感操作门（INTRUSIVE 需 scope 显式开启 + 人工确认）；
-  平台红线（如 bytesrc auto_test_allowed=false → 阻断自动探测）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import PolicyViolation
from spiritscout.core.models import RunMode
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.spec import RiskLevel, ToolSpec

# 各模式禁用的工具类别（G2）
_MODE_FORBIDDEN_TOOLS: dict[RunMode, set[str]] = {
    RunMode.SRC: set(),
    RunMode.CTF: {"port_scan", "nmap", "masscan"},   # CTF 模式不启用端口扫描（4.4）
    RunMode.COMPETITION: set(),                       # 由赛制规则动态追加
}

# 敏感操作动作集（G5，与 Scope.allow_sensitive_actions 联动）
SENSITIVE_ACTIONS = {"reverse_shell", "webshell_upload", "container_escape", "brute_force"}


@dataclass
class PlatformRule:
    """目标 SRC 平台红线（知识库规则的最小运行时视图， 由知识库装载）。"""

    platform: str = ""
    auto_test_allowed: bool = True
    requires_approval_for: set[str] = field(default_factory=set)  # 需报备的敏感测试


class PolicyEngine:
    def __init__(
        self,
        mode: RunMode,
        guard: ScopeGuard,
        *,
        platform_rule: PlatformRule | None = None,
        audit: AuditLogger | None = None,
        approval_gate: Callable[[str, str], Awaitable[bool]] | None = None,
    ):
        self.mode = mode
        self.guard = guard
        self.platform_rule = platform_rule or PlatformRule()
        self.audit = audit or NullAuditLogger()
        # 高风险动作即时确认门（G5 交互形态）：None=无交互面，敏感动作直接拒绝
        self.approval_gate = approval_gate
        self.forbidden_tools: set[str] = set(_MODE_FORBIDDEN_TOOLS.get(mode, set()))

    # ------------------------------------------------------------ 工具门

    async def ensure_tool_allowed(self, spec: ToolSpec, action: str | None = None) -> None:
        """异步工具门：敏感动作未授权且挂有审批门时，先即时询问操作者
        （分支挂起等待裁决）；允许 → 写入会话级批准集后放行，拒绝/无门 →
        落入 check_tool 按违规拒绝。破坏性工具与模式禁用不进入询问——
        G2/G3 是不可让渡的硬红线。"""
        act = action or spec.name
        if (
            spec.risk != RiskLevel.DESTRUCTIVE
            and spec.name not in self.forbidden_tools
            and (spec.risk == RiskLevel.INTRUSIVE or act in SENSITIVE_ACTIONS)
            and not self.guard.is_sensitive_allowed(act)
            and self.approval_gate is not None
        ):
            granted = await self.approval_gate(
                act, f"工具 {spec.name}（风险级 {spec.risk.value}）请求执行动作 {act}")
            if granted:
                self.guard.approve_sensitive(act)
        self.check_tool(spec, action)

    def check_tool(self, spec: ToolSpec, action: str | None = None) -> None:
        """工具调用前硬校验。任何违规 → PolicyViolation（拒绝执行）。"""
        if spec.risk == RiskLevel.DESTRUCTIVE:
            self._deny(f"破坏性工具一律禁止: {spec.name}（G3 禁 DoS）")
        if spec.name in self.forbidden_tools:
            self._deny(f"模式 {self.mode.value} 禁用工具: {spec.name}（G2 模式能力边界）")
        if spec.risk == RiskLevel.INTRUSIVE or (action and action in SENSITIVE_ACTIONS):
            if not self.guard.is_sensitive_allowed(action or spec.name):
                self._deny(
                    f"敏感操作未授权: {action or spec.name}（G5：需 scope.allow_sensitive_actions "
                    f"显式开启并完成人工确认）"
                )
            act = action or spec.name
            if act in self.platform_rule.requires_approval_for:
                self._deny(
                    f"平台 {self.platform_rule.platform} 要求敏感测试提前报备: {act}"
                )

    def check_automation_allowed(self) -> None:
        """平台禁自动化时阻断自动探测（ByteSRC 禁扫描器/自动化，只手工）。"""
        if not self.platform_rule.auto_test_allowed:
            self._deny(
                f"目标平台 {self.platform_rule.platform} 禁止自动化测试，"
                f"系统仅允许输出人工测试清单（平台红线）"
            )

    def forbid_tool(self, name: str) -> None:
        """动态追加禁用（渗透赛模式按赛方规则）。"""
        self.forbidden_tools.add(name)

    # ------------------------------------------------------------ 内部

    def _deny(self, reason: str) -> None:
        self.audit.log("policy.violation", "policy_engine", {"mode": self.mode.value, "reason": reason})
        raise PolicyViolation(reason)
