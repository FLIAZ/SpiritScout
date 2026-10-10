"""CVSS 3.1 基础分计算器（适配决策 A4：自实现，不引外部依赖）。

实现 FIRST 规范公式（https://www.first.org/cvss/v3.1/specification-document），
输入向量字符串，输出 base score 与严重度映射。
"""

from __future__ import annotations

import math

from spiritscout.core.models import Severity

_METRICS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "S": {"U": "U", "C": "C"},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_PR = {  # PR 取值随 Scope 变化
    "U": {"N": 0.85, "L": 0.62, "H": 0.27},
    "C": {"N": 0.85, "L": 0.68, "H": 0.5},
}


def _roundup(x: float) -> float:
    """CVSS 3.1 规范的小数进位（精确到 0.1，见规范附录 A）。"""
    i = round(x * 100000)
    if i % 10000 == 0:
        return i / 100000.0
    return (math.floor(i / 10000) + 1) / 10.0


def parse_vector(vector: str) -> dict[str, str]:
    """解析 'CVSS:3.1/AV:N/AC:L/...' 为指标字典。"""
    parts = vector.replace("CVSS:3.1/", "").split("/")
    out: dict[str, str] = {}
    for part in parts:
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[k] = v
    missing = {"AV", "AC", "PR", "UI", "S", "C", "I", "A"} - out.keys()
    if missing:
        raise ValueError(f"CVSS 向量缺少指标: {sorted(missing)}")
    return out


def cvss31_base_score(vector: str) -> float:
    """计算 CVSS 3.1 基础分。"""
    m = parse_vector(vector)
    scope = m["S"]
    isc_base = 1 - (1 - _METRICS["C"][m["C"]]) * (1 - _METRICS["I"][m["I"]]) * (1 - _METRICS["A"][m["A"]])
    if scope == "U":
        impact = 6.42 * isc_base
    else:
        impact = 7.52 * (isc_base - 0.029) - 3.25 * (isc_base - 0.02) ** 15
    exploitability = (
        8.22 * _METRICS["AV"][m["AV"]] * _METRICS["AC"][m["AC"]]
        * _PR[scope][m["PR"]] * _METRICS["UI"][m["UI"]]
    )
    if impact <= 0:
        return 0.0
    if scope == "U":
        return _roundup(min(impact + exploitability, 10))
    return _roundup(min(1.08 * (impact + exploitability), 10))


def severity_from_score(score: float) -> Severity:
    """CVSS 分数 → 系统严重度（收录门槛：MEDIUM 及以上，硬约束 C5）。"""
    if score <= 0:
        return Severity.INFO
    if score < 4.0:
        return Severity.LOW
    if score < 7.0:
        return Severity.MEDIUM
    if score < 9.0:
        return Severity.HIGH
    return Severity.CRITICAL
