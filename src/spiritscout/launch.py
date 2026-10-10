"""任务启动装配（Web 控制台会话创建的唯一装配入口）。

授权范围 / 授权对象 / 赛题清单 / 赛制规则的解析与校验只在此处维护，
避免多处事实源漂移（同源原则）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import yaml

from spiritscout.core.exceptions import RuleConflictError, SpiritScoutError
from spiritscout.core.ids import new_id
from spiritscout.core.models import Authorization, RunMode, Scope


@dataclass
class LaunchRequest:
    """启动参数（Web 请求体映射至此）。

    mode=None 即「智能模式」：模式不由用户选择——装配层先按结构化
    输入兜底判定（见 fallback_mode），运行期由主 Agent 的 LLM 路由按任务
    描述语义亲自裁决，必要时重建启动计划（模型是决策者，脚本是兜底）。
    """

    mode: RunMode | None = None
    target: str | None = None
    extra_scope: list[str] = field(default_factory=list)
    platform: str | None = None
    allow_sensitive: bool = False
    rps: float = 10.0
    flag_format: str | None = None
    challenges_yaml: str | None = None      # CTF/渗透赛赛题 YAML 文本
    rules_yaml: str | None = None           # 渗透赛赛制规则 YAML 文本
    accept_rule_conflicts: bool = False
    mission: str | None = None              # 自然语言任务意图（任务式启动）


@dataclass
class LaunchPlan:
    """装配产物：可直接传给 runtime 三个入口。"""

    authorization: Authorization
    target: str                             # SRC 目标 / CTF 占位 "ctf://challenges"
    challenges: list[dict]
    rules: object | None                    # CompetitionRules（仅渗透赛）
    flag_patterns: list[str] | None
    mission: str = ""                       # 任务意图原文（喂主 Agent 规划）
    mode_auto: bool = False                 # 智能模式：待 LLM 路由复核
    source_request: LaunchRequest | None = None  # 重建计划用（LLM 改判时）


# 任务意图中的目标提取：URL 优先，裸域名兜底
_URL_RE = re.compile(r"https?://[^\s，。；\"'<>()]+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b", re.IGNORECASE)


def extract_targets(mission: str) -> tuple[list[str], list[str]]:
    """从自然语言任务意图提取（URL 列表, 域名列表）——用户告诉主 Agent 的
    合约范围经此落到确定性授权边界（规则引擎在网络层执行用户原话）。"""
    urls = [u.rstrip("/").rstrip(".,;") for u in _URL_RE.findall(mission or "")]
    domains: list[str] = []
    for m in _DOMAIN_RE.findall(mission or ""):
        if not any(m in u for u in urls) and m not in domains:
            domains.append(m)
    dedup_urls: list[str] = []
    for u in urls:
        if u not in dedup_urls:
            dedup_urls.append(u)
    return dedup_urls, domains


def parse_challenges_yaml(text: str) -> list[dict]:
    """赛题 YAML 文本 → list[dict]（顶层列表或含 challenges 键的映射）。"""
    data = yaml.safe_load(text)
    if isinstance(data, dict) and "challenges" in data:
        data = data["challenges"]
    if not isinstance(data, list):
        raise SpiritScoutError("赛题格式错误（应为列表或含 challenges 键的 YAML）")
    return [dict(ch) for ch in data]


def parse_rules_yaml(text: str):
    """赛制规则 YAML 文本 → CompetitionRules。"""
    from spiritscout.competition import CompetitionRules

    return CompetitionRules.from_dict(yaml.safe_load(text) or {})


def _mission_flag_patterns(mission: str) -> list[str]:
    """任务描述中的 Flag 格式声明（如「flag 格式：SCTF{...}」）→ 正则列表。

    用户只需在任务里照抄赛方声明，格式边界由确定性提取落到
    FlagManager，无需手填正则（与赛制规则解析共用同一提取器）。
    """
    if not mission:
        return []
    from spiritscout.competition.rules import extract_flag_patterns_from_text

    return extract_flag_patterns_from_text(mission)


def fallback_mode(req: LaunchRequest) -> RunMode:
    """智能模式的结构化兜底判定：不看关键词、不做语义猜测——
    语义裁决归运行期 LLM 路由；此处只在模型不可用时保底：
    给了赛制规则 → 渗透赛；给了赛题清单 → CTF；否则 SRC。"""
    if req.rules_yaml:
        return RunMode.COMPETITION
    if req.challenges_yaml:
        return RunMode.CTF
    return RunMode.SRC


def prepare_launch(req: LaunchRequest) -> LaunchPlan:
    """模式前置校验 + 装配（不静默降级：缺参数即报错并说明）。

     任务式启动：给出自然语言任务意图（mission）时，目标/范围从意图中
    提取（URL/域名），SRC 不再强制 target。
     智能模式：mode=None 时先按结构化输入兜底（fallback_mode），运行期
    LLM 路由可改判并以 source_request 重建本计划。
    """
    mission = (req.mission or "").strip()
    m_urls, m_domains = extract_targets(mission)
    mode_auto = req.mode is None
    if mode_auto:
        req = LaunchRequest(**{**req.__dict__, "mode": fallback_mode(req)})
    if mission and not req.target and m_urls:
        req = LaunchRequest(**{**req.__dict__, "target": m_urls[0]})
    if req.mode == RunMode.SRC and not req.target:
        hint = ("；任务描述中未识别到目标 URL——请写明 http(s):// 地址"
                if mission else "——或在任务描述中写明目标")
        raise SpiritScoutError(f"SRC 模式需要目标 URL{hint}")
    if req.mode == RunMode.CTF and not req.challenges_yaml and not m_urls:
        raise SpiritScoutError(
            "CTF 模式需要赛题清单——粘贴赛题 YAML，或在任务描述中写明赛题 URL")
    if req.mode == RunMode.COMPETITION and not req.rules_yaml:
        raise SpiritScoutError(
            "渗透赛模式需要赛制规则 YAML（contest_type/flag_patterns/submit 等"
            "结构化裁决输入），暂不支持从任务描述推导")

    challenges = (parse_challenges_yaml(req.challenges_yaml)
                  if req.challenges_yaml else [])
    if req.mode == RunMode.CTF and not challenges and m_urls:
        # 任务描述推导赛题清单：每个 URL 合成一题，方向由分类器按
        # 任务描述关键词判定（misc 兜底），赛题入口自动并入授权范围
        challenges = [
            {"id": f"mission-{i + 1}", "title": f"任务描述赛题 {urlparse(u).hostname or u}",
             "url": u, "description": mission[:500]}
            for i, u in enumerate(m_urls)
        ]

    rules = None
    if req.mode == RunMode.COMPETITION:
        rules = parse_rules_yaml(req.rules_yaml or "")
        conflicts = rules.conflicts_with_baseline()
        if conflicts and not req.accept_rule_conflicts:
            listing = "\n".join(f"  - {c}" for c in conflicts)
            raise RuleConflictError(
                "赛制规则与合规基线冲突，需人工确认风险后才可继续："
                f"\n{listing}", conflicts)

    # 授权范围：默认限定目标 URL 前缀（最小授权原则）；extra_scope 可追加
    allowed_urls = [req.target.rstrip("/")] if req.target else []
    allowed_domains: list[str] = []
    host = urlparse(req.target).hostname if req.target else None
    if host:
        allowed_domains.append(host)
    for extra in req.extra_scope:
        (allowed_urls if extra.startswith(("http://", "https://"))
         else allowed_domains).append(extra)
    # 任务意图提取出的目标并入授权范围（用户原话即授权边界）
    for u in m_urls:
        if u not in allowed_urls:
            allowed_urls.append(u)
        uh = urlparse(u).hostname
        if uh and uh not in allowed_domains:
            allowed_domains.append(uh)
    for d in m_domains:
        if d not in allowed_domains:
            allowed_domains.append(d)
    # CTF/渗透赛：赛题入口 URL 自动并入授权范围（赛题即授权对象，5.2.1）
    for ch in challenges:
        url = str(ch.get("url", ""))
        if url.startswith(("http://", "https://")):
            allowed_urls.append(url.rstrip("/"))
            ch_host = urlparse(url).hostname
            if ch_host and ch_host not in allowed_domains:
                allowed_domains.append(ch_host)

    scope = Scope(
        allowed_domains=allowed_domains, allowed_urls=allowed_urls,
        max_requests_per_second=req.rps,
        allow_sensitive_actions=req.allow_sensitive,
        notes="启动装配授权声明",
    )
    authorization = Authorization(
        auth_id=new_id("auth"), mode=req.mode, scope=scope,
        target_description=req.target or "ctf://challenges",
        src_platform=req.platform, human_confirmed=True,
    )
    # Flag 格式边界：显式参数优先，其次任务描述中的赛方声明（确定性提取）
    flag_patterns = ([req.flag_format] if req.flag_format else None) \
        or (_mission_flag_patterns(mission) or None)
    return LaunchPlan(
        authorization=authorization,
        target=req.target or "ctf://challenges",
        challenges=challenges, rules=rules,
        flag_patterns=flag_patterns,
        mission=mission,
        mode_auto=mode_auto,
        source_request=req if mode_auto else None,
    )
