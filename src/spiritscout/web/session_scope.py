"""项目目标归一化与会话范围问题的输入清理。"""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse


def _is_scope_domain(host: str) -> bool:
    """主机名能否写进 allowed_domains（含 *. 通配）。

    IP 字面量走 allowed_cidrs 授权，塞进域名表不会匹配（ScopeGuard 对
    IP 只查网段），因此这里直接排除；端口、路径、空标签一律不认。
    """
    host = str(host or "").strip().lower()
    if host.startswith("*."):
        host = host[2:]
    if not host:
        return False
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    try:
        ipaddress.ip_address(ascii_host)
        return False
    except ValueError:
        pass
    labels = ascii_host.split(".")
    if len(labels) < 2:
        return False
    return all(
        label and len(label) <= 63 and not label.startswith("-")
        and not label.endswith("-")
        and all(ch.isalnum() or ch == "-" for ch in label)
        for label in labels)


def _project_target_value(value: str, kind: str = "") -> str:
    """项目记忆资产值 → 可提交授权确认的目标（URL origin 或域名）。

    记忆值是历史观测抽样，写法很杂（URL/域名/host:port/端点）。只放行
    能落进 allowed_urls/allowed_domains 且可被 ScopeGuard 命中匹配的
    形态；服务条目取主机名，URL 归一到 origin（同意一个站点服务）。
    """
    value = str(value or "").strip()
    if not value:
        return ""
    if "://" in value:
        parsed = urlparse(value)
        if parsed.scheme.lower() not in ("http", "https"):
            return ""
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError:
            return ""
        wildcard = host.startswith("*.")
        plain_host = host[2:] if wildcard else host
        if not _is_scope_domain(plain_host):
            return ""
        if wildcard:
            return "*." + plain_host
        return (f"{parsed.scheme.lower()}://{host}"
                + (f":{port}" if port else ""))
    host = value
    if kind == "service" and host.count(":") == 1:
        host = host.split(":", 1)[0]
    host = host.strip().strip("[]").lower().rstrip(".")
    wildcard = host.startswith("*.")
    plain_host = host[2:] if wildcard else host
    if not _is_scope_domain(plain_host):
        return ""
    return "*." + plain_host if wildcard else plain_host


def _project_target_key(target: str) -> str:
    """候选目标比较键：URL 归一到 origin，域名去尾点，大小写不敏感。"""
    value = str(target or "").strip().lower()
    if not value:
        return ""
    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").strip().rstrip(".")
        try:
            port = parsed.port
        except ValueError:
            return ""
        if not host:
            return ""
        return (f"{parsed.scheme.lower()}://{host}"
                + (f":{port}" if port else ""))
    return value.rstrip(".")


def _clean_scope_question(
        data: dict[str, Any], candidates: list[dict[str, str]],
) -> tuple[str, list[str], list[dict[str, Any]]] | None:
    """模型撰写的范围确认问句校验：目标只能照抄候选，不得发明新目标。"""
    question = str(data.get("question") or "").strip()[:800]
    if not question:
        return None
    raw_options = data.get("options")
    options = [str(o).strip()[:200] for o in (raw_options or [])[:6]
               if str(o).strip()] if isinstance(raw_options, list) else []
    if not options:
        options = ["同意测试以上目标", "这次先不测"]
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list):
        raw_targets = []
    by_key = {_project_target_key(c["target"]): c for c in candidates}
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_targets[:6]:
        raw = item.get("target") if isinstance(item, dict) else item
        key = _project_target_key(str(raw or ""))
        candidate = by_key.get(key)
        if candidate is None or key in seen:
            continue
        seen.add(key)
        why = (str(item.get("why") or "").strip()[:200]
               if isinstance(item, dict) else "")
        entry = {"target": candidate["target"], "why": why or candidate["why"]}
        if isinstance(item, dict) and isinstance(item.get("approved_by"), list):
            entry["approved_by"] = [option for option in options if option in item["approved_by"]]
        picked.append(entry)
    if not picked:
        picked = candidates[:6]          # 仅展示候选；缺少选项关联时仍需解释用户答复
    return question, options, picked
