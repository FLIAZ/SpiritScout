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
    platform_yaml: str | None = None        # 平台档案 YAML 文本（托管题/跑分平台）
    rules_yaml: str | None = None           # 渗透赛赛制规则 YAML 文本
    accept_rule_conflicts: bool = False
    mission: str | None = None              # 自然语言任务意图（任务式启动）
    has_uploads: bool = False               # 任务下达时携带了上传附件


@dataclass
class LaunchPlan:
    """装配产物：可直接传给 runtime 三个入口。"""

    authorization: Authorization
    target: str                             # SRC 目标 / CTF 占位 "ctf://challenges"
    challenges: list[dict]
    rules: object | None                    # CompetitionRules（仅渗透赛）
    flag_patterns: list[str] | None
    mission: str = ""                       # 任务意图原文（喂主 Agent 规划）
    platform_profile: dict | None = None    # 平台档案（含凭据；对运行时直供）
    mode_auto: bool = False                 # 智能模式：待 LLM 路由复核
    needs_mode_routing: bool = False        # 输入未明确声明模式时才需要 LLM 语义裁决
    source_request: LaunchRequest | None = None  # 重建计划用（LLM 改判时）


# 任务意图中的目标提取：URL 优先，裸域名兜底。
# 排除类含全角标点——中文任务描述「…cn（flag 格式：…）」的括号不该被吞进 URL
_URL_RE = re.compile(
    r"https?://[^\s，。；：、！？（）【】《》「」『』\"'<>()]+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b", re.IGNORECASE)

# 这里只识别用户已经明确写出的任务类型，不从漏洞名、URL 或业务词猜模式。
# 中文紧邻英文缩写时 ``\b`` 不可靠，因此使用 ASCII 标识符边界。
_CTF_DECLARATION_RE = re.compile(
    r"(?i)(?<![a-z0-9_])ctf(?![a-z0-9_])|"
    r"(?:拿|找|获取|夺取|提交|得到)\s*(?:到|出)?\s*flag(?![a-z0-9_])|"
    r"(?:一道|这道|解|破解)\s*(?:ctf\s*)?(?:题|赛题)")
_COMPETITION_DECLARATION_RE = re.compile(
    r"(?i)(?<![a-z0-9_])awd(?:-[a-z]+)?(?![a-z0-9_])|"
    r"渗透赛|攻防赛|攻防对抗|红蓝对抗")
