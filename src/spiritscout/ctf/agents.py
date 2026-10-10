"""CTF 五方向专项子 Agent（设计书 4.4.1~4.4.5）：纯 LLM 调查环。

框架只提供两样东西：**工具目录**与**结果流向**。
- 工具目录：各方向可调用的确定性动作（含通用 HTTP 探测通道 http_request，
  与文件/文本/编码分析工具）；动作参数由 LLM 按当前证据现场构造，不按
  固定模板出牌。
- 结果流向：①网络请求走同一 http_probe 通道（ScopeGuard/RPS/审计/审批
  边界不变）；②Flag 只由 FlagManager 确定性正则提取（LLM 声明不作数）；
  ③每次动作结果进本路线观察序列与题级共享观察（同伴路线与主 Agent 复盘
  都能读到）；④已尝试入口登记进环形台账（同伴路线避让，不重复做功）。

解题方式完全由 LLM 按题分析：路线生成时 LLM 按题面提出 K 个调查角度
（K 由主 Agent 路线预算裁决），每条角度路线是一条独立 LLM 迭代调查环——
LLM 决定下一步动作、读结果、再决策，直至收尾或触发安全顶（步数顶/
重复空转熔断）。LLM 不可用时调查环诚实停摆留痕，不做固定模板兜底。

不启用端口扫描（C7）；仅访问赛题入口（URL 需在授权范围内）与本地附件。
"""

from __future__ import annotations

import base64
import binascii
import codecs
import html
import json
import re
import urllib.parse
from typing import Any, ClassVar

from spiritscout.agents.ring import attempt_key
from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.core.models import AgentType, Task
from spiritscout.ctf.flags import FlagManager

# 题面中疑似编码串的最小长度（降噪）
_MIN_BLOB_LEN = 12
# 喂给 LLM 的单条观察摘录上限（控制 token 扇出）
_OBS_EXCERPT_CAP = 3000
# 调查上下文（喂给 LLM 的 JSON）总字符预算：工具/约束等契约字段永不截断，
# 观察与已试动作按剩余空间回填（新步优先）
_CTX_MAX_CHARS = 14000
# 调查环步数安全顶（每步一次 LLM 决策；到顶即收尾，防无限空转）
_INVESTIGATE_MAX_STEPS = 24
# 连续重复动作告警/熔断线（同一动作签名连续出现；告警先提示，再犯即收）
_DOOM_WARN_AT = 3
_DOOM_STOP_AT = 5
# 解题环单次 LLM 调用超时：重推理模型单次可达 3~4 分钟
_SOLVER_TIMEOUT = 420
# HTTP 动作参数上限（上下文保护，非行为限制；目标范围由 ScopeGuard 把关）
_HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
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


def _html_digest(body: str) -> str | None:
    """HTML 响应 → 结构化观察摘要（纯机械提取，不做判断）。

    样式/脚本块剔除（内联 CSS 会把真正的入口信息挤出上下文）；注释单列且
    靠前——开发备注类线索（接口地址、口令提示）常藏于此；另提取标题、表单、
    输入字段、链接、脚本引用与内联脚本。非 HTML 返回 None（回退原文截断）。
    """
    if not _HTML_HINT.search(body[:4000]):
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
    obs["excerpt"] = _html_digest(body) or body[:_OBS_EXCERPT_CAP]
    if flags:
        obs["flags"] = flags
    return obs


