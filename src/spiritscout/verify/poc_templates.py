"""PoC 模板库（MVP 内置最小集； 由 PoC 模板库装载扩充）。

每个模板产出两件产物：
1. poc_request —— 供核验流水线「实测」的结构化请求规格（经 http_probe 执行，
   全程 ScopeGuard 授权校验；系统永不 exec 生成的代码，G9 安全默认）；
2. poc_text —— 供报告展示的人类可复现 PoC（curl + Python requests 双形态）。
"""

from __future__ import annotations

from typing import Any

from spiritscout.core.models import Vulnerability

# 各漏洞类的复现方式注册（≥2 种交叉，硬约束 C5）
REPRO_METHODS: dict[str, list[str]] = {
    "sqli": ["error_replay", "boolean_replay"],
    "xss": ["reflect_replay", "reflect_alt_payload"],
    "ssrf": ["canary_replay", "canary_variant"],
    "info_disclosure": ["http_recheck", "http_recheck_alt_ua"],
}


def build_poc_request(vuln: Vulnerability) -> dict[str, Any] | None:
    """由证据链构造 PoC 实测请求规格。"""
    ev = vuln.evidence[0] if vuln.evidence else {}
    url = ev.get("url") or (vuln.target.rstrip("/") + vuln.endpoint)
    param = vuln.parameter
    if vuln.vuln_type == "sqli":
        payload = next(
            (e.get("payload") for e in vuln.evidence if e.get("payload")), "'")
        return {"method": "GET", "url": url, "params": {param: payload},
                "expect": "sql_error"}
    if vuln.vuln_type == "xss":
        payload = next(
            (e.get("payload") for e in vuln.evidence if e.get("payload")), None)
        if payload is None:
            return None
        return {"method": "GET", "url": url, "params": {param: payload},
                "expect": "raw_reflection", "canary": "spirxss"}
    if vuln.vuln_type == "ssrf":
        canary = ev.get("canary_url")
        if not canary:
            return None
        return {"method": "GET", "url": url, "params": {param: canary},
                "expect": "substring", "substring": "SPIR-SSRF-CANARY"}
    if vuln.vuln_type == "info_disclosure":
        marker = ev.get("marker")
        return {"method": "GET", "url": url, "params": {},
                "expect": "substring" if marker else "status_200",
                "substring": marker or ""}
    return None


def render_poc_text(vuln: Vulnerability, req: dict[str, Any]) -> str:
    """人类可复现 PoC 文本（curl + Python requests）。"""
    from urllib.parse import urlencode

    query = urlencode(req.get("params", {}))
    full_url = f"{req['url']}?{query}" if query else req["url"]
    curl = f"curl -i '{full_url}'"
    python = (
        "import requests\n\n"
        f"url = {req['url']!r}\n"
        f"params = {req.get('params', {})!r}\n"
        "resp = requests.get(url, params=params, timeout=10)\n"
        "print(resp.status_code)\n"
        "print(resp.text[:500])\n"
    )
    return f"### curl 复现\n\n```bash\n{curl}\n```\n\n### Python 复现\n\n```python\n{python}```"
