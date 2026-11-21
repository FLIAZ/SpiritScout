"""SRC 实际危害证明契约。

发现者可以提交任意漏洞候选，但进入可提交报告前需要通过与漏洞类型匹配的
影响证明。模型负责选择实验，验证器负责检查证明字段是否来自真实执行记录。
"""

from __future__ import annotations

from typing import Any

_PROOF_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "idor": ("identity_a", "identity_b", "object_owner", "control_ref", "variant_ref"),
    "authorization": ("identity_a", "identity_b", "control_ref", "variant_ref"),
    "access_control": ("identity_a", "identity_b", "control_ref", "variant_ref"),
    "xss": ("execution_ref", "context"),
    "ssrf": ("callback_ref",),
    "sqli": ("evidence_ref", "proof_kind"),
    "business_logic": ("before_state", "after_state", "invariant"),
    "payment_logic": ("before_state", "after_state", "invariant"),
    "file_upload": ("execution_ref", "execution_marker"),
    "rce": ("execution_ref", "execution_marker"),
}


def required_fields(vuln_type: str) -> tuple[str, ...]:
    """返回漏洞类型的最小影响证明字段。未知类型使用通用证据契约。"""

    return _PROOF_REQUIREMENTS.get(str(vuln_type or "").lower(), ("evidence_ref", "impact"))


def validate_impact_proof(
    vuln_type: str,
    proof: Any,
    *,
    execution_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """校验并规范化影响证明。

    这里只做结构和来源校验，危害定级仍交给模型与平台规则。引用执行记录时，
    execution_ids 必须来自本次独立复测，避免把历史或发现阶段文字当作影响证明。
    """

    if not isinstance(proof, dict):
        return None
    clean: dict[str, Any] = {}
    for key, value in proof.items():
        if isinstance(value, (str, int, float, bool)):
            clean[key] = value
        elif isinstance(value, list):
            clean[key] = [item for item in value if isinstance(item, (str, int, float, bool))][:32]
        elif isinstance(value, dict):
            clean[key] = {str(k): v for k, v in value.items()
                          if isinstance(v, (str, int, float, bool))}

    if any(not str(clean.get(field) or "").strip() for field in required_fields(vuln_type)):
        return None
    for field in ("execution_ref", "evidence_ref", "control_ref", "variant_ref", "callback_ref"):
        value = clean.get(field)
        if value and execution_ids is not None and str(value) not in execution_ids:
            return None
    if vuln_type.lower() == "xss" and clean.get("executed") is not True:
        return None
    if vuln_type.lower() in {"rce", "file_upload"} and not str(clean.get("execution_marker") or "").strip():
        return None
    clean["proof_type"] = vuln_type.lower()
    clean["verified"] = True
    return clean


def proof_status(vuln_type: str, proof: Any, *, execution_ids: set[str] | None = None) -> str:
    """返回报告层使用的影响证明状态。"""

    return "impact_proven" if validate_impact_proof(vuln_type, proof, execution_ids=execution_ids) else "technical_only"
