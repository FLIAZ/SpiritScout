"""SRC 并行只读基线；发布范围和观测证据分别保存。"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import secrets
import ssl
from urllib.parse import urlsplit

import httpx

from spiritscout.agents.scoped_assets import ScopedAssetPublisher, _within
from spiritscout.assets.cache import ProviderCache
from spiritscout.assets.models import AssetStage
from spiritscout.assets.providers.cyberspace import (
    HunterProvider,
    QuakeProvider,
    ZoomEyeProvider,
    effective_api_provider,
)
from spiritscout.assets.providers.enterprise import (
    AiqichaProvider,
    QichachaProvider,
    effective_enterprise_provider,
)
from spiritscout.assets.providers.fofa import FofaProvider, effective_fofa
from spiritscout.assets.providers.oneforall import OneForAllProvider
from spiritscout.assets.service import AssetCandidateService
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, Asset, BlackboardLayer, Scope
from spiritscout.traffic.proxy import tool_environment


def delegated_scope(target):
    if '://' in target:
        return Scope(allowed_urls=[target])
    try:
        return Scope(allowed_cidrs=[str(ipaddress.ip_network(target, strict=False))])
    except ValueError:
        return Scope(allowed_domains=[target])


async def discover_organization(infra, query: dict) -> dict:
    """组织线索只生成归属候选；没有明确网络范围时不做 DNS/HTTP 测试。"""
    organization = str(query.get('organization') or '').strip()
    regions = [str(value).strip() for value in query.get('regions', [])
               if isinstance(value, str) and value.strip()]
    ownership_rule = str(query.get('ownership_rule') or '').strip()
    if not organization:
        return {'error': '组织资产查询缺少公司或机构名称'}
    resources = getattr(infra, 'resources', None)
    fofa = effective_fofa(infra.cfg.asset_intelligence.fofa, resources)
    aiqicha = effective_enterprise_provider(
        'aiqicha', infra.cfg.asset_intelligence.aiqicha, resources)
    qichacha = effective_enterprise_provider(
        'qichacha', infra.cfg.asset_intelligence.qichacha, resources)
    fofa_result, aiqicha_result, qichacha_result = await asyncio.gather(
        FofaProvider(fofa).search_organization(
            organization, regions=regions, ownership_rule=ownership_rule,
            limiter=infra.builtin_ctx.get('limiter')),
        AiqichaProvider(aiqicha).search_organization(
            organization, regions=regions, ownership_rule=ownership_rule,
            ownership_percent=query.get('ownership_percent'),
            deep=int(query.get('deep') or 1),
            include_branches=bool(query.get('include_branches', True))),
        QichachaProvider(qichacha).search_organization(
            organization, regions=regions, ownership_rule=ownership_rule,
            ownership_percent=query.get('ownership_percent'),
            deep=int(query.get('deep') or 1),
            include_branches=bool(query.get('include_branches', True))),
    )
    service = AssetCandidateService(infra.blackboard, actor='scanner')
    candidates = []
    for result in (fofa_result, aiqicha_result, qichacha_result):
        for item in result.candidates[:infra.cfg.asset_intelligence.max_candidates_per_provider]:
            ownership_evidence = (item.observation or {}).get('ownership_evidence')
            icp_supported = result.provider in {'aiqicha', 'qichacha'} and isinstance(ownership_evidence, dict)
            observation = dict(item.observation or {})
            if icp_supported:
                observation.update({
                    'ownership_status': 'ownership_supported',
                    'ownership_confidence': 0.9,
                    'ownership_basis': 'ICP备案主体与组织查询匹配',
                })
            candidate = service.upsert(
                item.value, asset_type=item.asset_type,
                stage=AssetStage.OWNERSHIP_SUPPORTED if icp_supported else AssetStage.EXTERNAL_CANDIDATE,
                provider=result.provider, source_ref=item.source_ref,
                observation=observation,
                unknowns=(['待按项目规则确认测试资格', '尚未获得用户明确网络测试授权']
                          if icp_supported else
                          ['待按组织、地区与控股规则核验归属', '尚未获得用户明确网络测试授权']),
            )
            if candidate.value not in candidates:
                candidates.append(candidate.value)
    observations = {
        'fofa': fofa_result.summary(),
        'aiqicha': aiqicha_result.summary(),
        'qichacha': qichacha_result.summary(),
        'ownership_query': {
            'organization': organization, 'regions': regions,
            'ownership_rule': ownership_rule,
            'policy': 'candidate_only_no_network_probe',
        },
    }
    infra.blackboard.write(
        BlackboardLayer.RECON_CLUE, new_id('discovery'),
        {'clue_type': 'organization_asset_discovery', 'value': organization,
         'observations': observations, 'candidates': candidates,
         'verification_status': 'unverified'},
        agent_type=AgentType.SCANNER, updated_by='scanner')
    return {'published': [], 'candidates': candidates, 'observations': observations}


async def discover(infra, target, *, on_progress=None):
    from pathlib import Path

    boundary = delegated_scope(target)
    publisher = ScopedAssetPublisher(infra.convergence, infra.authorization.scope, boundary)
    candidate_service = AssetCandidateService(infra.blackboard)
    wildcard_target = '://' not in target and target.startswith('*.')
    host = urlsplit(target if '://' in target else '//' + target.lstrip('*.')).hostname
    if not host:
        return {'error': '无有效主机目标'}
    env = await tool_environment(Path(infra.cfg.workspace.root))
    options = {'trust_env': False, 'timeout': 15, 'follow_redirects': False}
    if env:
        options.update(proxy=env['HTTPS_PROXY'], verify=ssl.create_default_context(cafile=env['SSL_CERT_FILE']))
    observations: dict[str, object] = {}
    candidate_limit = infra.cfg.asset_intelligence.max_candidates_per_provider
    verification_batch_size = infra.cfg.asset_intelligence.verification_batch_size
    verification_concurrency = infra.cfg.asset_intelligence.verification_concurrency
    provider_cache = ProviderCache(
        Path(getattr(infra.cfg.workspace, "guard_root", "")
             or infra.cfg.workspace.root) / "asset-provider-cache.sqlite3",
        ttl_seconds=infra.cfg.asset_intelligence.provider_cache_ttl_seconds,
        max_entries=infra.cfg.asset_intelligence.provider_cache_max_entries,
    )
    domain_discovery = '://' not in target
    try:
        ipaddress.ip_address(host)
        passive_domain = False
    except ValueError:
        passive_domain = bool(host) and infra.authorization.scope.scope_mode != "explicit"
    queued: set[str] = set()
    queued_by_provider: dict[str, int] = {}
    verified_domains: set[str] = set()
    direct_published = False
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    progress_events = 0
    execution_stats = {
        'queued_candidates': 0, 'verification_batches': 0,
        'dns_verified': 0, 'live_endpoints': 0,
        'tool_cache_hits': 0, 'tool_shared_hits': 0,
        'provider_cache_hits': 0,
    }

    async def progress(kind: str, **detail) -> None:
        nonlocal progress_events
        progress_events += 1
        observations.setdefault('rolling_progress', []).append({
            'kind': kind, **detail,
        })
        if on_progress is None or (progress_events > 12 and progress_events % 25):
            return
        result = on_progress({'kind': kind, **detail})
        if inspect.isawaitable(result):
            await result

    def register_candidate(name: str, *, provider: str, source_ref: str,
                           observation: dict | None = None, passive: bool = False) -> None:
        value = str(name or '').strip().lower().rstrip('.')
        if not value or '*' in value:
            return
        try:
            ipaddress.ip_address(value)
            asset_type = 'ip'
        except ValueError:
            asset_type = 'domain'
        explicit_scope = (_within(value, infra.authorization.scope)
                          and _within(value, boundary))
        boundary_match = _within(value, boundary)
        organization_candidate = (
            infra.authorization.scope.scope_mode != "explicit"
            and (boundary_match or passive)
            and asset_type == "domain"
            and any(value == pattern or (pattern.startswith("*.")
                                        and value.endswith("." + pattern[2:]))
                    for pattern in infra.authorization.scope.organization_domains)
        )
        candidate_service.upsert(
            value, asset_type=asset_type,
            stage=(AssetStage.IN_SCOPE if explicit_scope else AssetStage.OWNERSHIP_SUPPORTED
                   if organization_candidate else AssetStage.EXTERNAL_CANDIDATE),
            provider=provider, source_ref=source_ref,
            observation={**(observation or {'verification_status': 'unverified'}),
                         'scope_status': 'allowed_test' if explicit_scope else
                         'candidate_only' if organization_candidate else 'blocked'},
            unknowns=["待核验组织归属" if organization_candidate else "待执行本地 DNS/HTTP 核验"],
        )
        provider_count = queued_by_provider.get(provider, 0)
        # 组织归属候选只入库和进入待核验队列，不主动发 DNS/HTTP 请求。
        if (not explicit_scope or asset_type != 'domain' or value in queued
                or (provider != 'target_declaration'
                    and provider_count >= candidate_limit)):
            return
        queued.add(value)
        execution_stats['queued_candidates'] += 1
        queued_by_provider[provider] = provider_count + 1
        queue.put_nowait(value)

    async def probe_url(name: str, url: str) -> None:
        result = await infra.tools.call(
            'http_probe', {'url': url, 'max_redirects': 0}, **infra.builtin_ctx)
        info = result.parsed or {}
        if result.reuse == 'cache':
            execution_stats['tool_cache_hits'] += 1
        elif result.reuse == 'shared':
            execution_stats['tool_shared_hits'] += 1
        observations.setdefault('http_endpoints', []).append({
            'url': url, 'ok': result.ok, 'invocation_id': result.invocation_id,
            'status_code': info.get('status_code'), 'final_url': info.get('final_url'),
            'redirect_not_followed': info.get('redirect_not_followed', False),
            'headers': info.get('headers') if isinstance(info.get('headers'), dict) else {},
            'error': result.error,
        })
        if not result.ok:
            return
        execution_stats['live_endpoints'] += 1
        fingerprint = info.get('fingerprint') if isinstance(info, dict) else {}
        candidate = candidate_service.upsert(
            name, asset_type='domain',
            stage=(AssetStage.FINGERPRINTED if fingerprint
                   else AssetStage.LIVE_VERIFIED),
            provider='http_probe', source_ref=result.invocation_id or '',
            observation={'url': url, 'status_code': info.get('status_code'),
                         'final_url': info.get('final_url'),
                         'fingerprint': fingerprint,
                         'verification_status': 'verified'},
            unknowns=[],
        )
        publisher.publish(Asset(
            asset_id=new_id('asset'), asset_type='url', value=url,
            source_agent='scanner', fingerprint=info,
            attrs={'sources': ['http_probe'],
                   'status_code': info.get('status_code'),
                   'final_url': info.get('final_url'),
                   'candidate_priority_score': candidate.priority_score},
        ))
        await progress(
            'live_asset', value=name, url=url,
            status_code=info.get('status_code'),
            final_url=info.get('final_url'),
            priority_score=candidate.priority_score,
        )

    async def verify_candidate(name: str) -> None:
        result = await infra.tools.call('dns_resolve', {'host': name}, **infra.builtin_ctx)
        if result.reuse == 'cache':
            execution_stats['tool_cache_hits'] += 1
        elif result.reuse == 'shared':
            execution_stats['tool_shared_hits'] += 1
        observations.setdefault('dns_candidates', []).append({
            'host': name, 'ok': result.ok, 'invocation_id': result.invocation_id,
            'ips': (result.parsed or {}).get('ips', []), 'error': result.error,
        })
        if not result.ok:
            return
        execution_stats['dns_verified'] += 1
        candidate = candidate_service.upsert(
            name, asset_type='domain', stage=AssetStage.DNS_VERIFIED,
            provider='dns_resolve', source_ref=result.invocation_id or '',
            observation={'ips': (result.parsed or {}).get('ips', []),
                         'verification_status': 'verified'},
            unknowns=["待执行 HTTP 存活核验"],
        )
        publisher.publish(Asset(
            asset_id=new_id('asset'), asset_type='domain', value=name,
            source_agent='scanner', attrs={'sources': ['dns_resolve'],
                                           'dns_invocation_id': result.invocation_id,
                                           'candidate_priority_score': candidate.priority_score},
        ))
        verified_domains.add(name)
        await progress(
            'dns_verified', value=name, ips=(result.parsed or {}).get('ips', []),
            priority_score=candidate.priority_score,
        )
        authority = '[' + name + ']' if ':' in name else name
        urls = [f'{scheme}://{authority}/' for scheme in ('https', 'http')]
        async with asyncio.TaskGroup() as group:
            for url in urls:
                if _within(url, infra.authorization.scope) and _within(url, boundary):
                    group.create_task(probe_url(name, url))

    async def verifier() -> None:
        while True:
            first = await queue.get()
            if first is None:
                queue.task_done()
                return
            names = [first]
            while len(names) < verification_batch_size:
                try:
                    candidate = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if candidate is None:
                    queue.task_done()
                    continue
                names.append(candidate)
            execution_stats['verification_batches'] += 1
            try:
                for name in names:
                    try:
                        await verify_candidate(name)
                    except Exception as exc:  # 单个候选失败不能终止整个滚动队列
                        observations.setdefault('candidate_errors', []).append({
                            'host': name, 'error': type(exc).__name__,
                        })
            finally:
                for _ in names:
                    queue.task_done()

    workers: list[asyncio.Task] = []
    if domain_discovery:
        if not wildcard_target:
            register_candidate(
                host, provider='target_declaration', source_ref=f'target:{host}',
                observation={'verification_status': 'declared'},
            )
        workers = [asyncio.create_task(verifier())
                   for _ in range(min(verification_concurrency, candidate_limit))]
    try:
        async with httpx.AsyncClient(**options) as client:
            async def query(name, url, params, *, attempts: int = 1):
                import json

                errors: list[str] = []
                for attempt in range(1, attempts + 1):
                    try:
                        await infra.builtin_ctx['limiter'].acquire()
                        async with client.stream(
                                'GET', url, params=params,
                                headers={'Accept-Encoding': 'identity'}) as response:
                            response.raise_for_status()
                            body = bytearray()
                            async for chunk in response.aiter_raw():
                                body.extend(chunk)
                                if len(body) > 2 * 1024 * 1024:
                                    raise ValueError('响应超过 2 MiB')
                            observations[name] = json.loads(body)
                            if errors:
                                observations.setdefault('provider_retries', {})[name] = errors
                            return observations[name]
                    except (httpx.HTTPError, ValueError) as exc:
                        errors.append(type(exc).__name__)
                        if attempt < attempts:
                            await asyncio.sleep(0)
                observations[name] = {'error': errors[-1] if errors else 'unknown'}
                return observations[name]

            jobs = ([query(record, 'https://dns.google/resolve',
                           {'name': host, 'type': record})
                     for record in ('A', 'AAAA', 'CNAME', 'NS', 'MX', 'TXT')]
                    if domain_discovery else [])
            if passive_domain or (domain_discovery and wildcard_target):
                async def query_passive():
                    payload = await query('passive_subdomains', 'https://crt.sh/', {
                        'q': '%.' + host, 'output': 'json'}, attempts=2)
                    candidates: set[str] = set()
                    if isinstance(payload, list):
                        for row in payload:
                            if isinstance(row, dict):
                                candidates.update(str(row.get('name_value', '')).splitlines())
                    if not candidates:
                        fallback = await query(
                            'certificate_transparency_fallback',
                            'https://api.certspotter.com/v1/issuances',
                            {'domain': host, 'include_subdomains': 'true',
                             'expand': 'dns_names'}, attempts=2)
                        if isinstance(fallback, list):
                            for row in fallback:
                                if isinstance(row, dict):
                                    candidates.update(str(value) for value in row.get('dns_names', [])
                                                      if isinstance(value, str))
                    for value in sorted(candidates)[:candidate_limit]:
                        register_candidate(
                            value, provider='certificate_transparency',
                            source_ref=f'ct:{host}',
                            observation={'verification_status': 'passive', 'source': 'certificate_transparency'},
                            passive=True,
                        )
                    observations['passive_subdomains'] = {
                        'candidate_count': len(candidates),
                        'fallback_used': bool(observations.get('certificate_transparency_fallback')),
                    }
                jobs.append(query_passive())

                oneforall_config = infra.cfg.asset_intelligence.oneforall
                if oneforall_config.enabled and oneforall_config.path:
                    async def query_oneforall():
                        result = await OneForAllProvider(
                            oneforall_config.path,
                            python=oneforall_config.python,
                            timeout_seconds=oneforall_config.timeout_seconds,
                        ).collect(host)
                        observations['oneforall'] = result.summary()
                        for candidate in result.candidates[:candidate_limit]:
                            register_candidate(
                                candidate.value, provider='oneforall',
                                source_ref=candidate.source_ref,
                                observation=candidate.observation, passive=True,
                            )
                    jobs.append(query_oneforall())

                async def wildcard_baseline():
                    random_host = f"ss-{secrets.token_hex(8)}.{host}"
                    dns = await infra.tools.call(
                        'dns_resolve', {'host': random_host}, **infra.builtin_ctx)
                    probe_results = []
                    for scheme in ('https', 'http'):
                        result = await infra.tools.call(
                            'http_probe', {'url': f'{scheme}://{random_host}/',
                                           'max_redirects': 0}, **infra.builtin_ctx)
                        probe_results.append({
                            'url': f'{scheme}://{random_host}/', 'ok': result.ok,
                            'status_code': (result.parsed or {}).get('status_code'),
                            'invocation_id': result.invocation_id, 'error': result.error,
                        })
                    observations['wildcard_baseline'] = {
                        'host': random_host, 'dns_ok': dns.ok,
                        'ips': (dns.parsed or {}).get('ips', []),
                        'http': probe_results,
                        'wildcard_suspected': bool(dns.ok or any(item['ok'] for item in probe_results)),
                        'verification_status': 'verified',
                    }
                if domain_discovery and wildcard_target:
                    jobs.append(wildcard_baseline())

                async def query_fofa():
                    selected = effective_fofa(
                        infra.cfg.asset_intelligence.fofa,
                        getattr(infra, 'resources', None))
                    cache_key = "fofa:" + hashlib.sha256(
                        f"{selected.api_url}|{selected.page_size}|domain:{host}".encode()
                    ).hexdigest()
                    provider = FofaProvider(selected)
                    result = provider_cache.get(cache_key) if selected.enabled else None
                    if result is None:
                        result = await provider.search_domain(
                            host, infra.builtin_ctx.get('limiter'))
                        provider_cache.put(cache_key, result)
                    else:
                        execution_stats['provider_cache_hits'] += 1
                    observations['fofa'] = result.summary()
                    for candidate in result.candidates[:candidate_limit]:
                        register_candidate(
                            candidate.value, provider=result.provider,
                            source_ref=candidate.source_ref,
                            observation=candidate.observation, passive=True,
                        )
                jobs.append(query_fofa())
                for provider_name, config, provider_type in (
                    ('hunter', infra.cfg.asset_intelligence.hunter, HunterProvider),
                    ('quake', infra.cfg.asset_intelligence.quake, QuakeProvider),
                    ('zoomeye', infra.cfg.asset_intelligence.zoomeye, ZoomEyeProvider),
                ):
                    async def query_provider(name=provider_name, selected_config=config,
                                             provider_cls=provider_type):
                        selected = effective_api_provider(
                            name, selected_config, getattr(infra, 'resources', None))
                        cache_key = name + ":" + hashlib.sha256(
                            f"{selected.api_url}|{selected.page_size}|domain:{host}".encode()
                        ).hexdigest()
                        result = provider_cache.get(cache_key) if selected.enabled else None
                        if result is None:
                            result = await provider_cls(selected).search_domain(
                                host, infra.builtin_ctx.get('limiter'))
                            provider_cache.put(cache_key, result)
                        else:
                            execution_stats['provider_cache_hits'] += 1
                        observations[name] = result.summary()
                        for candidate in result.candidates[:candidate_limit]:
                            register_candidate(
                                candidate.value, provider=result.provider,
                                source_ref=candidate.source_ref,
                                observation=candidate.observation, passive=True,
                            )
                    jobs.append(query_provider())
            if not domain_discovery and _within(target, infra.authorization.scope):
                async def probe():
                    nonlocal direct_published
                    result = await infra.tools.call(
                        'http_probe', {'url': target, 'max_redirects': 0},
                        **infra.builtin_ctx)
                    info = result.parsed if isinstance(result.parsed, dict) else {}
                    observations['http'] = {
                        'ok': result.ok, 'result': info,
                        'invocation_id': result.invocation_id, 'error': result.error,
                    }
                    if not result.ok:
                        return
                    candidate = candidate_service.upsert(
                        target, asset_type='url',
                        stage=(AssetStage.FINGERPRINTED if info.get('fingerprint')
                               else AssetStage.LIVE_VERIFIED),
                        provider='http_probe', source_ref=result.invocation_id or '',
                        observation={'status_code': info.get('status_code'),
                                     'final_url': info.get('final_url'),
                                     'verification_status': 'verified'},
                        unknowns=[],
                    )
                    publisher.publish(Asset(
                        asset_id=new_id('asset'), asset_type='url', value=target,
                        source_agent='scanner', fingerprint=info,
                        attrs={'sources': ['http_probe'],
                               'status_code': info.get('status_code'),
                               'final_url': info.get('final_url'),
                               'candidate_priority_score': candidate.priority_score},
                    ))
                    direct_published = True
                    await progress(
                        'live_asset', value=host, url=target,
                        status_code=info.get('status_code'),
                        final_url=info.get('final_url'),
                        priority_score=candidate.priority_score,
                    )
                jobs.append(probe())
            async with asyncio.TaskGroup() as group:
                for job in jobs:
                    group.create_task(job)
            # 被动同 IP 情报：只调用测绘 Provider，不向旁站发 HTTP/TCP 请求。
            ip_values = []
            for record in ('A', 'AAAA'):
                for answer in (observations.get(record) or {}).get('Answer', []):
                    value = str(answer.get('data') or '').strip()
                    try:
                        ipaddress.ip_address(value)
                    except ValueError:
                        continue
                    if value not in ip_values:
                        ip_values.append(value)
            selected_fofa = effective_fofa(
                infra.cfg.asset_intelligence.fofa, getattr(infra, 'resources', None))
            ip_neighbors = []
            if selected_fofa.enabled and ip_values:
                fofa = FofaProvider(selected_fofa)
                for ip in ip_values[:8]:
                    result = await fofa.search_ip(ip, infra.builtin_ctx.get('limiter'))
                    ip_neighbors.append({'ip': ip, 'result': result.summary()})
                    for candidate in result.candidates[:candidate_limit]:
                        register_candidate(
                            candidate.value, provider='fofa_reverse_ip',
                            source_ref=candidate.source_ref,
                            observation={**candidate.observation, 'relation': 'same_ip',
                                         'related_ip': ip, 'related_target': host},
                            passive=True,
                        )
            observations['passive_ip_neighbors'] = ip_neighbors
        if workers:
            await queue.join()
    finally:
        for _ in workers:
            queue.put_nowait(None)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
    cname_rows = (observations.get('CNAME') or {}).get('Answer', [])
    suffixes = {'.cloudfront.net': 'CloudFront', '.akamai.net': 'Akamai',
                '.akamaiedge.net': 'Akamai', '.cloudflare.net': 'Cloudflare'}
    indicators = []
    for answer in cname_rows:
        value = str(answer.get('data', '')).lower().rstrip('.')
        for suffix, vendor in suffixes.items():
            if value.endswith(suffix):
                indicators.append({'candidate': vendor, 'evidence': {'type': 'CNAME', 'value': value}})
    headers = (((observations.get('http') or {}).get('result') or {}).get('headers') or {})
    for key in headers:
        if key.lower() in {'cf-ray', 'x-sucuri-id', 'x-akamai-transformed'}:
            indicators.append({'candidate': 'CDN/WAF header indicator', 'evidence': {'header_name': key}})
    for endpoint in observations.get('http_endpoints', []):
        if not isinstance(endpoint, dict) or not endpoint.get('ok'):
            continue
        result_headers = endpoint.get('headers') if isinstance(endpoint.get('headers'), dict) else {}
        for key in result_headers:
            if key.lower() in {'cf-ray', 'x-sucuri-id', 'x-akamai-transformed'}:
                indicators.append({'candidate': 'CDN/WAF header indicator',
                                   'evidence': {'header_name': key,
                                                'url': endpoint.get('url')}})
    observations['cdn_waf'] = {'indicators': indicators, 'verification_status': 'unverified',
                               'note': '命中仅为静态特征线索，不证明部署或防护能力；不探测源站 IP'}
    observations['batch_execution'] = execution_stats
    published = sorted(verified_domains) if domain_discovery else (
        [target] if direct_published else [])
    # CNAME/NS/MX 和解析 IP 仅为证据，绝不自动加入后续目标队列。
    infra.blackboard.write(BlackboardLayer.RECON_CLUE, new_id('discovery'),
        {'clue_type': 'asset_discovery', 'value': target, 'observations': observations,
         'published': published, 'verification_status': 'unverified'},
        agent_type=AgentType.SCANNER, updated_by='scanner')
    return {'published': published, 'observations': observations}
