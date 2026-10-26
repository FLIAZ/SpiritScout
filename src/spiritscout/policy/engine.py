"""工具策略与分模式审批。

CTF、智能体模式自动授权并记录操作；SRC 模式按工具风险与动作类别审批。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import PolicyViolation
from spiritscout.core.models import RunMode
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.invocation import current_invocation
from spiritscout.tools.search_policy import is_search_tool
from spiritscout.tools.spec import ActionCategory, RiskLevel, ToolSpec

# 各模式禁用的工具类别（G2）
_MODE_FORBIDDEN_TOOLS: dict[RunMode, set[str]] = {
    RunMode.SRC: set(),
    RunMode.CTF: set(),                              # CTF 全自动授权，能力由题目与工具目录决定
    RunMode.COMPETITION: set(),                       # 由赛制规则动态追加
}

# 危险语义动作集（G5）：类别由模型在工具决策中如实自报（action 参数），
# 管道只认类别、不扫内容——不写关键词模板，不用内容猜测代替模型判断。
# 只有调用含义本身危险的动作才即时请示用户；intrusive_write（普通写入/
# 上传/重启容器等）如实留痕但不打断——审批以「本次调用要干什么」为准，
# 不再以工具能力级别整体判定。
DANGEROUS_ACTIONS = {
    ActionCategory.DELETE_FILES.value,
    ActionCategory.REVERSE_SHELL.value,
    ActionCategory.WEBSHELL_UPLOAD.value,
    ActionCategory.CONTAINER_ESCAPE.value,
    ActionCategory.BRUTE_FORCE.value,
}


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
        # SRC 高风险动作即时确认门：None=无交互面，敏感动作直接拒绝
        self.approval_gate = approval_gate
        self.forbidden_tools: set[str] = set(_MODE_FORBIDDEN_TOOLS.get(mode, set()))

    @property
    def automatic_approval(self) -> bool:
        """CTF 与智能体模式在工具审批层逐次自动放行。"""
        return self.mode in {RunMode.CTF, RunMode.COMPETITION}

    # ------------------------------------------------------------ 工具门

    @staticmethod
    def requires_approval(spec: ToolSpec, action: str | None) -> bool:
        """本次调用在 SRC 模式下是否需要即时请示用户。

        - 模型已声明行为类别：只看类别是否属危险语义——删除文件/反弹 shell/
          WebShell 上传/容器逃逸/爆破逐次审批，其余声明（含 intrusive_write）
          不打断；
        - 未声明类别但宿主将其定为 INTRUSIVE 且工具无行为声明机制：本次
          语义不可知，按能力级风险逐次审批兜底。带声明机制的工具注册时必有
          声明（registry 对缺失声明直接报错），不会走到这里。
        """
        if action is not None:
            return action in DANGEROUS_ACTIONS
        return spec.risk == RiskLevel.INTRUSIVE and not spec.action_required

    async def ensure_tool_allowed(self, spec: ToolSpec, action: str | None = None) -> None:
        """CTF / 智能体模式自动授权并记账，SRC 危险动作即时审批。"""
        act = action or spec.name
        if self.mode == RunMode.COMPETITION and (spec.internet_search or is_search_tool(spec.name)):
            self._deny("智能体模式禁止公开网页搜索，不允许访问 Exa 等外部搜索服务")
        if self.automatic_approval:
            # 模式已预授权所有命令；读取、敏感和破坏性能力都记录本次调用，
            # 不创建人工待办，也不伪造经过人工确认的记录。
            context = current_invocation.get()
            self.audit.log("policy.auto_approved", "policy_engine", {
                "mode": self.mode.value, "tool": spec.name, "action": act,
                "invocation_id": context.invocation_id if context else "",
                "params_digest": context.params_digest if context else "",
                "reason": "CTF 模式全自动操作预授权" if self.mode == RunMode.CTF
                          else "智能体模式全自动操作预授权"})
            if context is not None and context.record_approval is not None:
                context.record_approval(act, True)
            return
        granted = False
        if (
            spec.risk != RiskLevel.DESTRUCTIVE
            and spec.name not in self.forbidden_tools
            and self.requires_approval(spec, action)
        ):
            if self.approval_gate is None:
                # 无交互面：不能静默放行，留痕后交给 check_tool 拒绝
                self.audit.log("policy.approval_unavailable", "policy_engine",
                               {"tool": spec.name, "action": act})
            else:
                context = current_invocation.get()
                detail = f"工具 {spec.name}（风险级 {spec.risk.value}）请求执行动作 {act}"
                if context is not None:
                    detail += (f"；调用 {context.invocation_id}"
                               f"；参数摘要 {context.params_digest}")
                granted = await self.approval_gate(act, detail)
                if context is not None and context.record_approval is not None:
                    context.record_approval(act, granted)
        self.check_tool(spec, action, invocation_approved=granted)

    def check_tool(self, spec: ToolSpec, action: str | None = None, *,
                   invocation_approved: bool = False) -> None:
        """工具调用前硬校验。任何违规 → PolicyViolation（拒绝执行）。"""
        if self.mode == RunMode.COMPETITION and (spec.internet_search or is_search_tool(spec.name)):
            self._deny("智能体模式禁止公开网页搜索")
        if self.automatic_approval:
            return
        if spec.risk == RiskLevel.DESTRUCTIVE:
            self._deny(f"破坏性工具一律禁止: {spec.name}（G3 禁 DoS）")
        if spec.name in self.forbidden_tools:
            self._deny(f"模式 {self.mode.value} 禁用工具: {spec.name}（G2 模式能力边界）")
        if self.requires_approval(spec, action):
            if not invocation_approved:
                self._deny(
                    f"危险动作未授权: {action or spec.name}"
                    f"（G5：需用户对本次调用即时审批放行）"
                )
            act = action or spec.name
            if act in self.platform_rule.requires_approval_for:
                self._deny(
                    f"平台 {self.platform_rule.platform} 要求敏感测试提前报备: {act}"
                )
        elif action and action != ActionCategory.ANALYSIS.value:
            act = action
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
