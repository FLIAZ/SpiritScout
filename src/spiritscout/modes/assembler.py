"""运行模式对应的角色池与战术映射。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spiritscout.agents.business import BusinessContextAgent
from spiritscout.agents.hostops import HostVulnAgent, PortScanAgent, PostExploitAgent
from spiritscout.agents.recon import ReconAgent
from spiritscout.agents.sub.base import SubAgent, SubAgentContext
from spiritscout.agents.webvuln import WebVulnAgent
from spiritscout.competition import (
    CompetitionEngine,
    CompetitionRules,
    competition_strategy_prompt,
)
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
    strategy_prompt: str = ""
    competition_context: dict[str, Any] = field(default_factory=dict)
    single_executor: bool = False


def _has_http_entry(ch: dict[str, Any]) -> bool:
    """入口链接判定：url 字段（YAML 显式声明）或题面原文里的 http 链接。

    壳赛题不捆绑 url 字段（URL 可能只是平台地址，角色由模型判断），
    入口线索因此可能只活在题面原文里——分类、并行、轮换三处共用。
    """
    if str(ch.get("url") or "").startswith(("http://", "https://")):
        return True
    return any(("http://" in str(ch.get(k, "")) or "https://" in str(ch.get(k, "")))
               for k in ("title", "description", "category"))


_BINARY_EXTS = (".elf", ".bin", ".exe", ".out", ".so")
# 可执行二进制魔数：ELF、PE、Mach-O（两种端序）、胖二进制/Java class
# （0xCAFEBABE 两用，都属逆向材料）。PNG/ZIP/PCAP 等取证格式不在其列
# ——无扩展名的它们仍走 misc 兜底，misc 也在并行与轮换里常驻。
_BINARY_MAGIC = (b"\x7fELF", b"MZ", b"\xFE\xED\xFA\xCE", b"\xCE\xFA\xED\xFE",
                 b"\xFE\xED\xFA\xCF", b"\xCF\xFA\xED\xFE", b"\xCA\xFE\xBA\xBE")

# 标签只述事实不给结论：ZIP 容器说清可能的材料形态，方向仍由模型判断
_MAGIC_LABELS = ((b"\x7fELF", "ELF"), (b"MZ", "PE"),
                 (b"\xFE\xED\xFA", "Mach-O"), (b"\xCF\xFA\xED\xFE", "Mach-O"),
                 (b"\xCE\xFA\xED\xFE", "Mach-O"), (b"\xCA\xFE\xBA\xBE", "Java 类/胖二进制"),
                 (b"PK", "ZIP 容器（压缩包/Office/JAR/APK 皆可能）"),
                 (b"\x1f\x8b", "gzip 压缩流"), (b"\x89PNG", "PNG 图像"),
                 (b"\xff\xd8\xff", "JPEG 图像"), (b"%PDF", "PDF 文档"))


def _attachment_notes(ch: dict[str, Any]) -> list[str]:
    """附件感知注记：只报文件头识别的原始事实（魔数），不下方向结论。

    平台附件未落地或路径不可读时无事实不臆测；注记随题面进规划上下文，
    方向由模型读了判断（classify_challenge 仅在模型不可用时兜底派单）。
    """
    notes = []
    for name in (ch.get("attachments") or []):
        try:
            with open(str(name), "rb") as fh:
                head = fh.read(4)
        except OSError:
            continue
        label = next((label for magic, label in _MAGIC_LABELS
                      if head.startswith(magic)), "")
        if label:
            notes.append(f"{Path(name).name}：文件头为 {label}")
    return notes


def _planning_challenges(challenges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规划视图的赛题清单：原题面 + 附件魔数注记（感知事实，供模型判向）。"""
    return [{**ch, "attachment_notes": _attachment_notes(ch)} for ch in challenges]


