"""CTF 五方向专项子 Agent（设计书 4.4.1~4.4.5）：原生工具调用解题环。

框架只提供两样东西：**工具目录**与**结果流向**。
- 工具目录：各方向可调用的确定性动作（含通用 HTTP 探测通道 http_request，
  与文件/文本/编码分析工具），以原生 function calling 的 schema 交给模型；
  参数由 LLM 按当前证据现场构造，不按固定模板出牌。
- 结果流向：①网络请求走同一 http_probe 通道（范围标注/RPS/审计/审批
  边界不变）；②Flag 只由 FlagManager 确定性正则提取（LLM 声明不作数）；
  ③工具结果以 tool 消息回灌模型并进本路线观察序列与题级共享结论（同伴
  路线与主 Agent 复盘都能读到）；④已尝试入口登记进环形台账（同伴路线
  避让，不重复做功）。

解题方式是**一条持久对话**：首条消息装入题面、入口预感知与同伴结论，
完整系统提示只在第一次模型调用时注入；之后只续传题面、既有判断、工具证据
与新增动态，避免长系统提示在每轮重新压过现场推理。每轮模型决定调用哪些工具
（互不依赖的请求同轮并发），工具结果回灌，直到模型不再调用工具——那就是
它的结论。不设固定回合顶——收尾由模型自主决定，滥用由网关 token
预算与连续故障熔断兜底；已读取过的地址再次请求时直接回放
当时记录（重取与否由模型用 refresh 参数决定）；除此之外是否重复、
要不要换思路由模型自己判断（后端不做动作去重截断——归一化签名无法
真正判定两次调用意图相同）。LLM 不可用时
解题环诚实停摆留痕，不做固定模板兜底。

分支并行：路线预算由灵枢按任务复杂度裁定（1~8），K>1 时按题面提出
互不重叠的调查角度并行展开——分支共享现场（已探入口/笔记/Flag）
相互接力；任一分支夺旗会即时通报其余路线，是否收尾由模型依现场决定
（多 Flag 题还可继续深挖）；简单题自然落在单线。

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
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from spiritscout.agents.ring import attempt_key
from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, BlackboardLayer, Task
from spiritscout.ctf.flags import FlagManager
from spiritscout.gateway import world_state
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope
from spiritscout.tools.spec import ActionCategory

# 题面中疑似编码串的最小长度（降噪）
_MIN_BLOB_LEN = 12
# 外部安全技能库（NoorQureshi/security-skills，MIT，206 条）——
# 内存检索，kb_search 时按关键词匹配返回
_SEC_SKILLS: list[dict[str, Any]] | None = None

def _load_sec_skills() -> list[dict[str, Any]]:
    global _SEC_SKILLS
    if _SEC_SKILLS is None:
        f = Path(__file__).parent.parent / "knowledge" / "data" / "security_skills.json"
        try:
            import json as _json
            _SEC_SKILLS = _json.loads(f.read_text())
        except (OSError, ValueError):
            _SEC_SKILLS = []
    return _SEC_SKILLS
# 喂给 LLM 的单条观察摘录上限（控制 token 扇出）；页面观察是解题的第一手
# 情报，预算给足——链接/表单等接口清单与题面文案（可见文本）都要装得下
_OBS_EXCERPT_CAP = 4000
# 对话字符预算：超出后从最旧消息开始折叠工具结果（长对话的上下文治理）
_MSG_CHAR_BUDGET = 48000
# 单条工具结果回灌上限
_TOOL_RESULT_CAP = 6000
# 已读地址记录回放的文本上限（与观察摘录同量级，别在回放时又掐掉页面文案）
_FETCH_RECORD_CAP = 3600
# 沙箱输出回灌模型的上限（头尾保留；完整输出已落盘，不丢过程）
_SAND_MODEL_CAP = 3600
# 解题环单次 LLM 调用超时：重推理模型单次可达 3~4 分钟
_SOLVER_TIMEOUT = 420
# 单回合路线进度增量（进度展示用；无固定回合顶，进度饱和于 1.0）
_TURN_PROGRESS = 0.04
# HTTP 动作参数上限（上下文保护，非行为限制；测试边界由模型结合用户上下文维护）
_HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
# 语义上无副作用的读取方法（其余方法须模型自报 action 行为类别才执行）
_HTTP_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# HTML 结构探测：响应首段命中常见标签即按 HTML 摘要处理
_HTML_HINT = re.compile(
    r"<\s*(?:!doctype|html|head|body|div|span|a|p|form|table|script|style|title)\b",
    re.IGNORECASE)
# 观察保留的高信号响应头（跳转目标/会话/指纹/鉴权）
_OBS_HEADERS = ("location", "set-cookie", "server", "x-powered-by",
                "content-type", "www-authenticate", "x-flag", "allow")


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


_EMPTY_ASSISTANT_MARKER = re.compile(
    r"(?:empty message content|content is empty|message content sanitised|"
    r"空消息内容|提供的内容为空|内容为空.*协议)", re.IGNORECASE)


def _model_messages(messages: list[dict[str, Any]], *, initialized: bool) \
        -> list[dict[str, Any]]:
    """无状态端点每轮均保留系统约束，稳定前缀可由端点缓存复用。"""
    selected = messages
    # 发送视图与持久状态使用不同的容器：网关/后端即使对
    # message 字典增补字段，也不会改写缓存中的对话。
    view = [dict(message) for message in selected]
    for message in view:
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        content = str(message.get("content") or "").strip()
        if not content or _EMPTY_ASSISTANT_MARKER.search(content):
            names = [str((call.get("function") or {}).get("name") or "")
                     for call in message.get("tool_calls", [])
                     if isinstance(call, dict)]
            message["content"] = "已发起工具调用" + (
                "：" + "、".join(name for name in names if name)
                if any(names) else "。")
    return view


def _challenge_instance_blocker(status: Any, text: str) -> str:
    """识别比赛平台明确返回的实例生命周期错误。

    只接受状态码与平台原文的组合，不把普通业务 404 当作环境故障。该判断
    用于及时停止无效探测，保留证据并让用户回题目页重启实例。
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        return ""
    if code not in (404, 410, 502, 503, 504):
        return ""
    body = _collapse(text).lower()
    strong_markers = (
        "题目实例未找到",
        "题目实例不存在、已经销毁",
        "请从比赛题目页重新启动实例",
        "challenge-gateway / route-check",
    )
    if not any(marker in body for marker in strong_markers):
        return ""
    return (
        f"赛题实例不可用（平台网关 HTTP {code}）：实例不存在、已销毁或地址"
        "已失效。请从比赛题目页重新启动实例并提供新的入口地址。")


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
    # 段序即优先级：页面文案（可见文本）排在接口清单（链接/表单/脚本）之前
    # ——排在末尾时最容易被总帽截掉，而页面想让解题者读到的话（题面提示、
    # 使用说明）恰是第一手情报；文案过长时保头保尾（提示常在中后段）
    parts = [f"[HTML 摘要] 样式/脚本块已剔除（原文 {len(body)} 字符）"]
    if title:
        parts.append(f"[标题] {title}")
    if comments:
        parts.append("[注释] " + " ｜ ".join(c[:500] for c in comments[:5])[:1200])
    if text:
        visible = text[:1000]
        if len(text) > 1600:
            visible = (text[:1100]
                       + f"\n…[可见文本中段略去 {len(text) - 1600} 字符，末尾接下]…\n"
                       + text[-500:])
        parts.append("[可见文本] " + visible)
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
    if parsed.get("scope_note"):
        obs["scope_note"] = str(parsed["scope_note"])
    headers = {str(k).lower(): str(v)
               for k, v in (parsed.get("headers") or {}).items()}
    picked = {k: headers[k][:300] for k in _OBS_HEADERS if k in headers}
    if picked:
        obs["headers"] = picked
    body = res.stdout or ""
    ctype = headers.get("content-type", "")    # 脚本/样式/JSON 文本不走 HTML 摘要（否则会被当作标签剥离只剩碎片）
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


