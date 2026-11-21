"""灵枢原生企业资产来源：企业关系与 ICP 只形成待核验候选。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import deque
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urlsplit

import httpx

from spiritscout.assets.providers.base import ProviderCandidate, ProviderResult
from spiritscout.config.settings import EnterpriseProviderConfig
from spiritscout.core.exceptions import SpiritScoutError

_DOMAIN = re.compile(r"(?<![a-z0-9_-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", re.I)
_QCC_COMPANY = re.compile(r'KeyNo"\s*:\s*"([a-zA-Z0-9]+)"\s*,\s*"Name"\s*:\s*"([^"]+)"')
_QCC_DOMAIN_FIELD = re.compile(
    r'"(?:Domain|DomainName|WebSite|HomeSite|Url|Website)"\s*:\s*"([^"]+)"', re.I)


def effective_enterprise_provider(
        name: str, config: EnterpriseProviderConfig, resources) -> EnterpriseProviderConfig:
    if resources is None:
        return config
    managed = next((row for row in resources.settings("asset_provider")
                    if row["id"] == name), None)
    data = managed["data"] if managed else {}
    return config.model_copy(update={"enabled": data.get("enabled", config.enabled)})


def _cookie(config: EnterpriseProviderConfig) -> str:
    value = os.environ.get(config.cookie_env, "").strip()
    if value:
        return value
    try:
        from spiritscout.web import vault

        return vault.get_key(config.vault_key_name).strip()
    except (OSError, SpiritScoutError, ValueError, TypeError):
        return ""


def _domain(value: Any) -> str:
    text = unescape(str(value or "")).strip()
    parsed = urlsplit(text if "://" in text else "//" + text)
    host = (parsed.hostname or text).split("/")[0].split(":")[0].rstrip(".").lower()
    return host if _DOMAIN.fullmatch(host) else ""


def _percentage(value: Any) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(match.group()) if match else None


def _threshold(rule: str, explicit: float | None) -> float | None:
    return float(explicit) if explicit is not None else _percentage(rule)


def _candidate(provider: str, domain: str, index: int, evidence: dict[str, Any]) -> ProviderCandidate:
    return ProviderCandidate(
        value=domain, source_ref=f"{provider}:icp:row-{index + 1}",
        observation={
            "ownership_evidence": {
                **evidence, "source_provider": provider,
                "verification_status": "unverified",
            },
            "verification_status": "unverified",
        },
    )


@dataclass(frozen=True)
class _Company:
    company_id: str
    name: str
    relation_type: str = "query_match"
    ownership_percentage: float | None = None
    depth: int = 0


class EnterpriseProvider:
    name = "enterprise"

    def __init__(self, config: EnterpriseProviderConfig, *, cookie: str | None = None) -> None:
        self.config = config
        self.cookie = _cookie(config) if cookie is None else cookie

    async def search_organization(
            self, organization: str, *, regions: list[str] | None = None,
            ownership_rule: str = "", ownership_percent: float | None = None,
            deep: int = 1, include_branches: bool = True,
            transport: httpx.AsyncBaseTransport | None = None) -> ProviderResult:
        if not self.config.enabled:
            return ProviderResult(self.name, "disabled")
        if not self.cookie:
            return ProviderResult(self.name, "not_configured")
        try:
            async with httpx.AsyncClient(
                    base_url=self.config.base_url, trust_env=False, follow_redirects=False,
                    timeout=self.config.timeout_seconds, transport=transport,
                    headers={"Cookie": self.cookie, "Accept": "application/json,text/html,*/*",
                             "User-Agent": "Mozilla/5.0 SpiritScout Asset Intelligence"}) as client:
                companies = await self._search(client, organization, regions or [])
                if not companies:
                    return ProviderResult(self.name, "ok", returned=0)
                candidates = await self._collect(
                    client, companies, organization=organization, regions=regions or [],
                    ownership_rule=ownership_rule,
                    threshold=_threshold(ownership_rule, ownership_percent),
                    max_depth=min(max(int(deep or 1), 1), self.config.max_depth),
                    include_branches=include_branches)
            return ProviderResult(
                self.name, "ok", candidates=candidates, returned=len(candidates),
                truncated=len(candidates) >= self.config.max_companies * self.config.page_size)
        except _CredentialRejected:
            return ProviderResult(self.name, "failed", error="credentials_rejected")
        except (httpx.HTTPError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return ProviderResult(self.name, "failed", error="request_failed")

    async def _request(self, client: httpx.AsyncClient, path: str, **kwargs) -> httpx.Response:
        response = await client.get(path, **kwargs)
        if response.status_code in {401, 403} or response.status_code in {301, 302, 303, 307, 308}:
            raise _CredentialRejected
        response.raise_for_status()
        text = response.text.lower()
        if any(marker in text for marker in ("安全验证", "登录后查看", "cookie已过期", "请登录")):
            raise _CredentialRejected
        return response

    async def _search(self, client: httpx.AsyncClient, organization: str,
                      regions: list[str]) -> list[_Company]:
        raise NotImplementedError

    async def _collect(self, client: httpx.AsyncClient, companies: list[_Company], **kwargs) -> list[ProviderCandidate]:
        raise NotImplementedError


class AiqichaProvider(EnterpriseProvider):
    name = "aiqicha"

    async def _search(self, client: httpx.AsyncClient, organization: str,
                      regions: list[str]) -> list[_Company]:
        response = await self._request(client, "/s/advanceFilterAjax", params={
            "q": organization, "p": 1, "s": min(self.config.page_size, 50), "f": "{}"})
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else {}
        rows = data.get("resultList") if isinstance(data, dict) else []
        transform = int(payload.get("ddw") or 0) if isinstance(payload, dict) else 0
        companies = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("entName") or row.get("titleName") or row.get("name") or "").strip()
            address = str(row.get("regAddr") or row.get("district") or row.get("province") or "")
            if regions and not any(region.lower() in address.lower() for region in regions):
                continue
            company_id = _decode_aiqicha_pid(str(row.get("pid") or ""), transform)
            if company_id and name:
                companies.append(_Company(company_id, name))
        return companies[:self.config.max_companies]

    async def _json_list(self, client: httpx.AsyncClient, path: str, pid: str) -> list[dict[str, Any]]:
        response = await self._request(client, path, params={
            "p": 1, "size": self.config.page_size, "pid": pid})
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else {}
        rows = data.get("list") if isinstance(data, dict) else []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    async def _collect(self, client: httpx.AsyncClient, companies: list[_Company], *,
                       organization: str, regions: list[str], ownership_rule: str,
                       threshold: float | None, max_depth: int,
                       include_branches: bool) -> list[ProviderCandidate]:
        queue = deque(companies)
        seen_companies: set[str] = set()
        candidates: list[ProviderCandidate] = []
        seen_domains: set[str] = set()
        while queue and len(seen_companies) < self.config.max_companies:
            company = queue.popleft()
            if company.company_id in seen_companies:
                continue
            seen_companies.add(company.company_id)
            basic_response, icp_rows, invest_rows, branch_rows = await asyncio.gather(
                self._request(client, "/detail/basicAllDataAjax", params={"pid": company.company_id}),
                self._json_list(client, "/detail/icpinfoAjax", company.company_id),
                self._json_list(client, "/detail/investajax", company.company_id),
                self._json_list(client, "/detail/branchajax", company.company_id)
                if include_branches else asyncio.sleep(0, result=[]),
            )
            basic_payload = basic_response.json()
            basic = ((basic_payload.get("data") or {}).get("basicData")
                     if isinstance(basic_payload, dict) else {}) or {}
            evidence = {
                "company_name": str(basic.get("entName") or company.name)[:300],
                "unified_social_credit_code": str(basic.get("taxNo") or "")[:80],
                "region": str(basic.get("regAddr") or "")[:300],
                "status": str(basic.get("openStatus") or "")[:80],
                "legal_person": str(basic.get("legalPerson") or "")[:120],
                "relation_type": company.relation_type,
                "ownership_percentage": company.ownership_percentage,
                "relation_depth": company.depth,
                "organization_query": organization[:300],
                "required_regions": regions[:20],
                "ownership_rule": ownership_rule[:500],
            }
            for row in icp_rows:
                domains = row.get("domain") if isinstance(row.get("domain"), list) else [row.get("domain")]
                for raw in domains:
                    value = _domain(raw or row.get("homeSite"))
                    if not value or value in seen_domains:
                        continue
                    seen_domains.add(value)
                    candidates.append(_candidate(self.name, value, len(candidates), {
                        **evidence, "icp_number": str(row.get("icpNo") or "")[:120],
                        "website_name": str(row.get("siteName") or "")[:200],
                    }))
            if company.depth >= max_depth - 1:
                continue
            for row in invest_rows:
                percentage = _percentage(row.get("regRate"))
                if threshold is not None and (percentage is None or percentage < threshold):
                    continue
                child_id = str(row.get("pid") or "").strip()
                child_name = str(row.get("entName") or "").strip()
                if child_id and child_name:
                    queue.append(_Company(child_id, child_name, "investment", percentage,
                                          company.depth + 1))
            for row in branch_rows:
                child_id = str(row.get("pid") or "").strip()
                child_name = str(row.get("entName") or "").strip()
                if child_id and child_name:
                    queue.append(_Company(child_id, child_name, "branch", None,
                                          company.depth + 1))
        return candidates


class QichachaProvider(EnterpriseProvider):
    name = "qichacha"

    async def _search(self, client: httpx.AsyncClient, organization: str,
                      regions: list[str]) -> list[_Company]:
        response = await self._request(client, "/web/search", params={"key": organization})
        rows = _QCC_COMPANY.findall(response.text)
        return [_Company(company_id, unescape(name))
                for company_id, name in rows[:self.config.max_companies]]

    async def _collect(self, client: httpx.AsyncClient, companies: list[_Company], *,
                       organization: str, regions: list[str], ownership_rule: str,
                       threshold: float | None, max_depth: int,
                       include_branches: bool) -> list[ProviderCandidate]:
        del threshold, max_depth, include_branches
        candidates: list[ProviderCandidate] = []
        seen: set[str] = set()
        for company in companies:
            response = await self._request(client, f"/firm/{company.company_id}.html")
            text = unescape(response.text.replace("\\/", "/"))
            for value in _QCC_DOMAIN_FIELD.findall(text):
                domain = _domain(value)
                if (not domain or domain in seen or domain.endswith("qcc.com")
                        or domain.endswith("qichacha.com")):
                    continue
                seen.add(domain)
                candidates.append(_candidate(self.name, domain, len(candidates), {
                    "company_name": company.name[:300],
                    "relation_type": company.relation_type,
                    "relation_depth": company.depth,
                    "organization_query": organization[:300],
                    "required_regions": regions[:20],
                    "ownership_rule": ownership_rule[:500],
                    "evidence_kind": "company_detail_domain_reference",
                }))
        return candidates


class _CredentialRejected(Exception):
    pass


def _decode_aiqicha_pid(value: str, transform: int) -> str:
    if transform not in {1, 2}:
        return value
    tables = {
        1: str.maketrans("0123456789", "0123547698"),
        2: str.maketrans("0123456789", "0123689457"),
    }
    return value.translate(tables[transform])
