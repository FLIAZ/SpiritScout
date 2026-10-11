"""CTF 五方向专项子 Agent（设计书 4.4.1~4.4.5）：原生工具调用解题环。

框架只提供两样东西：**工具目录**与**结果流向**。
- 工具目录：各方向可调用的确定性动作（含通用 HTTP 探测通道 http_request，
  与文件/文本/编码分析工具），以原生 function calling 的 schema 交给模型；
  参数由 LLM 按当前证据现场构造，不按固定模板出牌。
- 结果流向：①网络请求走同一 http_probe 通道（ScopeGuard/RPS/审计/审批
  边界不变）；②Flag 只由 FlagManager 确定性正则提取（LLM 声明不作数）；
  ③工具结果以 tool 消息回灌模型并进本路线观察序列与题级共享结论（同伴
  路线与主 Agent 复盘都能读到）；④已尝试入口登记进环形台账（同伴路线
  避让，不重复做功）。

解题方式是**一条持久对话**：首条消息装入题面、入口预感知与同伴结论，
之后每轮模型决定调用哪些工具（互不依赖的请求同轮并发），工具结果回灌，
直到模型不再调用工具——那就是它的结论。步数顶与重复空转熔断是仅有的
两个安全顶；LLM 不可用时解题环诚实停摆留痕，不做固定模板兜底。

简单题简单做：默认单线调查（1 条路线），未破题由主 Agent 复盘另起一轮
换角度再攻，而不是一上来就并行铺开多条分支。

不启用端口扫描（C7）；仅访问赛题入口（URL 需在授权范围内）与本地附件。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import html
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, ClassVar

from spiritscout.agents.ring import attempt_key
from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.core.models import AgentType, Task
from spiritscout.ctf.flags import FlagManager

# 题面中疑似编码串的最小长度（降噪）
_MIN_BLOB_LEN = 12
# 喂给 LLM 的单条观察摘录上限（控制 token 扇出）
_OBS_EXCERPT_CAP = 3000
# 对话字符预算：超出后从最旧消息开始折叠工具结果（长对话的上下文治理）
_MSG_CHAR_BUDGET = 48000
# 单条工具结果回灌上限
_TOOL_RESULT_CAP = 6000
# 解题环回合安全顶（每回合一次 LLM 调用，可并发多个工具；到顶强制收尾）
_INVESTIGATE_MAX_STEPS = 24
# 连续重复动作告警/强制收尾线（同一动作签名连续出现；告警先提示，再犯即收）
_DOOM_WARN_AT = 3
_DOOM_STOP_AT = 4
# 解题环单次 LLM 调用超时：重推理模型单次可达 3~4 分钟
_SOLVER_TIMEOUT = 420
# 解题环单回合输出上限（结论/工具参数所需，不需要长篇）
_SOLVER_MAX_TOKENS = 4096
# 步数顶到达后的收尾指令（剥离工具后的一次纯文本回合）
_FORCE_FINISH = ("回合预算已用尽。不要再调用工具，直接基于已获得的证据给出"
                 "最终结论：拿到了什么、还差什么、卡在哪里。")
# HTTP 动作参数上限（上下文保护，非行为限制；目标范围由 ScopeGuard 把关）
_HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
# HTML 结构探测：响应首段命中常见标签即按 HTML 摘要处理
_HTML_HINT = re.compile(
    r"<\s*(?:!doctype|html|head|body|div|span|a|p|form|table|script|style|title)\b",
    re.IGNORECASE)
# 观察保留的高信号响应头（跳转目标/会话/指纹/鉴权）
_OBS_HEADERS = ("location", "set-cookie", "server", "x-powered-by",
                "content-type", "www-authenticate", "x-flag", "allow")
# 解题思维流程（提示词层的方法引导，不限定具体动作；具体出牌仍由 LLM 按题决定）
_METHODOLOGY = (
    "解题思路像人一样：拿到题先把这道题当成一个正常功能**完整用一遍**——"
    "打开页面就顺着它的引导点进去、把表单填了、把按钮按了、按它设计的流程"
    "从头走到尾，边用边想「这东西是干什么的、谁在用、正常走完会发生什么」；"
    "用通了、看懂它怎么运转了，再回头看哪一步可以做得不对（参数被改、步骤"
    "被跳过、状态被伪造、数据被换掉），针对那个点去试。不要一上来就盯着"
    "静态文件反复读，也不要脱离这个应用本身去猜通用漏洞；每步能说出"
    "「我看到/试出了什么，接下来想验证什么」。")


def _challenge_text(ch: dict[str, Any]) -> str:
    """赛题载荷的全部字符串（含嵌套，逐段截断）——题面线索不限于固定字段名。"""
    parts: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 2 or len(parts) > 40:
            return
        if isinstance(node, str):
            if node.strip():
                parts.append(node[:4000])
        elif isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:10]:
                walk(value, depth + 1)

    walk(ch, 0)
    return "\n".join(parts)


def _decode_blobs(text: str) -> list[str]:
    """文本中的疑似编码串 → 链式解码结果（去重保序，≤20 条）。"""
    out: list[str] = []
    for blob in re.findall(rf"[A-Za-z0-9+/=_-]{{{_MIN_BLOB_LEN},}}", text)[:20]:
        decoded = _decode_chain(blob)
        if decoded and decoded != blob and decoded not in out:
            out.append(decoded)
    return out


def _challenge_view(ch: dict[str, Any], *, total_cap: int = 6000) -> dict[str, Any]:
    """赛题载荷的 LLM 上下文视图：全字段透传（截断），附件单列不走此口。

    总长超预算时逐档收紧单字段上限——视图整体可入上下文，调用侧不触发截断。
    """
    view: dict[str, Any] = {}
    for per_field in (1500, 700, 300, 120):
        view = {}
        for key, value in list(ch.items())[:20]:
            if key == "attachments":
                continue
            if isinstance(value, str):
                view[key] = value[:per_field]
            elif isinstance(value, (int, float, bool)) or value is None:
                view[key] = value
            elif isinstance(value, list):
                view[key] = [str(x)[: min(per_field, 200)] for x in value[:10]]
            elif isinstance(value, dict):
                view[key] = {str(kk)[:40]: str(vv)[: min(per_field, 200)]
                             for kk, vv in list(value.items())[:10]}
        if len(json.dumps(view, ensure_ascii=False)) <= total_cap:
            break
    return view


def _collapse(text: str) -> str:
    """空白折叠（多行 HTML 片段 → 单行可读文本）。"""
    return re.sub(r"\s+", " ", text).strip()


def _html_digest(body: str) -> str | None:
    """HTML 响应 → 结构化观察摘要（纯机械提取，不做判断）。

    样式/脚本块剔除（内联 CSS 会把真正的入口信息挤出上下文）；注释单列且
    靠前——开发备注类线索（接口地址、口令提示）常藏于此；另提取标题、表单、
    输入字段、链接、脚本引用与内联脚本。非 HTML 返回 None（回退文本提取）。
    """
    # HTML 文档以标签开头；JS/CSS/JSON 文本里出现 "<div" 之类字面量不算 HTML
    head = body.lstrip("﻿ \t\r\n")[:200].lower()
    if not head.startswith("<") or not _HTML_HINT.search(body[:4000]):
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    title = _collapse(m.group(1))[:150] if m else ""
    script_srcs = list(dict.fromkeys(
        s[:150] for s in re.findall(
            r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", body, re.IGNORECASE)))
    inline_js = [s for s in (_collapse(x) for x in re.findall(
        r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script\s*>", body,
        re.IGNORECASE | re.DOTALL)) if s]
    stripped = re.sub(r"<(style|script|noscript)\b[^>]*>.*?</\1\s*>", " ",
                      body, flags=re.IGNORECASE | re.DOTALL)
    comments = [c for c in (_collapse(x) for x in re.findall(
        r"<!--(.*?)-->", stripped, re.DOTALL)) if c and not c.startswith("[if")]
    stripped = re.sub(r"<!--.*?-->", " ", stripped, flags=re.DOTALL)
    forms: list[str] = []
    for tag in re.findall(r"<form\b[^>]*>", stripped, re.IGNORECASE)[:5]:
        action = re.search(r"\baction=[\"']([^\"']*)[\"']", tag, re.IGNORECASE)
        method = re.search(r"\bmethod=[\"']([^\"']*)[\"']", tag, re.IGNORECASE)
        forms.append(f"{method.group(1).upper() if method else 'GET'} "
                     f"{action.group(1) if action else ''}".strip())
    field_bits: list[str] = []
    for attrs in re.findall(r"<input\b([^>]*)>", stripped, re.IGNORECASE)[:10]:
        nm = re.search(r"\bname=[\"']([^\"']+)[\"']", attrs, re.IGNORECASE)
        if not nm:
            continue
        val = re.search(r"\bvalue=[\"']([^\"']*)[\"']", attrs, re.IGNORECASE)
        field_bits.append(
            f"{nm.group(1)}={val.group(1)[:60]}" if val else nm.group(1))
    links: list[str] = []
    for href in re.findall(
            r"<(?:a|link|area)\b[^>]*\bhref=[\"']([^\"']+)[\"']",
            stripped, re.IGNORECASE):
        if href.startswith(("#", "javascript:", "mailto:", "data:")) or href in links:
            continue
        links.append(href[:200])
        if len(links) >= 25:
            break
    text = _collapse(html.unescape(re.sub(r"<[^>]+>", " ", stripped)))
    parts = [f"[HTML 摘要] 样式/脚本块已剔除（原文 {len(body)} 字符）"]
    if title:
        parts.append(f"[标题] {title}")
    if comments:
        parts.append("[注释] " + " ｜ ".join(c[:500] for c in comments[:5])[:1200])
    if links:
        parts.append("[链接] " + " ".join(links)[:500])
    if forms:
        parts.append("[表单] " + " ｜ ".join(forms)[:300])
    if field_bits:
        parts.append("[输入字段] " + ", ".join(field_bits[:10])[:300])
    if script_srcs:
        parts.append("[脚本引用] " + " ".join(script_srcs)[:250])
    if inline_js:
        parts.append("[内联脚本] " + " ｜ ".join(inline_js[:3])[:600])
    if text:
        parts.append("[可见文本] " + text[:1000])
    return "\n".join(parts)[:_OBS_EXCERPT_CAP]


_STR_LITERAL = re.compile(r"""["'`]([^"'`\n]{2,200})["'`]""")
_CALL_SITE = re.compile(
    r"""(?:fetch|axios(?:\.\w+)?|\$\.(?:get|post|ajax|getJSON)|\.open|WebSocket"""
    r"""|EventSource|\.(?:get|post|put|delete|patch))\s*\(\s*["'`]([^"'`\s]{1,200})["'`]""",
    re.IGNORECASE)
_TOKEN_HINT = re.compile(
    r"(token|key|secret|passw|admin|cmd|command|exec|shell|flag|auth)", re.IGNORECASE)


def _text_digest(body: str, content_type: str = "") -> str | None:
    """非 HTML 文本（JS/JSON/纯文本）→ 机械提取：调用点、地址串、敏感词字面量。

    前端代码与接口应答是理解应用行为的现场；整段压缩代码直接进上下文会挤爆
    预算且读不出结构，这里只做机械提取（提出来的都是原文片段，不做判断），
    另附首尾原文——怎么用它由模型决定。
    """
    if len(body) < 200 and not _CALL_SITE.search(body):
        return None
    sites = list(dict.fromkeys(m for m in _CALL_SITE.findall(body) if m.strip()))[:30]
    paths = list(dict.fromkeys(
        s for s in _STR_LITERAL.findall(body)
        if s.startswith(("/", "http://", "https://", "ws://", "wss://"))
        and not s.startswith(("//", "/**"))))[:40]
    tokens = list(dict.fromkeys(
        s for s in _STR_LITERAL.findall(body)
        if _TOKEN_HINT.search(s) and 2 < len(s) < 120))[:15]
    if not (sites or paths or tokens):
        return None
    kind = "JSON/文本" if "json" in content_type.lower() else "脚本/文本"
    note = "结构提取如下，尾部附首尾原文"
    if len(body) > 6000:
        note = ("超出单次上下文预算，结构提取 + 首尾原文如下；"
                "需要完整内容可用 headers.Range 分段请求")
    parts = [f"[{kind} 提取] 原文 {len(body)} 字符（{note}）"]
    if sites:
        parts.append("[调用点] " + " ｜ ".join(sites)[:900])
    if paths:
        parts.append("[地址串] " + " ".join(paths)[:700])
    if tokens:
        parts.append("[敏感词字面量] " + " ｜ ".join(tokens)[:500])
    head = body[:1400]
    tail = body[-700:] if len(body) > 2200 else ""
    parts.append("[原文首段] " + head)
    if tail:
        parts.append("[原文尾段] " + tail)
    return "\n".join(parts)[:_OBS_EXCERPT_CAP]


def _http_observation(desc: str, res: Any, url: str,
                      flags: list[str]) -> dict[str, Any]:
    """HTTP 结果 → 观察条目：状态/跳转/高信号响应头/正文摘要（HTML 结构化）。"""
    parsed = res.parsed if isinstance(res.parsed, dict) else {}
    obs: dict[str, Any] = {"action": desc,
                           "status": parsed.get("status_code", 0)}
    final = str(parsed.get("final_url") or "")
    if final and final.rstrip("/") != url.rstrip("/"):
        obs["final_url"] = final[:300]
    headers = {str(k).lower(): str(v)
               for k, v in (parsed.get("headers") or {}).items()}
    picked = {k: headers[k][:300] for k in _OBS_HEADERS if k in headers}
    if picked:
        obs["headers"] = picked
    body = res.stdout or ""
    ctype = headers.get("content-type", "")
    # 脚本/样式/JSON 文本不走 HTML 摘要（否则会被当作标签剥离只剩碎片）
    scripty = any(k in ctype.lower() for k in
                  ("javascript", "ecmascript", "json", "css"))
    obs["excerpt"] = ((None if scripty else _html_digest(body))
                      or _text_digest(body, ctype)
                      or body[:_OBS_EXCERPT_CAP])
    if flags:
        obs["flags"] = flags
    return obs


def _signal_line(obs: dict[str, Any]) -> str:
    """观察 → 一行信号（进共享缓存给同伴看）：标题/注释线索/flag 计数。"""
    excerpt = str(obs.get("excerpt") or "")
    bits: list[str] = []
    for marker in ("[标题] ", "[注释] "):
        idx = excerpt.find(marker)
        if idx >= 0:
            bits.append(_collapse(excerpt[idx + len(marker):].split("\n")[0])[:80])
    if obs.get("final_url"):
        bits.append(f"→ {str(obs['final_url'])[:60]}")
    if obs.get("flags"):
        bits.append(f"flag×{len(obs['flags'])}")
    return (" " + " ｜ ".join(bits)) if bits else ""


@dataclass
class _ToolOutcome:
    """一次工具调用的结果：text 回灌模型，其余喂路线状态与前端轨迹。"""

    ok: bool
    text: str
    value: float = 0.0
    findings: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    observation: dict[str, Any] | None = None


def _fn(name: str, description: str, properties: dict[str, Any],
        required: list[str] | None = None) -> dict[str, Any]:
    """OpenAI 形态的函数工具 schema（网关统一转译到各协议）。"""
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       **({"required": required} if required else {})}}}