_SRC_DECLARATION_RE = re.compile(
    r"(?i)(?<![a-z0-9_])src(?![a-z0-9_])|"
    r"漏洞赏金|漏洞挖掘|授权渗透(?:测试)?|安全评估")


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
    """赛题 YAML 文本 → list[dict]（顶层列表或含 challenges 键的映射）。

    解析与结构错误包装为可读的域校验错误（422），不落 500。
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpiritScoutError(f"赛题 YAML 解析失败: {exc}") from exc
    if isinstance(data, dict) and "challenges" in data:
        data = data["challenges"]
    if not isinstance(data, list):
        raise SpiritScoutError("赛题格式错误（应为列表或含 challenges 键的 YAML）")
    challenges: list[dict] = []
    for i, ch in enumerate(data):
        if not isinstance(ch, dict):
            raise SpiritScoutError(
                f"赛题第 {i + 1} 项必须是键值映射，收到 {type(ch).__name__}")
        challenges.append(dict(ch))
    return challenges


def parse_rules_yaml(text: str):
    """赛制规则 YAML 文本 → CompetitionRules（解析/校验错误可读化）。"""
    from spiritscout.competition import CompetitionRules

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise SpiritScoutError(f"赛制规则 YAML 解析失败: {exc}") from exc
    try:
        return CompetitionRules.from_dict(data)
    except SpiritScoutError:
        raise
    except Exception as exc:
        raise SpiritScoutError(f"赛制规则校验失败: {exc}") from exc


def _mission_flag_patterns(mission: str) -> list[str]:
    """任务描述中的 Flag 格式声明（如「flag 格式：SCTF{...}」）→ 正则列表。

    用户只需在任务里照抄赛方声明，格式边界由确定性提取落到
    FlagManager，无需手填正则（与赛制规则解析共用同一提取器）。
    """
    if not mission:
        return []
    from spiritscout.competition.rules import extract_flag_patterns_from_text

    return extract_flag_patterns_from_text(mission)


def declared_mode(req: LaunchRequest) -> RunMode | None:
    """读取用户已经明确给出的模式声明。

    结构化赛制/赛题优先；自然语言只接受 ``CTF``、``拿 flag``、``AWD``、
    ``授权渗透`` 这类强声明。返回 ``None`` 表示确实需要 LLM 理解上下文，
    避免把可确定的任务交给模型再猜一次。
    """
    if req.rules_yaml:
        return RunMode.COMPETITION
    if req.challenges_yaml:
        return RunMode.CTF
    mission = req.mission or ""
    if _COMPETITION_DECLARATION_RE.search(mission):
        return RunMode.COMPETITION
    if _CTF_DECLARATION_RE.search(mission):
        return RunMode.CTF
    if _SRC_DECLARATION_RE.search(mission):
        return RunMode.SRC
    return None


def fallback_mode(req: LaunchRequest) -> RunMode:
    """智能模式的确定性兜底。

    明确声明直接采用；只有附件且没有在线目标时按离线赛题装配；其余先按
    SRC 构造可校验的授权计划，并在运行前交给 LLM 做语义裁决。
    """
    declared = declared_mode(req)
    if declared is not None:
        return declared
    if req.has_uploads and not extract_targets(req.mission or "")[0]:
        # 只有附件、没有目标 URL：SRC 无处安放目标，只能按赛题处理
        # （附件文件由创建流程注入赛题载荷；运行期 LLM 路由仍可改判）
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
    # 智能模式下模式与意图一律交由 LLM 路由裁决——声明词正则只在模型
    # 不可用时作兜底参考（fallback_guess），不在运行期代替模型决策
    needs_mode_routing = mode_auto
    if mode_auto:
        req = LaunchRequest(**{**req.__dict__, "mode": fallback_mode(req)})
    if mission and not req.target and m_urls:
        req = LaunchRequest(**{**req.__dict__, "target": m_urls[0]})
    elif mission and not req.target and m_domains:
        # 纯域名授权（如 SRC「*.bilibili.com」清单）同样要有工作入口，
        # 否则 plan.target 落到 ctf:// 占位符，侦察/测绘步骤全打在伪协议上
        req = LaunchRequest(**{**req.__dict__, "target": "https://" + m_domains[0]})
    # 目标硬校验只对用户显式选择的模式生效。智能模式（mode_auto）下
    # 不在创建时拦——「你好」这类普通对话、纯规则说明都不该被目标
    # 校验挡在门外；意图（对话/规则/任务）由运行期 LLM 路由裁决，
    # 真任务缺目标时灵枢会主动向用户询问。
    if req.mode == RunMode.SRC and not req.target and not mode_auto:
        hint = ("；任务描述中未识别到目标 URL——请写明 http(s):// 地址"
                if mission else "——或在任务描述中写明目标")
        raise SpiritScoutError(f"SRC 模式需要目标 URL{hint}")
    if (req.mode == RunMode.CTF and not req.challenges_yaml and not m_urls
            and not req.has_uploads and not req.platform_yaml and not mode_auto):
        raise SpiritScoutError(
            "CTF 模式需要赛题清单——粘贴赛题 YAML，写明赛题 URL，上传题目附件，"
            "或提供平台档案（托管题由平台列题）")
    if req.mode == RunMode.COMPETITION and not req.rules_yaml and not mode_auto:
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
    elif req.mode == RunMode.CTF and not challenges and req.has_uploads:
        # 只有附件的赛题（离线题常态）：题目壳由装配合成，附件文件由
        # 会话创建流程注入 attachments 字段——此处不知道落地路径
        challenges = [{"id": "mission-1", "title": "任务附件赛题",
                       "description": mission[:500]}]

    rules = None
    if req.mode == RunMode.COMPETITION:
        rules = parse_rules_yaml(req.rules_yaml or "")
        conflicts = rules.conflicts_with_baseline()
        if conflicts and not req.accept_rule_conflicts:
            listing = "\n".join(f"  - {c}" for c in conflicts)
            raise RuleConflictError(
                "赛制规则与合规基线冲突，需人工确认风险后才可继续："
                f"\n{listing}", conflicts)

    # 平台档案（显式提供）：解析校验失败即报错；托管题由平台列题/开容器
    platform_profile: dict | None = None
    if req.platform_yaml:
        from spiritscout.ctf.platform import parse_platform_profile

        try:
            platform_profile = parse_platform_profile(req.platform_yaml).to_dict()
        except ValueError as exc:
            raise SpiritScoutError(f"平台档案校验失败: {exc}") from exc

    # 授权范围：默认限定目标 URL 前缀（最小授权原则）；extra_scope 可追加
    allowed_urls = [req.target.rstrip("/")] if req.target else []
    allowed_domains: list[str] = []
    for extra in req.extra_scope:
        (allowed_urls if extra.startswith(("http://", "https://"))
         else allowed_domains).append(extra)
    # 任务意图提取出的目标并入授权范围（用户原话即授权边界）
    for u in m_urls:
        if u not in allowed_urls:
            allowed_urls.append(u)
    for d in m_domains:
        if d not in allowed_domains:
            allowed_domains.append(d)
    # CTF/渗透赛：赛题入口 URL 自动并入授权范围（赛题即授权对象，5.2.1）
    for ch in challenges:
        url = str(ch.get("url", ""))
        if url.startswith(("http://", "https://")):
            allowed_urls.append(url.rstrip("/"))
    # 平台宿主并入授权域：平台 API 只由装配层直连（不是测试目标），并入
    # 保证纯托管题任务（目标由平台运行期下发）有可校验的范围声明
    if platform_profile:
        host = urlparse(str(platform_profile.get("base_url") or "")).hostname or ""
        if host and host not in allowed_domains:
            allowed_domains.append(host)

    # 赛制禁打列表是唯一硬边界：规则里的 denied_hosts 并入授权 Scope，
    # 由 ScopeGuard.ensure_not_denied 逐请求校验（此前解析了却无人消费）
    denied_hosts = list(rules.denied_hosts) if rules is not None else []
    scope = Scope(
        allowed_domains=allowed_domains, allowed_urls=allowed_urls,
        denied=denied_hosts,
        max_requests_per_second=req.rps,
        allow_sensitive_actions=req.allow_sensitive,
        # 携带附件即已授权分析这些本地文件（离线题常态）——授权数据里
        # 如实表达本地分析，不伪造域名/URL、不扩大网络范围
        local_analysis=bool(req.has_uploads),
        notes="启动装配授权声明",
    )
    # SRC 的工作目标必须落在真实资产上：显式 target 优先；没有时从装配后的
    # 授权范围取第一个 URL/域名（续传合并 extra_scope 的常见场景）——否则
    # 会落到 CTF 占位符 "ctf://challenges"，侦察/测绘把 "challenges" 当主机名。
    # 多域目标由规划节点各自指定 target（见 MasterAgent._task_target）。
    target = req.target or ""
    if req.mode == RunMode.SRC and (
            not target or target.startswith("ctf://")):
        if allowed_urls:
            target = allowed_urls[0]
        elif allowed_domains:
            target = "https://" + allowed_domains[0]
    authorization = Authorization(
        auth_id=new_id("auth"), mode=req.mode, scope=scope,
        target_description=target or (
            "ctf://challenges" if req.mode != RunMode.SRC else "按授权范围"),
        src_platform=req.platform, human_confirmed=True,
    )
    # Flag 格式边界：显式参数优先，其次任务描述中的赛方声明（确定性提取）
    flag_patterns = ([req.flag_format] if req.flag_format else None) \
        or (_mission_flag_patterns(mission) or None)
    return LaunchPlan(
        authorization=authorization,
        target=target or ("ctf://challenges" if req.mode != RunMode.SRC else ""),
        challenges=challenges, rules=rules,
        flag_patterns=flag_patterns,
        mission=mission,
        platform_profile=platform_profile,
        mode_auto=mode_auto,
        needs_mode_routing=needs_mode_routing,
        source_request=req if mode_auto else None,
    )
