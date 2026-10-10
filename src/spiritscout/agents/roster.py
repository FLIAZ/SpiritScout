"""Agent 编队名册：全系统 Agent 的中文别名与职责，单一事实源。

Web 编队页、拓扑视图、任务列表的显示名一律取自此表，避免多处硬编码漂移。
name 为中文别名（用户面向），duty 为一句话职责，default_tier 为未显式分配
模型时的网关路由档位（4.9.1 五档）。
"""

from __future__ import annotations

from spiritscout.core.models import AgentType

ROSTER: list[dict[str, str]] = [
    {"key": AgentType.MASTER.value, "name": "灵枢",
     "duty": "指挥中枢：理解任务意图，规划 DAG，向各子 Agent 下发任务并复盘收敛",
     "default_tier": "flagship"},
    {"key": AgentType.RECON.value, "name": "斥候",
     "duty": "侦察测绘：指纹识别、路径发现、敏感信息收集",
     "default_tier": "fast"},
    {"key": AgentType.PORT_SCAN.value, "name": "听风",
     "duty": "端口与服务探测（SRC 模式；CTF 模式禁用）",
     "default_tier": "fast"},
    {"key": AgentType.WEB_VULN.value, "name": "破壁",
     "duty": "Web 漏洞探测：SQL 注入 / XSS / SSRF / 信息泄露等",
     "default_tier": "code"},
    {"key": AgentType.HOST_VULN.value, "name": "贯甲",
     "duty": "主机与服务漏洞探测",
     "default_tier": "code"},
    {"key": AgentType.POST_EXPLOIT.value, "name": "深耕",
     "duty": "后渗透：凭据、横向移动、拓扑测绘",
     "default_tier": "rigorous"},
    {"key": AgentType.CTF_WEB.value, "name": "蛛丝",
     "duty": "CTF Web 方向解题",
     "default_tier": "code"},
    {"key": AgentType.CTF_PWN.value, "name": "破壳",
     "duty": "CTF Pwn 方向解题",
     "default_tier": "rigorous"},
    {"key": AgentType.CTF_REVERSE.value, "name": "回溯",
     "duty": "CTF 逆向方向解题",
     "default_tier": "rigorous"},
    {"key": AgentType.CTF_CRYPTO.value, "name": "译码",
     "duty": "CTF 密码学方向解题",
     "default_tier": "code"},
    {"key": AgentType.CTF_MISC.value, "name": "拾遗",
     "duty": "CTF Misc 方向解题",
     "default_tier": "fast"},
    {"key": AgentType.REPORT.value, "name": "书记",
     "duty": "报告编纂：收录漏洞整理成渗透报告 / WriteUp",
     "default_tier": "fast"},
]

_BY_KEY: dict[str, dict[str, str]] = {r["key"]: r for r in ROSTER}


def roster_view() -> list[dict[str, str]]:
    """Web 编队页数据：完整名册（含中文别名/职责/默认档位）。"""
    return [dict(r) for r in ROSTER]


def display_name(key: str) -> str:
    """Agent 显示名：中文别名（未知 key 原样返回，不静默吞掉新类型）。"""
    entry = _BY_KEY.get(key)
    return entry["name"] if entry else key
