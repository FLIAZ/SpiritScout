"""SRC 并行只读基线；发布范围和观测证据分别保存。"""
from __future__ import annotations

import asyncio
import ipaddress
import ssl
from collections import deque
from urllib.parse import urlsplit

import httpx

from spiritscout.agents.scoped_assets import ScopedAssetPublisher, _within
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


async def discover(infra, target):
    from pathlib import Path

    boundary = delegated_scope(target)
    publisher = ScopedAssetPublisher(infra.convergence, infra.authorization.scope, boundary)
    host = urlsplit(target if '://' in target else '//' + target.lstrip('*.')).hostname
    if not host:
        return {'error': '无有效主机目标'}
    env = await tool_environment(Path(infra.cfg.workspace.root))
    options = {'trust_env': False, 'timeout': 15, 'follow_redirects': False}
    if env:
        options.update(proxy=env['HTTPS_PROXY'], verify=ssl.create_default_context(cafile=env['SSL_CERT_FILE']))
    observations = {}
    async with httpx.AsyncClient(**options) as client:
        async def query(name, url, params):
            try:
                await infra.builtin_ctx['limiter'].acquire()
                async with client.stream('GET', url, params=params, headers={'Accept-Encoding': 'identity'}) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            raise ValueError('响应超过 2 MiB')
                    import json

                    observations[name] = json.loads(body)
            except (httpx.HTTPError, ValueError) as exc:
                observations[name] = {'error': type(exc).__name__}

        jobs = [query(record, 'https://dns.google/resolve', {'name': host, 'type': record})
                for record in ('A', 'AAAA', 'CNAME', 'NS', 'MX', 'TXT')]
        if target.startswith('*.') and _within('candidate.' + host, infra.authorization.scope):
            jobs.append(query('passive_subdomains', 'https://crt.sh/', {'q': '%.' + host, 'output': 'json'}))
        if '://' in target and _within(target, infra.authorization.scope):
            async def probe():
                result = await infra.tools.call('http_probe', {'url': target, 'max_redirects': 0}, **infra.builtin_ctx)
                observations['http'] = {'ok': result.ok, 'result': result.parsed, 'invocation_id': result.invocation_id}
                if result.ok:
                    publisher.publish(Asset(asset_id=new_id('asset'), asset_type='url', value=target,
                        source_agent='scanner', fingerprint=result.parsed or {}))
            jobs.append(probe())
        async with asyncio.TaskGroup() as group:
            for job in jobs:
                group.create_task(job)
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
    observations['cdn_waf'] = {'indicators': indicators, 'verification_status': 'unverified',
                               'note': '命中仅为静态特征线索，不证明部署或防护能力；不探测源站 IP'}
    candidates = set()
    if isinstance(observations.get('passive_subdomains'), list):
        for row in observations['passive_subdomains']:
            if isinstance(row, dict):
                candidates.update(str(row.get('name_value', '')).splitlines())
        observations['passive_subdomains'] = {'candidate_count': len(candidates)}
    if _within(host, boundary):
        candidates.add(host)
    published = []
    for name in sorted(candidates):
        if '*' in name:
            continue
        try:
            ipaddress.ip_address(name)
            asset_type = 'ip'
        except ValueError:
            asset_type = 'domain'
        result = publisher.publish(Asset(asset_id=new_id('asset'), asset_type=asset_type, value=name,
            source_agent='scanner', attrs={'discovery': observations if name == host else {'source': 'certificate_transparency'}}))
        if result:
            published.append(name)
    if '://' not in target and published:
        # 证书记录只提供候选身份。对已通过双重范围校验的名称补充存活与指纹，
        # 有界工作器避免为大量被动记录同时创建网络请求。
        urls = deque()
        for name in published:
            authority = '[' + name + ']' if ':' in name else name
            for scheme in ('https', 'http'):
                url = f'{scheme}://{authority}/'
                if _within(url, infra.authorization.scope) and _within(url, boundary):
                    urls.append(url)
        endpoints = []
        async def worker():
            while urls:
                url = urls.popleft()
                result = await infra.tools.call('http_probe', {'url': url, 'max_redirects': 0}, **infra.builtin_ctx)
                info = result.parsed or {}
                endpoints.append({'url': url, 'ok': result.ok, 'invocation_id': result.invocation_id,
                                  'status_code': info.get('status_code'), 'error': result.error})
                if result.ok:
                    publisher.publish(Asset(asset_id=new_id('asset'), asset_type='url', value=url,
                        source_agent='scanner', fingerprint=info))
                    for key in info.get('headers', {}):
                        if key.lower() in {'cf-ray', 'x-sucuri-id', 'x-akamai-transformed'}:
                            indicators.append({'candidate': 'CDN/WAF header indicator',
                                               'evidence': {'header_name': key, 'url': url}})
        async with asyncio.TaskGroup() as group:
            for _ in range(min(4, len(urls))):
                group.create_task(worker())
        observations['http_endpoints'] = endpoints
    # CNAME/NS/MX 和解析 IP 仅为证据，绝不自动加入后续目标队列。
    infra.blackboard.write(BlackboardLayer.RECON_CLUE, new_id('discovery'),
        {'clue_type': 'asset_discovery', 'value': target, 'observations': observations,
         'published': published, 'verification_status': 'unverified'},
        agent_type=AgentType.SCANNER, updated_by='scanner')
    return {'published': published, 'observations': observations}
