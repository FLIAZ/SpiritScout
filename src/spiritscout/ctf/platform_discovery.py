"""智能体赛事平台接口发现：有界读取入口、接口描述与同源前端脚本。

发现阶段只收集证据，不调用候选业务接口。平台档案仍由模型结合用户原话
与证据生成，并在会话装配层通过证据引用和同源约束后才接入适配器。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from spiritscout.core.audit import AuditLogger, NullAuditLogger

_DOC_PATHS = (
    "/openapi.json",
    "/swagger.json",
    "/api/openapi.json",
    "/v3/api-docs",
    "/api-docs",
    "/docs",
    "/swagger-ui/",
)
_SCRIPT_RE = re.compile(r'''(?i)<script[^>]+src=["']([^"']+)["']''')
_MAX_ENTRY_URLS = 2
_MAX_SCRIPTS = 4
_MAX_REQUESTS = 24
_MAX_DOCUMENT_BYTES = 512 * 1024
_MAX_TOTAL_CHARS = 60_000


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}"


def _same_origin(left: str, right: str) -> bool:
    return bool(_origin(left) and _origin(left) == _origin(right))


@dataclass(frozen=True)
class DiscoveryDocument:
    url: str
    status: int
    content_type: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "content_type": self.content_type,
            "text": self.text,
        }


async def _read_same_origin(client: httpx.AsyncClient, url: str,
                            entry_url: str) -> DiscoveryDocument | None:
    current = url
    for _ in range(4):
        try:
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = str(response.headers.get("location") or "").strip()
                    if not location:
                        return None
                    target = urljoin(str(response.url), location)
                    if not _same_origin(entry_url, target) or (
                            response.url.scheme == "https" and
                            urlsplit(target).scheme == "http"):
                        return None
                    current = target
                    continue
                if not 200 <= response.status_code < 300:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_DOCUMENT_BYTES:
                        del body[_MAX_DOCUMENT_BYTES:]
                        break
                content_type = str(response.headers.get("content-type") or "")
                return DiscoveryDocument(
                    url=str(response.url), status=int(response.status_code),
                    content_type=content_type.split(";", 1)[0].lower(),
                    text=body.decode(response.encoding or "utf-8", errors="replace"),
                )
        except (httpx.HTTPError, OSError, UnicodeError):
            return None
    return None


def _unique_http_urls(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if _origin(text) and text not in result:
            result.append(text)
    return result


def _marker_is_supported(marker: str, text: str) -> bool:
    parts = [part for part in re.split(r"\{[^{}]+\}", marker) if part]
    cursor = 0
    for part in parts:
        found = text.find(part, cursor)
        if found < 0:
            return False
        cursor = found + len(part)
    return bool(parts)


def _field_path_is_supported(path: str, text: str) -> bool:
    tokens = re.findall(r"[A-Za-z0-9_-]+", str(path or ""))
    return bool(tokens) and all(token in text for token in tokens)


def declared_profile_is_supported(raw: dict[str, Any], mission: str) -> bool:
    """确认任务原文确实声明了可自动接入的列题与提交契约。"""
    if not isinstance(raw, dict):
        return False
    text = str(mission or "")
    upper = text.upper()
    ops = raw.get("ops") or raw.get("endpoints")
    if not isinstance(ops, dict):
        return False
    base_url = str(raw.get("base_url") or raw.get("base") or "")
    if base_url and base_url not in text:
        return False
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict):
        return False
    if any(str(key) not in text or str(value) not in text
           for key, value in headers.items()):
        return False
    if not isinstance(ops.get("list"), dict) or not isinstance(ops.get("submit"), dict):
        return False
    for name, op in ops.items():
        if name not in {"list", "start", "close", "submit", "hint", "precheck"}:
            return False
        if not isinstance(op, dict):
            return False
        marker = str(op.get("url") or op.get("path") or op.get("endpoint") or "")
        if not _marker_is_supported(marker, text):
            return False
        method = str(op.get("method") or ("GET" if name in {"list", "hint", "precheck"}
                                           else "POST")).upper()
        if method not in upper:
            return False
        for attr in ("items_field", "addr_field", "accepted_field", "duplicate_code"):
            value = str(op.get(attr) or "")
            if value and not _field_path_is_supported(value, text):
                return False
        for block_name in ("query", "body", "params", "json", "data"):
            block = op.get(block_name)
            if block is not None and not isinstance(block, dict):
                return False
            if any(str(key) not in text for key in (block or {})):
                return False
    fields = raw.get("challenge_fields") or raw.get("fields") or {}
    if not isinstance(fields, dict) or not fields.get("id"):
        return False
    if any(not _field_path_is_supported(str(path), text)
           for path in fields.values() if path):
        return False
    limits = raw.get("limits") or {}
    if not isinstance(limits, dict):
        return False
    return all(str(value) in text for value in limits.values())


async def discover_platform_documents(
        entry_urls: list[str], *, audit: AuditLogger | None = None,
        timeout: float = 8.0) -> list[dict[str, Any]]:
    """从用户明确给出的赛事入口收集平台接口证据。

    每个入口仅读取入口页、固定的公开接口描述路径和入口页引用的少量同源
    脚本；不枚举目录、不跨主机、不调用发现到的业务接口。
    """
    logger = audit or NullAuditLogger()
    entries = _unique_http_urls(entry_urls)[:_MAX_ENTRY_URLS]
    if not entries:
        return []
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 4.0)),
        follow_redirects=False,
        headers={
            "Accept": "text/html, application/json, application/javascript, text/plain;q=0.8",
            "User-Agent": "SpiritScout-Platform-Discovery/1",
        },
    )
    documents: list[DiscoveryDocument] = []
    requested: set[str] = set()

    async def read(url: str, entry: str) -> DiscoveryDocument | None:
        if len(requested) >= _MAX_REQUESTS or url in requested:
            return None
        requested.add(url)
        return await _read_same_origin(client, url, entry)

    try:
        for entry in entries:
            entry_doc = await read(entry, entry)
            if entry_doc is not None:
                documents.append(entry_doc)
            origin = _origin(entry)
            doc_urls = [origin + path for path in _DOC_PATHS]
            found = await asyncio.gather(*(read(url, entry) for url in doc_urls))
            documents.extend(item for item in found if item is not None)
            if entry_doc is None:
                continue
            scripts = []
            for source in _SCRIPT_RE.findall(entry_doc.text):
                script_url = urljoin(entry_doc.url, source)
                if _same_origin(entry, script_url) and script_url not in scripts:
                    scripts.append(script_url)
            found_scripts = await asyncio.gather(
                *(read(url, entry) for url in scripts[:_MAX_SCRIPTS]))
            documents.extend(item for item in found_scripts if item is not None)
    finally:
        await client.aclose()

    remaining = _MAX_TOTAL_CHARS
    result: list[dict[str, Any]] = []
    for document in documents:
        if remaining <= 0:
            break
        text = document.text[:remaining]
        remaining -= len(text)
        result.append(DiscoveryDocument(
            url=document.url, status=document.status,
            content_type=document.content_type, text=text).to_dict())
    logger.log("platform.fact", "platform_discovery", {
        "action": "platform_discovery_completed",
        "entries": entries,
        "requests": len(requested),
        "documents": [{"url": item["url"], "status": item["status"],
                       "content_type": item["content_type"],
                       "chars": len(item["text"])} for item in result],
    })
    return result


def discovered_profile_is_supported(raw: dict[str, Any],
                                    documents: list[dict[str, Any]],
                                    mission: str = "") -> bool:
    """核对自动生成档案的关键接口确有同源、可复核的发现证据。"""
    if not isinstance(raw, dict):
        return False
    ops = raw.get("ops") or raw.get("endpoints")
    evidence = raw.get("evidence")
    if not isinstance(ops, dict) or not isinstance(evidence, dict):
        return False
    by_url = {
        str(item.get("url") or ""): str(item.get("text") or "")
        for item in documents if isinstance(item, dict) and item.get("url")
    }
    base_url = str(raw.get("base_url") or raw.get("base") or "")
    allowed_origins = {_origin(url) for url in by_url}
    if not _origin(base_url) or _origin(base_url) not in allowed_origins:
        return False
    support_text = "\n".join(by_url.values()) + "\n" + str(mission or "")
    headers = raw.get("headers") or {}
    if not isinstance(headers, dict):
        return False
    for key, value in headers.items():
        if str(key) not in support_text or str(value) not in support_text:
            return False

    if not isinstance(ops.get("list"), dict) or not isinstance(ops.get("submit"), dict):
        return False
    list_proof = evidence.get("list")
    if not isinstance(list_proof, dict):
        return False
    list_source = by_url.get(str(list_proof.get("source_url") or ""), "")
    fields = raw.get("challenge_fields") or raw.get("fields") or {}
    if not isinstance(fields, dict) or not fields.get("id"):
        return False
    if any(not _field_path_is_supported(str(path), list_source)
           for path in fields.values() if path):
        return False
    limits = raw.get("limits") or {}
    if not isinstance(limits, dict):
        return False
    if any(str(value) not in support_text for value in limits.values()):
        return False
    for name, op in ops.items():
        if name not in {"list", "start", "close", "submit", "hint", "precheck"}:
            return False
        op = ops.get(name)
        proof = evidence.get(name)
        if not isinstance(op, dict) or not isinstance(proof, dict):
            return False
        source_url = str(proof.get("source_url") or "")
        excerpt = " ".join(str(proof.get("excerpt") or "").split())
        source = " ".join(by_url.get(source_url, "").split())
        if len(excerpt) < 8 or excerpt not in source:
            return False
        op_url = str(op.get("url") or "")
        if op_url and (not _same_origin(base_url, op_url)):
            return False
        path = str(op.get("path") or op.get("endpoint") or "")
        if path.startswith(("http://", "https://")) and not _same_origin(base_url, path):
            return False
        marker = op_url or path
        if not marker:
            return False
        if not _marker_is_supported(marker, excerpt):
            return False
        method = str(op.get("method") or ("GET" if name in {"list", "hint", "precheck"}
                                           else "POST")).upper()
        if method not in excerpt.upper():
            return False
        for attr in ("items_field", "addr_field", "accepted_field", "duplicate_code"):
            value = str(op.get(attr) or "")
            if value and not _field_path_is_supported(value, source):
                return False
        for block_name in ("query", "body", "params", "json", "data"):
            block = op.get(block_name)
            if block is not None and not isinstance(block, dict):
                return False
            for key in (block or {}):
                if str(key) not in excerpt and str(key) not in support_text:
                    return False
    return True