class _DeltaEmitter:
    """模型流式增量 → 审计事件（节流合并，SSE 原样转播给前端轨迹）。

    增量只服务观感：合并窗口内的碎片拼成一条事件，避免逐 token 刷屏。
    """

    def __init__(self, audit: Any, agent_id: str, *, route: str,
                 challenge: str, turn: int = 0, interval: float = 0.25):
        self.audit = audit
        self.agent_id = agent_id
        self.route = route
        self.challenge = challenge
        self.turn = turn
        self.interval = interval
        self._buf: list[str] = []
        self._kind = ""
        self._last = time.monotonic()

    def __call__(self, kind: str, text: str) -> None:
        if kind == "reset":
            self._buf.clear()
            self._kind = ""
            return
        if kind == "tool_call":
            return          # 参数碎片不转播；整轮结束后统一发 tool_call 事件
        if kind != self._kind:
            self.flush()
            self._kind = kind
        self._buf.append(text)
        if sum(map(len, self._buf)) >= 240 or \
                time.monotonic() - self._last >= self.interval:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf.clear()
        self._last = time.monotonic()
        if text.strip():
            self.audit.log("agent.delta", self.agent_id, {
                "route": self.route, "challenge": self.challenge,
                "turn": self.turn, "kind": self._kind or "text",
                "text": text[:4000]})


def _obs_text(obs: dict[str, Any]) -> str:
    """结构化观察 → 回灌模型的工具结果文本。"""
    lines = [f"{obs.get('action', '')} → {obs.get('status', 0)}"]
    if obs.get("final_url"):
        lines.append(f"最终地址: {obs['final_url']}")
    if obs.get("headers"):
        lines.append("响应头: " + json.dumps(
            obs["headers"], ensure_ascii=False)[:600])
    if obs.get("excerpt"):
        lines.append("正文:\n" + str(obs["excerpt"]))
    if obs.get("flags"):
        lines.append("Flag 已捕获并提交（FlagManager 确定性提取）: "
                     + ", ".join(str(f) for f in obs["flags"]))
    return "\n".join(lines)