def _binary_attachments(ch: dict[str, Any]) -> bool:
    """二进制附件判定：扩展名或文件魔数（ELF/PE）。

    逆向题附件常无扩展名（crackme、chall），只认扩展名会把它漏进
    杂项兜底；魔数读文件头 4 字节，路径不存在或不可读时静默略过
    （分类在多处调用，平台附件可能尚未落地）。
    """
    names = [str(a) for a in (ch.get("attachments") or [])]
    if any(n.lower().endswith(_BINARY_EXTS) for n in names):
        return True
    for name in names:
        try:
            with open(name, "rb") as fh:
                head = fh.read(4)
        except OSError:
            continue
        if head.startswith(_BINARY_MAGIC):
            return True
    return False


def classify_challenge(challenge: dict[str, Any]) -> str:
    """赛题方向自动归类（5.2.1：未标注方向时按入口特征识别）。

    证据强度排序：显式标注 > 二进制附件 > 题面关键词 >
    弱方向附件（取证媒体/压缩包）> 入口链接。二进制附件给谁由题面
    说了算——逆向词（逆向/reverse/crackme/反编译）指向 reverse，
    其余归 pwn（远程利用视角），轮换候选里两方向互备；题面关键词
    先于弱附件（www.zip 发的常是 Web 源码，文本说了算）；壳赛题
    不捆绑 url 字段，入口链接可能只活在题面原文里，兜底时从文本找。
    """
    direction = str(challenge.get("direction", "")).lower().strip()
    if direction in _DIRECTION_TO_TACTIC:
        return direction
    blob = " ".join(str(challenge.get(k, "")) for k in
                    ("title", "description", "category")).lower()
    names = [str(a).lower() for a in (challenge.get("attachments") or [])]
    if _binary_attachments(challenge):
        if any(k in blob for k in ("逆向", "reverse", "crackme", "反编译")):
            return "reverse"
        return "pwn"
    for direction, keywords in (
        ("web", ("web", "网站", "页面")),
        ("crypto", ("rsa", "aes", "cipher", "密码", "加密", "base64", "编码")),
        ("reverse", ("逆向", "reverse", "crackme", "反编译")),
        ("pwn", ("pwn", "nc ", "栈", "溢出", "二进制", "elf")),
        ("misc", ("misc", "隐写", "stego", "流量", "pcap", "图片")),
    ):
        if any(k in blob for k in keywords):
            return direction
    if any(n.endswith((".pcap", ".png", ".jpg", ".wav", ".zip")) for n in names):
        return "misc"
    if _has_http_entry(challenge):
        return "web"
    return "misc"


