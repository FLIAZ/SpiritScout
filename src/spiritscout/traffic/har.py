"""受限 HAR 解析器。

只提取测试规划需要的 URL、HTTP 方法和参数名；Cookie、Authorization、
请求体值和响应正文不会写入黑板，避免把浏览器凭据复制到模型上下文。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import ScopeViolation
from spiritscout.core.models import AgentType, Asset, BlackboardLayer
from spiritscout.policy.scope import ScopeGuard

_MAX_HAR_BYTES = 10 * 1024 * 1024
_MAX_ENTRIES = 5000
_MAX_PARAMS = 64
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_UUID_OR_LONG_ID = re.compile(
    r"^(?:\d{2,}|[0-9a-f]{16,}|[0-9a-f]{8}-[0-9a-f-]{27,})$", re.IGNORECASE)


@dataclass(frozen=True)
class HarEndpoint:
    url: str
    method: str
    params: tuple[str, ...]
    param_locations: dict[str, str]
    template: str
    first_seen_index: int
    status_code: int | None


def _parameter_names(request: dict[str, Any], url: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if name:
            found[str(name)[:128]] = "query"
    for item in request.get("queryString") or []:
        if isinstance(item, dict) and item.get("name"):
            found[str(item["name"])[:128]] = "query"
    post = request.get("postData") or {}
    for item in post.get("params") or []:
        if isinstance(item, dict) and item.get("name"):
            found[str(item["name"])[:128]] = "body"
    mime = str(post.get("mimeType") or "").lower()
    text = post.get("text")
    if isinstance(text, str) and len(text) <= 1_000_000:
        if "json" in mime:
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                for name in payload:
                    found[str(name)[:128]] = "json"
        elif "x-www-form-urlencoded" in mime:
            for name, _ in parse_qsl(text, keep_blank_values=True):
                if name:
                    found[str(name)[:128]] = "body"
    return dict(list(found.items())[:_MAX_PARAMS])


def _endpoint_url(raw: str) -> str | None:
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    # HAR 可能记录 https://user:pass@host/ 形式的 Basic Auth。
    # 资产 URL 只保留 origin 和路径，不得携带用户信息。
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def _template(url: str) -> str:
    parsed = urlsplit(url)
    parts = ["{id}" if _UUID_OR_LONG_ID.match(p) else p
             for p in parsed.path.split("/")]
    return urlunsplit((parsed.scheme, parsed.netloc, "/".join(parts), "", ""))


def parse_har(path: str | Path) -> list[HarEndpoint]:
    """解析单个 HAR；结构或大小非法时抛 ValueError。"""
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"HAR 文件不存在: {source}")
    size = source.stat().st_size
    if size > _MAX_HAR_BYTES:
        raise ValueError(f"HAR 文件超过 {_MAX_HAR_BYTES // 1024 // 1024} MiB 上限")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"HAR 解析失败: {exc}") from exc
    entries = ((data.get("log") or {}).get("entries") or []) \
        if isinstance(data, dict) else []
    if not isinstance(entries, list):
        raise TypeError("HAR log.entries 必须是数组")

    endpoints: dict[tuple[str, str], HarEndpoint] = {}
    for entry_index, entry in enumerate(entries[:_MAX_ENTRIES]):
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict):
            continue
        method = str(request.get("method") or "GET").upper()
        raw_url = str(request.get("url") or "")
        url = _endpoint_url(raw_url)
        if method not in _HTTP_METHODS or url is None:
            continue
        locations = _parameter_names(request, raw_url)
        key = (method, url)
        previous = endpoints.get(key)
        if previous:
            locations = {**previous.param_locations, **locations}
        response = entry.get("response") if isinstance(entry, dict) else None
        raw_status = response.get("status") if isinstance(response, dict) else None
        try:
            status_code = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status_code = None
        endpoints[key] = HarEndpoint(
            url=url, method=method, params=tuple(locations),
            param_locations=locations, template=_template(url),
            first_seen_index=(previous.first_seen_index if previous else entry_index),
            status_code=(status_code if status_code is not None else
                         previous.status_code if previous else None),
        )
    return list(endpoints.values())


def import_har_directory(
    directory: str | Path,
    *,
    blackboard,
    guard: ScopeGuard,
    audit: AuditLogger,
) -> dict[str, int]:
    """导入附件目录内的 HAR；越界条目丢弃并留统计，不阻断任务。"""
    root = Path(directory)
    stats = {"files": 0, "endpoints": 0, "out_of_scope": 0, "errors": 0}
    if not root.is_dir():
        return stats
    for path in sorted(root.glob("*.har"))[:20]:
        stats["files"] += 1
        try:
            endpoints = parse_har(path)
        except (TypeError, ValueError) as exc:
            stats["errors"] += 1
            audit.log("traffic.har_error", "traffic_import", {
                "file": path.name, "error": str(exc)[:300]})
            continue
        for endpoint in endpoints:
            try:
                guard.check_url(endpoint.url)
            except ScopeViolation:
                stats["out_of_scope"] += 1
                continue
            digest = hashlib.sha256(
                f"{endpoint.method}\0{endpoint.url}".encode()).hexdigest()[:20]
            asset = Asset(
                asset_id=f"har-{digest}", asset_type="endpoint", value=endpoint.url,
                attrs={
                    "method": endpoint.method,
                    "params": list(endpoint.params),
                    "param_locations": endpoint.param_locations,
                    "template": endpoint.template,
                    "first_seen_index": endpoint.first_seen_index,
                    "status_code": endpoint.status_code,
                    "source_file": path.name,
                },
                source_agent="traffic_import",
            )
            blackboard.write(
                BlackboardLayer.ASSET, asset.asset_id,
                asset.model_dump(mode="json"), agent_type=AgentType.MASTER,
                updated_by="traffic_import",
                dedup_key=f"endpoint|{endpoint.method}|{endpoint.url}")
            stats["endpoints"] += 1
    if stats["files"]:
        audit.log("traffic.har_import", "traffic_import", stats)
    return stats