class CTFBaseAgent(SubAgent):
    """CTF 专项子 Agent 基座：工具目录 + 结果流向 + 原生工具调用解题环。"""

    direction = "misc"
    # 环形共享缓存命名空间：同题各方向 Agent 共写共读（现场/笔记/Flag）
    shared_namespace: ClassVar[str] = "ctf_shared"
    flag_manager: FlagManager | None = None
    # 本方向工具目录（原生 function calling schema）；子类在此之上增补方向工具
    tool_schemas: ClassVar[list[dict[str, Any]]] = [
        _fn("http_request",
            "向赛题目标发一次 HTTP 请求并拿到响应（状态码/响应头/正文摘要）。"
            "path 传站内相对路径（推荐），或 url 传完整地址（须在授权范围内）。"
            "需要看某个页面或接口的真实响应时用它；正文较长被截断时，"
            "可用 headers 里的 Range（如 {\"Range\": \"bytes=5000-10000\"}）"
            "分段取完整内容。",
            {
                "method": {"type": "string", "enum": list(_HTTP_METHODS),
                           "description": "HTTP 方法，缺省 GET"},
                "path": {"type": "string",
                         "description": "站内路径，如 /api/v1/users"},
                "url": {"type": "string",
                        "description": "完整 URL（与 path 二选一）"},
                "params": {"type": "object", "description": "查询参数（键值对）"},
                "headers": {"type": "object", "description": "额外请求头（键值对）"},
                "data": {"type": "string", "description": "请求体原文（POST/PUT 用）"},
            }),
        _fn("shell",
            "在隔离沙箱里执行一条 shell 命令（bash -c），拿到输出与退出码。"
            "适合 curl/nc/jq/grep/文件操作等一切命令行动作；系统文件只读，"
            "你的工作区可写，联网仅限授权目标。命令全文留痕于审计。",
            {
                "command": {"type": "string", "description": "要执行的命令"},
                "timeout": {"type": "number",
                            "description": "超时秒数（缺省 60，上限 300）"},
            }, required=["command"]),
        _fn("python",
            "在隔离沙箱里执行一段 Python 代码（python3），拿到输出与退出码。"
            "已装 requests/httpx/websockets/pwntools 等常用库；适合 WebSocket/"
            "socket 协议交互、数据计算与解码、脚本化构造请求。脚本与产物"
            "落在工作区（后续可用附件工具继续分析）。",
            {
                "code": {"type": "string", "description": "完整 Python 代码"},
                "timeout": {"type": "number",
                            "description": "超时秒数（缺省 60，上限 300）"},
            }, required=["code"]),
        _fn("analyze_text",
            "对题面文本做编码链解码（Base64/Hex/URL/Rot13 递归）与 Flag 提取。"
            "怀疑线索藏在题面或编码串里时用它；已经分析过就不必再来。",
            {}),
        _fn("analyze_attachments",
            "对全部附件做魔数识别 + 字符串提取 + Flag 提取。有附件时先跑它，"
            "方向工具（checksec/algo_hints/deep_strings）依赖它的结果。",
            {}),
        _fn("note",
            "把关键发现写进团队共享笔记——同题并行工作的其他方向 Agent 下一"
            "回合就能看到（如「/api/v1/admin 需要 adm_token」「终端接口是 "
            "POST /api/exec，命令放 cmd 参数」）。有值得同伴接力的发现时写一条。",
            {"text": {"type": "string", "description": "要共享的发现，一句话"}},
            required=["text"]),
    ]
    # 方向知识（思路启发，不是步骤；LLM 按题面证据自选方向）
    direction_brief: ClassVar[str] = ""
    # 解题路线的安全顶是回合上限与重复空转熔断；健康度剪枝面向多路线资源
    # 再分配场景，逐轮探索的起步进展分天然低于阈值，会误杀在途调查——豁免
    health_prune_enabled: ClassVar[bool] = False

    def __init__(self, ctx, *, flag_manager: FlagManager | None = None, **kw):
        super().__init__(ctx, **kw)
        self.flag_manager = flag_manager

    # ------------------------------------------------------------ 赛题载荷

    @staticmethod
    def challenge_of(task: Task) -> dict[str, Any]:
        # 载荷可能显式为 null（如复盘新增节点未绑定赛题）——一律归零为 {}
        return task.payload.get("challenge") or {}

    def challenge_id(self, task: Task) -> str:
        return str(self.challenge_of(task).get("id", task.task_id))

    # ------------------------------------------------------------ Flag 闭环

    async def _harvest(self, text: str, task: Task, source: str) -> list[str]:
        """文本 → Flag 提取 → 提交 → 返回接受的 Flag 值列表。"""
        if not text or self.flag_manager is None:
            return []
        candidates = self.flag_manager.extract(
            text, challenge_id=self.challenge_id(task), found_by=self.agent_id)
        accepted: list[str] = []
        for cand in candidates:
            await self.flag_manager.submit(cand)
            self._record_flag(task, cand.value)   # 共享缓存：同伴下一回合即见
            if cand.accepted:
                accepted.append(cand.value)
        return accepted

    async def _harvest_http(self, text: str, task: Task, source: str) -> list[str]:
        """HTTP 响应取证：原文 + 内嵌编码串链式解码 → Flag 提取（与题面同规格）。"""
        accepted = await self._harvest(text, task, source)
        decoded = _decode_blobs(text)
        if decoded:
            accepted.extend(await self._harvest(
                "\n".join(decoded), task, source + ":decoded"))
        return accepted

    # ------------------------------------------------------------ 路线生成

    def route_templates(self, task: Task) -> list[RoutePlan]:
        """模板解题已删除：保留 ABC 契约，返回单条无固定步骤的解题路线。"""
        return [RoutePlan(name="solve", strategy="自主解题：从题面与目标出发自行决定方向",
                          steps=[{"action": "investigate"}])]

    async def plan_routes(self, task: Task) -> list[RoutePlan]:
        """单线调查为默认：一条路线 = 一条持久解题对话。

        简单题简单做——不再为「取个路线名」多花一次 LLM 调用；K>1
        （主 Agent 明确要求多角度并发）时才由 LLM 按题提出 K 个角度。
        """
        k = task.max_routes if task.max_routes is not None else 1
        k = max(1, min(int(k), self.max_routes))
        plans: list[RoutePlan] = []
        if k > 1:
            angles = await self._llm_propose_angles(task, k)
            plans = [RoutePlan(name=a["name"], strategy=a["angle"],
                               steps=[{"action": "investigate"}])
                     for a in angles]
        if not plans:
            plans = [RoutePlan(
                name="solve",
                strategy=self.direction_brief or "自主解题：从题面与目标出发自行决定方向",
                steps=[{"action": "investigate"}])]
        if self.ctx.knowledge is not None:
            self._attach_kb_guidance(task, plans)
        return plans

    async def _llm_propose_angles(self, task: Task, k: int) -> list[dict[str, str]]:
        """LLM 按题面提出 k 个互不重叠的调查角度（仅在多角度并发时使用）。"""
        ch = self.challenge_of(task)
        data = await self._llm_chat_json("route_planner", {
            "challenge": _challenge_view(ch),
            "attachments": [str(a)[:300] for a in ch.get("attachments", [])[:10]],
            "slots": k,
            "tools": [t["function"]["name"] for t in self.tool_schemas],
            "direction_brief": self.direction_brief,
            "constraints": (
                "返回 JSON：{\"angles\": [{\"name\": 英文短名(小写字母数字下划线), "
                "\"angle\": 一句话调查角度}]}。角度必须针对本题的具体信息"
                "（题面/附件/目标特征），互相不重叠，最多 slots 条；"
                + _METHODOLOGY +
                " 角度先覆盖完整体验与目标理解，再考虑针对已见线索的深入利用；"
                "不要以具体攻击技术（注入/爆破等）作为初始角度——除非题面信息"
                "已明确指向它。题目信息不足以区分时给出 1 条即可。"),
        }, max_user_chars=8000, timeout_seconds=_SOLVER_TIMEOUT,
            reasoning_effort=self.task_effort(task))
        raw_angles = (data or {}).get("angles")
        if not isinstance(raw_angles, list):
            return []
        angles: list[dict[str, str]] = []
        names: set[str] = set()
        for raw in raw_angles:
            if len(angles) >= k or not isinstance(raw, dict):
                break
            name = re.sub(r"[^a-z0-9_]+", "_",
                          str(raw.get("name", "")).lower()).strip("_")[:32]
            angle = str(raw.get("angle", "")).strip()[:200]
            if not name or not angle or name in names:
                continue
            names.add(name)
            angles.append({"name": name, "angle": angle})
        return angles

    # ------------------------------------------------------------ 解题环

    async def execute_step(self, task, route, plan, step) -> StepResult:
        if step["action"] == "investigate":
            return await self._solve_turn(task, route, plan)
        return StepResult(ok=False, note=f"未知步骤: {step['action']}")

    def _inv_state(self, task: Task, route) -> dict[str, Any]:
        """按 题×路线×任务 维度的解题状态（对话/尝试签名/收尾标记）。

        任务维度进键：同一赛题的重攻轮次是全新任务，对话从零开始，
        但题级共享结论与环形台账跨轮可见（不重复做功）。
        """
        key = f"inv|{self.challenge_id(task)}|{route.name}|{task.task_id}"
        state = self.cache.get(self.route_namespace, key)
        if not isinstance(state, dict):
            state = {"messages": [], "seed": [], "observations": [], "tried": [],
                     "flags": [], "concluded": False, "stall": "", "doom": 0,
                     "turn": 0, "llm_failures": 0}
            self.cache.put(self.route_namespace, key, state)
        return state

    def _save_inv_state(self, task: Task, route, state: dict[str, Any]) -> None:
        self.cache.put(self.route_namespace,
                       f"inv|{self.challenge_id(task)}|{route.name}|{task.task_id}",
                       state)

    def _shared_conclusions(self, task: Task) -> list[str]:
        """题级共享结论：各路线收尾结论（同伴路线与复盘都能读到）。"""
        got = self.cache.get(self.shared_namespace,
                             f"shared|{self.challenge_id(task)}")
        return got if isinstance(got, list) else []

    def _record_conclusion(self, task: Task, route, summary: str) -> None:
        key = f"shared|{self.challenge_id(task)}"
        got = self.cache.get(self.shared_namespace, key)
        conclusions = got if isinstance(got, list) else []
        conclusions.append(f"[{self.direction}/{route.name}] {summary}"[:300])
        self.cache.put(self.shared_namespace, key, conclusions[-12:])

    # ------------------------------------------------------------ 环形共享缓存
    # 同题的所有方向 Agent（并行工作）共用一份现场：已探入口、共享笔记、
    # 已夺 Flag。任何一方写入，其余方下一回合即见——不重复做功、相互接力。

    def _shared_site(self, task: Task) -> dict[str, Any]:
        got = self.cache.get(self.shared_namespace,
                             f"site|{self.challenge_id(task)}")
        return got if isinstance(got, dict) else {}

    def _record_site(self, task: Task, url: str, line: str) -> None:
        key = f"site|{self.challenge_id(task)}"
        site = self._shared_site(task)
        site[url[:200]] = f"{self.direction}: {line}"[:200]
        if len(site) > 60:                      # 保序裁剪：丢最旧的
            for old in list(site)[:len(site) - 60]:
                site.pop(old, None)
        self.cache.put(self.shared_namespace, key, site)

    def _shared_notes(self, task: Task) -> list[str]:
        got = self.cache.get(self.shared_namespace,
                             f"notes|{self.challenge_id(task)}")
        return got if isinstance(got, list) else []

    def _record_note(self, task: Task, text: str) -> None:
        key = f"notes|{self.challenge_id(task)}"
        entry = f"[{self.direction}] {text}"[:400]
        notes = self._shared_notes(task)
        if entry in notes:          # 同伴/自己已写过同一条，不重复登记
            return
        notes.append(entry)
        self.cache.put(self.shared_namespace, key, notes[-20:])

    def _shared_flags(self, task: Task) -> list[dict[str, str]]:
        got = self.cache.get(self.shared_namespace,
                             f"flags|{self.challenge_id(task)}")
        return [f for f in got if isinstance(f, dict)] if isinstance(got, list) else []

    def _record_flag(self, task: Task, value: str) -> None:
        key = f"flags|{self.challenge_id(task)}"
        flags = self._shared_flags(task)
        if value not in [f.get("value") for f in flags]:
            flags.append({"value": value, "by": self.direction,
                          "round": int(getattr(task, "round_no", 0) or 0)})
            self.cache.put(self.shared_namespace, key, flags)

    def _peer_flags(self, task: Task, state: dict[str, Any], *,
                    same_round: bool = False) -> list[str]:
        """同伴（其他方向）已捕获、且本路线尚未持有的 Flag。

        same_round=True 只认本轮并行的同伴——上一轮的候选可能未经判定，
        不足以让新方向直接收工（信息照常可见，只是不当作终止信号）。
        """
        mine = set(state.get("flags") or [])
        rnd = int(getattr(task, "round_no", 0) or 0)
        out: list[str] = []
        for f in self._shared_flags(task):
            if f.get("by") == self.direction or f.get("value") in mine:
                continue
            if same_round and int(f.get("round", 0) or 0) != rnd:
                continue
            out.append(str(f["value"]))
        return out

    def _peer_updates(self, task: Task, state: dict[str, Any]) -> str:
        """同伴动态：自本路线上次查看以来，其他方向新增的入口/笔记/Flag。"""
        seen = state.setdefault("peer_seen", {"site": [], "notes": 0, "flags": []})
        lines: list[str] = []
        site = self._shared_site(task)
        for url, line in [(u, v) for u, v in site.items()
                          if u not in seen["site"]
                          and not v.startswith(f"{self.direction}:")][-12:]:
            lines.append(f"· {url} → {line}")
            seen["site"].append(url)
        seen["site"] = seen["site"][-200:]
        notes = self._shared_notes(task)
        for note in notes[seen["notes"]:][-8:]:
            if not note.startswith(f"[{self.direction}]"):
                lines.append(f"· 同伴笔记 {note}")
        seen["notes"] = len(notes)
        concl = self._shared_conclusions(task)
        for c in concl[seen.get("concl", 0):][-4:]:
            if not c.startswith(f"[{self.direction}/"):
                lines.append(f"· 同伴结论 {c}")
        seen["concl"] = len(concl)
        fresh = [v for v in self._peer_flags(task, state) if v not in seen["flags"]]
        if fresh:
            lines.append("· 同伴已捕获 Flag：" + ", ".join(fresh))
            seen["flags"].extend(fresh)
        if not lines:
            return ""
        return ("## 同伴动态（并行工作的其他方向 Agent 刚做了什么，"
                "避免重复、可以直接接力）\n" + "\n".join(lines[:16]))

    def _ring_actor(self, route) -> str:
        """环形台账身份按路线区分：同 Agent 的并行路线之间也互相避让。"""
        return f"{self.agent_id}|{route.name}"

    def _ring_name(self, route) -> str:
        return f"{self.agent_type.value}/{route.name}"

    async def _solve_turn(self, task: Task, route, plan: RoutePlan) -> StepResult:
        """解题环单回合：一次原生工具调用回合（可并发多个工具）。

        边界（与全系统同一原则）：LLM 只决定「试什么」，执行走同一
        http_probe 通道（ScopeGuard/RPS/审计/审批不变）；Flag 只由
        FlagManager 正则提取；LLM 不可用 → 本路线诚实停摆（无模板兜底）。
        """
        state = self._inv_state(task, route)
        if state["concluded"] or state["stall"]:
            return StepResult(ok=True, value=0.0,
                              note=state["stall"] or "本路线已收尾")
        # 环形协同终止：本轮并行的同伴方向已夺旗 → 本路线立即收尾（收敛阀）
        peers = self._peer_flags(task, state, same_round=True)
        if peers:
            state["concluded"] = True
            summary = f"同伴方向已捕获 Flag（{', '.join(peers[:3])}），本路线收尾"
            self._record_conclusion(task, route, summary)
            self.ctx.audit.log("agent.conclude", self.agent_id, {
                "route": route.name, "challenge": self.challenge_id(task),
                "turn": state["turn"], "summary": summary})
            return StepResult(ok=True, value=0.0, note=summary)

        boot_value, boot_note = 0.0, ""
        if not state["messages"]:
            boot_value, boot_note = await self._inv_bootstrap(task, route, state)
            self._seed_messages(task, route, plan, state)
        elif (update := self._peer_updates(task, state)):
            # 同伴动态作为新消息注入：并行 Agent 的现场即时互通
            state["messages"].append({"role": "user", "content": update})

        state["turn"] += 1
        turn = state["turn"]
        forced = (turn > _INVESTIGATE_MAX_STEPS
                  or state["doom"] >= _DOOM_STOP_AT)
        self._compact_messages(state["messages"])
        messages: list[dict[str, Any]] = state["messages"]
        if forced:
            messages = [*messages, {"role": "user", "content": _FORCE_FINISH}]
        emitter = _DeltaEmitter(self.ctx.audit, self.agent_id, route=route.name,
                                challenge=self.challenge_id(task), turn=turn)
        self.ctx.audit.log("agent.turn", self.agent_id, {
            "route": route.name, "challenge": self.challenge_id(task),
            "turn": turn, "budget": f"{turn}/{_INVESTIGATE_MAX_STEPS}",
            "forced_finish": forced, "angle": plan.strategy[:160]})
        try:
            resp = await self.ctx.gateway.chat(
                messages, task_type="execution",
                max_tokens=_SOLVER_MAX_TOKENS, timeout_seconds=_SOLVER_TIMEOUT,
                tools=None if forced else self.tool_schemas,
                tool_choice=None if forced else "auto",
                on_delta=emitter,
                reasoning_effort=self.task_effort(task))
        except Exception as exc:  # noqa: BLE001 —— 单次调用失败允许下一回合重试
            emitter.flush()
            state["llm_failures"] += 1
            self.ctx.audit.log("agent.error", self.agent_id, {
                "route": route.name, "challenge": self.challenge_id(task),
                "turn": turn, "error": str(exc)[:300]})
            if state["llm_failures"] >= 2:
                state["stall"] = f"LLM 连续不可用，调查停摆（无模板兜底）：{str(exc)[:120]}"
            note = self._extend_or_finish(
                plan, state, f"第 {turn} 回合：LLM 调用失败（{str(exc)[:80]}）")
            self._save_inv_state(task, route, state)
            return StepResult(ok=True, value=boot_value,
                              note=self._merge_note(boot_note, note))
        emitter.flush()
        state["llm_failures"] = 0

        calls = [c for c in (resp.tool_calls or []) if isinstance(c, dict)]
        assistant: dict[str, Any] = {"role": "assistant",
                                     "content": resp.content or ""}
        if calls:
            assistant["tool_calls"] = [
                {"id": c.get("id") or f"call_{i}", "type": "function",
                 "function": {
                     "name": str(c.get("name") or ""),
                     "arguments": c.get("raw") or json.dumps(
                         c.get("arguments") or {}, ensure_ascii=False)}}
                for i, c in enumerate(calls)]
        state["messages"].append(assistant)

        if not calls:
            # 不再调用工具 = 调查结束，这条消息就是结论
            summary = (resp.content or "").strip()
            state["concluded"] = True
            self._record_conclusion(task, route, summary or "模型未给出结论文本")
            self.ctx.audit.log("agent.conclude", self.agent_id, {
                "route": route.name, "challenge": self.challenge_id(task),
                "turn": turn, "summary": summary[:800]})
            note = f"收尾（第 {turn} 回合）：{summary[:160] or '无结论文本'}"
            self._save_inv_state(task, route, state)
            return StepResult(ok=True, value=boot_value,
                              note=self._merge_note(boot_note, note))

        self.ctx.audit.log("agent.tool_call", self.agent_id, {
            "route": route.name, "challenge": self.challenge_id(task),
            "turn": turn,
            "calls": [{"id": c.get("id"), "name": c.get("name"),
                       "arguments": c.get("arguments")} for c in calls]})
        outcomes = await asyncio.gather(
            *(self._run_tool_call(task, route, state, call) for call in calls))
        total_value = 0.0
        notes: list[str] = []
        findings: list[dict[str, Any]] = []
        for call, out in zip(calls, outcomes):
            state["messages"].append({
                "role": "tool", "tool_call_id": call.get("id") or "call_0",
                "content": (out.text or "（无输出）")[:_TOOL_RESULT_CAP]})
            total_value += out.value
            findings.extend(out.findings)
            if out.note:
                notes.append(out.note)
            self.ctx.audit.log("agent.tool_result", self.agent_id, {
                "route": route.name, "challenge": self.challenge_id(task),
                "id": call.get("id"), "name": call.get("name"), "ok": out.ok,
                "note": out.note[:200], "result": (out.text or "")[:1200]})
        if state["doom"] >= _DOOM_STOP_AT:
            state["messages"].append({"role": "user", "content": (
                "你已经连续重复同一动作。停止重复：换一个完全不同的思路，"
                "或直接给出当前结论。")})
        head = f"第 {turn} 回合"
        note = f"{head}：" + "；".join(notes) if notes else head
        note = self._extend_or_finish(plan, state, note)
        self._save_inv_state(task, route, state)
        return StepResult(
            ok=True, value=min(boot_value + total_value, 1.0),
            progress_delta=1.0 / _INVESTIGATE_MAX_STEPS,
            findings=findings, note=self._merge_note(boot_note, note[:300]))

    # ------------------------------------------------------------ 工具执行

    async def _run_tool_call(self, task: Task, route, state: dict[str, Any],
                             call: dict[str, Any]) -> _ToolOutcome:
        """单次工具调用：确定性校验 → 执行 → 结果回灌（异常也回灌，模型自纠）。"""
        name = str(call.get("name") or "")
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        if call.get("parse_error"):
            return _ToolOutcome(ok=False, text=(
                f"调用参数不是合法 JSON（{call['parse_error']}）。"
                "请重新调用并给出合法的 JSON 参数。"))
        if not name:
            return _ToolOutcome(ok=False, text="工具名缺失，请重新调用。")
        if name == "note":
            text = str(args.get("text", "") or "").strip()
            if not text:
                return _ToolOutcome(ok=False, text="note 需要 text 参数。")
            self._record_note(task, text)
            self.ctx.audit.log("agent.note", self.agent_id, {
                "route": route.name, "challenge": self.challenge_id(task),
                "text": text[:400]})
            return _ToolOutcome(ok=True, value=0.05, note=f"共享笔记：{text[:60]}",
                                text="已写入团队共享笔记，同伴下一回合可见。")
        sig = self._call_signature(name, self._sig_args(task, args))
        if sig in state["tried"]:
            state["doom"] += 1
            note = f"重复动作跳过: {name}"
            if state["doom"] >= _DOOM_WARN_AT:
                note += f"（连续 {state['doom']} 次，已提示换方向）"
            state["observations"].append(
                {"action": name, "result": "重复动作（本路线已做过）"})
            return _ToolOutcome(ok=False, note=note, text=(
                "你之前已经执行过完全相同的调用，结果就在上文对话里。"
                "不要重复，换一个思路或直接给出结论。"))
        state["tried"].append(sig)
        state["doom"] = 0
        try:
            if name == "http_request":
                out = await self._tool_http(task, route, state, args)
            elif name in ("shell", "python"):
                out = await self._tool_sandbox(task, route, state, name, args)
            elif name == "analyze_text":
                out = await self._tool_analyze_text(task)
            elif name == "analyze_attachments":
                out = await self._tool_analyze_attachments(task)
            else:
                out = await self._direction_tool(task, route, state, name, args)
                if out is None:
                    out = _ToolOutcome(ok=False, text=(
                        f"工具 {name} 不在本方向工具目录内，请使用已提供的工具。"))
        except Exception as exc:  # noqa: BLE001 —— 工具异常作为结果回灌，模型自行纠偏
            out = _ToolOutcome(
                ok=False,
                text=f"工具执行异常：{type(exc).__name__}: {str(exc)[:200]}")
            self.ctx.audit.log("agent.error", self.agent_id,
                               {"tool": name, "error": str(exc)[:200]})
        return out

    def _sig_args(self, task: Task, args: dict[str, Any]) -> dict[str, Any]:
        """动作签名入参：HTTP 调用按「完整地址+方法+参数+请求头」归一化，
        使 path 写法与 url 写法、参数顺序不同的同一请求视为同一动作；
        请求头是签名的一部分——换头重试（如 WS 握手 key、Range 分段）
        是有意的变体请求，不按重复动作熔断。"""
        if "path" not in args and "url" not in args:
            return args
        resolved = self._resolve_http(task, args)
        if resolved is None:
            return args
        url, params, headers, data = resolved
        return {"url": url, "method": str(args.get("method", "GET")).upper(),
                "params": params or {}, "headers": headers or {},
                "data": (data or "")[:400]}

    @staticmethod
    def _call_signature(name: str, args: dict[str, Any]) -> str:
        try:
            payload = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = str(args)
        return f"{name}|{payload}"[:400]

    def _resolve_http(self, task: Task, args: dict[str, Any]
                      ) -> tuple[str, dict[str, str] | None,
                                 dict[str, str] | None, str | None] | None:
        """HTTP 入参归一化：path/url → 完整地址，参数/头/体按上限裁剪。

        解析结果同时用于动作签名（去重）与实际请求——同一请求的两种写法
        归一到同一个签名。非法目标返回 None。
        """
        ch = self.challenge_of(task)
        base = str(ch.get("url") or task.target or "").rstrip("/")
        path = str(args.get("path", "") or "").strip()
        full = str(args.get("url", "") or "").strip()
        url = ""
        if path.startswith("/") and "://" not in path and len(path) <= 300 and base:
            url = base + path
        elif full.startswith(("http://", "https://")) and len(full) <= 400:
            url = full
        if not url:
            return None
        params = args.get("params")
        if isinstance(params, dict) and params:
            params = {str(k)[:32]: str(v)[:400] for k, v in list(params.items())[:6]}
        else:
            params = None
        headers = args.get("headers")
        if isinstance(headers, dict) and headers:
            headers = {str(k)[:40]: str(v)[:300] for k, v in list(headers.items())[:8]}
        else:
            headers = None
        data = args.get("data")
        if data is not None and not isinstance(data, str):
            data = json.dumps(data, ensure_ascii=False)
        if isinstance(data, str):
            data = data[:2000]
        return url, params, headers, data

    async def _tool_http(self, task: Task, route, state: dict[str, Any],
                         args: dict[str, Any]) -> _ToolOutcome:
        """通用 HTTP 动作：LLM 提议 → 确定性校验 → 环形避让 → 执行 → 观察。

        目标范围由 ScopeGuard 把关（http_probe 内），参数上限仅防上下文污染。
        """
        resolved = self._resolve_http(task, args)
        if resolved is None:
            return _ToolOutcome(ok=False, text=(
                "目标不合法：path 需为以 / 开头的站内路径，或 url 传完整 "
                "http(s) 地址（须在授权范围内）。"))
        url, params, headers, data = resolved
        method = str(args.get("method", "GET") or "GET").upper()
        if method not in _HTTP_METHODS:
            return _ToolOutcome(
                ok=False, text=f"方法 {method} 不支持，可用：{', '.join(_HTTP_METHODS)}")
        base = str(self.challenge_of(task).get("url") or task.target or "").rstrip("/")
        shown = url[len(base):] if base and url.startswith(base) else url
        desc = f"{method} {shown[:100] or '/'}"

        # 环形避让：同伴（含本 Agent 其他路线/前轮尝试）在此入口无产出 → 不重复做功
        akey = attempt_key(method, url, params)
        if self.ctx.ring is not None:
            prior = self.ctx.ring.should_avoid(akey, agent_id=self._ring_actor(route))
            if prior is not None:
                state["observations"].append(
                    {"action": desc, "result":
                     f"环形避让：{prior['agent_name']} 已试此入口（无产出），未重复"})
                self.ctx.audit.log("ring.avoid", self.agent_id, {
                    "route": route.name, "key": akey[:200],
                    "prior_agent": prior["agent_name"]})
                return _ToolOutcome(ok=False, note=f"环形避让：跳过 {desc}", text=(
                    f"{prior['agent_name']} 已经试过 {desc} 且没有产出，"
                    "本次未重复请求。换一个入口或思路。"))

        request: dict[str, Any] = {"url": url, "method": method}
        if params:
            request["params"] = params
        if headers:
            request["headers"] = headers
        if data:
            request["data"] = data
        res = await self.ctx.tools.call("http_probe", request, **self.ctx.builtin_ctx)
        if not res.ok:
            if self.ctx.ring is not None:
                self.ctx.ring.record_attempt(
                    akey, agent_id=self._ring_actor(route),
                    agent_name=self._ring_name(route), action="http_request",
                    outcome=0.0, note=str(res.error)[:120])
            state["observations"].append(
                {"action": desc, "result": f"请求失败: {str(res.error)[:100]}"})
            return _ToolOutcome(ok=False, note=f"{desc} → 请求失败", text=(
                f"请求失败（{desc}）：{str(res.error)[:300]}"))
        accepted = await self._harvest_http(res.stdout, task, f"llm:{desc[:40]}")
        status = res.parsed.get("status_code", 0)
        state["flags"].extend(accepted)
        obs = _http_observation(desc, res, url, accepted)
        state["observations"].append(obs)
        # 环形共享缓存：入口与关键信号登记，同伴不再重复探同一入口
        signal = _signal_line(obs)
        self._record_site(task, url, f"{status}{signal}")
        value = (0.25 if status < 400 else 0.05) + 0.6 * len(accepted)
        if self.ctx.ring is not None:
            self.ctx.ring.record_attempt(
                akey, agent_id=self._ring_actor(route),
                agent_name=self._ring_name(route), action="http_request",
                outcome=value, note=f"{status} flag={len(accepted)}")
        return _ToolOutcome(
            ok=True, value=value, observation=obs,
            findings=[{"type": "flag", "value": f} for f in accepted],
            note=f"{desc} → {status} flag={len(accepted)}",
            text=_obs_text(obs))

    async def _tool_sandbox(self, task: Task, route, state: dict[str, Any],
                            name: str, args: dict[str, Any]) -> _ToolOutcome:
        """沙箱执行动作（shell/python）：LLM 出代码 → bwrap 沙箱运行 → 观察。

        与 http_request 同一原则：LLM 只决定「跑什么」，执行边界由沙箱
        （整机只读 + 主目录遮蔽 + 工作区可写）与审计留痕约束；输出照常过
        Flag 提取与观察序列。
        """
        param = "command" if name == "shell" else "code"
        code = str(args.get(param, "") or "").strip()
        if not code:
            return _ToolOutcome(ok=False, text=f"{name} 工具需要 {param} 参数。")
        try:
            timeout = float(args.get("timeout") or 60)
        except (TypeError, ValueError):
            timeout = 60.0
        first = code.splitlines()[0][:80]
        desc = f"{name}: {first}"
        res = await self.ctx.tools.call(
            "sandbox_exec",
            {"code": code, "language": "python" if name == "python" else "shell",
             "timeout": timeout},
            **self.ctx.builtin_ctx)
        if not res.ok and not (res.stdout or res.stderr):
            state["observations"].append(
                {"action": desc, "result": f"执行失败: {str(res.error)[:100]}"})
            return _ToolOutcome(ok=False, note=f"{desc} → 执行失败", text=(
                f"{desc} 执行失败：{str(res.error)[:300]}"))
        rc = res.parsed.get("exit_code", res.returncode)
        accepted = await self._harvest(
            (res.stdout or "") + "\n" + (res.stderr or ""), task, f"llm:{name}")
        state["flags"].extend(accepted)
        status = (f"超时终止（{timeout:.0f}s）" if res.timed_out
                  else f"退出码 {rc}")
        state["observations"].append(
            {"action": desc,
             "result": f"{status}；输出 {len(res.stdout or '')} 字符"})
        lines = [f"$ {first}", f"（{status}，耗时 {res.duration_ms / 1000:.1f}s）"]
        if res.stdout:
            lines.append("stdout:\n" + res.stdout)
        if res.stderr:
            lines.append("stderr:\n" + res.stderr)
        if res.timed_out:
            lines.append("提示：超时通常意味着脚本在等待输入或死循环——"
                         "交互式脚本请改为非交互（预置输入/设置超时）。")
        if accepted:
            lines.append("Flag 已捕获并提交（FlagManager 确定性提取）: "
                         + ", ".join(accepted))
        return _ToolOutcome(
            ok=True, value=0.05,
            findings=[{"type": "flag", "value": f} for f in accepted],
            note=f"{desc} → {status} flag={len(accepted)}",
            text="\n".join(lines))

    async def _tool_analyze_text(self, task: Task) -> _ToolOutcome:
        """题面文本取证：全部载荷字符串的编码串链式解码 + 零宽字符统计 + Flag 提取。"""
        text = _challenge_text(self.challenge_of(task))
        decoded_texts = _decode_blobs(text)
        accepted = await self._harvest("\n".join([text, *decoded_texts]), task, "description")
        zw = len(re.findall(r"[\u200b\u200c\u200d\u2060]", text))
        obs = {"action": "analyze_text",
               "result": f"可解码串 {len(decoded_texts)} 个，零宽字符 {zw} 个"}
        if accepted:
            obs["flags"] = accepted
        state_note = (f"题面分析：{len(decoded_texts)} 个可解码串，"
                      f"零宽字符={zw} flag={len(accepted)}")
        body = ("题面分析结果：可解码串 "
                f"{len(decoded_texts)} 个，零宽字符 {zw} 个。\n"
                + ("解码结果:\n" + "\n".join(d[:600] for d in decoded_texts[:6])
                   if decoded_texts else "（无可解码串）")
                + (f"\nFlag 已捕获并提交: {', '.join(accepted)}" if accepted else ""))
        return _ToolOutcome(ok=True, value=0.15 + 0.6 * len(accepted),
                            findings=[{"type": "flag", "value": f} for f in accepted],
                            note=state_note, observation=obs, text=body)

    async def _ensure_attachments(self, task: Task) -> dict[str, dict[str, Any]]:
        """附件解析结果（缺则补跑 file_analyze）——方向工具的共同前置。"""
        parsed_map: dict[str, dict[str, Any]] = {}
        for path in self.challenge_of(task).get("attachments", []):
            cached = self.cache.get(self.route_namespace, f"file|{path}")
            if isinstance(cached, dict) and cached:
                parsed_map[str(path)] = cached
                continue
            res = await self.ctx.tools.call(
                "file_analyze", {"path": path}, **self.ctx.builtin_ctx)
            if res.ok and isinstance(res.parsed, dict):
                self.cache.put(self.route_namespace, f"file|{path}", res.parsed)
                parsed_map[str(path)] = res.parsed
        return parsed_map

    async def _tool_analyze_attachments(self, task: Task) -> _ToolOutcome:
        """对全部附件做魔数识别 + 字符串提取，文本进 Flag 提取。"""
        attachments = self.challenge_of(task).get("attachments", [])
        if not attachments:
            return _ToolOutcome(ok=False, text="本题没有附件。")
        findings: list[dict[str, Any]] = []
        notes: list[str] = []
        body_lines: list[str] = []
        all_strings: list[str] = []
        for path in attachments:
            res = await self.ctx.tools.call(
                "file_analyze", {"path": path}, **self.ctx.builtin_ctx)
            if not res.ok:
                notes.append(f"{path}: {res.error}")
                body_lines.append(f"{path}: 解析失败 {str(res.error)[:120]}")
                continue
            parsed = res.parsed
            notes.append(f"{path}: magic={parsed['magic']} size={parsed['size']}")
            strings = parsed.get("strings", [])
            all_strings.extend(strings)
            findings.append({"type": "file", "path": path,
                             "magic": parsed["magic"], "size": parsed["size"]})
            self.cache.put(self.route_namespace, f"file|{path}", parsed)
            body_lines.append(
                f"{path}: magic={parsed['magic']} size={parsed['size']} "
                f"is_elf={parsed.get('is_elf')}\n  字符串样本: "
                + " | ".join(str(s)[:120] for s in strings[:40]))
        accepted = await self._harvest("\n".join(all_strings), task, "attachment_strings")
        findings.extend({"type": "flag", "value": f} for f in accepted)
        if accepted:
            body_lines.append("Flag 已捕获并提交: " + ", ".join(accepted))
        return _ToolOutcome(ok=True, value=0.3 + 0.5 * len(accepted),
                            findings=findings, note="; ".join(notes),
                            text="\n".join(body_lines)[:_TOOL_RESULT_CAP])

    async def _direction_tool(self, task: Task, route, state: dict[str, Any],
                              name: str, args: dict[str, Any]) -> _ToolOutcome | None:
        """方向专属工具钩子；返回 None = 不在本方向工具目录内。"""
        return None

    # ------------------------------------------------------------ 首轮感知

    async def _inv_bootstrap(self, task: Task, route, state: dict[str, Any]) -> tuple[float, str]:
        """首轮确定性感知：把「能直接看见的东西」装进初始上下文。

        有 URL → 抓赛题入口页；否则 → 附件感知 + 题面文本。感知同样过
        Flag 提取与环形登记（与后续工具调用同一通道），结果进 seed 文本，
        由 _seed_messages 拼进首条用户消息——模型第一步就已带着现场。
        """
        ch = self.challenge_of(task)
        url = str(ch.get("url") or task.target or "")
        if url.startswith(("http://", "https://")):
            akey = attempt_key("GET", url)
            prior = (self.ctx.ring.should_avoid(akey, agent_id=self._ring_actor(route))
                     if self.ctx.ring is not None else None)
            if prior is not None:
                state["seed"].append(
                    f"入口页 {url}：{prior['agent_name']} 已试过且无产出（环形避让，未重复请求）")
                return 0.0, f"环形共享：{prior['agent_name']} 已试入口页（无产出）"
            res = await self.ctx.tools.call("http_probe", {"url": url},
                                            **self.ctx.builtin_ctx)
            if not res.ok:
                state["seed"].append(f"入口页 {url}：不可达（{str(res.error)[:120]}）")
                if self.ctx.ring is not None:
                    self.ctx.ring.record_attempt(
                        akey, agent_id=self._ring_actor(route),
                        agent_name=self._ring_name(route), action="bootstrap",
                        outcome=0.0, note="入口页不可达")
                return 0.0, "赛题入口不可达"
            accepted = await self._harvest_http(res.stdout, task, "bootstrap:page")
            status = res.parsed.get("status_code", 0)
            state["flags"].extend(accepted)
            obs = _http_observation(f"GET {url}", res, url, accepted)
            state["observations"].append(obs)
            self._record_site(task, url, f"{status}{_signal_line(obs)}")
            state["seed"].append("入口页预感知：\n" + _obs_text(obs))
            value = (0.3 if status < 400 else 0.05) + 0.6 * len(accepted)
            if self.ctx.ring is not None:
                self.ctx.ring.record_attempt(
                    akey, agent_id=self._ring_actor(route),
                    agent_name=self._ring_name(route), action="bootstrap",
                    outcome=value, note=f"入口页 {status}")
            return value, f"入口页 {status} flag={len(accepted)}"
        attachments = ch.get("attachments", [])
        if attachments:
            out = await self._tool_analyze_attachments(task)
            state["flags"].extend(f["value"] for f in out.findings if f["type"] == "flag")
            state["seed"].append("附件预感知：\n" + out.text)
            return out.value, out.note[:120]
        out = await self._tool_analyze_text(task)
        state["flags"].extend(f["value"] for f in out.findings if f["type"] == "flag")
        state["seed"].append("题面预感知：\n" + out.text)
        return out.value, out.note[:120]

    # ------------------------------------------------------------ 提示词与上下文

    def _solver_system_prompt(self, task: Task) -> str:
        scope = self.ctx.policy.guard.scope
        allowed = scope.allowed_domains + scope.allowed_cidrs + scope.allowed_urls
        return (
            f"你是灵探 SpiritScout 的 CTF {self.direction} 方向解题 Agent，"
            f"正在解一道已获授权的 CTF 赛题。授权范围严格限于: {allowed}；"
            f"禁止访问范围外目标，禁止破坏性操作。\n"
            f"{self.direction_brief}\n"
            f"{_METHODOLOGY}\n"
            "工作方式：工具就是你的手——需要看什么就用工具去拿，不要凭空猜测"
            "页面内容或响应。每一轮可以调用一个或多个工具：互不依赖的请求放在"
            "同一轮并发发出，省时间。\n"
            "动手能力：要跑命令用 shell，要写代码用 python——它们在隔离沙箱里"
            "执行（系统文件只读、你的工作区可写、联网仅限授权目标）。"
            "WebSocket/socket 等协议交互、数据计算、脚本化构造请求，"
            "凡是 http_request 够不着的，一律用它们去完成。\n"
            "协同：同一道题可能有其他方向的 Agent 在并行工作，你们共享现场——"
            "别人探过的入口、写下的笔记、拿到的 Flag 会以「同伴动态」出现在对话里；"
            "有值得同伴立刻知道的发现（接口/参数/口令线索）就用 note 工具写一条，"
            "发现别人已经解决或已经试过的，不要重复做功。\n"
            "收尾：当你不再调用工具时，你最后一条消息就是交给主 Agent 的结论——"
            "简明写清「拿到了什么（Flag 原文）、依据是什么、还差什么」。拿到 Flag "
            "或确认无解空间时立即收尾，不要空转。\n"
            "纪律：不要重复调用完全相同的请求；结果里已经有答案就不要重复验证；"
            "简单题简单做——用最少步骤验证最直接的线索，不要为一道小题铺开大扫描。"
        )

    def _seed_messages(self, task: Task, route, plan: RoutePlan,
                       state: dict[str, Any]) -> None:
        """首条用户消息：题面 + 入口预感知 + 同伴/前轮结论 + 目标。"""
        ch = self.challenge_of(task)
        parts: list[str] = []
        target = str(ch.get("url") or task.target or "")
        if target:
            parts.append(f"## 目标入口\n{target}")
        parts.append("## 题目\n" + json.dumps(
            _challenge_view(ch), ensure_ascii=False, indent=1))
        attachments = [str(a) for a in ch.get("attachments", [])[:10]]
        if attachments:
            parts.append("## 附件\n" + "\n".join(attachments))
        if state["seed"]:
            parts.append("## 系统预感知（已替你抓取）\n" + "\n\n".join(state["seed"]))
        prior = self._shared_conclusions(task)[-6:]
        if prior:
            parts.append("## 已有结论（同伴路线/前轮尝试，避免重复做功）\n"
                         + "\n".join(prior))
        notes = [n for n in self._shared_notes(task) if not n.startswith(
            f"[{self.direction}]")][-8:]
        if notes:
            parts.append("## 同伴共享笔记（并行工作的其他方向 Agent 写下）\n"
                         + "\n".join(notes))
        site = self._shared_site(task)
        shown_site = list(site.items())[-20:]
        if shown_site:
            parts.append("## 已探入口（团队共享，含同伴；不必重复请求）\n"
                         + "\n".join(f"· {u} → {v}" for u, v in shown_site))
        peer_flags = self._peer_flags(task, state)
        if peer_flags:
            parts.append("## 同伴已捕获 Flag\n" + ", ".join(peer_flags))
        state.setdefault("peer_seen", {
            "site": [u for u, _ in shown_site],
            "notes": len(self._shared_notes(task)),
            "concl": len(self._shared_conclusions(task)),
            "flags": peer_flags})
        parts.append("## 任务\n夺取本题 Flag。Flag 常藏在响应正文/注释/编码串/"
                     "响应头或附件里；拿到就收尾。")
        state["messages"] = [
            {"role": "system", "content": self._solver_system_prompt(task)},
            {"role": "user", "content": "\n\n".join(parts)[:24000]},
        ]

    @staticmethod
    def _compact_messages(messages: list[dict[str, Any]]) -> None:
        """对话压缩：超预算时从最旧开始把长工具结果/助手长文折叠成占位符。

        保留 system 与首条用户消息（题面契约不丢）；折叠只影响旧回合。
        """
        total = sum(len(str(m.get("content") or "")) for m in messages)
        if total <= _MSG_CHAR_BUDGET:
            return
        for msg in messages[2:]:
            if total <= _MSG_CHAR_BUDGET:
                break
            content = str(msg.get("content") or "")
            if msg.get("role") not in ("tool", "assistant") or len(content) <= 400:
                continue
            keep = content[:200] + "…（早前内容已折叠，需要时重新获取）"
            total -= len(content) - len(keep)
            msg["content"] = keep

    @staticmethod
    def _merge_note(prefix: str, note: str) -> str:
        return f"{prefix}；{note}" if prefix else note

    @staticmethod
    def _extend_or_finish(plan: RoutePlan, state: dict[str, Any], note: str) -> str:
        """动态延展：未收尾则追加下一回合占位步（回合安全顶内）；到顶即收尾。"""
        if state["concluded"] or state["stall"]:
            return note
        if len(plan.steps) < _INVESTIGATE_MAX_STEPS:
            plan.steps.append({"action": "investigate"})
        else:
            state["concluded"] = True
            note += "；回合安全顶收尾"
        return note