class CTFBaseAgent(SubAgent):
    """CTF 专项子 Agent 基座：工具目录 + 结果流向 + LLM 调查环。"""

    direction = "misc"
    flag_manager: FlagManager | None = None
    # 本方向可调用的工具目录：action → 一句话用途（作为 LLM 的动作空间契约）
    investigate_tools: ClassVar[dict[str, str]] = {
        "http_request": "向赛题目标发一次 HTTP 请求（method/path/url/params/headers/data 可控）",
        "analyze_text": "对题面文本做编码链解码与 Flag 提取",
        "analyze_attachments": "对全部附件做魔数识别 + 字符串提取 + Flag 提取",
        "conclude": "结束本路线的调查并给出结论",
    }
    # 方向方法论（思路启发，不是步骤；LLM 按题面证据自选方向）
    direction_brief: ClassVar[str] = ""
    # 调查路线的安全顶是步数上限与重复空转熔断；健康度剪枝面向多路线资源
    # 再分配场景，逐轮探索的起步进展分天然低于阈值，会误杀在途调查——豁免
    health_prune_enabled: ClassVar[bool] = False

    def __init__(self, ctx, *, flag_manager: FlagManager | None = None, **kw):
        super().__init__(ctx, **kw)
        self.flag_manager = flag_manager

    # ------------------------------------------------------------ 赛题载荷

    @staticmethod
    def challenge_of(task: Task) -> dict[str, Any]:
        return task.payload.get("challenge", {})

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
        """模板解题已删除：保留 ABC 契约，返回单条无固定步骤的通用调查路线。

        正常路径走 plan_routes（LLM 按题提出 K 个调查角度）；本方法仅在
        绕过 plan_routes 的调用下兜底，不含任何固定动作。
        """
        return [RoutePlan(name="investigate",
                          strategy="自主调查：从题面与目标出发，自行决定调查方向",
                          steps=[{"action": "investigate"}])]

    async def plan_routes(self, task: Task) -> list[RoutePlan]:
        """LLM 按题面提出 K 个调查角度，每个角度一条调查环路线。

        K = 主 Agent 路线预算（未指定用默认条数）。角度提议失败或 LLM 关闭
        时退回单条通用调查路线（自主调查，仍无固定步骤）——不静默、不阻断。
        """
        k = task.max_routes if task.max_routes is not None else self.default_routes
        k = max(1, min(int(k), self.max_routes))
        angles = await self._llm_propose_angles(task, k)
        if angles:
            plans = [
                RoutePlan(name=a["name"], strategy=a["angle"],
                          steps=[{"action": "investigate"}])
                for a in angles
            ]
        else:
            plans = [RoutePlan(
                name="investigate",
                strategy="自主调查：从题面与目标出发，自行决定调查方向",
                steps=[{"action": "investigate"}])]
        if self.ctx.knowledge is not None:
            self._attach_kb_guidance(task, plans)
        return plans

    async def _llm_propose_angles(self, task: Task, k: int) -> list[dict[str, str]]:
        """LLM 按题面提出 k 个互不重叠的调查角度（路线生成即按题分析）。"""
        ch = self.challenge_of(task)
        data = await self._llm_chat_json("route_planner", {
            "challenge": _challenge_view(ch),
            "attachments": [str(a)[:300] for a in ch.get("attachments", [])[:10]],
            "slots": k,
            "tools": self.investigate_tools,
            "direction_brief": self.direction_brief,
            "constraints": (
                "返回 JSON：{\"angles\": [{\"name\": 英文短名(小写字母数字下划线), "
                "\"angle\": 一句话调查角度}]}。角度必须针对本题的具体信息"
                "（题面/附件/目标特征），互相不重叠，最多 slots 条；"
                "题目信息不足以区分时给出 1 条即可。"),
        }, max_user_chars=8000, timeout_seconds=_SOLVER_TIMEOUT)
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

    # ------------------------------------------------------------ 调查环

    async def execute_step(self, task, route, plan, step) -> StepResult:
        if step["action"] == "investigate":
            return await self._investigate_step(task, route, plan)
        return StepResult(ok=False, note=f"未知步骤: {step['action']}")

    def _inv_state(self, task: Task, route) -> dict[str, Any]:
        """按 题×路线 维度的调查状态（观察序列/尝试签名/收尾标记）。"""
        key = f"inv|{self.challenge_id(task)}|{route.name}"
        state = self.cache.get(self.route_namespace, key)
        if not isinstance(state, dict):
            state = {"bootstrapped": False, "observations": [], "tried": [],
                     "flags": [], "concluded": False, "stall": "", "doom": 0}
            self.cache.put(self.route_namespace, key, state)
        return state

    def _save_inv_state(self, task: Task, route, state: dict[str, Any]) -> None:
        self.cache.put(self.route_namespace,
                       f"inv|{self.challenge_id(task)}|{route.name}", state)

    def _shared_conclusions(self, task: Task) -> list[str]:
        """题级共享观察：各路线收尾结论（同伴路线与复盘都能读到）。"""
        got = self.cache.get(self.route_namespace,
                             f"shared|{self.challenge_id(task)}")
        return got if isinstance(got, list) else []

    def _record_conclusion(self, task: Task, route, summary: str) -> None:
        key = f"shared|{self.challenge_id(task)}"
        got = self.cache.get(self.route_namespace, key)
        conclusions = got if isinstance(got, list) else []
        conclusions.append(f"[{route.name}] {summary}"[:300])
        self.cache.put(self.route_namespace, key, conclusions[-12:])

    def _ring_actor(self, route) -> str:
        """环形台账身份按路线区分：同 Agent 的并行路线之间也互相避让。"""
        return f"{self.agent_id}|{route.name}"

    def _ring_name(self, route) -> str:
        return f"{self.agent_type.value}/{route.name}"

    async def _investigate_step(self, task: Task, route, plan: RoutePlan) -> StepResult:
        """调查环单轮：首轮确定性感知 → LLM 决策下一动作 → 执行 → 记录观察。

        边界（与全系统同一原则）：LLM 只决定「试什么」，执行走同一
        http_probe 通道（ScopeGuard/RPS/审计/审批不变）；Flag 只由
        FlagManager 正则提取；LLM 不可用 → 本路线诚实停摆（无模板兜底）。
        """
        state = self._inv_state(task, route)
        if state["concluded"] or state["stall"]:
            return StepResult(ok=True, value=0.0,
                              note=state["stall"] or "本路线已收尾")
        boot_value, boot_note = 0.0, ""
        if not state["bootstrapped"]:
            boot_value, boot_note = await self._inv_bootstrap(task, route, state)
            state["bootstrapped"] = True

        decision = await self._llm_chat_json(
            "ctf_solver", self._inv_context(task, plan, state),
            max_user_chars=_CTX_MAX_CHARS, timeout_seconds=_SOLVER_TIMEOUT)
        if decision is None:
            state["stall"] = "LLM 不可用，调查停摆（无模板兜底，等待重试）"
            self._save_inv_state(task, route, state)
            return StepResult(ok=True, value=boot_value,
                              note=self._merge_note(boot_note, state["stall"]))
        action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
        atype = str(action.get("type", "conclude"))
        analysis = str(decision.get("analysis", "")).strip()[:100]

        sig = self._action_signature(atype, action)
        if sig in state["tried"]:
            # 重复动作：不发请求（省流量）但记观察，连续重复即熔断
            state["doom"] += 1
            note = f"重复动作跳过: {atype}"
            state["observations"].append({"action": atype, "result": "重复动作（本路线已做过）"})
            if state["doom"] >= _DOOM_WARN_AT:
                state["observations"].append(
                    {"action": "系统提示", "result": "你在重复同一动作，必须换一个思路"})
                note += f"（连续 {state['doom']} 次，已提示换方向）"
            if state["doom"] >= _DOOM_STOP_AT:
                state["concluded"] = True
                note += "；重复空转熔断收尾"
            note = self._extend_or_finish(plan, state, note)
            self._save_inv_state(task, route, state)
            return StepResult(ok=True, value=boot_value,
                              note=self._merge_note(boot_note, note))
        state["doom"] = 0
        state["tried"].append(sig)

        result, desc = await self._inv_dispatch(task, route, state, atype, action)
        note = f"LLM: {desc} → {result.note}" if result.note else f"LLM: {desc}"
        if analysis:
            note += f"｜{analysis}"
        if boot_note:
            note = self._merge_note(boot_note, note)
        note = self._extend_or_finish(plan, state, note)
        self._save_inv_state(task, route, state)
        return StepResult(
            ok=True, value=min(boot_value + result.value, 1.0),
            progress_delta=1.0 / _INVESTIGATE_MAX_STEPS,
            findings=result.findings, note=note[:300])

    @staticmethod
    def _merge_note(prefix: str, note: str) -> str:
        return f"{prefix}；{note}" if prefix else note

    @staticmethod
    def _extend_or_finish(plan: RoutePlan, state: dict[str, Any], note: str) -> str:
        """动态延展：未收尾则追加下一轮占位步（步数安全顶内）；到顶即收尾。"""
        if state["concluded"] or state["stall"]:
            return note
        if len(plan.steps) < _INVESTIGATE_MAX_STEPS:
            plan.steps.append({"action": "investigate"})
        else:
            state["concluded"] = True
            note += "；步数安全顶收尾"
        return note

    @staticmethod
    def _action_signature(atype: str, action: dict[str, Any]) -> str:
        """动作签名：类型 + 关键参数（重复判定/上下文 tried 共用）。"""
        keys = ("method", "path", "url", "params", "data", "target", "param")
        payload = {k: action.get(k) for k in keys if action.get(k) not in (None, "")}
        return json.dumps({"type": atype, **payload}, sort_keys=True,
                          ensure_ascii=False)[:400]

    def _inv_context(self, task: Task, plan: RoutePlan, state: dict[str, Any]) -> dict[str, Any]:
        """喂给 LLM 的调查上下文：题面 + 本路线角度 + 观察 + 同伴结论 + 工具目录。

        分区预算：工具/约束等契约字段永在；观察与已试动作按剩余空间回填
        （新步优先，装不下的旧观察丢弃）——整体不触发调用侧截断，契约不被切尾。
        """
        ch = self.challenge_of(task)
        ctx: dict[str, Any] = {
            "challenge": _challenge_view(ch),
            "route_angle": plan.strategy,
            "tools": self.investigate_tools,
            "direction_brief": self.direction_brief,
            "constraints": (
                "返回 JSON：{\"analysis\": 对当前证据的推理（考点/技术栈/为什么"
                "选这一步）, \"action\": {...}, \"rationale\": 一句话理由}。"
                "action.type 取工具目录之一：http_request 形如 {\"type\":"
                "\"http_request\", \"method\": \"GET|POST|...\", \"path\": \"/站内路径\""
                " 或 \"url\": \"完整URL\", \"params\": {...}, \"headers\": {...}, "
                "\"data\": \"请求体\"}；analyze_text / analyze_attachments 无需参数；"
                "conclude 带 {\"summary\": \"结论\"}。"
                "规则：每步只做最有信息量的一件事；tried 中的动作禁止重复；"
                "拿到疑似 Flag 线索先验证；信息耗尽或判断无解空间时 conclude。"),
            "attachments": [str(a)[:300] for a in ch.get("attachments", [])[:10]],
            "flags_found": state["flags"],
            "step_budget": f"{len(state['tried'])}/{_INVESTIGATE_MAX_STEPS}",
            "sibling_conclusions": [str(c)[:200]
                                    for c in self._shared_conclusions(task)[-6:]],
            "observations": [],
            "tried": [],
        }
        # 余量 800：覆盖调用侧注入的督导建议等附加上下文
        budget = _CTX_MAX_CHARS - len(json.dumps(ctx, ensure_ascii=False)) - 800
        observations: list[dict[str, Any]] = []
        for obs in reversed(state["observations"][-8:]):
            cost = len(json.dumps(obs, ensure_ascii=False)) + 2
            if cost > budget:
                continue
            observations.append(obs)
            budget -= cost
        ctx["observations"] = list(reversed(observations))
        tried: list[str] = []
        for sig in reversed(state["tried"][-15:]):
            item = str(sig)[:200]
            cost = len(json.dumps(item, ensure_ascii=False)) + 2
            if cost > budget:
                continue
            tried.append(item)
            budget -= cost
        ctx["tried"] = list(reversed(tried))
        return ctx

    # ------------------------------------------------------------ 首轮感知

    async def _inv_bootstrap(self, task: Task, route, state: dict[str, Any]) -> tuple[float, str]:
        """首轮确定性感知：把「能直接看见的东西」放进观察，供 LLM 第一步分析。

        有 URL → 抓赛题入口页；否则 → 附件感知 + 题面文本。
        感知结果同样过 Flag 提取与环形登记（与后续步骤同一通道）。
        """
        ch = self.challenge_of(task)
        url = str(ch.get("url") or task.target or "")
        if url.startswith(("http://", "https://")):
            akey = attempt_key("GET", url)
            prior = (self.ctx.ring.should_avoid(akey, agent_id=self._ring_actor(route))
                     if self.ctx.ring is not None else None)
            if prior is not None:
                state["observations"].append(
                    {"action": f"GET {url}", "result":
                     f"环形避让：{prior['agent_name']} 已试此入口（无产出）"})
                return 0.0, f"环形共享：{prior['agent_name']} 已试入口页（无产出）"
            res = await self.ctx.tools.call("http_probe", {"url": url},
                                            **self.ctx.builtin_ctx)
            if not res.ok:
                state["observations"].append(
                    {"action": f"GET {url}", "result": f"不可达: {str(res.error)[:100]}"})
                if self.ctx.ring is not None:
                    self.ctx.ring.record_attempt(
                        akey, agent_id=self._ring_actor(route),
                        agent_name=self._ring_name(route), action="bootstrap",
                        outcome=0.0, note="入口页不可达")
                return 0.0, "赛题入口不可达"
            accepted = await self._harvest_http(res.stdout, task, "bootstrap:page")
            status = res.parsed.get("status_code", 0)
            state["flags"].extend(accepted)
            state["observations"].append(
                _http_observation(f"GET {url}", res, url, accepted))
            value = (0.3 if status < 400 else 0.05) + 0.6 * len(accepted)
            if self.ctx.ring is not None:
                self.ctx.ring.record_attempt(
                    akey, agent_id=self._ring_actor(route),
                    agent_name=self._ring_name(route), action="bootstrap",
                    outcome=value, note=f"入口页 {status}")
            return value, f"入口页 {status} flag={len(accepted)}"
        attachments = ch.get("attachments", [])
        if attachments:
            sr = await self._analyze_attachments(task)
            state["flags"].extend(f["value"] for f in sr.findings if f["type"] == "flag")
            state["observations"].append(
                {"action": "analyze_attachments", "result": sr.note[:200],
                 "flags": [f["value"] for f in sr.findings if f["type"] == "flag"]})
            return sr.value, sr.note[:120]
        sr = await self._tool_analyze_text(task)
        state["flags"].extend(f["value"] for f in sr.findings if f["type"] == "flag")
        state["observations"].append({"action": "analyze_text", "result": sr.note[:200]})
        return sr.value, sr.note[:120]

    # ------------------------------------------------------------ 工具执行

    async def _inv_dispatch(self, task: Task, route, state: dict[str, Any],
                            atype: str, action: dict[str, Any]) -> tuple[StepResult, str]:
        """按动作类型分发到工具；不识别/不合规 → 确定性拦截（LLM 下轮可纠）。"""
        if atype == "conclude":
            summary = str(action.get("summary", "")).strip()[:200] or "LLM 判断调查完成"
            state["concluded"] = True
            self._record_conclusion(task, route, summary)
            return StepResult(ok=True, value=0.0, note=f"收尾：{summary}"), "conclude"
        if atype == "http_request":
            return await self._inv_http_request(task, route, state, action)
        if atype == "analyze_text":
            sr = await self._tool_analyze_text(task)
            state["flags"].extend(f["value"] for f in sr.findings if f["type"] == "flag")
            return sr, "analyze_text"
        if atype == "analyze_attachments":
            sr = await self._analyze_attachments(task)
            state["flags"].extend(f["value"] for f in sr.findings if f["type"] == "flag")
            return sr, "analyze_attachments"
        sr = await self._direction_tool(task, state, atype, action)
        if sr is not None:
            state["flags"].extend(f["value"] for f in sr.findings if f["type"] == "flag")
            return sr, atype
        state["observations"].append(
            {"action": atype, "result": "动作不在工具目录内，已拦截"})
        return (StepResult(ok=True, value=0.0,
                           note=f"动作不合规已拦截: {atype}"), atype)

    async def _inv_http_request(self, task: Task, route, state: dict[str, Any],
                                action: dict[str, Any]) -> tuple[StepResult, str]:
        """通用 HTTP 动作：LLM 提议 → 确定性校验 → 环形避让 → 执行 → 观察。

        目标范围由 ScopeGuard 把关（http_probe 内），参数上限仅防上下文污染。
        """
        ch = self.challenge_of(task)
        base = str(ch.get("url") or task.target or "").rstrip("/")
        method = str(action.get("method", "GET")).upper()
        path = str(action.get("path", "") or "")
        full = str(action.get("url", "") or "")
        desc = f"{method} {path or full[:60] or '?'}"
        if method not in _HTTP_METHODS:
            return (StepResult(ok=True, value=0.0,
                               note=f"方法不支持已拦截: {method}"), desc)
        url = ""
        if path.startswith("/") and "://" not in path and len(path) <= 300 and base:
            url = base + path
        elif full.startswith(("http://", "https://")) and len(full) <= 400:
            url = full
        if not url:
            return (StepResult(ok=True, value=0.0,
                               note="目标不合法已拦截（需站内 path 或完整 url）"), desc)
        params = action.get("params")
        if isinstance(params, dict) and params:
            params = {str(k)[:32]: str(v)[:400] for k, v in list(params.items())[:6]}
        else:
            params = None
        headers = action.get("headers")
        if isinstance(headers, dict) and headers:
            headers = {str(k)[:40]: str(v)[:300] for k, v in list(headers.items())[:8]}
        else:
            headers = None
        data = action.get("data")
        if data is not None and not isinstance(data, str):
            data = json.dumps(data, ensure_ascii=False)
        if isinstance(data, str):
            data = data[:2000]

        # 环形避让：同伴（含本 Agent 其他路线）在此入口无产出 → 不重复做功
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
                return (StepResult(ok=True, value=0.0,
                                   note=f"环形共享：{prior['agent_name']} 已试此入口"
                                        f"（无产出），跳过"), desc)

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
            return StepResult(ok=True, value=0.0, note=f"{desc} → 请求失败"), desc
        accepted = await self._harvest_http(res.stdout, task, f"llm:{desc[:40]}")
        status = res.parsed.get("status_code", 0)
        state["flags"].extend(accepted)
        state["observations"].append(_http_observation(desc, res, url, accepted))
        value = (0.25 if status < 400 else 0.05) + 0.6 * len(accepted)
        if self.ctx.ring is not None:
            self.ctx.ring.record_attempt(
                akey, agent_id=self._ring_actor(route),
                agent_name=self._ring_name(route), action="http_request",
                outcome=value, note=f"{status} flag={len(accepted)}")
        return StepResult(
            ok=True, value=value,
            findings=[{"type": "flag", "value": f} for f in accepted],
            note=f"{desc} → {status} flag={len(accepted)}"), desc

    async def _tool_analyze_text(self, task: Task) -> StepResult:
        """题面文本取证：全部载荷字符串的编码串链式解码 + 零宽字符统计 + Flag 提取。"""
        text = _challenge_text(self.challenge_of(task))
        decoded_texts = _decode_blobs(text)
        accepted = await self._harvest("\n".join([text, *decoded_texts]), task, "description")
        zw = len(re.findall(r"[\u200b\u200c\u200d\u2060]", text))
        return StepResult(
            ok=True, value=0.15 + 0.6 * len(accepted),
            findings=[{"type": "flag", "value": f} for f in accepted],
            note=f"题面分析：{len(decoded_texts)} 个可解码串，"
                 f"零宽字符={zw} flag={len(accepted)}")

    async def _analyze_attachments(self, task: Task) -> StepResult:
        """对全部附件做魔数识别 + 字符串提取，文本进 Flag 提取。"""
        attachments = self.challenge_of(task).get("attachments", [])
        if not attachments:
            return StepResult(ok=True, value=0.0, note="无附件")
        findings: list[dict[str, Any]] = []
        notes = []
        all_strings: list[str] = []
        for path in attachments:
            res = await self.ctx.tools.call(
                "file_analyze", {"path": path}, **self.ctx.builtin_ctx)
            if not res.ok:
                notes.append(f"{path}: {res.error}")
                continue
            parsed = res.parsed
            notes.append(f"{path}: magic={parsed['magic']} size={parsed['size']}")
            all_strings.extend(parsed.get("strings", []))
            findings.append({"type": "file", "path": path,
                             "magic": parsed["magic"], "size": parsed["size"]})
            self.cache.put(self.route_namespace, f"file|{path}", parsed)
        accepted = await self._harvest("\n".join(all_strings), task, "attachment_strings")
        findings.extend({"type": "flag", "value": f} for f in accepted)
        return StepResult(ok=True, value=0.3 + 0.5 * len(accepted),
                          findings=findings, note="; ".join(notes))

    async def _direction_tool(self, task: Task, state: dict[str, Any],
                              atype: str, action: dict[str, Any]) -> StepResult | None:
        """方向专属工具钩子；返回 None = 不在本方向工具目录内。"""
        return None


