"""核验基线规则（设计书 4.3.2 初筛/收录基线，规则与阈值均可配置化）。

规则引擎是裁决者：初筛淘汰、收录门槛、CVSS 向量模板全部以显式规则表达，
LLM 仅在排查建议环节参与（建议不直接进入裁决）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from spiritscout.core.models import Severity, Vulnerability

# 各漏洞类的保守 CVSS 3.1 向量模板（初判定级；核验后可按证据调整）
DEFAULT_CVSS_VECTORS: dict[str, str] = {
    "sqli": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N",          # 6.5
    "xss": "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",           # 6.1
    "ssrf": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N",          # 5.3
    "info_disclosure": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N",  # 5.3
    "rce": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",           # 9.8
}

# 误报特征签名（MVP 内置； 由误报特征库装载扩充）
FP_SIGNATURES: list[dict] = [
    {"id": "fp-encoded-reflect", "vuln_type": "xss",
     "desc": "反射内容经 HTML 实体编码，浏览器不可执行",
     "check": "xss_encoded"},
    {"id": "fp-static-error-page", "vuln_type": "sqli",
     "desc": "报错特征来自静态自定义错误页（基线响应即含同样特征）",
     "check": "error_in_baseline"},
    {"id": "fp-canary-no-reflect", "vuln_type": "ssrf",
     "desc": "canary 出现在响应中但为原样回显参数而非实际请求结果",
     "check": "ssrf_echo_only"},
]


@dataclass
class ScreenVerdict:
    passed: bool
    reason: str = ""


@dataclass
class BaselineRules:
    """初筛与收录基线（默认值即设计书 4.3.2/硬约束 C5）。"""

    min_severity_to_pool: Severity = Severity.MEDIUM   # 收录门槛：中危及以上
    min_confidence: float = 0.5                        # 初筛置信度下限
    min_reproductions: int = 2                         # 交叉复现方式数（≥2 种）
    cvss_vectors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CVSS_VECTORS))

    def screen(self, vuln: Vulnerability) -> ScreenVerdict:
        """初筛：无证据 / 低置信 / 信息级直接淘汰（仅记录，不进复现环节）。"""
        if not vuln.evidence:
            return ScreenVerdict(False, "无证据链，初筛淘汰")
        if vuln.confidence < self.min_confidence:
            return ScreenVerdict(False, f"置信度 {vuln.confidence:.2f} 低于初筛下限")
        if vuln.severity == Severity.INFO:
            return ScreenVerdict(False, "信息级发现仅记录（G4）")
        return ScreenVerdict(True)

    def accept_to_pool(self, vuln: Vulnerability) -> ScreenVerdict:
        """收录判定：中危及以上 且 实际可利用（PoC 实测通过）——硬约束 C5。"""
        if vuln.severity.rank < self.min_severity_to_pool.rank:
            return ScreenVerdict(False, f"{vuln.severity.value} 低于收录门槛，仅记录不输出")
        if not (vuln.poc_verified and vuln.exploitable):
            return ScreenVerdict(False, "PoC 未实测通过/不可利用，不收录")
        return ScreenVerdict(True)