# ================================================================ Web 专项


class CTFWebAgent(CTFBaseAgent):
    """Web 专项（4.4.1）：LLM 按题提出调查角度，逐环自主探测。"""

    agent_type = AgentType.CTF_WEB
    direction = "web"
    route_namespace = "ctf_web"
    direction_brief = (
        "先把入口页当用户界面用起来：按页面引导点开各个功能、提交表单、走完"
        "它的正常业务流程，同时从响应内容（结构、注释、链接、表单、脚本、接口"
        "与响应头）读懂这个应用是做什么的、有哪些功能与角色，再按证据选方向；"
        "Web 题常见考点（在信息支撑下选用，不逐条硬套）：源码/"
        "备份泄露（.git、.bak、www.zip）、robots.txt 与隐藏路径、参数注入"
        "（SQLi/命令注入/模板注入——模板 canary 如 {{7*7}} 被求值为 49 即命中）、"
        "文件包含与路径穿越、类型混淆与弱比较、反序列化、条件竞争、JWT 伪造、"
        "响应头/HTML 注释/JS 中的线索。")


# ================================================================ Crypto 专项


class CTFCryptoAgent(CTFBaseAgent):
    """Crypto 专项（4.4.4）：编码链/古典密码工具 + LLM 按题分析。

    RSA/AES/ECC 现代密码攻击依赖 SageMath/z3， 经工具层接入（不静默降级）；
    数学求解超出工具能力时，由 LLM 把结构分析与可验证线索写入结论。
    """

    agent_type = AgentType.CTF_CRYPTO
    direction = "crypto"
    route_namespace = "ctf_crypto"
    direction_brief = (
        "Crypto 题：先看题面/附件里的密文与算法描述。编码题用 decode_chain "
        "逐层剥；纯字母密文可试 caesar_brute（全位移）与 rail_fence（定栏还原）；"
        "现代密码（RSA/AES/ECC）先做结构分析——参数提取、小指数、共模、已知"
        "明文、padding 线索——把结构结论与卡点写进结论。")
    tool_schemas: ClassVar[list[dict[str, Any]]] = [
        *CTFBaseAgent.tool_schemas,
        _fn("decode_chain", "对指定串做 Base64/Hex/URL/Rot13 递归解码。",
            {"target": {"type": "string",
                        "description": "待解码的串；缺省用题面里的密文"}}),
        _fn("caesar_brute", "对纯字母密文做凯撒全位移爆破（26 个候选）。",
            {"target": {"type": "string",
                        "description": "纯字母密文；缺省用题面里的密文"}}),
        _fn("rail_fence", "对纯字母密文做栅栏密码定栏还原。",
            {"target": {"type": "string",
                        "description": "纯字母密文；缺省用题面里的密文"}}),
    ]

    def _ciphertext(self, task: Task) -> str:
        ch = self.challenge_of(task)
        blob = str(ch.get("ciphertext") or ch.get("description") or "")
        return blob.strip()

    async def _direction_tool(self, task, route, state, name, args):
        target = str(args.get("target", "") or "").strip() or self._ciphertext(task)
        if name == "decode_chain":
            if not target:
                return _ToolOutcome(ok=False, text="没有可解码的目标串：请在 target 里给出密文。")
            decoded = _decode_chain(target, max_depth=4)
            accepted = await self._harvest(decoded or "", task, "decode_chain")
            if not decoded or decoded == target:
                return _ToolOutcome(ok=False, text="这串没有被 Base64/Hex/URL/Rot13 识别出可解码层。")
            return _ToolOutcome(
                ok=True, value=0.3 + 0.6 * len(accepted),
                findings=[{"type": "flag", "value": f} for f in accepted],
                note=f"解码链结果 {len(decoded)} 字符 flag={len(accepted)}",
                text="解码结果:\n" + decoded[:3000]
                     + (f"\nFlag 已捕获并提交: {', '.join(accepted)}" if accepted else ""))
        if name == "caesar_brute":
            hits = _caesar_brute(target)
            if not hits:
                return _ToolOutcome(ok=False, text="目标串不是纯字母，凯撒爆破不适用。")
            accepted = await self._harvest("\n".join(hits), task, "caesar")
            return _ToolOutcome(
                ok=True, value=0.2 + 0.6 * len(accepted),
                findings=[{"type": "flag", "value": f} for f in accepted],
                note=f"凯撒爆破 {len(hits)} 个候选位移 flag={len(accepted)}",
                text="26 个位移结果:\n" + "\n".join(hits)[:3000]
                     + (f"\nFlag 已捕获并提交: {', '.join(accepted)}" if accepted else ""))
        if name == "rail_fence":
            hits = _rail_fence_brute(target)
            if not hits:
                return _ToolOutcome(ok=False, text="目标串不是纯字母或太短，栅栏还原不适用。")
            accepted = await self._harvest("\n".join(hits), task, "rail_fence")
            return _ToolOutcome(
                ok=True, value=0.2 + 0.5 * len(accepted),
                findings=[{"type": "flag", "value": f} for f in accepted],
                note=f"栅栏还原 {len(hits)} 个候选栏数 flag={len(accepted)}",
                text="各栏数还原结果:\n" + "\n".join(hits)[:3000]
                     + (f"\nFlag 已捕获并提交: {', '.join(accepted)}" if accepted else ""))
        return None


