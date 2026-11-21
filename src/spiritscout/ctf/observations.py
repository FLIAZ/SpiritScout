"""HTTP 响应与文本的确定性观察投影，不负责执行或解题决策。"""
from __future__ import annotations

import html
import json
import re
from typing import Any

# 单条观察摘录上限；页面文案与接口线索共同占用该预算。
_OBS_EXCERPT_CAP = 4000


# 仅响应首段含 HTML 标签时按页面摘要处理。
_HTML_HINT = re.compile(
    r"<\s*(?:!doctype|html|head|body|div|span|a|p|form|table|script|style|title)\b",
    re.IGNORECASE)


# 观察保留的高信号响应头（跳转、会话、指纹、鉴权）。
_OBS_HEADERS = ("location", "set-cookie", "server", "x-powered-by",
                "content-type", "www-authenticate", "x-flag", "allow")


def _collapse(text: str) -> str:
    """空白折叠（多行 HTML 片段 → 单行可读文本）。"""
    return re.sub(r"\s+", " ", text).strip()


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
    if parsed.get("redirects"):
        # 中转跳的原始响应头（尤其 302 + Set-Cookie）必须回传给模型，
        # 否则登录/提交后的会话证据只能靠猜 Cookie 值补。
        obs["redirects"] = [{
            "status": int(hop.get("status") or 0),
            "url": str(hop.get("url") or "")[:300],
            "location": str(hop.get("location") or "")[:300],
            **({"set_cookie": str(hop["set_cookie"])[:300]}
               if hop.get("set_cookie") else {}),
        } for hop in (parsed["redirects"] or [])[:8]]
    if parsed.get("evidence_files"):
        obs["evidence_files"] = {str(k): str(v)[:300]
                                 for k, v in (parsed["evidence_files"] or {}).items()}
    body = res.stdout or ""
    ctype = headers.get("content-type", "")    # 脚本/样式/JSON 文本不走 HTML 摘要（否则会被当作标签剥离只剩碎片）
    scripty = any(k in ctype.lower() for k in
                  ("javascript", "ecmascript", "json", "css"))
    if parsed.get("body_ref"):
        # 新 HTTP 通道已保存正文：短源码原样返回，长源码只预览，禁用二次有损摘要。
        obs.update({key: parsed.get(key) for key in
                    ("body_ref", "body_path", "body_chars", "download_complete", "preview_truncated", "response_is_partial", "text_decode_lossy")})
        obs["preview_truncated"] = bool(parsed.get("preview_truncated")) or len(body) > 3500
        obs["excerpt"] = (body if len(body) <= 3500 else
                          body[:2600] + "\n...[展示省略，非完整源码]...\n" + body[-850:])
    else:
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


def _obs_text(obs: dict[str, Any]) -> str:
    """结构化观察 → 回灌模型的工具结果文本。"""
    lines = [f"{obs.get('action', '')} → {obs.get('status', 0)}"]
    if obs.get("final_url"):
        lines.append(f"最终地址: {obs['final_url']}")
    if obs.get("body_ref"):
        lines.append(f"原文 body_ref={obs['body_ref']}；已保存 {obs.get('body_chars')} 字符；"
                     + ("响应下载完整" if obs.get("download_complete") else "响应下载不完整，已达到正文下载上限")
                     + ("；当前为 HTTP 206 局部响应，不代表完整资源" if obs.get("response_is_partial") else "")
                     + ("；以下仅展示首尾预览" if obs.get("preview_truncated") else
                        "；以下为完整响应正文" if obs.get("download_complete") else "；以下为已保存的部分正文")
                     + "。需要源码细节调用 http_response_read 读取或搜索，不要重复请求网页。")
        if obs.get("body_path"):
            lines.append("源码文件（沙箱只读，可直接解析常量）: " + str(obs["body_path"]))
        if obs.get("text_decode_lossy"):
            lines.append("文本解码存在替换字符；原始字节保存在同目录 body.bin，可按实际编码重新解析。")
    if obs.get("redirects"):
        lines.append("跳转链原始响应头（含 Set-Cookie，会话凭据以此为准）:")
        for hop in obs["redirects"]:
            line = f"- {hop.get('status')} {hop.get('url')} → {hop.get('location')}"
            if hop.get("set_cookie"):
                line += f"；Set-Cookie: {hop['set_cookie']}"
            lines.append(line)
    if obs.get("scope_note"):
        lines.append(str(obs["scope_note"]))
    if obs.get("headers"):
        lines.append("响应头: " + json.dumps(
            obs["headers"], ensure_ascii=False)[:600])
    if obs.get("body_path"):
        lines.append("同目录原始证据可直接用 shell 读取: request.http（本次实际发送的完整请求，"
                     "含工具会话携带的 Cookie）/ metadata.json / body.bin。")
    if obs.get("excerpt"):
        lines.append("正文:\n" + str(obs["excerpt"]))
    if obs.get("flags"):
        lines.append("Flag 已捕获并提交（FlagManager 确定性提取）: "
                     + ", ".join(str(f) for f in obs["flags"]))
    return "\n".join(lines)
