"""Hunter、Quake 与 ZoomEye 空间测绘适配器；结果始终是未验证候选。"""

from __future__ import annotations

import base64
import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from spiritscout.assets.providers.base import ProviderCandidate, ProviderResult
from spiritscout.config.settings import AssetApiProviderConfig
from spiritscout.core.exceptions import SpiritScoutError


def effective_api_provider(name: str, config: AssetApiProviderConfig, resources) -> AssetApiProviderConfig:
    if resources is None:
        return config
    managed = next((row for row in resources.settings("asset_provider")
                    if row["id"] == name), None)
    data = managed["data"] if managed else {}
    return config.model_copy(update={"enabled": data.get("enabled", config.enabled)})


def _api_key(config: AssetApiProviderConfig) -> str:
    value = os.environ.get(config.api_key_env, "").strip()
    if value:
        return value
    try:
        from spiritscout.web import vault

        return vault.get_key(config.vault_key_name).strip()
    except (OSError, SpiritScoutError, ValueError, TypeError):
        return ""


def _hostname(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else "//" + text)
    return (parsed.hostname or "").rstrip(".").lower()


def _candidate(value: Any, provider: str, index: int, claim: dict[str, Any]) -> ProviderCandidate | None:
    host = _hostname(value)
    if not host:
        return None
    return ProviderCandidate(
        value=host, source_ref=f"{provider}:domain:page-1:row-{index + 1}",
        observation={"provider_claim": claim, "verification_status": "unverified"},
    )


class _ApiProvider:
    name = ""

    def __init__(self, config: AssetApiProviderConfig, *, api_key: str | None = None) -> None:
        self.config = config
        self.api_key = _api_key(config) if api_key is None else api_key

    async def search_domain(self, domain: str, limiter=None, *,
                            transport: httpx.AsyncBaseTransport | None = None) -> ProviderResult:
        if not self.config.enabled:
            return ProviderResult(self.name, "disabled")
        if not self.api_key:
            return ProviderResult(self.name, "not_configured")
        try:
            if limiter is not None:
                await limiter.acquire()
            async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False,
                    timeout=self.config.timeout_seconds, transport=transport) as client:
                response = await self._request(client, domain)
                response.raise_for_status()
                payload = response.json()
            return self._parse(payload)
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return ProviderResult(self.name, "failed", error="request_failed")

    async def _request(self, client: httpx.AsyncClient, domain: str) -> httpx.Response:
        raise NotImplementedError

    def _parse(self, payload: Any) -> ProviderResult:
        raise NotImplementedError


class HunterProvider(_ApiProvider):
    name = "hunter"

    async def _request(self, client: httpx.AsyncClient, domain: str) -> httpx.Response:
        query = base64.urlsafe_b64encode(f'domain.suffix="{domain}"'.encode()).decode()
        return await client.get(self.config.api_url, params={
            "api-key": self.api_key, "search": query, "page": 1,
            "page_size": self.config.page_size, "is_web": 3,
        }, headers={"Accept": "application/json"})

    def _parse(self, payload: Any) -> ProviderResult:
        if not isinstance(payload, dict) or int(payload.get("code") or 0) != 200:
            return ProviderResult(self.name, "failed", error="provider_rejected_query")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = data.get("arr") if isinstance(data.get("arr"), list) else []
        candidates: list[ProviderCandidate] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            item = _candidate(row.get("domain") or row.get("url") or row.get("web_title"),
                              self.name, index, {
                                  "ip": str(row.get("ip") or "")[:80],
                                  "port": str(row.get("port") or "")[:12],
                                  "protocol": str(row.get("protocol") or "")[:32],
                                  "title": str(row.get("web_title") or "")[:200],
                              })
            if item:
                candidates.append(item)
        total = int(data.get("total") or len(rows))
        return ProviderResult(self.name, "ok", candidates=candidates, returned=total,
                              truncated=total > len(rows))


class QuakeProvider(_ApiProvider):
    name = "quake"

    async def _request(self, client: httpx.AsyncClient, domain: str) -> httpx.Response:
        return await client.post(self.config.api_url, json={
            "query": f'domain: "{domain}"', "start": 0,
            "size": self.config.page_size, "latest": True,
        }, headers={"X-QuakeToken": self.api_key, "Accept": "application/json"})

    def _parse(self, payload: Any) -> ProviderResult:
        if not isinstance(payload, dict) or int(payload.get("code") or 0) not in {0, 200}:
            return ProviderResult(self.name, "failed", error="provider_rejected_query")
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        candidates: list[ProviderCandidate] = []
        for index, row in enumerate(data):
            if not isinstance(row, dict):
                continue
            service = row.get("service") if isinstance(row.get("service"), dict) else {}
            http = service.get("http") if isinstance(service.get("http"), dict) else {}
            item = _candidate(http.get("host") or row.get("domain"), self.name, index, {
                "ip": str(row.get("ip") or "")[:80],
                "port": str(row.get("port") or "")[:12],
                "service": str(service.get("name") or "")[:80],
                "title": str(http.get("title") or "")[:200],
            })
            if item:
                candidates.append(item)
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        total = int(meta.get("pagination", {}).get("total") or len(data)) \
            if isinstance(meta.get("pagination"), dict) else len(data)
        return ProviderResult(self.name, "ok", candidates=candidates, returned=total,
                              truncated=total > len(data))


class ZoomEyeProvider(_ApiProvider):
    name = "zoomeye"

    async def _request(self, client: httpx.AsyncClient, domain: str) -> httpx.Response:
        return await client.get(self.config.api_url, params={
            "query": f'hostname:"{domain}"', "page": 1,
        }, headers={"API-KEY": self.api_key, "Accept": "application/json"})

    def _parse(self, payload: Any) -> ProviderResult:
        if not isinstance(payload, dict) or payload.get("error"):
            return ProviderResult(self.name, "failed", error="provider_rejected_query")
        rows = payload.get("matches") if isinstance(payload.get("matches"), list) else []
        candidates: list[ProviderCandidate] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            portinfo = row.get("portinfo") if isinstance(row.get("portinfo"), dict) else {}
            item = _candidate(portinfo.get("hostname") or row.get("hostname"), self.name, index, {
                "ip": str(row.get("ip") or "")[:80],
                "port": str(portinfo.get("port") or "")[:12],
                "service": str(portinfo.get("service") or "")[:80],
                "title": str(portinfo.get("title") or "")[:200],
            })
            if item:
                candidates.append(item)
        total = int(payload.get("total") or len(rows))
        return ProviderResult(self.name, "ok", candidates=candidates, returned=total,
                              truncated=total > len(rows))