# ================================================================ Misc 专项


class CTFMiscAgent(CTFBaseAgent):
    """Misc 专项（4.4.5）：附件/字符串/编码工具 + LLM 按题分析。

    zsteg/binwalk/tshark 等隐写工具链 经 CLI 封装接入（6.8）。
    """

    agent_type = AgentType.CTF_MISC
    direction = "misc"
    route_namespace = "ctf_misc"
    direction_brief = (
        "Misc 题：附件魔数与元信息、strings 深挖、题面零宽字符与编码串是主要"
        "线索面。文件尾部附加数据、多余分块、时间戳异常等隐写信号若超出工具"
        "能力，把观察与假设写进结论，供复盘换工具方向。")
    tool_schemas: ClassVar[list[dict[str, Any]]] = [
        *CTFBaseAgent.tool_schemas,
        _fn("deep_strings",
            "对附件字符串做编码串链式解码（Base64/Hex/Rot13 等）。"
            "analyze_attachments 给出的字符串里疑似有编码串时用它。", {}),
    ]

    async def _direction_tool(self, task, route, state, name, args):
        if name == "deep_strings":
            return await self._deep_strings(task)
        return None

    async def _deep_strings(self, task: Task) -> _ToolOutcome:
        parsed_map = await self._ensure_attachments(task)
        if not parsed_map:
            return _ToolOutcome(ok=False, text="本题没有可分析的附件。")
        decoded_hits: list[str] = []
        for parsed in parsed_map.values():
            for s in parsed.get("strings", []):
                if len(s) >= _MIN_BLOB_LEN and re.fullmatch(r"[A-Za-z0-9+/=_-]+", s):
                    decoded = _decode_chain(s)
                    if decoded and decoded != s:
                        decoded_hits.append(decoded)
        accepted = await self._harvest("\n".join(decoded_hits), task, "deep_strings")
        if not decoded_hits:
            return _ToolOutcome(ok=False, text="附件字符串里没有被识别出的编码串。")
        return _ToolOutcome(
            ok=True, value=0.2 + 0.5 * len(accepted),
            findings=[{"type": "flag", "value": f} for f in accepted],
            note=f"字符串深挖：{len(decoded_hits)} 个可解码串 flag={len(accepted)}",
            text="解码命中:\n" + "\n".join(h[:600] for h in decoded_hits[:20])
                 + (f"\nFlag 已捕获并提交: {', '.join(accepted)}" if accepted else ""))