def _candidate_directions(ch: dict[str, Any]) -> list[str]:
    """按赛题形态列出可用解题方向（优先级序，去重保序）——重攻轮换角度用。

    只列该形态下真正说得通的方向：misc 的场是附件（其专属工具面依赖
    附件与编码/隐写），pwn/reverse 的场是二进制附件；纯 URL 的 Web 题
    不轮换到杂项——方向试尽即回主方向重开对话换角度（新会话不带旧
    对话的偏见），分支并行在主方向内部以多路线展开。
    """
    order = [classify_challenge(ch)]
    if _binary_attachments(ch):
        order += ["pwn", "reverse"]
    if ch.get("attachments"):
        order += ["misc", "crypto"]
    if _has_http_entry(ch) and classify_challenge(ch) != "web":
        order += ["web"]          # 有 URL 但主判非 Web：Web 视角兜底
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
    纯 URL 题不并行杂项、重攻时也不轮换到杂项（没有附件就没有杂项的
    专属线索面）——并行分支在主方向内部以多路线展开（路线预算由灵枢
    按复杂度裁定，分支共享现场相互接力）。
    """
    primary = classify_challenge(ch)
    has_url = _has_http_entry(ch)
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
                      tried: dict[str, set[str]] | None = None,
                      deferred: set[str] | None = None,
                      skip_solved: bool = False) -> dict[str, Any]:
    """CTF 规则派单（5.2.1）：模型规划的兜底路径——无模型演练或规划失败时用；
    方向选择首选主 Agent 读题面与附件证据的模型判断。

    首轮：每题按形态并行下发 1~2 个方向节点（无依赖、同时开工），各方向
    Agent 共享现场（环形共享缓存）协同解题。重攻轮（round_no>0）：对尚未
    拿到 Flag 且本轮尚无待发节点的题，换一个尚未试过的方向再攻（战术名带
    @轮次 后缀，绕开「同战术不重建」的去重）；方向试尽则回主方向重开新
    对话换角度——跨轮不重复做功由环形台账与题级共享结论保证。
    skip_solved（续传首轮）：已破题的赛题不再重解，只派未破题的。
    deferred：模型已明确暂缓的赛题。只有复盘模型解除暂缓后才重新派发，
    后端不按固定次数把暂缓题自动塞回队列。
    """
    solved = solved or set()
    pending = pending or set()
    tried = tried or {}
    deferred = deferred or set()
    nodes: list[dict[str, Any]] = []
    for ch in challenges:
        cid = str(ch.get("id") or ch.get("title") or "?")
        if cid in deferred:
            continue
        if (round_no > 0 or skip_solved) and (cid in solved or cid in pending):
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
    analysis = (f"CTF 赛题分发：{len(nodes)} 个方向节点并行开工（工具操作自动审批）"
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
        resume: bool = False,
        platform: Any = None,
    ) -> ModeAssembly:
        if mode == RunMode.SRC:
            return self._assemble_src()
        if mode == RunMode.CTF:
            return self._assemble_ctf(challenges or [], flag_manager,
                                      resume=resume, platform=platform)
        if mode == RunMode.AWDP:
            # AWDP 修补：CTF 装配全套（题目分发/求解环/工具面）+ 修补目标
            assembly = self._assemble_ctf(challenges or [], flag_manager,
                                          resume=resume, platform=platform)
            assembly.mode = RunMode.AWDP
            if assembly.single_executor:
                assembly.notes = ["灵枢轻量 AWDP：直接完成用户当前目标；要求修补时保留正常功能并交付原始目录结构的补丁"]
                return assembly
            assembly.notes = ["AWDP 修补模式：每题找漏洞→最小修改→语法自检→"
                              "按原始目录结构打包补丁（tar.gz）并 submit_fix 登记；"
                              "修复不得破坏正常功能"]
            assembly.strategy_prompt = (
                "AWDP 修补赛：目标是产出可提交的修复补丁而不是 flag。每道题"
                "先读懂服务源码/二进制找到漏洞点，做最小必要修改（不要重写无关"
                "逻辑、不要删功能），自检语法（php -l / ruby -c / javac / "
                "编译验证），最后只把被修改的文件按原始相对路径打包成 "
                "fix.tar.gz 并调用 submit_fix 登记。")
            return assembly
        return self._assemble_competition(challenges or [], rules, flag_manager,
                                          competition_engine, platform=platform)

    # ------------------------------------------------------------ SRC

    def _assemble_src(self) -> ModeAssembly:
        return ModeAssembly(
            mode=RunMode.SRC,
            agents={
                AgentType.BUSINESS_CONTEXT: BusinessContextAgent(self.ctx, **self.kw),
                AgentType.RECON: ReconAgent(self.ctx, **self.kw),
                AgentType.PORT_SCAN: PortScanAgent(self.ctx, **self.kw),
                AgentType.WEB_VULN: WebVulnAgent(self.ctx, **self.kw),
                AgentType.HOST_VULN: HostVulnAgent(self.ctx, **self.kw),
                AgentType.POST_EXPLOIT: PostExploitAgent(self.ctx, **self.kw),
            },
            notes=["SRC 模式：业务体验 → 侦察/测绘 → 主机审计 → Web 探测 → 横向 → 全链路核验 → 报告"],
        )

    # ------------------------------------------------------------ CTF

    def _assemble_ctf(
        self, challenges: list[dict[str, Any]], flag_manager: FlagManager | None,
        *, resume: bool = False, platform: Any = None,
    ) -> ModeAssembly:
        config = getattr(getattr(self.ctx, "gateway", None), "config", None)
        if (getattr(config, "lightweight_local", False)
                and getattr(self.ctx, "mode", None) in (RunMode.CTF, RunMode.AWDP)):
            from spiritscout.ctf.single import SingleCTFAgent

            return ModeAssembly(mode=RunMode.CTF,
                agents={AgentType.CTF_MISC: SingleCTFAgent(self.ctx, flag_manager=flag_manager, **self.kw)},
                flag_manager=flag_manager, single_executor=True,
                notes=["灵枢轻量模式：同一工具循环直接处理用户目标，不启动主控规划、督导或专项派遣"],
                competition_context={"challenges": _planning_challenges(challenges)})
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
            if getattr(flag_manager, "local_only", False):
                solved = flag_manager.completed_ids()
            if platform is not None and platform.has_completion_signal():
                # 平台按题给出完成态：完成判定以平台为准——多 Flag 题拿到
                # 一个 Flag 不等于完赛（未竟 Flag 仍会重攻补分），只有平台
                # 通报完成的题才停止派发
                solved = {cid for cid in
                          (str(ch.get("id") or "") for ch in challenges)
                          if cid and platform.is_completed(cid)}
            pending: set[str] = set()
            deferred: set[str] = set()
            tried: dict[str, set[str]] = {}
            for node in (state.get("dag") or {}).values():
                meta_ch = (node.get("meta") or {}).get("challenge") or {}
                cid = str(meta_ch.get("id") or "")
                if cid and (node.get("meta") or {}).get("deferred"):
                    deferred.add(cid)
                if cid and meta_ch.get("direction"):
                    tried.setdefault(cid, set()).add(str(meta_ch["direction"]))
                if str(node.get("status")) == TaskStatus.PENDING.value and cid:
                    pending.add(cid)
            return ctf_plan_provider(
                challenges, round_no=int(state.get("round_no", 0) or 0),
                solved=solved, pending=pending, tried=tried,
                deferred=deferred,
                skip_solved=resume and int(state.get("round_no", 0) or 0) == 0)

        notes = [("CTF 模式：五方向专项，所有工具操作自动批准并留痕；"
                  "赛题方向由主 Agent 读题面与附件证据判定（模型规划），"
                  "规则派单仅兜底（5.2.1）")]
        if platform is not None:
            notes.append("平台托管：列题/开容器/回收/提交由灵枢侧适配器执行，"
                         "子 Agent 不接触平台凭据")
        return ModeAssembly(
            mode=RunMode.CTF,
            agents=agents,
            tactic_map=dict(CTF_TACTIC_MAP),
            plan_provider=_plan,
            flag_manager=flag_manager,
            notes=notes,
            # 赛题进规划上下文：模型派单读题面/附件证据，解析层据此恢复完整题面
            competition_context={"challenges": _planning_challenges(challenges)},
        )

    # ------------------------------------------------------------ 渗透赛

    def _assemble_competition(
        self,
        challenges: list[dict[str, Any]],
        rules: CompetitionRules | None,
        flag_manager: FlagManager | None,
        engine: CompetitionEngine | None,
        platform: Any = None,
    ) -> ModeAssembly:
        rules = rules or CompetitionRules()
        notes: list[str] = []
        conflicts = rules.conflicts_with_baseline()
        if conflicts:
            notes.extend(f"规则冲突待人工确认: {c}" for c in conflicts)

        from spiritscout.agents.master.agent import TACTIC_AGENT_MAP

        # 赛制名称不裁剪能力、不绑定执行模板；角色与任务依赖由模型选择。
        assembly = self._assemble_src()
        ctf = self._assemble_ctf(challenges, flag_manager, platform=platform)
        assembly.agents.update(ctf.agents)
        assembly.mode = RunMode.COMPETITION
        assembly.tactic_map = {**TACTIC_AGENT_MAP, **CTF_TACTIC_MAP}
        assembly.notes = ["开放赛制：按用户规则、平台事实和实时得分反馈动态规划"]
        # 无模型的离线演练仅处理已提供的题目，不凭赛制标签发明攻击流程。
        if not self.kw["enable_llm_planning"] and challenges:
            assembly.plan_provider = ctf.plan_provider
        assembly.flag_manager = flag_manager
        assembly.competition_engine = engine
        assembly.strategy_prompt = competition_strategy_prompt(rules.contest_type)
        assembly.competition_context = {
            "contest_label": rules.contest_type.value,
            "rules": dict(rules.raw),
            "challenges": _planning_challenges(challenges),
        }
        assembly.notes.extend(notes)
        return assembly
