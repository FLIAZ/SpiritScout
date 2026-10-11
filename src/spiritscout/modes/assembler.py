"""模式装配器：运行模式 → 子 Agent 池 + 战术映射 + 模式模块。

硬约束落地：
- C7：CTF 模式不装配任何端口扫描能力（ReconAgent 在 CTF 下自动剔除
  服务发现路线；CTF 五方向直接面向赛题入口）；
- 4.5.2：AWD 攻击线允许端口扫描（赛方授权 IP 段内），靶场渗透赛同常规渗透；
- 5.2.1：CTF 赛题未标注方向时按入口特征自动归类（URL/附件类型/二进制）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from spiritscout.agents.recon import ReconAgent
from spiritscout.agents.sub.base import SubAgent, SubAgentContext
from spiritscout.agents.webvuln import WebVulnAgent
from spiritscout.competition import CompetitionEngine, CompetitionRules, ContestType
from spiritscout.core.models import AgentType, RunMode, TaskStatus
from spiritscout.ctf.agents import (
    CTFCryptoAgent,
    CTFMiscAgent,
    CTFPwnAgent,
    CTFReverseAgent,
    CTFWebAgent,
)
from spiritscout.ctf.flags import FlagManager

# CTF 战术标识（非 ATT&CK，DAG 自定义阶段，5.2.1 按方向分发）
CTF_TACTIC_MAP: dict[str, tuple[AgentType, str]] = {
    "CTF-WEB": (AgentType.CTF_WEB, "ctf_solve"),
    "CTF-PWN": (AgentType.CTF_PWN, "ctf_solve"),
    "CTF-REVERSE": (AgentType.CTF_REVERSE, "ctf_solve"),
    "CTF-CRYPTO": (AgentType.CTF_CRYPTO, "ctf_solve"),
    "CTF-MISC": (AgentType.CTF_MISC, "ctf_solve"),
}
_DIRECTION_TO_TACTIC = {t.split("-", 1)[1].lower(): t for t in CTF_TACTIC_MAP}


@dataclass
class ModeAssembly:
    """模式装配产物。"""

    mode: RunMode
    agents: dict[AgentType, SubAgent]
    tactic_map: dict[str, tuple[AgentType, str]] | None = None  # 覆盖主 Agent 默认映射
    plan_provider: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None
    flag_manager: FlagManager | None = None
    competition_engine: CompetitionEngine | None = None
    notes: list[str] = field(default_factory=list)


def classify_challenge(challenge: dict[str, Any]) -> str:
    """赛题方向自动归类（5.2.1：未标注方向时按入口特征识别）。"""
    direction = str(challenge.get("direction", "")).lower().strip()
    if direction in _DIRECTION_TO_TACTIC:
        return direction
    blob = " ".join(str(challenge.get(k, "")) for k in
                    ("title", "description", "category", "url")).lower()
    for direction, keywords in (
        ("web", ("http", "url", "web", "网站", "页面")),
        ("crypto", ("rsa", "aes", "cipher", "密码", "加密", "base64", "编码")),
        ("reverse", ("逆向", "reverse", "crackme", "反编译")),
        ("pwn", ("pwn", "nc ", "栈", "溢出", "二进制", "elf")),
        ("misc", ("misc", "隐写", "stego", "流量", "pcap", "图片")),
    ):
        if any(k in blob for k in keywords):
            return direction
    attachments = challenge.get("attachments", [])
    if attachments:
        name = str(attachments[0]).lower()
        if name.endswith((".elf", ".bin", ".exe", ".out")):
            return "pwn"
        if name.endswith((".pcap", ".png", ".jpg", ".wav", ".zip")):
            return "misc"
    return "misc"


def _candidate_directions(ch: dict[str, Any]) -> list[str]:
    """按赛题形态列出可用解题方向（优先级序，去重保序）——重攻轮换角度用。

    只列该形态下真正说得通的方向（有 URL 才是 web/misc 的场，二进制附件
    才谈 pwn/reverse），不摆一套对本题无意义的通用清单。
    """
    order = [classify_challenge(ch)]
    if str(ch.get("url") or "").startswith(("http://", "https://")):
        order += ["web", "misc"]
    names = [str(a).lower() for a in ch.get("attachments", [])]
    if any(n.endswith((".elf", ".bin", ".exe", ".out", ".so")) for n in names):
        order += ["pwn", "reverse", "misc"]
    elif names:
        order += ["misc", "crypto", "reverse"]
    order += ["misc"]
    seen: set[str] = set()
    out: list[str] = []
    for d in order:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _parallel_directions(ch: dict[str, Any]) -> list[str]:
    """首轮并行方向：默认单方向起步，第二个方向必须有本方向的专属线索面。

    misc 的场是附件与编码/隐写类题面（其专属工具 deep_strings 依赖附件）；
    纯 URL 题并行 misc 只会让两个 Agent 打同一入口做同质重复功，杂项视角
    留给重攻轮换角度（_candidate_directions）。
    """
    primary = classify_challenge(ch)
    has_url = str(ch.get("url") or "").startswith(("http://", "https://"))
    attachments = ch.get("attachments") or []
    if primary == "misc":
        # 判为杂项但给了 URL：Web 视角工具面更宽，两个视角都成立
        return ["web", "misc"] if has_url else ["misc"]
    if not has_url and not attachments:
        return [primary]
    if has_url and not attachments:
        return [primary]  # 纯 URL 题：单方向主攻
    return [primary, "misc"]  # 有附件的题：杂项帮看附件


def ctf_plan_provider(challenges: list[dict[str, Any]], *,
                      round_no: int = 0,
                      solved: set[str] | None = None,
                      pending: set[str] | None = None,
                      tried: dict[str, set[str]] | None = None) -> dict[str, Any]:
    """CTF 元规划（规则化，5.2.1）：跳过侦察，按方向生成 DAG 节点。

    首轮：每题按形态并行下发 1~2 个方向节点（无依赖、同时开工），各方向
    Agent 共享现场（环形共享缓存）协同解题。重攻轮（round_no>0）：对尚未
    拿到 Flag 且本轮尚无待发节点的题，换一个尚未试过的方向再攻（战术名带
    @轮次 后缀，绕开「同战术不重建」的去重）；方向试尽则回主方向重开新
    对话换角度——跨轮不重复做功由环形台账与题级共享结论保证。
    """
    solved = solved or set()
    pending = pending or set()
    tried = tried or {}
    nodes: list[dict[str, Any]] = []
    for ch in challenges:
        cid = str(ch.get("id") or ch.get("title") or "?")
        if round_no > 0 and (cid in solved or cid in pending):
            continue
        if round_no == 0:
            directions = _parallel_directions(ch)
        else:
            done = tried.get(cid, set())
            directions = [next((d for d in _candidate_directions(ch)
                                if d not in done), classify_challenge(ch))]
        for direction in directions:
            tactic = _DIRECTION_TO_TACTIC[direction]
            title = f"[{direction}] {ch.get('title', cid)}"
            meta_ch = {**ch, "direction": direction}
            if round_no > 0:
                tactic = f"{tactic}@{round_no}"
                title = f"重攻 #{round_no} {title}"
                meta_ch["retry_round"] = round_no
            nodes.append({
                "tactic": tactic, "title": title, "depends_on": [],
                "meta": {"challenge": meta_ch},
            })
    analysis = (f"CTF 赛题分发：{len(nodes)} 个方向节点并行开工（跳过端口扫描，C7）"
                if round_no == 0 else
                f"CTF 重攻轮 #{round_no}：{len(nodes)} 个方向节点，换角度再攻")
    return {"analysis": analysis, "dag_nodes": nodes}


class ModeAssembler:
    """按模式装配 Agent 集群与模块。"""

    def __init__(
        self,
        sub_ctx: SubAgentContext,
        *,
        default_routes: int = 3,
        max_routes: int = 8,
        enable_llm_planning: bool = True,
    ):
        self.ctx = sub_ctx
        self.kw = {"default_routes": default_routes, "max_routes": max_routes,
                   "enable_llm_planning": enable_llm_planning}

    # ------------------------------------------------------------ 入口

    def assemble(
        self,
        mode: RunMode,
        *,
        challenges: list[dict[str, Any]] | None = None,
        rules: CompetitionRules | None = None,
        flag_manager: FlagManager | None = None,
        competition_engine: CompetitionEngine | None = None,
    ) -> ModeAssembly:
        if mode == RunMode.SRC:
            return self._assemble_src()
        if mode == RunMode.CTF:
            return self._assemble_ctf(challenges or [], flag_manager)
        return self._assemble_competition(challenges or [], rules, flag_manager,
                                          competition_engine)

    # ------------------------------------------------------------ SRC

    def _assemble_src(self) -> ModeAssembly:
        return ModeAssembly(
            mode=RunMode.SRC,
            agents={
                AgentType.RECON: ReconAgent(self.ctx, **self.kw),
                AgentType.WEB_VULN: WebVulnAgent(self.ctx, **self.kw),
            },
            notes=["SRC 模式：侦察 → Web 探测 → 全链路核验 → 报告"],
        )

    # ------------------------------------------------------------ CTF

    def _assemble_ctf(
        self, challenges: list[dict[str, Any]], flag_manager: FlagManager | None
    ) -> ModeAssembly:
        agents: dict[AgentType, SubAgent] = {
            AgentType.CTF_WEB: CTFWebAgent(self.ctx, flag_manager=flag_manager, **self.kw),
            AgentType.CTF_PWN: CTFPwnAgent(self.ctx, flag_manager=flag_manager, **self.kw),
            AgentType.CTF_REVERSE: CTFReverseAgent(self.ctx, flag_manager=flag_manager, **self.kw),
            AgentType.CTF_CRYPTO: CTFCryptoAgent(self.ctx, flag_manager=flag_manager, **self.kw),
            AgentType.CTF_MISC: CTFMiscAgent(self.ctx, flag_manager=flag_manager, **self.kw),
        }

        def _plan(state: dict[str, Any]) -> dict[str, Any]:
            """CTF 元规划：首轮并行分发 + 未破题的重攻轮（Flag 状态驱动）。"""
            solved = ({f.challenge_id for f in flag_manager.accepted_flags()}
                      if flag_manager is not None else set())
            pending: set[str] = set()
            tried: dict[str, set[str]] = {}
            for node in (state.get("dag") or {}).values():
                meta_ch = (node.get("meta") or {}).get("challenge") or {}
                cid = str(meta_ch.get("id") or "")
                if cid and meta_ch.get("direction"):
                    tried.setdefault(cid, set()).add(str(meta_ch["direction"]))
                if str(node.get("status")) == TaskStatus.PENDING.value and cid:
                    pending.add(cid)
            return ctf_plan_provider(
                challenges, round_no=int(state.get("round_no", 0) or 0),
                solved=solved, pending=pending, tried=tried)

        return ModeAssembly(
            mode=RunMode.CTF,
            agents=agents,
            tactic_map=dict(CTF_TACTIC_MAP),
            plan_provider=_plan,
            flag_manager=flag_manager,
            notes=[("CTF 模式：五方向专项，不启用端口扫描（C7）；"
                    "赛题直接分发至专项子 Agent（5.2.1）；未破题自动换角度重攻")],
        )

    # ------------------------------------------------------------ 渗透赛

    def _assemble_competition(
        self,
        challenges: list[dict[str, Any]],
        rules: CompetitionRules | None,
        flag_manager: FlagManager | None,
        engine: CompetitionEngine | None,
    ) -> ModeAssembly:
        rules = rules or CompetitionRules()
        notes: list[str] = []
        conflicts = rules.conflicts_with_baseline()
        if conflicts:
            notes.extend(f"规则冲突待人工确认: {c}" for c in conflicts)

        if rules.contest_type == ContestType.JEOPARDY:
            assembly = self._assemble_ctf(challenges, flag_manager)
            assembly.mode = RunMode.COMPETITION
            assembly.notes.append("Jeopardy 赛制：按 6.7 公式动态排题（Jeopardy=CTF 池）")
        elif rules.contest_type == ContestType.RANGE:
            assembly = self._assemble_src()
            assembly.mode = RunMode.COMPETITION
            assembly.notes.append("靶场渗透赛：常规渗透链路，提交切换为赛方接口（5.3.4）")
        else:  # AWD：攻击线（含端口扫描，赛方授权段内）+ 防守线骨架
            agents: dict[AgentType, SubAgent] = {
                AgentType.RECON: ReconAgent(self.ctx, **self.kw),
                AgentType.WEB_VULN: WebVulnAgent(self.ctx, **self.kw),
            }
            assembly = ModeAssembly(
                mode=RunMode.COMPETITION, agents=agents,
                tactic_map=None,
                notes=[("AWD 赛制：攻击线允许端口扫描（仅限赛方授权 IP 段）；"
                        "防守线（自查/加固）为骨架，动态换防由 CompetitionEngine 驱动")],
            )
        assembly.flag_manager = flag_manager
        assembly.competition_engine = engine
        assembly.notes.extend(notes)
        return assembly