# ================================================================ Reverse 专项


class CTFReverseAgent(CTFBaseAgent):
    """Reverse 专项（4.4.3）：二进制情报工具 + LLM 按题分析。

    Ghidra 脚本化/angr 符号执行/z3 求解 经工具层接入（6.8）。
    """

    agent_type = AgentType.CTF_REVERSE
    direction = "reverse"
    route_namespace = "ctf_reverse"
    direction_brief = (
        "Reverse 题：先 analyze_attachments 拿魔数/架构/strings，再用 algo_hints "
        "归纳算法线索（xor/rc4/aes/tea 等）；从字符串里找校验比较串与自定义表；"
        "题面描述算法时按其步骤手工推演，把中间结论写进观察。需要动态调试或"
        "符号执行（超出工具能力）时，把线索与卡点写进结论。")
    tool_schemas: ClassVar[list[dict[str, Any]]] = [
        *CTFBaseAgent.tool_schemas,
        _fn("algo_hints",
            "从附件字符串归纳加密/编码算法线索（xor/rc4/aes/tea/base64 等）。"
            "逆向题定位算法时用它缩小范围。", {}),
    ]

    async def _direction_tool(self, task, route, state, name, args):
        if name == "algo_hints":
            return await self._algo_hints(task)
        return None

    async def _algo_hints(self, task: Task) -> _ToolOutcome:
        parsed_map = await self._ensure_attachments(task)
        if not parsed_map:
            return _ToolOutcome(ok=False, text="本题没有可分析的附件。")
        hints: set[str] = set()
        for parsed in parsed_map.values():
            blob = "\n".join(parsed.get("strings", []))
            for kw in ("xor", "rc4", "aes", "des", "base64", "sha", "md5", "tea"):
                if re.search(rf"(?i)\b{kw}\b", blob):
                    hints.add(kw)
        if not hints:
            return _ToolOutcome(ok=False, text="附件字符串里没有识别出常见算法线索。")
        return _ToolOutcome(ok=True, value=0.2,
                            note=f"二进制算法线索: {sorted(hints)}",
                            text="算法线索: " + ", ".join(sorted(hints)))


