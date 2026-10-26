"""解析和校验赛制规则。"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class ContestType(str, enum.Enum):
    ADAPTIVE = "adaptive"      # 开放赛制：按用户规则与现场由模型规划
    JEOPARDY = "jeopardy"      # 解题赛
    AWD = "awd"                # 攻防对抗赛
    RANGE = "range"            # 靶场渗透赛

    @classmethod
    def _missing_(cls, value: object) -> ContestType | None:
        """旧标签继续兼容；新赛制名称原样承载，不作为校验白名单。"""
        if not isinstance(value, str) or not value.strip():
            return None
        member = str.__new__(cls, value)
        member._name_ = None
        member._value_ = value
        return member


def extract_flag_patterns_from_text(doc: str) -> list[str]:
    """从自然语言文本提取 Flag 格式声明 → 正则列表（空=未声明）。

    优先「Flag 格式：XXX{...}」显式声明（前缀可不含 flag 字样），
    兜底匹配文本中出现的 xxflagxx{...} 字面量。赛制规则解析与任务
    启动装配共用此确定性提取（同源原则）。
    """
    declared = re.search(
        r"(?:flag\s*格式|flag\s*format)\s*[:：]?\s*([A-Za-z0-9_]+)\{",
        doc, re.IGNORECASE)
    if declared:
        return [re.escape(declared.group(1)) + r"\{[^}\s]{1,128}\}"]
    flag_fmt = re.findall(
        r"([A-Za-z0-9_]*flag[A-Za-z0-9_]*\{[^}]*\})", doc, re.IGNORECASE)
    if flag_fmt:
        prefix = re.match(r"([A-Za-z0-9_]+)\{", flag_fmt[0])
        if prefix:
            return [re.escape(prefix.group(1)) + r"\{[^}\s]{1,128}\}"]
    return []


@dataclass
class SubmitInterface:
    """Flag/报告提交接口规范。"""

    endpoint: str = ""
    auth_token_ref: str = ""           # 凭据引用（不明文入审计，G8）
    rate_limit_per_minute: int = 10
    method: str = "POST"


@dataclass
class CompetitionRules:
    """赛制规则的结构化视图。"""

    contest_type: ContestType = ContestType.ADAPTIVE
    flag_patterns: list[str] = field(default_factory=list)   # 空则用默认 flag{...}
    submit: SubmitInterface = field(default_factory=SubmitInterface)
    dynamic_scoring: bool = False        # 动态分值（血条/时间衰减）
    allowed_cidrs: list[str] = field(default_factory=list)   # AWD 允许攻击的 IP 段
    denied_hosts: list[str] = field(default_factory=list)    # 旧排除声明（模型上下文参考，不编译为执行门）
    forbidden_behaviors: list[str] = field(default_factory=list)  # 赛方禁止行为
    start_time: datetime | None = None
    end_time: datetime | None = None
    attack_window_seconds: int = 300     # AWD 每轮攻击窗口
    defend_window_seconds: int = 300     # AWD 加固窗口
    raw: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------ 解析

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompetitionRules:
        submit = SubmitInterface(**{k: v for k, v in data.get("submit", {}).items()
                                    if k in SubmitInterface.__dataclass_fields__})
        return cls(
            contest_type=ContestType(data.get("contest_type") or "adaptive"),
            flag_patterns=list(data.get("flag_patterns", [])),
            submit=submit,
            dynamic_scoring=bool(data.get("dynamic_scoring", False)),
            allowed_cidrs=list(data.get("allowed_cidrs", [])),
            denied_hosts=list(data.get("denied_hosts", [])),
            forbidden_behaviors=list(data.get("forbidden_behaviors", [])),
            attack_window_seconds=int(data.get("attack_window_seconds", 300)),
            defend_window_seconds=int(data.get("defend_window_seconds", 300)),
            raw=data,
        )

    @classmethod
    def from_text(cls, doc: str) -> CompetitionRules:
        """提取显式格式/限速/网段事实；原文交模型理解，不做赛制分类。"""
        data: dict[str, Any] = {}
        # 不从关键词推断赛制，原文与未知规则字段交由模型理解。
        data["rules_text"] = doc
        # Flag 格式声明（确定性提取，与任务启动装配同源）
        patterns = extract_flag_patterns_from_text(doc)
        if patterns:
            data["flag_patterns"] = patterns
        rate = re.search(r"(\d+)\s*(?:次|requests?)\s*/\s*(?:分钟|min)", doc)
        if rate:
            data["submit"] = {"rate_limit_per_minute": int(rate.group(1))}
        cidrs = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b", doc)
        if cidrs:
            data["allowed_cidrs"] = cidrs
        return cls.from_dict(data)

    # ------------------------------------------------------------ 冲突检测

    def conflicts_with_baseline(self) -> list[str]:
        """与内置合规基线交叉校验（4.5.1：冲突项上报人工确认）。"""
        conflicts: list[str] = []
        if self.contest_type == ContestType.AWD and not self.allowed_cidrs:
            conflicts.append("AWD 赛制未声明允许攻击的 IP 段，攻击线不得启动（防越权）")
        if self.submit.endpoint and not self.submit.endpoint.startswith("https://"):
            conflicts.append(f"提交接口非 HTTPS: {self.submit.endpoint}（凭据泄露风险，G8）")
        if self.submit.rate_limit_per_minute > 60:
            conflicts.append(f"提交频率 {self.submit.rate_limit_per_minute}/min 异常偏高，"
                             f"疑似规则抽取错误")
        return conflicts