# ================================================================ Web 专项


class CTFWebAgent(CTFBaseAgent):
    """Web 专项（4.4.1）：LLM 按题提出调查角度，逐环自主探测。"""

    agent_type = AgentType.CTF_WEB
    direction = "web"
    route_namespace = "ctf_web"
    direction_brief = (
        "Web 题常见考点（按题面证据选用，不逐条硬套）：源码/备份泄露（.git、"
        ".bak、www.zip）、robots.txt 与隐藏路径、参数注入（SQLi/命令注入/模板"
        "注入——模板 canary 如 {{7*7}} 被求值为 49 即命中）、文件包含与路径穿越、"
        "类型混淆与弱比较、反序列化、条件竞争、JWT 伪造、响应头/HTML 注释/JS "
        "中的线索。先读题面页与响应内容，再按证据选方向。")


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
        "明文、padding 线索——把结构结论与卡点写进观察，供后续决策。")
    investigate_tools: ClassVar[dict[str, str]] = {
        **CTFBaseAgent.investigate_tools,
        "decode_chain": "对指定串（缺省题面密文）做 Base/Hex/URL/Rot13 链式解码",
        "caesar_brute": "对纯字母密文做凯撒全位移爆破",
        "rail_fence": "对纯字母密文做栅栏定栏还原",
    }

    def _ciphertext(self, task: Task) -> str:
        ch = self.challenge_of(task)
        blob = str(ch.get("ciphertext") or ch.get("description") or "")
        return blob.strip()

    async def _direction_tool(self, task, state, atype, action):
        target = str(action.get("target", "") or "").strip() or self._ciphertext(task)
        if atype == "decode_chain":
            if not target:
                return StepResult(ok=False, note="无目标串可解码")
            decoded = _decode_chain(target, max_depth=4)
            accepted = await self._harvest(decoded or "", task, "decode_chain")
            return StepResult(ok=bool(decoded), value=0.3 + 0.6 * len(accepted),
                              findings=[{"type": "flag", "value": f} for f in accepted],
                              note=f"解码链结果 {len(decoded or '')} 字符 flag={len(accepted)}")
        if atype == "caesar_brute":
            hits = _caesar_brute(target)
            if not hits:
                return StepResult(ok=False, note="目标串非纯字母，凯撒爆破不适用")
            accepted = await self._harvest("\n".join(hits), task, "caesar")
            return StepResult(ok=True, value=0.2 + 0.6 * len(accepted),
                              findings=[{"type": "flag", "value": f} for f in accepted],
                              note=f"凯撒爆破 {len(hits)} 个候选位移 flag={len(accepted)}")
        if atype == "rail_fence":
            hits = _rail_fence_brute(target)
            if not hits:
                return StepResult(ok=False, note="目标串非纯字母，栅栏还原不适用")
            accepted = await self._harvest("\n".join(hits), task, "rail_fence")
            return StepResult(ok=True, value=0.2 + 0.5 * len(accepted),
                              findings=[{"type": "flag", "value": f} for f in accepted],
                              note=f"栅栏还原 {len(hits)} 个候选栏数 flag={len(accepted)}")
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
    investigate_tools: ClassVar[dict[str, str]] = {
        **CTFBaseAgent.investigate_tools,
        "deep_strings": "对附件字符串做编码串链式解码（先 analyze_attachments 拿 strings）",
    }

    async def _direction_tool(self, task, state, atype, action):
        if atype == "deep_strings":
            return await self._deep_strings(task)
        return None

    async def _deep_strings(self, task: Task) -> StepResult:
        attachments = self.challenge_of(task).get("attachments", [])
        decoded_hits: list[str] = []
        for path in attachments:
            cached = self.cache.get(self.route_namespace, f"file|{path}")
            strings = (cached or {}).get("strings", [])
            for s in strings:
                if len(s) >= _MIN_BLOB_LEN and re.fullmatch(r"[A-Za-z0-9+/=_-]+", s):
                    decoded = _decode_chain(s)
                    if decoded and decoded != s:
                        decoded_hits.append(decoded)
        accepted = await self._harvest("\n".join(decoded_hits), task, "deep_strings")
        return StepResult(ok=True, value=0.2 + 0.5 * len(accepted),
                          findings=[{"type": "flag", "value": f} for f in accepted],
                          note=f"字符串深挖：{len(decoded_hits)} 个可解码串 flag={len(accepted)}")


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
    investigate_tools: ClassVar[dict[str, str]] = {
        **CTFBaseAgent.investigate_tools,
        "algo_hints": "从附件字符串归纳加密/编码算法线索",
    }

    async def _direction_tool(self, task, state, atype, action):
        if atype == "algo_hints":
            return await self._algo_hints(task)
        return None

    async def _algo_hints(self, task: Task) -> StepResult:
        attachments = self.challenge_of(task).get("attachments", [])
        hints: set[str] = set()
        for path in attachments:
            cached = self.cache.get(self.route_namespace, f"file|{path}")
            strings = (cached or {}).get("strings", [])
            blob = "\n".join(strings)
            for kw in ("xor", "rc4", "aes", "des", "base64", "sha", "md5", "tea"):
                if re.search(rf"(?i)\b{kw}\b", blob):
                    hints.add(kw)
        return StepResult(ok=True, value=0.2 if hints else 0.05,
                          note=f"二进制算法线索: {sorted(hints) or '未识别'}")


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
    investigate_tools: ClassVar[dict[str, str]] = {
        **CTFBaseAgent.investigate_tools,
        "checksec": "归纳附件 ELF 的保护机制清单（canary/fortify 静态信号）",
        "danger_funcs": "提取危险函数线索与漏洞类假设",
    }
    # 危险函数 → 漏洞类线索（4.4.2 二进制漏洞分析的最小静态信号）
    _DANGER_FUNCS: ClassVar[dict[str, str]] = {
        "gets": "栈溢出（无边界输入）", "strcpy": "栈/堆溢出",
        "sprintf": "格式化字符串/溢出", "scanf": "格式化字符串风险",
        "free": "UAF/Double Free 审计点", "system": "ret2system 利用目标",
        "mprotect": "shellcode 执行铺垫",
    }

    async def _direction_tool(self, task, state, atype, action):
        if atype == "checksec":
            return await self._checksec(task)
        if atype == "danger_funcs":
            return await self._danger_funcs(task)
        return None

    async def _checksec(self, task: Task) -> StepResult:
        attachments = self.challenge_of(task).get("attachments", [])
        notes = []
        for path in attachments:
            cached = self.cache.get(self.route_namespace, f"file|{path}")
            parsed = cached or {}
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
        return StepResult(ok=bool(notes), value=0.2, note="; ".join(notes) or "无附件")

    async def _danger_funcs(self, task: Task) -> StepResult:
        attachments = self.challenge_of(task).get("attachments", [])
        leads: list[str] = []
        for path in attachments:
            cached = self.cache.get(self.route_namespace, f"file|{path}")
            strings = "\n".join((cached or {}).get("strings", []))
            for func, hint in self._DANGER_FUNCS.items():
                if re.search(rf"\b{func}\b", strings):
                    leads.append(f"{func}: {hint}")
        return StepResult(ok=True, value=0.15 * min(len(leads), 3),
                          findings=[{"type": "pwn_lead", "value": lead} for lead in leads],
                          note=f"危险函数线索 {len(leads)} 条" if leads else "未见危险函数")


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