# ================================================================ Pwn 专项


class CTFPwnAgent(CTFBaseAgent):
    """Pwn 专项（4.4.2）：保护机制/危险函数情报工具 + LLM 按题分析。

    pwntools 模板化利用链生成/ROP 拼接 接入（6.8）；
    本专项完成结构化侦察（ELF 识别、保护机制清单、危险函数情报）。
    """

    agent_type = AgentType.CTF_PWN
    direction = "pwn"
    route_namespace = "ctf_pwn"
    direction_brief = (
        "Pwn 题：analyze_attachments 拿 ELF 识别；checksec 归纳保护机制（字符串"
        "级静态信号）；danger_funcs 提取危险函数线索（gets/strcpy/system 等）"
        "与漏洞类假设；结合题面远程入口（nc 地址/协议）把利用思路与卡点写进"
        "结论。")
    tool_schemas: ClassVar[list[dict[str, Any]]] = [
        *CTFBaseAgent.tool_schemas,
        _fn("checksec",
            "归纳附件 ELF 的保护机制清单（canary/fortify 静态信号）与架构。", {}),
        _fn("danger_funcs",
            "提取危险函数线索（gets/strcpy/system 等）与对应的漏洞类假设。", {}),
    ]
    # 危险函数 → 漏洞类线索（4.4.2 二进制漏洞分析的最小静态信号）
    _DANGER_FUNCS: ClassVar[dict[str, str]] = {
        "gets": "栈溢出（无边界输入）", "strcpy": "栈/堆溢出",
        "sprintf": "格式化字符串/溢出", "scanf": "格式化字符串风险",
        "free": "UAF/Double Free 审计点", "system": "ret2system 利用目标",
        "mprotect": "shellcode 执行铺垫",
    }

    async def _direction_tool(self, task, route, state, name, args):
        if name == "checksec":
            return await self._checksec(task)
        if name == "danger_funcs":
            return await self._danger_funcs(task)
        return None

    async def _checksec(self, task: Task) -> _ToolOutcome:
        parsed_map = await self._ensure_attachments(task)
        if not parsed_map:
            return _ToolOutcome(ok=False, text="本题没有可分析的附件。")
        notes: list[str] = []
        for path, parsed in parsed_map.items():
            if not parsed.get("is_elf"):
                notes.append(f"{path}: 非 ELF（magic={parsed.get('magic', '?')}）")
                continue
            strings = "\n".join(parsed.get("strings", []))
            protections = {
                "canary": "__stack_chk_fail" in strings,
                "fortify": "_chk@" in strings or "__printf_chk" in strings,
            }
            notes.append(f"{path}: ELF {parsed.get('elf_class') or '?'} "
                         f"canary={protections['canary']} fortify={protections['fortify']}")
            self.cache.put(self.route_namespace, f"protections|{path}", protections)
        return _ToolOutcome(ok=True, value=0.2,
                            note="; ".join(notes) or "无附件",
                            text="保护机制情报:\n" + "\n".join(notes))

    async def _danger_funcs(self, task: Task) -> _ToolOutcome:
        parsed_map = await self._ensure_attachments(task)
        if not parsed_map:
            return _ToolOutcome(ok=False, text="本题没有可分析的附件。")
        leads: list[str] = []
        for parsed in parsed_map.values():
            strings = "\n".join(parsed.get("strings", []))
            for func, hint in self._DANGER_FUNCS.items():
                if re.search(rf"\b{func}\b", strings):
                    leads.append(f"{func}: {hint}")
        if not leads:
            return _ToolOutcome(ok=False, text="附件字符串里没有出现常见危险函数。")
        return _ToolOutcome(
            ok=True, value=0.15 * min(len(leads), 3),
            findings=[{"type": "pwn_lead", "value": lead} for lead in leads],
            note=f"危险函数线索 {len(leads)} 条",
            text="危险函数线索:\n" + "\n".join(leads))


