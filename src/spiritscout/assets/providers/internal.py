"""灵枢自有资产收集器。

只使用公开 CT 查询和 DNS 查询，不复制或链接 GPL 工具代码。
"""

from __future__ import annotations

import asyncio
import json
import socket
from urllib.parse import quote

import httpx

from spiritscout.assets.providers.base import (
    OutputCallback,
    ProviderCandidate,
    ProviderResult,
)

_DEFAULT_LABELS = (
    "www", "api", "app", "admin", "admin-dev", "dev", "test", "staging", "stage",
    "uat", "pre", "m", "mobile", "portal", "auth", "sso", "vpn", "oa", "mail",
    "cdn", "static", "img", "assets", "download", "upload", "git", "gitlab",
    "jenkins", "ci", "monitor", "grafana", "kibana", "docker", "registry",
)


class InternalAssetProvider:
    name = "spiritscout_internal"

    def __init__(self, *, timeout_seconds: int = 30, labels: tuple[str, ...] = _DEFAULT_LABELS) -> None:
        self.timeout_seconds = max(5, int(timeout_seconds))
        self.labels = tuple(dict.fromkeys(labels))

    async def collect(self, domain: str, *, brute: bool = False,
                      on_output: OutputCallback | None = None) -> ProviderResult:
        domain = domain.strip().lower().rstrip(".")
        if not domain:
            return ProviderResult(self.name, "failed", error="empty_domain")
        def output(message: str) -> None:
            if on_output:
                on_output(message)

        candidates: dict[str, ProviderCandidate] = {}
        errors: list[str] = []
        output(f"查询证书透明度记录：{domain}")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = await client.get(
                    f"https://crt.sh/?q=%25.{quote(domain)}&output=json",
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                rows = response.json()
            for row in rows if isinstance(rows, list) else []:
                for raw in str(row.get("name_value") or "").splitlines():
                    host = raw.strip().lower().removeprefix("*.").rstrip(".")
                    if host == domain or not host.endswith("." + domain):
                        continue
                    candidates.setdefault(host, self._candidate(host, domain, "ct"))
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors.append(f"ct:{type(exc).__name__}")
            output(f"证书查询失败：{type(exc).__name__}: {exc}")
        output(f"证书查询完成：{len(candidates)} 个候选子域名")
        if brute:
            output(f"开始 DNS 子域枚举：{len(self.labels)} 个名称")
            names = [f"{label}.{domain}" for label in self.labels]
            async def resolve(host: str) -> bool:
                found = await self._resolve(host)
                if found:
                    output(f"DNS 已解析：{host}")
                return found

            results = await asyncio.gather(*(resolve(host) for host in names), return_exceptions=True)
            for host, result in zip(names, results):
                if result is True:
                    candidates.setdefault(host, self._candidate(host, domain, "dns-brute"))
                elif isinstance(result, Exception):
                    errors.append(f"dns:{type(result).__name__}")
        output(f"收集器完成：{len(candidates)} 个候选资产")
        status = "ok" if candidates or not errors else "failed"
        return ProviderResult(self.name, status, candidates=list(candidates.values()),
                              returned=len(candidates), error=";".join(dict.fromkeys(errors))[:500])

    async def _resolve(self, host: str) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM),
                timeout=min(5.0, float(self.timeout_seconds)),
            )
            return True
        except (asyncio.TimeoutError, OSError, socket.gaierror):
            return False

    @staticmethod
    def _candidate(host: str, domain: str, source: str) -> ProviderCandidate:
        return ProviderCandidate(
            value=host,
            source_ref=f"internal:{source}:{host}",
            observation={
                "source": "spiritscout_internal",
                "verification_status": "passive" if source == "ct" else "dns_resolved",
                "scope_status": "candidate_only",
                "related_domain": domain,
                "collection_method": source,
            },
        )