def _head_tail(text: str, cap: int) -> str:
    """超长输出的上下文摘要：头尾保留、中段略去（完整内容已落盘可查）。"""
    if len(text) <= cap:
        return text
    dropped = len(text) - cap
    return (text[:cap * 3 // 4]
            + f"\n…[中段略去 {dropped} 字符——完整输出已存工作区文件，"
              "需要时用沙箱读取]…\n"
            + text[-(cap // 4):])


@dataclass
class _ToolOutcome:
    """一次工具调用的结果：text 回灌模型，其余喂路线状态与前端轨迹。"""

    ok: bool
    text: str
    value: float = 0.0
    findings: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    observation: dict[str, Any] | None = None
    empty: bool = False        # 成功执行但无有效输出（喂空转感知，不做拦截）


def _fn(name: str, description: str, properties: dict[str, Any],
        required: list[str] | None = None) -> dict[str, Any]:
    """OpenAI 形态的函数工具 schema（网关统一转译到各协议）。"""
    if "action" in properties:
        properties = {**properties, "action": {
            **properties["action"], "enum": [a.value for a in ActionCategory],
        }}
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
    if obs.get("scope_note"):
        lines.append(str(obs["scope_note"]))
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
            "GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS 全部支持——表单提交、"
            "接口调用、登录动作都用它：method 指定方法，data 放请求体"
            "（表单串按 x-www-form-urlencoded 发，JSON 原文按 application/json "
            "发；要别的类型或精确控制时用 headers 显式给 Content-Type）。"
            "path 传站内相对路径（推荐），或 url 传完整地址（须在授权范围内）。"
            "正文较长被截断时，可用 headers 里的 Range"
            "（如 {\"Range\": \"bytes=5000-10000\"}）分段取完整内容。",
            {
                "method": {"type": "string", "enum": list(_HTTP_METHODS),
                           "description": "HTTP 方法，全部可用；缺省 GET，"
                                          "提交表单/调用接口时用 POST"},
                "path": {"type": "string",
                         "description": "站内路径，如 /api/v1/users"},
                "url": {"type": "string",
                        "description": "完整 URL（与 path 二选一）"},
                "params": {"type": "object", "description": "查询参数（键值对）"},
                "headers": {"type": "object", "description": "额外请求头（键值对）"},
                "data": {"type": "string", "description": "请求体原文（POST/PUT 用）"},
                "refresh": {"type": "boolean",
                            "description": "true=对该地址重新发起真实请求（页面"
                            "状态可能已变化时用）；缺省 false——已读取过的地址"
                            "直接回放当时的记录"},
                "action": {"type": "string",
                           "description": "本次请求的行为类别（如实自报）。"
                           "GET/HEAD/OPTIONS 纯读取可不填；POST/PUT/PATCH/"
                           "DELETE 这类有副作用的请求必填：analysis=只是"
                           "查询/读取；intrusive_write=写入/上传/增删改数据；"
                           "brute_force=爆破；webshell_upload=上传 WebShell。"
                           "危险类别（删除/爆破/反弹连接/WebShell/逃逸）会"
                           "即时请示用户；其余如实注明即可。"
                           "缺声明的副作用请求会被退回。"},
            }),
        _fn("shell",
            "在隔离沙箱里执行一条 shell 命令（bash -c），拿到输出与退出码。"
            "适合 HTTP 交互、socket 通信、文件检索与处理等命令行动作；"
            "具体命令以实际执行为准，不确定时先 `command -v <name>` 探测；"
            "工作目录在会话内持久（脚本与产物落那里，跨回合直接可用；"
            "/tmp 每次运行即焚），系统文件只读，联网仅限授权目标。命令全文"
            "留痕于审计。每次调用"
            "必须用 action 如实声明这条命令要做什么——审计与审批以它为准。",
            {
                "command": {"type": "string", "description": "要执行的命令"},
                "action": {"type": "string",
                           "description": "本次执行的行为类别（如实自报，缺了"
                           "不执行）：analysis=只读分析/读取/计算/解码；"
                           "delete_files=删除文件；reverse_shell=反弹 shell；"
                           "webshell_upload=上传 WebShell；container_escape="
                           "容器/沙箱逃逸尝试；brute_force=爆破；"
                           "intrusive_write=其他有副作用的写入/上传。删除/"
                           "爆破/反弹连接/WebShell/逃逸会即时请示用户，每次"
                           "单独批准；其余如实注明即可。"},
                "timeout": {"type": "number",
                            "description": "超时秒数（缺省 60，上限 300）"},
            }, required=["command", "action"]),
        _fn("python",
            "在隔离沙箱里执行一段 Python 代码（python3），拿到输出与退出码。"
            "适合 WebSocket/socket 协议交互、数据计算与解码、脚本化构造请求；"
            "具体有哪些库以实际导入结果为准——不确定时先 `python3 -c "
            "'import <模块>'` 或 `pip list | grep -i <关键词>` 查看，"
            "不要凭记忆假设。"
            "工作目录会话内持久，产物跨回合直接可用（/tmp 每次运行即焚）；"
            "产物也可用附件分析工具继续研究。每次调用必须用 action 如实"
            "声明这段代码要做什么——审计与审批以它为准。",
            {
                "code": {"type": "string", "description": "完整 Python 代码"},
                "action": {"type": "string",
                           "description": "本次执行的行为类别（如实自报，缺了"
                           "不执行）：analysis=只读分析/读取/计算/解码；"
                           "delete_files=删除文件；reverse_shell=反弹 shell；"
                           "webshell_upload=上传 WebShell；container_escape="
                           "容器/沙箱逃逸尝试；brute_force=爆破；"
                           "intrusive_write=其他有副作用的写入/上传。删除/"
                           "爆破/反弹连接/WebShell/逃逸会即时请示用户，每次"
                           "单独批准；其余如实注明即可。"},
                "timeout": {"type": "number",
                            "description": "超时秒数（缺省 60，上限 300）"},
            }, required=["code", "action"]),
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
        _fn("asset_memory_lookup", "读取同一主机的跨会话历史观测；历史不代表当前事实，不含凭据或授权。",
            {"host": {"type": "string"}, "query": {"type": "string"}, "offset":{"type":"integer","minimum":0}}, required=["host"]),
        _fn("kb_search",
            "检索本地经验知识库：敏感路径/备份文件清单、PHP 弱类型等平台特性"
            "事实、模板注入 canary、隐写与提权检查点、攻击路径模板与核验方法。"
            "遇到不熟悉的平台特性或想核对常见检查点时用它；它是参考资料，"
            "采纳与否由你基于现场证据判断。",
            {"query": {"type": "string",
                       "description": "检索词，如：备份文件 / PHP 弱类型 / "
                                      "隐写 / 提权 / 模板注入"}},
            required=["query"]),
        _fn("ask_user",
            "向用户提问并等待答复。遇到授权范围外的目标、拿不准的路线分叉、"
            "需要用户拍板的取舍时用它：问题用你自己的话写清「看到了什么、"
            "为什么想继续、不同选择意味着什么」；options 给 2~4 个你建议的"
            "选项（用户也可以自由输入其他意见）；targets 填你希望纳入测试"
            "范围的目标，形如 [{\"target\": \"主机或 URL\", \"why\": \"一句话"
            "说明它是什么、为什么需要（面向普通用户）\"}]——用户勾选后系统才"
            "真正放行；只列授权范围外的新目标，不要罗列已在范围内的。一次性把"
            "问题问清楚，不要反复追问。",
            {"question": {"type": "string",
                          "description": "要问用户的问题（自己组织语言）"},
             "options": {"type": "array", "items": {"type": "string"},
                         "description": "建议选项（2~4 个，用户可自由输入）"},
             "multiple": {"type": "boolean",
                          "description": ("选项可多选：仅当选项彼此不互斥、"
                                          "用户可能同时选多项时设 true"
                                          "（如「接下来优先看哪些方向」"
                                          "「需要开通哪些账号/环境」）；"
                                          "「允许/拒绝」这类互斥决策保持 "
                                          "false。多选选项要写成可独立"
                                          "成立的动作或结论")},
             "targets": {"type": "array",
                         "items": {"type": "object",
                                   "properties": {
                                       "target": {"type": "string",
                                                  "description": "主机或 URL"},
                                       "why": {"type": "string",
                                               "description": "面向普通用户的一句话说明"}},
                                   "required": ["target"]},
                         "description": "希望纳入本会话范围的新目标（含一句话说明）"}},
            required=["question"]),
        _fn("submit_flag",
            "把你判定为本题 Flag 的值提交（判定权在你：依据题面给定的格式与"
            "你拿到的证据）。后端只做登记、去重与赛方接口提交，应答（对/错/"
            "未判定）会回灌给你；同一个值不要重复提交。",
            {"flag": {"type": "string",
                      "description": "Flag 原文（照抄证据里的值，不要加工）"},
             "note": {"type": "string",
                      "description": "判定依据（一句话：来源与推导）"}},
            required=["flag"]),
        _fn("spawn",
            "派生一个分支执行者去独立推进一个子问题，其最终结论会回传给你。"
            "brief 写清：为什么需要这个分支、它专属的任务是什么、解决什么"
            "问题、与当前工作的边界（它不做什么）；expect 写清期望回传的"
            "结论。各司其职：子问题值得独立推进时才派，顺手能做的不要派。",
            {"brief": {"type": "string",
                       "description": "分支任务简报（为什么派/做什么/边界）"},
             "expect": {"type": "string",
                        "description": "期望回传的结论"}},
            required=["brief"]),
    ]
    # 解题路线的安全顶是回合上限与重复空转熔断；健康度剪枝面向多路线资源
    # 再分配场景，逐轮探索的起步进展分天然低于阈值，会误杀在途调查——豁免
    health_prune_enabled: ClassVar[bool] = False

    def __init__(self, ctx, *, flag_manager: FlagManager | None = None, **kw):
        super().__init__(ctx, **kw)
        self.flag_manager = flag_manager
        self._merge_mcp_schemas()

    @staticmethod
    def _mcp_tool_schema(prefix: str, entry: dict[str, Any]) -> dict[str, Any]:
        """单个 MCP 工具的模型 schema（原样透传服务器的 inputSchema）。"""
        schema = entry.get("schema") or {}
        return {
            "type": "function",
            "function": {
                "name": prefix,
                "description": (f"[MCP:{entry.get('server')}] "
                                f"{schema.get('description') or entry.get('tool')}"),
                "parameters": schema.get("inputSchema") or {
                    "type": "object", "properties": {}},
            },
        }

    def _merge_mcp_schemas(self) -> None:
        """活目录对账：把尚未并入的 MCP 工具补进本实例工具清单。

        MCP 服务器在会话基建里后台续握、就绪一台注册一台——已运行的
        Agent 下回合对账即可见（类级目录不动，实例副本追加）。
        """
        mcp = getattr(self.ctx, "mcp_tools", None) or {}
        have = {s["function"]["name"] for s in self.tool_schemas}
        fresh = [self._mcp_tool_schema(p, e) for p, e in mcp.items() if p not in have]
        if fresh:
            self.tool_schemas = [*self.tool_schemas, *fresh]

    # ------------------------------------------------------------ 赛题载荷

    @staticmethod
    def challenge_of(task: Task) -> dict[str, Any]:
        # 载荷可能显式为 null（如复盘新增节点未绑定赛题）——一律归零为 {}
        return task.payload.get("challenge") or {}

    def challenge_id(self, task: Task) -> str:
        return str(self.challenge_of(task).get("id", task.task_id))

    def _tool_ctx(self, task: Task) -> dict[str, Any]:
        """本题工具调用上下文：沙箱工作目录按赛题隔离。

        多题并行（或同题多分支重攻）时，各题的脚本、产物与上次输出日志
        落 ``sandbox/<赛题 id>/``，互不覆盖；无赛题节点回落任务 id。
        SRC 等非赛题链路不经这里，维持会话级工作目录。
        """
        ctx = getattr(self.ctx, "builtin_ctx", None) or {}
        return {**ctx, "workspace_scope": self.challenge_id(task)}

    # ------------------------------------------------------------ Flag 闭环

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
        # 下发决策与执行并行（不挡开工），首轮 plan 常在裁定落地前运行：
        # 未裁定时默认单分支起步（CTF 解题是串行推进，并行分支做重复功；
        # 灵枢裁定 k>1 时才并行，由模型提出互不重叠的角度），
        # 裁定后的轮次按预算由 LLM 提出互不重叠的角度。
        decided = task.max_routes is not None
        k = max(1, min(int(task.max_routes if decided else 1), self.max_routes))
        plans: list[RoutePlan] = []
        if k > 1 and decided:
            angles = await self._llm_propose_angles(task, k)
            plans = [RoutePlan(name=a["name"], strategy=a["angle"],
                               steps=[{"action": "investigate"}])
                     for a in angles]
        if not plans:
            plans = [RoutePlan(
                name="solve",
                strategy="自主解题：从题面与目标出发自行决定方向",
                steps=[{"action": "investigate"}])]
            if k > 1:
                # 角度提案不可用时维持单线语义的第二条路线：不注入任何
                # 方法论提示，解题思路由 Agent 自己的思维链构造
                plans.append(RoutePlan(
                    name="solve_alt", strategy="",
                    steps=[{"action": "investigate"}]))
        plans = plans[:k]
        if self.ctx.knowledge is not None:
            self._attach_kb_guidance(task, plans)
        return plans

    async def _llm_propose_angles(self, task: Task, k: int) -> list[dict[str, str]]:
        """LLM 按题面提出 k 个互不重叠的调查角度（仅在多角度并发时使用）。

        只约束输出形态与分工程度，不引导解题方法——思路由各路线自己构造。
        重攻轮携带已探现场（入口/笔记/结论），提案模型据此才能真正分工：
        冷题面只会得出「信息不足给 1 条」，并发预算就名存实亡了。
        """
        ch = self.challenge_of(task)
        site = self._shared_site(task)
        self._merge_mcp_schemas()   # 后台就绪的 MCP 工具对角度提案同样可见
        data = await self._llm_chat_json("route_planner", {
            "challenge": _challenge_view(ch),
            "attachments": [str(a)[:300] for a in ch.get("attachments", [])[:10]],
            "slots": k,
            "tools": [t["function"]["name"] for t in self.tool_schemas],
            "known": {
                "已探入口": [f"{u} → {v}" for u, v in list(site.items())[-20:]],
                "共享笔记": self._shared_notes(task)[-8:],
                "已有结论": self._shared_conclusions(task)[-6:],
            },
            "constraints": (
                "返回 JSON：{\"angles\": [{\"name\": 英文短名(小写字母数字下划线), "
                "\"angle\": 一句话调查角度}]}。主 Agent 已为本题裁定 "
                f"{k} 路并发预算，各路线独立拿到完整工具集并行推进：角度必须"
                "针对本题的具体信息（题面/附件/目标特征/已探现场），互相不"
                "重叠，已知信息足以区分时给出 slots 条；确实不足以区分时"
                "才减少条数。"),
        }, max_user_chars=12000, timeout_seconds=_SOLVER_TIMEOUT,
            reasoning_effort=self.task_effort(task), task=task)
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
            state = {"messages": [], "seed": [], "observations": [],
                     "flags": [], "concluded": False, "stall": "",
                     "turn": 0, "llm_failures": 0, "empty_streak": 0,
                     "system_initialized": False, "fetches": {}}
            self.cache.put(self.route_namespace, key, state)
        else:
            # 兼容升级前已经落盘的活跃会话。只有出现过助手应答，才说明首轮
            # 初始化确实成功；仅有 seed/system/user 的失败会话仍需完成初始化。
            state.setdefault("system_initialized", any(
                message.get("role") == "assistant"
                for message in state.get("messages", [])
                if isinstance(message, dict)))
        # 升级兼容：旧会话状态缺新字段时补齐（记录回放 / 空转感知 / 空答修复）
        state.setdefault("fetches", {})
        state.setdefault("empty_streak", 0)
        state.setdefault("empty_replies", 0)
        return state

    def _save_inv_state(self, task: Task, route, state: dict[str, Any]) -> None:
        self.cache.put(self.route_namespace,
                       f"inv|{self.challenge_id(task)}|{route.name}|{task.task_id}",
                       state)
        journal = getattr(self.ctx, "journal", None)
        if journal is not None:
            journal.checkpoint("ctf_conversation", f"{task.task_id}:{route.route_id}",
                               "turn_saved", state)

    def _shared_conclusions(self, task: Task) -> list[str]:
        """题级共享结论：各路线收尾结论（同伴路线与复盘都能读到）。"""
        got = self.cache.get(self.shared_namespace,
                             f"shared|{self.challenge_id(task)}")
        return got if isinstance(got, list) else []

    def _record_conclusion(self, task: Task, route, summary: str) -> None:
        key = f"shared|{self.challenge_id(task)}"
        got = self.cache.get(self.shared_namespace, key)
        conclusions = got if isinstance(got, list) else []
        conclusions.append(f"[{self.direction}/{route.name}] {summary}"[:1000])
        self.cache.put(self.shared_namespace, key, conclusions[-12:])

    def _record_finding(self, task: Task, kind: str, text: str,
                        url: str = "", route: str = "") -> None:
        """发现台账：关键信息同步落黑板（拓扑「发现」组的数据源）。

        共享缓存服务并行同伴的即时接力，随会话消亡；黑板记录进入会话
        数据面——拓扑图的发现组与报告都从这里取，跨轮持久、不占任何
        Agent 的上下文。dedup_key 保证同一发现只存一条。agent/route 是
        产出者标注：拓扑据此画「路线 → 其发现」的出处连线，不凭空造边。
        """
        ch = self.challenge_of(task)
        self.ctx.blackboard.write(
            BlackboardLayer.TOPOLOGY, new_id("finding"),
            {"kind": "finding", "type": kind, "text": str(text)[:400],
             "url": str(url)[:300], "by": self.direction,
             "agent": self.agent_type.value, "route": str(route)[:40],
             "challenge": str(ch.get("id") or "")[:80],
             "round": int(getattr(task, "round_no", 0) or 0),
             "ts": time.time()},
            agent_type=self.agent_type, updated_by=self.agent_id,
            # 摘要用 crc32 而非内建 hash()：hash() 带进程随机盐，重启后
            # 同一发现会算出不同键 → 去重失效、发现组被重复记录刷满
            dedup_key=(f"finding|{self.challenge_id(task)}|{kind}|"
                       f"{zlib.crc32((str(url) if url else str(text)[:200]).encode()):08x}"))

    # ------------------------------------------------------------ 环形共享缓存
    # 同题的所有方向 Agent（并行工作）共用一份现场：已探入口、共享笔记、
    # 已夺 Flag。任何一方写入，其余方下一回合即见——不重复做功、相互接力。

    def _shared_site(self, task: Task) -> dict[str, Any]:
        got = self.cache.get(self.shared_namespace,
                             f"site|{self.challenge_id(task)}")
        return got if isinstance(got, dict) else {}

    def _record_site(self, task: Task, url: str, line: str,
                     route: str = "") -> None:
        key = f"site|{self.challenge_id(task)}"
        site = self._shared_site(task)
        site[url[:200]] = f"{self.direction}: {line}"[:200]
        if len(site) > 60:                      # 保序裁剪：丢最旧的
            for old in list(site)[:len(site) - 60]:
                site.pop(old, None)
        self.cache.put(self.shared_namespace, key, site)
        self._record_finding(task, "asset", f"{url} {line}", url=url, route=route)

    def _shared_notes(self, task: Task) -> list[str]:
        got = self.cache.get(self.shared_namespace,
                             f"notes|{self.challenge_id(task)}")
        return got if isinstance(got, list) else []

    def _record_note(self, task: Task, text: str, route: str = "") -> None:
        key = f"notes|{self.challenge_id(task)}"
        entry = f"[{self.direction}] {text}"[:400]
        notes = self._shared_notes(task)
        if entry not in notes:          # 同伴/自己已写过同一条，不重复登记
            notes.append(entry)
            self.cache.put(self.shared_namespace, key, notes[-20:])
            # 笔记同时落黑板拓扑层：共享缓存随会话消亡，黑板是跨轮持久的
            # 集体记忆——拓扑「发现」组、复盘与报告都从这里取
            self._record_finding(task, "note", text, route=route)

    def _shared_flags(self, task: Task) -> list[dict[str, str]]:
        got = self.cache.get(self.shared_namespace,
                             f"flags|{self.challenge_id(task)}")
        return [f for f in got if isinstance(f, dict)] if isinstance(got, list) else []

    @staticmethod
    def _flag_verdict(f: dict) -> str:
        """共享 Flag 记录的裁决态：accepted / rejected / manual / pending。"""
        return str(f.get("verdict") or "pending")

    @classmethod
    def _flag_line(cls, f: dict) -> str:
        """Flag 共享记录 → 带裁决态的一行展示（同伴可见、裁决变化再可见）。"""
        tag = {"accepted": "平台判定正确",
               "rejected": "平台判定错误（勿重复提交）",
               "manual": "无自动判定通道，待人工确认"}.get(
            cls._flag_verdict(f), "候选（未判定）")
        return f"{f.get('value', '')}（{tag}）"

    def _record_flag(self, task: Task, value: str, verdict: str = "pending",
                     route: str = "") -> None:
        """登记 Flag 候选并维护裁决态：首次登记 pending，平台应答回填。

        同值重复登记只推进裁决态（触发者/轮次不变）——提交结果要传给
        并行同伴：accepted 才是收敛信号，rejected 防止重复试错。
        """
        key = f"flags|{self.challenge_id(task)}"
        flags = self._shared_flags(task)
        for f in flags:
            if f.get("value") == value:
                if self._flag_verdict(f) != verdict:
                    f["verdict"] = verdict
                    self.cache.put(self.shared_namespace, key, flags)
                return
        flags.append({"value": value, "by": self.direction,
                      "route": str(route)[:40],
                      "round": int(getattr(task, "round_no", 0) or 0),
                      "verdict": verdict})
        self.cache.put(self.shared_namespace, key, flags)
        self._record_finding(task, "flag", value, route=route)

    def _peer_flags(self, task: Task, state: dict[str, Any], *,
                    same_round: bool = False) -> list[dict]:
        """同伴（其他方向）已捕获、且本路线尚未持有的 Flag 记录（带裁决态）。

        same_round=True 只认本轮并行的同伴——上一轮的候选可能未经判定，
        不足以让新方向直接收工（信息照常可见，只是不当作终止信号）。
        """
        mine = set(state.get("flags") or [])
        rnd = int(getattr(task, "round_no", 0) or 0)
        out: list[dict] = []
        for f in self._shared_flags(task):
            if f.get("by") == self.direction or f.get("value") in mine:
                continue
            if same_round and int(f.get("round", 0) or 0) != rnd:
                continue
            out.append(f)
        return out

    def _peer_accepted_flags(self, task: Task,
                             state: dict[str, Any]) -> list[str]:
        """本轮并行同伴（不含本方向自己）获平台判定正确的 Flag 值。

        识别口径此前不看提交者：本路线刚提交成功的 Flag 下一回合会被当成
        「同伴夺旗」通报（文本谎报出处、多 Flag 题失去再挖机会）。归属
        只能看提交者方向 by——route 名会撞：各方向路线默认全部叫 solve，
        拿名字比会把同伴战果统统滤掉（通报从此静默）。
        """
        rnd = int(getattr(task, "round_no", 0) or 0)
        mine = set(state.get("flags") or [])
        return [f["value"] for f in self._shared_flags(task)
                if f.get("by") != self.direction
                and int(f.get("round", 0) or 0) == rnd
                and f["value"] not in mine
                and self._flag_verdict(f) == "accepted"]

    def _peer_win_notice(self, task: Task, route,
                         state: dict[str, Any]) -> str:
        """同伴夺旗的事实通报（每枚 Flag 只通报一次；收不收工由模型定）。

        收敛不再由后端下令：把已获平台判定正确的同伴 Flag 原样交给模型，
        由它结合现场决定继续（多 Flag 题/未竟面）还是收尾。
        """
        seen = state.setdefault("peer_win_seen", [])
        fresh = [v for v in self._peer_accepted_flags(task, state)
                 if v not in seen]
        if not fresh:
            return ""
        seen.extend(fresh)
        self.ctx.audit.log("agent.peer_win", self.agent_id, {
            "route": route.name, "challenge": self.challenge_id(task),
            "turn": state["turn"], "flags": fresh[:4]})
        return ("## 同伴夺旗（事实通报）\n"
                "本轮并行同伴的这些 Flag 已获平台判定正确："
                + ", ".join(fresh[:4]) + "。\n"
                "这通常意味着该攻击面已被他路拿下——是否收尾由你决定："
                "本题若已无未竟面（或多 Flag 题已无别的目标），直接给出"
                "收尾结论；若还有值得继续的攻击面，说明理由后继续。")

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
        # 笔记/结论按内容水位去重：共享列表有存储上限（写满后 len 不再
        # 增长），按长度记账会让上限之外的新条目永久静默
        notes = self._shared_notes(task)
        seen_notes = seen.get("notes_seen")
        if not isinstance(seen_notes, list):
            # 旧检查点形态（按长度记账）：以旧水位为基线迁移一次，
            # 已汇报过的旧条目不被当新动态重播
            legacy = seen.get("notes")
            legacy = legacy if isinstance(legacy, int) and legacy > 0 else 0
            seen_notes = list(notes[:legacy])
        fresh_notes = [n for n in notes if n not in seen_notes]
        seen["notes_seen"] = (seen_notes + fresh_notes)[-64:]
        for note in [n for n in fresh_notes
                     if not n.startswith(f"[{self.direction}]")][-8:]:
            lines.append(f"· 同伴笔记 {note}")
        concl = self._shared_conclusions(task)
        seen_concl = seen.get("concl_seen")
        if not isinstance(seen_concl, list):
            legacy = seen.get("concl")
            legacy = legacy if isinstance(legacy, int) and legacy > 0 else 0
            seen_concl = list(concl[:legacy])
        fresh_concl = [c for c in concl if c not in seen_concl]
        seen["concl_seen"] = (seen_concl + fresh_concl)[-48:]
        for c in [c for c in fresh_concl
                  if not c.startswith(f"[{self.direction}/")][-4:]:
            lines.append(f"· 同伴结论 {c}")
        fresh = [f for f in self._peer_flags(task, state)
                 if self._flag_line(f) not in seen["flags"]]
        if fresh:
            # 按裁决态展示：候选只是线索，判对是捷报，判错是禁提交清单
            lines.append("· 同伴 Flag 动态：" + ", ".join(
                self._flag_line(f) for f in fresh))
            seen["flags"].extend(self._flag_line(f) for f in fresh)
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
        token = current_execution_scope.set(ExecutionScope(
            task_id=task.task_id, route_id=route.route_id, agent_id=self.agent_id))
        try:
            return await self._solve_turn_inner(task, route, plan)
        finally:
            current_execution_scope.reset(token)

    async def _solve_turn_inner(self, task: Task, route, plan: RoutePlan) -> StepResult:
        """解题环单回合：一次原生工具调用回合（可并发多个工具）。

        边界（与全系统同一原则）：LLM 只决定「试什么」，执行走同一
        http_probe 通道（范围标注/RPS/审计/审批不变）；Flag 只由
        模型判定后经 FlagManager 提交；LLM 不可用 → 本路线诚实停摆（无模板兜底）。
        """
        state = self._inv_state(task, route)
        if state["concluded"] or state["stall"]:
            return StepResult(ok=True, value=0.0,
                              final_note=True,
                              note=state["stall"] or "本路线已收尾")
        # 并行同伴夺旗 → 事实通报（不再由后端下令收尾）：本轮并行同伴的
        # Flag 获平台判定正确，每枚只通报一次；继续深挖（多 Flag 题/未竟面）
        # 还是收尾，由模型依现场决定。候选/未判定不算战果；判错的进提示词
        # 当禁提交清单。
        win_notice = self._peer_win_notice(task, route, state)

        boot_value, boot_note = 0.0, ""
        if not state["messages"]:
            try:
                boot_value, boot_note = await self._inv_bootstrap(task, route, state)
            except Exception as exc:  # noqa: BLE001 —— 首轮感知失败不终结调查：
                # 把障碍原样交给模型（见过现场才知道怎么绕：换入口/换工具/先读题面）
                state["seed"].append(
                    f"首轮感知失败: {type(exc).__name__}: {str(exc)[:200]}"
                    "——请换可行的入口或工具继续调查")
                boot_note = f"首轮感知失败: {str(exc)[:120]}"
            if state["stall"]:
                summary = str(state["stall"])
                self._record_conclusion(task, route, summary)
                self.ctx.audit.log("agent.blocked", self.agent_id, {
                    "route": route.name, "challenge": self.challenge_id(task),
                    "reason": summary})
                self._save_inv_state(task, route, state)
                return StepResult(
                    ok=True, value=boot_value, final_note=True,
                    note=self._merge_note(boot_note, summary))
            self._seed_messages(task, route, plan, state)
            if win_notice:
                state["messages"].append({"role": "user", "content": win_notice})
        else:
            if win_notice:
                state["messages"].append({"role": "user", "content": win_notice})
            if (update := self._peer_updates(task, state)):
                # 同伴动态作为新消息注入：并行 Agent 的现场即时互通
                state["messages"].append({"role": "user", "content": update})
            steer = (self.ctx.ring.steering_for(self.agent_type.value)
                     if self.ctx.ring is not None else None)
            if steer and float(steer.get("ts") or 0) > float(state.get("steer_seen") or 0):
                # 灵枢督导（含用户会话中发来的意见）注入活跃解题对话；
                # 一条建议只注入一次（ts 水位），采纳与否由模型自行权衡
                state["messages"].append({"role": "user", "content":
                    f"灵枢督导（全局方向建议，结合现场自行权衡）：{steer['hint']}"})
                state["steer_seen"] = steer["ts"]

        fragment, changed = self._sync_world_state(state)
        if fragment:
            state["messages"].append({"role": "user", "content": fragment})
            self.ctx.audit.log("world_state.update", self.agent_id, {
                "route": route.name, "keys": changed})
        state["turn"] += 1
        turn = state["turn"]
        include_initial_system = not bool(state.get("system_initialized"))
        self._compact_messages(
            state["messages"], initialized=not include_initial_system)
        messages: list[dict[str, Any]] = _model_messages(
            state["messages"], initialized=not include_initial_system)
        emitter = _DeltaEmitter(self.ctx.audit, self.agent_id, route=route.name,
                                challenge=self.challenge_id(task), turn=turn)
        self.ctx.audit.log("agent.turn", self.agent_id, {
            "route": route.name, "challenge": self.challenge_id(task),
            "turn": turn,
            "initial_system_prompt": include_initial_system,
            "angle": plan.strategy[:160] if turn == 1 else ""})
        try:
            self._merge_mcp_schemas()   # 后台就绪的 MCP 工具本回合起可见
            resp = await self.ctx.gateway.chat(
                messages, task_type="execution",
                # 不传 max_tokens：由网关按模型档案的真实上限执行（如 128K）。
                # 此前硬编码 4096 时思考型模型把额度耗在思考上正文为空
                # （收尾空结论），长脚本参数中途截断引发协议层报错连坐停摆。
                timeout_seconds=_SOLVER_TIMEOUT,
                tools=self.tool_schemas, tool_choice="auto",
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
        state["system_initialized"] = True

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
            if not summary:
                # 协议修复：正文为空且无工具调用，多见于思考型模型把输出
                # 额度耗在思考段——空回合不是结论，注入提示再给一次机会；
                # 连续两次仍空才按无结论收尾（对话现场已保留）
                state["empty_replies"] = int(state.get("empty_replies") or 0) + 1
                if state["empty_replies"] < 2:
                    state["messages"].append({"role": "user", "content": (
                        "（上一回合没有收到你的输出：既没有工具调用，也没有正文。）"
                        "请继续调查；若你已判定调查结束，请写出明确结论——"
                        "拿到了什么、依据是什么、还差什么。")})
                    note = self._extend_or_finish(
                        plan, state, f"第 {turn} 回合：模型空输出，已请其重述")
                    self._save_inv_state(task, route, state)
                    return StepResult(ok=True, value=boot_value,
                                      note=self._merge_note(boot_note, note))
            else:
                state["empty_replies"] = 0
            state["conclusion_text"] = summary[:2000]
            state["concluded"] = True
            if not state["conclusion_text"].strip():
                state["conclusion_text"] = "（模型未输出结论文本，最后动作为工具调用）"
            self._record_conclusion(task, route, summary or "模型未给出结论文本")
            self.ctx.audit.log("agent.conclude", self.agent_id, {
                "route": route.name, "challenge": self.challenge_id(task),
                "turn": turn, "summary": summary[:2000]})
            # 结论已随上面的 agent.conclude 单独上时间线：置位让任务级
            # 收尾（base._run_task）不再把同一段文本拼进汇总结论重复展示
            route.conclusion_published = True
            # 结论文本（最终答案与依据，常含 Markdown 表格）走长限额，
            # WriteUp 的「解题依据」段不再半途截断
            note = f"收尾（第 {turn} 回合）：{summary[:1500] or '无结论文本'}"
            self._save_inv_state(task, route, state)
            return StepResult(ok=True, value=boot_value, final_note=True,
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
        empty_streak = int(state.get("empty_streak") or 0)
        for idx, (call, out) in enumerate(zip(calls, outcomes)):
            text = out.text or "（无输出）"
            if idx == 0 and out.ok and empty_streak >= 3:
                # 感知标注（非规则）：连续空输出是「方法可能不成立」的事实信号，
                # 是否换路由模型自行判断——后端不截断不强制
                text += (f"\n（感知：本路线已连续 {empty_streak} 次工具调用"
                         "输出为空——若当前方法可能不成立，把已排除的假设用 "
                         "note 记下后换条路；是否调整由你判断）")
            state["messages"].append({
                "role": "tool", "tool_call_id": call.get("id") or "call_0",
                "content": text[:_TOOL_RESULT_CAP]})
            total_value += out.value
            findings.extend(out.findings)
            if out.note:
                notes.append(out.note)
            self.ctx.audit.log("agent.tool_result", self.agent_id, {
                "route": route.name, "challenge": self.challenge_id(task),
                "id": call.get("id"), "name": call.get("name"), "ok": out.ok,
                "note": out.note[:200], "result": (out.text or "")[:1200]})
        state["empty_streak"] = (empty_streak + 1) if outcomes and all(
            o.ok and o.empty for o in outcomes) else 0
        head = f"第 {turn} 回合"
        note = f"{head}：" + "；".join(notes) if notes else head
        note = self._extend_or_finish(plan, state, note)
        self._save_inv_state(task, route, state)
        return StepResult(
            ok=True, value=min(boot_value + total_value, 1.0),
            progress_delta=_TURN_PROGRESS,
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
        allowed_tools = {
            str(schema.get("function", {}).get("name") or "")
            for schema in self.tool_schemas
        }
        if name not in allowed_tools:
            return _ToolOutcome(
                ok=False,
                text=f"工具 {name} 不在 {self.agent_type.value} 的工具目录内。",
            )
        if name == "note":
            text = str(args.get("text", "") or "").strip()
            if not text:
                return _ToolOutcome(ok=False, text="note 需要 text 参数。")
            self._record_note(task, text, route=route.name)
            self.ctx.audit.log("agent.note", self.agent_id, {
                "route": route.name, "challenge": self.challenge_id(task),
                "text": text[:400]})
            return _ToolOutcome(ok=True, value=0.05, note=f"共享笔记：{text[:60]}",
                                text="已写入团队共享笔记并落黑板（跨轮可见），"
                                     "同伴下一回合即可看到。")
        try:
            if name == "http_request":
                out = await self._tool_http(task, route, state, args)
            elif name in ("shell", "python"):
                out = await self._tool_sandbox(task, route, state, name, args)
            elif name == "analyze_text":
                out = await self._tool_analyze_text(task)
            elif name == "analyze_attachments":
                out = await self._tool_analyze_attachments(task)
            elif name == "asset_memory_lookup":
                host = str(args.get("host", "")).strip().lower()
                resources = getattr(self.ctx.knowledge, "resources", None)
                if not host or resources is None:
                    raise ValueError("需要主机名，且共享资产库必须已装配")
                page = resources.assets(query=str(args.get("query", "")),host=host,offset=max(0,int(args.get("offset",0))),limit=20)
                out = _ToolOutcome(ok=True, value=0, note="已读取历史资产，仍需按任务复核",
                    text=json.dumps({**page,"notice":"历史参考，不代表本次已验证；旧授权和凭据不会复用"},ensure_ascii=False))
            elif name == "kb_search":
                out = await self._tool_kb_search(args)
            elif name == "ask_user":
                out = await self._tool_ask_user(task, route, state, args)
            elif name == "submit_flag":
                out = await self._tool_submit_flag(task, route, state, args)
            elif name == "spawn":
                out = await self._tool_spawn(task, route, state, args)
            elif name == "probe_paths":
                out = await self._tool_probe_paths(task, route, state, args)
            elif name.startswith("mcp__"):
                out = await self._tool_mcp(task, name, args)
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

    # ------------------------------------------------------------ 知识库 / 路径探测 / MCP

    _PROBE_PATHS: ClassVar[tuple[str, ...]] = (
        "/robots.txt", "/sitemap.xml", "/.git/HEAD", "/.svn/entries", "/.env",
        "/.env.bak", "/config.php.bak", "/backup.zip", "/www.zip", "/web.rar",
        "/backup.tar.gz", "/index.php.bak", "/.DS_Store", "/composer.json",
        "/package.json", "/phpinfo.php", "/info.php", "/test.php", "/debug",
        "/console", "/actuator", "/swagger", "/api-docs", "/.well-known/",
        "/server-status", "/flag.txt", "/flag", "/readme.txt", "/README.md",
    )

    async def _tool_probe_paths(self, task: Task, route, state,
                                args: dict[str, Any]) -> _ToolOutcome:
        """敏感路径批量探测：逐条复用 http_probe 通道（范围标注/限速/审计不变）。"""
        ch = self.challenge_of(task)
        base = str(ch.get("url") or task.target or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            return _ToolOutcome(ok=False, text="probe_paths 需要题面有 HTTP 入口。")
        raw = args.get("paths") if isinstance(args.get("paths"), list) else []
        paths = [str(p) for p in raw if str(p).startswith("/")] \
            or list(self._PROBE_PATHS)
        paths = paths[:40]
        hits: list[str] = []
        failed = 0
        for p in paths:
            res = await self.ctx.tools.call(
                "http_probe", {"url": base + p}, **self._tool_ctx(task))
            if not res.ok:
                failed += 1
                continue
            parsed = res.parsed if isinstance(res.parsed, dict) else {}
            code = parsed.get("status_code", "?")
            if code in (404, "404"):
                continue
            title = str(parsed.get("title", "")).strip()[:40]
            size = parsed.get("content_length", len(res.stdout or ""))
            hits.append(f"{p} → {code} len={size}" + (f" title={title}" if title else ""))
        if not hits:
            return _ToolOutcome(ok=True, value=0.05,
                                note=f"敏感路径探测 {len(paths)} 条未命中",
                                text=(f"探测了 {len(paths)} 条常见敏感路径"
                                      f"（{failed} 条请求失败），其余 404——清单之外，"
                                      "回到页面功能本身找线索。"))
        body = "命中:\n" + "\n".join(hits[:20]) + f"\n（共 {len(hits)}/{len(paths)} 命中）"
        return _ToolOutcome(ok=True, value=min(0.2 + 0.15 * len(hits), 0.9),
                            findings=[],
                            note=f"敏感路径探测命中 {len(hits)}/{len(paths)}",
                            text=body[:_TOOL_RESULT_CAP])

    async def _tool_submit_flag(self, task: Task, route, state: dict[str, Any],
                                args: dict[str, Any]) -> _ToolOutcome:
        """模型判定的 Flag 提交：登记（去重）→ 赛方接口 → 应答回灌。

        判定权在模型——后端不做内容正则筛选；格式匹配只作登记信息。
        提交成功即写入共享缓存，并行分支据此收敛。
        """
        value = str(args.get("flag", "") or "").strip()
        if not value:
            return _ToolOutcome(ok=False, text="submit_flag 需要 flag 参数（Flag 原文）。")
        if self.flag_manager is None:
            return _ToolOutcome(ok=False, text="本题未装配 Flag 管理器，无法提交。")
        cand = self.flag_manager.register(
            value, challenge_id=self.challenge_id(task), found_by=self.agent_id)
        if cand is None:
            return _ToolOutcome(ok=True, value=0.0, note=f"Flag 重复: {value[:40]}",
                                text="该 Flag 值此前已登记过，无需重复提交。")
        self._record_flag(task, cand.value,
                          route=route.name)   # 先登记候选（并行方向即刻可见）
        result = await self.flag_manager.submit(cand)
        verdict = {True: "判定正确", False: "判定错误", None: "未获平台判定"}[
            result.accepted]
        # 裁决态回填共享记录：accepted 才是同伴的收敛信号；rejected 让同伴
        # 不再重复提交同一个错值；无判定通道（manual）留给人工确认
        self._record_flag(task, cand.value,
                          {True: "accepted", False: "rejected"}.get(
                              result.accepted, "manual"), route=route.name)
        self.ctx.audit.log("agent.flag_submit", self.agent_id, {
            "route": route.name, "challenge": self.challenge_id(task),
            "flag_id": cand.flag_id, "verdict": str(result.accepted),
            "note": str(args.get("note", ""))[:200]})
        text = (f"已登记并提交：{cand.value}\n平台应答：{verdict}"
                + (f"（{result.submit_response[:200]}）" if result.submit_response else ""))
        return _ToolOutcome(
            ok=True,
            value=0.9 if result.accepted is True else (
                0.2 if result.accepted is False else 0.6),
            findings=[{"type": "flag", "value": cand.value}],
            note=f"Flag 提交 {cand.value} → {verdict}", text=text)

    async def _tool_spawn(self, task: Task, route, state: dict[str, Any],
                          args: dict[str, Any]) -> _ToolOutcome:
        """派生分支执行者：以当前任务 + 模型撰写的简报起一条独立解题对话，
        阻塞至其收尾，最终结论回灌给当前对话。

        派生时机、数量与分工由模型按任务复杂度自行决策（brief 须说明
        为什么/做什么/边界）；层级上限两代（子→孙）是唯一护栏——防的
        是无限递归，不是进度。分支与父分支共享题级现场（笔记/入口/
        Flag 即时互通，任一路线夺旗全体收敛）。
        """
        depth = int(state.get("depth") or 0)
        if depth >= 2:
            return _ToolOutcome(ok=False, text=(
                "分支层级已达上限（子→孙）：更深的子问题请并入当前工作推进，"
                "或把要点用 note 交给同伴。"))
        brief = str(args.get("brief", "") or "").strip()
        if not brief:
            return _ToolOutcome(ok=False, text="spawn 需要 brief 参数（派什么/为什么/边界）。")
        expect = str(args.get("expect", "") or "").strip()
        seq = int(state.get("child_seq") or 0) + 1
        state["child_seq"] = seq
        # 子路线也要有真实身份：_solve_turn 构造 ExecutionScope 与 journal
        # checkpoint 都读 route.route_id——缺了它 spawn 100% 摔 AttributeError
        child_route = SimpleNamespace(name=f"{route.name}·子{seq}",
                                      route_id=new_id("route"))
        child_plan = RoutePlan(name=f"spawn{seq}", strategy=brief[:200],
                               steps=[{"action": "investigate"}])
        child_state = self._inv_state(task, child_route)
        child_state["depth"] = depth + 1
        self.ctx.audit.log("agent.spawn", self.agent_id, {
            "route": route.name, "challenge": self.challenge_id(task),
            "child": child_route.name, "depth": depth + 1,
            "brief": brief[:400], "expect": expect[:200]})
        state["observations"].append(
            {"action": f"spawn {child_route.name}",
             "result": f"派生分支：{brief[:120]}"})
        while not child_state.get("concluded") and not child_state.get("stall"):
            await self._solve_turn(task, child_route, child_plan)
        conclusion = str(child_state.get("conclusion_text")
                         or child_state.get("stall")
                         or "（分支未给出结论文本）")
        flags = len(child_state.get("flags") or [])
        self.ctx.audit.log("agent.spawn_done", self.agent_id, {
            "route": route.name, "child": child_route.name,
            "flags": flags, "conclusion": conclusion[:400]})
        text = (f"分支「{brief[:120]}」已收尾，结论：\n{conclusion[:2400]}"
                + (f"\n（该分支捕获 Flag {flags} 个）" if flags else ""))
        return _ToolOutcome(ok=True, value=0.4 if flags else 0.2,
                            note=f"分支 {child_route.name} 收尾 flag={flags}",
                            text=text)

    async def _tool_ask_user(self, task: Task, route, state: dict[str, Any],
                             args: dict[str, Any]) -> _ToolOutcome:
        """向用户提问并挂起等待作答（问题与选项由模型撰写，后端只送达回收）。

        用户答复（点选选项或自由输入）与勾选扩围的目标原样回灌；会话停止
        时按「无答复」唤醒，模型据此自行决定继续或收尾。
        """
        question = str(args.get("question", "") or "").strip()
        if not question:
            return _ToolOutcome(ok=False, text="ask_user 需要 question 参数。")
        options = [str(o).strip()[:200] for o in (args.get("options") or [])[:6]
                   if isinstance(o, (str, int, float)) and str(o).strip()]
        from spiritscout.policy.questions import normalize_targets
        targets = normalize_targets(args.get("targets"))
        broker = getattr(self.ctx, "questions", None)
        if broker is None:
            return _ToolOutcome(ok=False, text=(
                "当前会话没有可用的用户询问通道——基于已有证据自行决策，"
                "并在结论中说明该不确定点。"))
        multiple = bool(args.get("multiple"))
        self.ctx.audit.log("agent.ask_user", self.agent_id, {
            "route": route.name, "challenge": self.challenge_id(task),
            "question": question[:500], "options": options,
            "multiple": multiple, "targets": targets})
        result = await broker.ask(self.agent_type.value, question, options, targets,
                                  multiple=multiple)
        answer = str(result.get("answer") or "").strip()
        added = result.get("scope_added") or []
        if result.get("cancelled"):
            return _ToolOutcome(ok=True, value=0.0,
                                note="ask_user → 会话停止，未获答复",
                                text="用户尚未作答（会话已停止）。基于已有证据自行决策。")
        lines = [f"用户已答复：{answer or '（未给出明确选择，请基于现有证据继续）'}"]
        if added:
            lines.append("以下目标已由用户确认加入本会话授权范围，可继续测试: "
                         + ", ".join(added))
        self.ctx.audit.log("agent.ask_answered", self.agent_id, {
            "route": route.name, "answer": answer[:300], "scope_added": added})
        return _ToolOutcome(ok=True, value=0.1,
                            note=f"用户答复: {answer[:60]}",
                            text="\n".join(lines))

    async def _tool_kb_search(self, args: dict[str, Any]) -> _ToolOutcome:
        """经验知识库检索：检查清单/攻击路径/狩猎模板/平台规则（参考资料）。"""
        query = str(args.get("query", "") or "").strip()
        if not query:
            return _ToolOutcome(ok=False, text="kb_search 需要 query 检索词。")
        kb = self.ctx.knowledge
        if kb is None:
            return _ToolOutcome(ok=False, text="经验知识库未装配。")
        try:
            hits = kb.search(query, limit=6)
        except Exception as exc:  # noqa: BLE001 —— 检索失败作为结果回灌
            return _ToolOutcome(ok=False, text=f"知识库检索失败: {exc}")
        # 安全技能库检索（NoorQureshi/security-skills，MIT）：关键词匹配
        skill_hits = []
        q_lower = query.lower()
        resources = getattr(kb, "resources", None)
        skills = resources.skills(query, include_body=True) if resources else _load_sec_skills()
        for sk in skills:
            if not sk.get("enabled", True):
                continue
            hay = (sk.get("skill_id", "") + " " + sk.get("description", "")).lower()
            if any(w in hay for w in q_lower.split() if len(w) >= 2):
                skill_hits.append(sk)
        if skill_hits:
            body_lines = ["\n=== 安全技能库命中（NoorQureshi/security-skills，MIT）==="]
            for sk in skill_hits[:3]:
                body_lines.append(f"[{sk['skill_id']}] {sk['description'][:120]}")
                body_lines.append(sk["body"][:1500])
                body_lines.append("---")
            body = "\n".join(body_lines)[:4000]
        else:
            body = None
        # 本会话 MCP 工具目录命中（活目录：用户经网页端安装/移除的服务器随
        # 会话装配即时反映，无需入库——想到能力却没想起工具名时在此对上）
        mcp_hits: list[tuple[str, str]] = []
        for exposed, entry in (getattr(self.ctx, "mcp_tools", None) or {}).items():
            desc = str((entry.get("schema") or {}).get("description", ""))
            hay = " ".join((exposed, str(entry.get("server", "")),
                            str(entry.get("tool", "")), desc)).lower()
            if any(w in hay for w in q_lower.split() if len(w) >= 2):
                mcp_hits.append((exposed, desc))
        mcp_text = ""
        if mcp_hits:
            lines = ["\n=== 本会话 MCP 工具命中（当前会话真实目录）==="]
            lines += [f"{exposed} — {desc[:160]}"
                      for exposed, desc in mcp_hits[:8]]
            mcp_text = "\n".join(lines)[:2000]
        if not hits and not skill_hits and not mcp_hits:
            return _ToolOutcome(ok=False, text=(
                f"经验库没有命中「{query[:60]}」——按你收集到的信息自行分析。"))
        blocks: list[str] = []
        for h in hits:
            entry = kb.get_entry(h["base"], h["entry_key"])
            if entry:
                blocks.append(self._kb_entry_digest(h["base"], entry))
        titles = "、".join(str(h.get("title", ""))[:24] for h in hits[:4])
        kb_text = "\n\n".join(blocks)[:_TOOL_RESULT_CAP] or "（条目内容为空）"
        skill_text = ("\n\n" + body if body else "")
        return _ToolOutcome(ok=True, value=0.15,
                            note=f"经验库命中 {len(blocks)} 条: {titles}"
                                 + (f" + 安全技能 {len(skill_hits)} 条" if skill_hits else "")
                                 + (f" + MCP 工具 {len(mcp_hits)} 个" if mcp_hits else ""),
                            text=(kb_text + skill_text + mcp_text)[:_TOOL_RESULT_CAP])

    @staticmethod
    def _kb_entry_digest(base: str, e: dict[str, Any]) -> str:
        """条目 → 给模型看的紧凑摘要（检查点/命令/命中信号/转向）。"""
        if base == "redteam_entries":
            lines = [f"【{e.get('title')}】(entry_id={e.get('entry_id')})"]
            for s in e.get("steps", []):
                lines.append(f"- {s.get('action')}｜做法: {s.get('command')}"
                             f"｜命中信号: {s.get('expected')}")
            if e.get("verify_method"):
                lines.append(f"核验: {e['verify_method']}")
            if e.get("fail_handling"):
                lines.append(f"不适用时: {e['fail_handling']}")
            return "\n".join(lines)
        if base == "attack_paths":
            lines = [(f"【路径模板】{e.get('scenario')}"
                      f"｜入口条件: {e.get('entry_conditions')}")]
            for s in e.get("step_sequence", [])[:10]:
                lines.append(f"- {s.get('step')} {s.get('action')}"
                             f"（{s.get('tools')}）→ {s.get('verify')}")
            if e.get("decision_points"):
                lines.append(f"分支: {e['decision_points']}")
            return "\n".join(lines)
        if base == "hunt_templates":
            return "\n".join([
                f"【狩猎模板】{e.get('scene')}｜前置: {e.get('precondition')}",
                "步骤: " + " → ".join(str(s) for s in e.get("step_sequence", [])[:8]),
                f"常见坑: {e.get('common_pitfall')}"])
        parts = [f"【{e.get(k)}】" for k in
                 ("verify_method", "include_condition", "note", "operation",
                  "representative_scene", "rules", "name", "desc", "data")
                 if e.get(k)]
        return "｜".join(parts[:3]) or json.dumps(e, ensure_ascii=False)[:300]

    async def _tool_mcp(self, task: Task, name: str,
                        args: dict[str, Any]) -> _ToolOutcome:
        """外部 MCP 工具调用：统一经过 ToolRegistry 的工具权限与危险动作审批。"""
        entry = (getattr(self.ctx, "mcp_tools", None) or {}).get(name)
        if entry is None:
            return _ToolOutcome(ok=False,
                                text=f"MCP 工具 {name} 不在当前会话工具目录。")
        res = await self.ctx.tools.call(name, args, **self._tool_ctx(task))
        text = res.stdout or res.error or res.stderr
        note = f"MCP {entry['server']}/{entry['tool']}"
        return _ToolOutcome(ok=res.ok,
                            value=0.2,
                            findings=[],
                            note=note[:120],
                            text=text[:_TOOL_RESULT_CAP] or "（无输出）")

    def _resolve_http(self, task: Task, args: dict[str, Any]
                      ) -> tuple[str, dict[str, str] | None,
                                 dict[str, str] | None, str | None] | None:
        """HTTP 入参归一化：path/url → 完整地址，参数/头/体按上限裁剪。

        只服务于实际请求构造（path 写法与 url 写法等价）。非法目标返回 None。
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

        测试边界由模型结合用户上下文维护（http_probe 内），参数上限仅防上下文污染。
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
        # 有副作用的请求须模型自报行为类别（审批与审计的依据），缺声明不执行；
        # 纯读取方法（GET/HEAD/OPTIONS）不需要——是否敏感由策略门按类别判断
        action = str(args.get("action", "") or "").strip()
        if method not in _HTTP_SAFE_METHODS and not action:
            return _ToolOutcome(ok=False, text=(
                f"{method} 是有副作用的请求，需要 action 参数如实声明行为类别"
                "（analysis / intrusive_write / brute_force / webshell_upload 等）；"
                "缺声明不执行。纯读取请用 GET/HEAD。"))
        base = str(self.challenge_of(task).get("url") or task.target or "").rstrip("/")
        shown = url[len(base):] if base and url.startswith(base) else url
        desc = f"{method} {shown[:100] or '/'}"

        # 记录回放（record-first）：本路线已读取过的地址直接回放当时记录，
        # 不再发请求；需要重新获取（页面状态可能已变化）时模型显式加
        # refresh 参数。Range 等头不同的请求键不同，分段取原文不受影响。
        refresh = bool(args.get("refresh"))
        hsig = ""
        if headers:
            hsig = "|" + "|".join(sorted(
                f"{k}:{str(v)[:60]}" for k, v in headers.items()))[:300]
        # 跨分支共享缓存：同伴已读取过的裸 GET 直接回放，不重复发请求。
        # 回放必须自我披露：回灌文本若无标记，模型会把旧录像当成新鲜响应
        # （登录后再访问仍见登录前快照 → 误判未登录 → 死循环）；带参数的
        # GET 不回放——缓存键不含参数，回放会答非所问。键含方法与请求头
        # 指纹：POST 的应答不能被同伴的 GET 回放（表单提交/登录跳转的
        # 应答顶掉页面快照），Range 等头不同的分段读取各记各的
        shared_key = f"fetch|{method}|{url}{hsig}"
        shared_fetch = self.cache.get(self.shared_namespace, shared_key)
        if (shared_fetch and not refresh and method == "GET"
                and not params and not data):
            state["observations"].append({
                "action": desc,
                "result": "跨分支记录回放（同伴已读取，本次未重复请求）"})
            return _ToolOutcome(ok=True, value=0.0,
                note=f"{desc} → 跨分支回放",
                text=("（记录回放：同伴此前已读取该地址，以下为当时的记录，"
                      "本次未发请求；页面状态可能已变化——登录/写入之后要看"
                      "最新现场，在参数中加 \"refresh\": true 重新获取）\n"
                      + str(shared_fetch))[:_FETCH_RECORD_CAP])
        rkey = attempt_key(method, url, params) + hsig
        fetches = state.setdefault("fetches", {})
        if (method == "GET" and not params and not data and not refresh
                and rkey in fetches):
            record = fetches[rkey]
            prior = record.get("obs") if isinstance(record.get("obs"), dict) else {}
            state["observations"].append({
                "action": desc,
                "result": f"记录回放（第 {record.get('round', '?')} 回合已读取）"})
            self.ctx.audit.log("agent.reread", self.agent_id, {
                "route": route.name, "challenge": self.challenge_id(task),
                "turn": int(state.get("turn") or 0), "url": url[:200],
                "from_round": record.get("round")})
            return _ToolOutcome(
                ok=True, value=0.0, note=f"{desc} → 记录回放",
                text=("（记录回放：该地址已在第 "
                      f"{record.get('round', '?')} 回合读取过，以下为当时的记录，"
                      "本次未重复请求；页面状态可能已变化需要重新获取时，"
                      "在参数中加 \"refresh\": true）\n" + _obs_text(prior))
                [:_FETCH_RECORD_CAP])

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
        if action:
            request["action"] = action
        if params:
            request["params"] = params
        if headers:
            request["headers"] = headers
        if data:
            request["data"] = data
        res = await self.ctx.tools.call("http_probe", request, **self._tool_ctx(task))
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
        status = res.parsed.get("status_code", 0)
        obs = _http_observation(desc, res, url, [])
        state["observations"].append(obs)
        fetches[rkey] = {"round": int(state.get("turn") or 0), "obs": obs}
        if method == "GET" and not params and not data:
            # 只有裸 GET 进跨分支回放池：POST/带参请求的应答进池会被
            # 同伴的 GET 回放成语义错位的“现场”（登录态、写操作结果）
            self.cache.put(self.shared_namespace, shared_key,
                           _obs_text(obs)[:_FETCH_RECORD_CAP])
        # 环形共享缓存：入口与关键信号登记，同伴不再重复探同一入口
        signal = _signal_line(obs)
        self._record_site(task, url, f"{status}{signal}", route=route.name)
        value = 0.25 if status < 400 else 0.05
        blocker = _challenge_instance_blocker(status, res.stdout)
        if blocker:
            state["stall"] = blocker
        if self.ctx.ring is not None:
            self.ctx.ring.record_attempt(
                akey, agent_id=self._ring_actor(route),
                agent_name=self._ring_name(route), action="http_request",
                outcome=value, note=f"{status}")
        return _ToolOutcome(
            ok=True, value=value, observation=obs,
            findings=[],
            note=f"{desc} → {status}",
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
        # 行为类别须模型如实自报（通用执行入口无法确认性质就不执行——
        # 不以 readonly 自动放行）；危险语义由策略门即时请示用户
        action = str(args.get("action", "") or "").strip()
        if not action:
            return _ToolOutcome(ok=False, text=(
                f"{name} 工具需要 action 参数：如实声明本次执行的行为类别"
                "（analysis / delete_files / reverse_shell / webshell_upload / "
                "container_escape / brute_force / intrusive_write）。"
                "这是危险动作审批与审计的依据，缺声明不执行。"))
        try:
            timeout = float(args.get("timeout") or 60)
        except (TypeError, ValueError):
            timeout = 60.0
        first = code.splitlines()[0][:80]
        desc = f"{name}: {first}"
        res = await self.ctx.tools.call(
            "sandbox_exec",
            {"code": code, "language": "python" if name == "python" else "shell",
             "timeout": timeout, "log_name": f"output_{route.name}",
             "action": action},
            **self._tool_ctx(task))
        if not res.ok and not (res.stdout or res.stderr):
            state["observations"].append(
                {"action": desc, "result": f"执行失败: {str(res.error)[:100]}"})
            return _ToolOutcome(ok=False, note=f"{desc} → 执行失败", text=(
                f"{desc} 执行失败：{str(res.error)[:300]}"))
        rc = res.parsed.get("exit_code", res.returncode)
        status = (f"超时终止（{timeout:.0f}s）" if res.timed_out
                  else f"退出码 {rc}")
        state["observations"].append(
            {"action": desc,
             "result": f"{status}；输出 {len(res.stdout or '')} 字符"})
        workdir = str((res.parsed or {}).get("workdir") or "")
        out_file = str((res.parsed or {}).get("output_file") or "")
        head = (f"（{status}，耗时 {res.duration_ms / 1000:.1f}s，"
                f"工作目录 {workdir or '见输出'}——会话内持久；/tmp 即焚）")
        lines = [f"$ {first}", head]
        if res.stdout:
            lines.append("stdout:\n" + _head_tail(res.stdout, _SAND_MODEL_CAP))
        if res.stderr:
            lines.append("stderr:\n" + _head_tail(res.stderr, 1200))
        if out_file:
            label = "输出已轮转，仅保留最近两代" if res.parsed.get("output_rotated") else "输出已存盘"
            lines.append(f"{label}（不占上下文）: {out_file}"
                         "——需要细节时在沙箱里按需读取")
            if res.parsed.get("previous_output_file"):
                lines.append(f"上一代输出: {res.parsed['previous_output_file']}")
        if res.timed_out:
            lines.append("提示：超时通常意味着脚本在等待输入或死循环——"
                         "交互式脚本请改为非交互（预置输入/设置超时）。")
        return _ToolOutcome(
            ok=True, value=0.05, empty=not (res.stdout or "").strip(),
            findings=[],
            note=f"{desc} → {status}",
            text="\n".join(lines))

    async def _tool_analyze_text(self, task: Task) -> _ToolOutcome:
        """题面文本取证：全部载荷字符串的编码串链式解码 + 零宽字符统计 + Flag 提取。"""
        text = _challenge_text(self.challenge_of(task))
        decoded_texts = _decode_blobs(text)
        zw = len(re.findall(r"[\u200b\u200c\u200d\u2060]", text))
        obs = {"action": "analyze_text",
               "result": f"可解码串 {len(decoded_texts)} 个，零宽字符 {zw} 个"}
        state_note = f"题面分析：{len(decoded_texts)} 个可解码串，零宽字符={zw}"
        body = ("题面分析结果：可解码串 "
                f"{len(decoded_texts)} 个，零宽字符 {zw} 个。\n"
                + ("解码结果:\n" + "\n".join(d[:600] for d in decoded_texts[:6])
                   if decoded_texts else "（无可解码串）"))
        return _ToolOutcome(ok=True, value=0.15,
                            findings=[],
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
                "file_analyze", {"path": path}, **self._tool_ctx(task))
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
                "file_analyze", {"path": path}, **self._tool_ctx(task))
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
        return _ToolOutcome(ok=True, value=0.3,
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
                                            **self._tool_ctx(task))
            if not res.ok:
                state["seed"].append(f"入口页 {url}：不可达（{str(res.error)[:120]}）")
                if self.ctx.ring is not None:
                    self.ctx.ring.record_attempt(
                        akey, agent_id=self._ring_actor(route),
                        agent_name=self._ring_name(route), action="bootstrap",
                        outcome=0.0, note="入口页不可达")
                return 0.0, "赛题入口不可达"
            status = res.parsed.get("status_code", 0)
            obs = _http_observation(f"GET {url}", res, url, [])
            state["observations"].append(obs)
            state.setdefault("fetches", {})[attempt_key("GET", url, None)] = {
                "round": 0, "obs": obs}
            self._record_site(task, url, f"{status}{_signal_line(obs)}",
                              route=route.name)
            state["seed"].append("入口页预感知：\n" + _obs_text(obs))
            value = 0.3 if status < 400 else 0.05
            blocker = _challenge_instance_blocker(status, res.stdout)
            if blocker:
                state["stall"] = blocker
            if self.ctx.ring is not None:
                self.ctx.ring.record_attempt(
                    akey, agent_id=self._ring_actor(route),
                    agent_name=self._ring_name(route), action="bootstrap",
                    outcome=value, note=f"入口页 {status}")
            return value, blocker or f"入口页 {status}"
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

    def _world_environment(self) -> dict[str, Any]:
        builtin = getattr(self.ctx, "builtin_ctx", None) or {}
        workspace = str(builtin.get("workspace_root") or "")
        artifacts = str(Path(workspace) / "artifacts") if workspace else ""
        return world_state.environment(workspace=workspace, artifacts=artifacts)

    def _world_snapshot(self) -> dict[str, Any]:
        guard = getattr(getattr(self.ctx, "policy", None), "guard", None)
        return world_state.snapshot(guard=guard, env=self._world_environment(),
                                    blackboard=getattr(self.ctx, "blackboard", None))

    def _sync_world_state(self, state: dict[str, Any]) -> tuple[str, list[str]]:
        """授权扩围/环境变化时追加一条自包含片段；无变化不占上下文。"""
        current = self._world_snapshot()
        previous = state.get("world_state")
        fragment = world_state.update(previous, current)
        changed = [] if fragment is None else [
            key for key, value in current.items()
            if not isinstance(previous, dict) or previous.get(key) != value]
        state["world_state"] = current
        return fragment or "", changed

    def _solver_system_prompt(self) -> str:
        from spiritscout.agents.profiles import agent_system_prompt
        from spiritscout.core.models import RunMode

        prompt = (
            agent_system_prompt(self.agent_type)
            + "\n"
            "工作方式：工具就是你的手——需要看什么就用工具去拿，不要凭空猜测"
            "页面内容或响应。拿到页面先读懂它：可见文案、注释、表单与链接是"
            "出题人放在现场的第一手情报，往往比直接上手打接口更能说明这道题"
            "要什么。每一轮可以调用一个或多个工具：互不依赖的请求放在"
            "同一轮并发发出，省时间。\n"
            "环境与知识：工具列表里的工具与 mcp__ 前缀的外部工具都是你的手；"
            "工具、解释器与库是否可用以实际执行结果为准，不确定时先探测；"
            "经验库里的检查清单与平台特性事实用 kb_search 检索——它们是"
            "参考资料，采纳与否由你基于现场证据判断。\n"
            "动手能力：要跑命令用 shell，要写代码用 python——它们在隔离沙箱里"
            "执行（系统文件只读，网络操作须遵守用户上下文中的测试边界）。工作目录在会话内持久："
            "脚本与产物放在那里，后续回合直接可用；/tmp 每次运行即焚，需要"
            "留存的文件不要写进 /tmp。WebSocket/socket 等协议交互、数据计算、"
            "脚本化构造请求，凡是 http_request 够不着的，一律用它们去完成。\n"
            "交付文件：需要给用户留存的分析产物写到工作目录的 ../artifacts/，"
            "会话的产出与事实页会列出这些文件；结论注明文件名、来源与证据，"
            "临时计算文件继续放在工作目录。\n"
            "协同：同一道题可能有其他方向的 Agent 在并行工作，你们共享现场——"
            "别人探过的入口、写下的笔记、拿到的 Flag 会以「同伴动态」出现在对话里；"
            "有值得同伴立刻知道的发现（接口/参数/口令线索）就用 note 工具写一条，"
            "发现别人已经解决或已经试过的，不要重复做功。\n"
            "提问：遇到授权范围外的目标或需要用户拍板的分叉，用 ask_user 向"
            "用户提问——问题与选项用你自己的话写清楚；用户同意的目标会被"
            "加入本会话范围，之后可正常测试。\n"
            "团队扩展：把任务拆成多个可独立验证的子问题——用 spawn 派生"
            "分支执行者并行推进，brief 写清「为什么需要它、它专属的任务、"
            "解决什么问题、与当前工作的边界」；其结论会回传给你。各司其职："
            "每个分支只做分内之事，顺手能做的不要派。\n"
            "收尾：判定拿到 Flag 时先用 submit_flag 提交（写明依据），拿到平台"
            "应答再收尾；当你不再调用工具时，最后一条消息就是交给主 Agent 的"
            "结论——简明写清「拿到了什么（Flag 原文）、依据是什么、还差什么」。"
            "确认无解题空间时直接收尾，不要空转。\n"
            "纪律：对话里已经记录了你发过的每个请求与结果——重复发出完全相同"
            "的调用不会有任何新信息，是否算「同一动作」由你自己判断（换了参数、"
            "换了头、换了时机就是不同的请求，可以再试）；结果里已经有答案就不要"
            "重复验证；连续多轮没有新发现时，主动换思路或直接给出结论，不要"
            "原地打转；简单题简单做——用最少步骤验证最直接的线索。省回合就是省"
            "时间：能放到同一个脚本/同一轮里一次做完的探查（连读多个页面、连试"
            "多个入口、批量看响应），不要拆成多轮一次做一件——每多一轮就多付"
            "一次完整的思考时间。"
        )
        if getattr(self.ctx, "mode", None) == RunMode.COMPETITION:
            from spiritscout.competition import competition_solver_prompt

            prompt += "\n" + competition_solver_prompt()
        return prompt

    def _seed_messages(self, task: Task, route, plan: RoutePlan,
                       state: dict[str, Any]) -> None:
        """首条用户消息：题面 + 入口预感知 + 同伴/前轮结论 + 目标。"""
        ch = self.challenge_of(task)
        parts: list[str] = []
        world = self._world_snapshot()
        if world:
            parts.append(world_state.render(world))
        state["world_state"] = world
        recovery = task.payload.get("execution_recovery")
        if recovery:
            parts.append("## 历史执行核对\n" + json.dumps(recovery, ensure_ascii=False))
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
            parts.append("## 同伴 Flag 动态（候选仅参考；判对即战果，判错勿重复提交）\n"
                         + "\n".join(self._flag_line(f) for f in peer_flags))
        state.setdefault("peer_seen", {
            "site": [u for u, _ in shown_site],
            "notes": len(self._shared_notes(task)),
            "concl": len(self._shared_conclusions(task)),
            "flags": [self._flag_line(f) for f in peer_flags]})
        rules = str((task.payload or {}).get("rules") or "")
        task_rules_part = ("## 用户既定规则（全程遵守，含终止条件）\n" + rules
                           if rules else "")
        exps = task.payload.get("recent_experience") or []
        if exps:
            parts.append("## 实战经验（同类场景的参考事实与信号，非步骤；"
                         "采纳与否由现场证据判断）\n"
                         + "\n".join(f"· {e}" for e in exps[-5:]))
        angle = str(plan.strategy or "").strip()
        if angle:
            parts.append("## 本路线调查角度（与并行分支分工，不重复做功）\n"
                         + angle[:200])
        task_part = ("## 任务\n夺取本题 Flag；判定拿到后用 submit_flag 提交，"
                     "拿到应答即收尾。")
        history = self.ctx.blackboard.read(BlackboardLayer.TOPOLOGY, "historical-assets")
        if history:
            parts.append("## 同一资产的历史参考（本次未复核，不能沿用旧授权）\n" + json.dumps(history, ensure_ascii=False)[:8000])
        # 项目说明作为 world_state.project_rules 完整下发，不重复注入正文；
        # 历史记忆（资产/发现）仍放正文末尾，预算不够时先丢它。
        project_memory = self.ctx.blackboard.read(
            BlackboardLayer.TOPOLOGY, "project-memory")
        project_rules = ""
        if project_memory:
            project_rules = str(project_memory.get("description") or "").strip()
            history_view = {key: value for key, value in project_memory.items()
                            if key != "description"}
            parts.append("## 所属项目的历史记忆（同项目跨会话参考，本次未复核，"
                         "不构成授权范围）\n"
                         + json.dumps(history_view, ensure_ascii=False)[:3000])
        rules_part = ""
        if project_rules and not world.get("project_rules"):
            rules_part = ("## 所属项目的测试规则（用户写在项目说明里，全程遵守；"
                          "不扩大本次授权，与当前授权冲突时以当前授权为准）\n"
                          + project_rules)
        world_part = parts.pop(0) if world else ""
        body = "\n\n".join(parts)
        fixed = len(world_part) + len(rules_part) + len(task_rules_part) + len(task_part) + 8
        body_budget = max(4000, 24000 - fixed)
        state["messages"] = [
            {"role": "system", "content": self._solver_system_prompt()},
            {"role": "user", "content": "\n\n".join(
                part for part in (world_part, body[:body_budget], task_rules_part, rules_part,
                                  task_part) if part)},
        ]

    @staticmethod
    def _compact_messages(messages: list[dict[str, Any]], *,
                          initialized: bool = False) -> None:
        """对话压缩：超预算时从最旧开始折叠，锚点不丢、折叠可恢复。

        - system 与首条用户消息（题面契约）永不折叠；
        - 工具/助手长文折叠为「头 + 尾」摘录：头保方法与现场，尾常是结论
          与落点地址；HTTP GET 可记录回放、沙箱输出在工作区文件——折叠
          不等于丢失；
        - user 消息完整保留：授权更新和用户答复不能按观察正文节选，
          不通过关键字判断其是否涉及测试边界。
        """
        sent_view = _model_messages(messages, initialized=initialized)
        total = sum(len(str(m.get("content") or "")) for m in sent_view)
        if total <= _MSG_CHAR_BUDGET:
            return
        for i, msg in enumerate(messages[2:], start=2):
            if total <= _MSG_CHAR_BUDGET:
                break
            content = str(msg.get("content") or "")
            role = msg.get("role")
            if role not in ("tool", "assistant") or len(content) <= 400:
                continue
            keep = (content[:200]
                    + "…（早前内容已折叠，头尾摘录；完整内容可按记录重取）"
                    + content[-120:])
            total -= len(content) - len(keep)
            msg["content"] = keep

    @staticmethod
    def _merge_note(prefix: str, note: str) -> str:
        if prefix and prefix == note:
            return note
        return f"{prefix}；{note}" if prefix else note

    @staticmethod
    def _extend_or_finish(plan: RoutePlan, state: dict[str, Any], note: str) -> str:
        """动态延展：未收尾则追加下一回合占位步。

        不设固定回合顶：收尾由模型自主决定（不再调用工具即结论），
        或由用户手动停止会话；后端规则不做进度的刽子手。
        """
        if state["concluded"] or state["stall"]:
            return note
        plan.steps.append({"action": "investigate"})
        return note


# ================================================================ Web 专项


class CTFWebAgent(CTFBaseAgent):
    """Web 专项（4.4.1）：LLM 按题提出调查角度，逐环自主探测。"""

    agent_type = AgentType.CTF_WEB
    direction = "web"
    route_namespace = "ctf_web"
    tool_schemas: ClassVar[list[dict[str, Any]]] = [
        *CTFBaseAgent.tool_schemas,
        _fn("probe_paths",
            "仅在入口页面、技术栈或题面给出相应线索后，批量验证少量敏感路径；"
            "优先传自定义 paths，不把默认清单当作理解业务的替代品。",
            {"paths": {"type": "array", "items": {"type": "string"},
                       "description": "由现场证据推导的路径列表（以 / 开头）"}}),
    ]


# ================================================================ Crypto 专项


class CTFCryptoAgent(CTFBaseAgent):
    """Crypto 专项（4.4.4）：编码链/古典密码工具 + LLM 按题分析。

    RSA/AES/ECC 现代密码攻击依赖 SageMath/z3， 经工具层接入（不静默降级）；
    数学求解超出工具能力时，由 LLM 把结构分析与可验证线索写入结论。
    """

    agent_type = AgentType.CTF_CRYPTO
    direction = "crypto"
    route_namespace = "ctf_crypto"
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
            if not decoded or decoded == target:
                return _ToolOutcome(ok=False, text="这串没有被 Base64/Hex/URL/Rot13 识别出可解码层。")
            return _ToolOutcome(
                ok=True, value=0.3,
                findings=[],
                note=f"解码链结果 {len(decoded)} 字符",
                text="解码结果:\n" + decoded[:3000])
        if name == "caesar_brute":
            hits = _caesar_brute(target)
            if not hits:
                return _ToolOutcome(ok=False, text="目标串不是纯字母，凯撒爆破不适用。")
            return _ToolOutcome(
                ok=True, value=0.2,
                findings=[],
                note=f"凯撒爆破 {len(hits)} 个候选位移",
                text="26 个位移结果:\n" + "\n".join(hits)[:3000])
        if name == "rail_fence":
            hits = _rail_fence_brute(target)
            if not hits:
                return _ToolOutcome(ok=False, text="目标串不是纯字母或太短，栅栏还原不适用。")
            return _ToolOutcome(
                ok=True, value=0.2,
                findings=[],
                note=f"栅栏还原 {len(hits)} 个候选栏数",
                text="各栏数还原结果:\n" + "\n".join(hits)[:3000])
        return None


# ================================================================ Misc 专项


class CTFMiscAgent(CTFBaseAgent):
    """Misc 专项（4.4.5）：附件/字符串/编码工具 + LLM 按题分析。

    zsteg/binwalk/tshark 等隐写工具链 经 CLI 封装接入（6.8）。
    """

    agent_type = AgentType.CTF_MISC
    direction = "misc"
    route_namespace = "ctf_misc"
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

        if not decoded_hits:
            return _ToolOutcome(ok=False, text="附件字符串里没有被识别出的编码串。")
        return _ToolOutcome(
            ok=True, value=0.2,
            findings=[],
            note=f"字符串深挖：{len(decoded_hits)} 个可解码串",
            text="解码命中:\n" + "\n".join(h[:600] for h in decoded_hits[:20]))


# ================================================================ Reverse 专项


class CTFReverseAgent(CTFBaseAgent):
    """Reverse 专项（4.4.3）：二进制情报工具 + LLM 按题分析。

    Ghidra 脚本化/angr 符号执行/z3 求解 经工具层接入（6.8）。
    """

    agent_type = AgentType.CTF_REVERSE
    direction = "reverse"
    route_namespace = "ctf_reverse"
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