# ================================================================ 编码/古典密码工具


def _decode_chain(text: str, max_depth: int = 4) -> str:
    """编码变换链自动推理：Base64/Hex/URL/Rot13 逐层尝试（4.4.4 编码变换链）。

    每层只接受「可打印且发生变化」的解码，避免指数爆炸。
    """
    current = text.strip()
    for _ in range(max_depth):
        next_text = _try_single_decode(current)
        if next_text is None or next_text == current:
            break
        current = next_text
    return current


def _try_single_decode(text: str) -> str | None:
    candidates: list[str] = []
    # Base64（需合法字符集与长度）
    if re.fullmatch(r"[A-Za-z0-9+/=]+", text) and len(text) % 4 == 0:
        try:
            candidates.append(base64.b64decode(text, validate=True)
                                .decode("utf-8", errors="strict"))
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
    # Hex
    if re.fullmatch(r"(?:[0-9a-fA-F]{2})+", text) and len(text) >= 4:
        try:
            candidates.append(bytes.fromhex(text).decode("utf-8", errors="strict"))
        except UnicodeDecodeError:
            pass
    # URL
    if "%" in text:
        decoded = urllib.parse.unquote(text)
        if decoded != text:
            candidates.append(decoded)
    # Rot13（字母串）
    if re.fullmatch(r"[A-Za-z]+", text):
        candidates.append(codecs.decode(text, "rot_13"))
    for cand in candidates:
        if cand and all(ch.isprintable() or ch.isspace() for ch in cand):
            return cand
    return None


def _caesar_brute(text: str) -> list[str]:
    """凯撒全位移爆破（仅对纯字母密文），返回全部位移结果。"""
    if not re.fullmatch(r"[A-Za-z]+", text.strip()):
        return []
    out = []
    for shift in range(1, 26):
        out.append("".join(
            chr((ord(c) - base - shift) % 26 + base) if c.isalpha() else c
            for c in text.strip()
            for base in (ord('A') if c.isupper() else ord('a'),)
        ))
    return out


def _rail_fence_brute(text: str, max_rails: int = 6) -> list[str]:
    """栅栏密码定栏还原（W 型简化为之字形标准栅栏）。"""
    cipher = text.strip()
    if len(cipher) < 4 or not cipher.isalpha():
        return []
    results = []
    for rails in range(2, min(max_rails, len(cipher) - 1) + 1):
        # 计算每栏字符数
        pattern = list(range(rails)) + list(range(rails - 2, 0, -1))
        counts = [0] * rails
        for i in range(len(cipher)):
            counts[pattern[i % len(pattern)]] += 1
        # 切分密文并按栏重排
        rows: list[list[str]] = []
        idx = 0
        for count in counts:
            rows.append(list(cipher[idx: idx + count]))
            idx += count
        pos = [0] * rails
        plain = []
        for i in range(len(cipher)):
            rail = pattern[i % len(pattern)]
            plain.append(rows[rail][pos[rail]])
            pos[rail] += 1
        results.append("".join(plain))
    return results
