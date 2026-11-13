"""FOFA 资产情报适配器；返回值始终是未验证候选。"""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import urlsplit

import httpx

from spiritscout.assets.providers.base import ProviderCandidate, ProviderResult
from spiritscout.config.settings import FofaConfig
from spiritscout.core.exceptions import SpiritScoutError


def effective_fofa(config: FofaConfig, resources) -> FofaConfig:
    """资源控制台的启用状态覆盖系统默认；凭据仍仅来自环境变量或 Vault。"""
    if resources is None:
        return config
    managed = next((row for row in resources.settings("asset_provider")
                    if row["id"] == "fofa"), None)
    data = managed["data"] if managed else {}
    return config.model_copy(update={"enabled": data.get("enabled", config.enabled)})


class FofaProvider:
    name = "fofa"
    fields = "host,ip,port,protocol,title,domain"

    def __init__(self, config, *, credentials: dict[str, str] | None = None) -> None:
        self.config = config
        self.credentials = self._credentials(config) if credentials is None else credentials

    @staticmethod
    def _credentials(config) -> dict[str, str]:
        email = os.environ.get(config.email_env, "").strip()
        key = os.environ.get(config.api_key_env, "").strip()
        if email and key:
            return {"email": email, "key": key}
        try:
            from spiritscout.web import vault

            saved = vault.get_key(config.vault_key_name)
            data = json.loads(saved) if saved else {}
            if isinstance(data, dict):
                email = email or str(data.get("email") or "").strip()
                key = key or str(data.get("key") or data.get("api_key") or "").strip()
        except (OSError, SpiritScoutError, ValueError, TypeError):
            pass
        return {"email": email, "key": key}

    async def search_domain(self, domain: str, limiter=None, *,
                            transport: httpx.AsyncBaseTransport | None = None) -> ProviderResult:
        if not self.config.enabled:
            return ProviderResult(self.name, "disabled")
        if not self.credentials.get("email") or not self.credentials.get("key"):
            return ProviderResult(self.name, "not_configured")
        query = f'domain="{domain}"'
        params = {
            "email": self.credentials["email"],
            "key": self.credentials["key"],
            "qbase64": base64.b64encode(query.encode()).decode(),
            "fields": self.fields,
            "size": self.config.page_size,
            "page": 1,
        }
        try:
            if limiter is not None:
                await limiter.acquire()
            async with httpx.AsyncClient(
                    trust_env=False, follow_redirects=False,
                    timeout=self.config.timeout_seconds, transport=transport) as client:
                response = await client.get(
                    self.config.api_url, params=params,
                    headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict) or payload.get("error"):
                return ProviderResult(self.name, "failed", error="provider_rejected_query")
            rows = payload.get("results") if isinstance(payload.get("results"), list) else []
        except (httpx.HTTPError, ValueError, TypeError):
            return ProviderResult(self.name, "failed", error="request_failed")
        candidates: list[ProviderCandidate] = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            values = [*row, *([""] * max(0, 6 - len(row)))]
            host, ip, port, protocol, title, row_domain = values[:6]
            value = str(row_domain or "").strip().rstrip(".").lower()
            if not value:
                parsed = urlsplit(str(host) if "://" in str(host) else "//" + str(host))
                value = (parsed.hostname or "").rstrip(".").lower()
            if not value or value in seen:
                continue
            seen.add(value)
            candidates.append(ProviderCandidate(
                value=value, source_ref=f"fofa:page-1:row-{index + 1}",
                observation={
                    "provider_claim": {
                        "ip": str(ip)[:80], "port": str(port)[:12],
                        "protocol": str(protocol)[:32], "title": str(title)[:200],
                    },
                    "verification_status": "unverified",
                },
            ))
        total = int(payload.get("size") or len(rows))
        return ProviderResult(
            self.name, "ok", candidates=candidates, returned=total,
            truncated=total > len(rows),
        )
