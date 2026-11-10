"""扫描产出的发布范围；不改变工具层 ScopeGuard 的标注契约。"""
from __future__ import annotations

import ipaddress
from urllib.parse import unquote, urlsplit

from spiritscout.core.models import AgentType, Scope


def _host(value):
    return value.rstrip('.').lower().encode('idna').decode('ascii')


def _within(value: str, scope: Scope) -> bool:
    try:
        if not isinstance(value, str) or not value or any(ord(char) < 33 for char in value):
            return False
        parsed = urlsplit(value if '://' in value else '//' + value)
        host = _host(parsed.hostname or '')
        if not host or parsed.username is not None or parsed.password is not None:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            if any(address in ipaddress.ip_network(cidr, strict=False) for cidr in scope.allowed_cidrs):
                return True
        elif any(host == _host(domain) if not domain.startswith('*.') else
                 host.endswith('.' + _host(domain[2:])) and host != _host(domain[2:])
                 for domain in scope.allowed_domains):
            return True
        if parsed.scheme not in {'http', 'https'}:
            return False
        for allowed in scope.allowed_urls:
            boundary = urlsplit(allowed)
            if (parsed.scheme, host, parsed.port or (443 if parsed.scheme == 'https' else 80)) != (
                    boundary.scheme, _host(boundary.hostname or ''), boundary.port or (443 if boundary.scheme == 'https' else 80)):
                continue
            path, base = unquote(parsed.path or '/'), unquote(boundary.path or '/').rstrip('/')
            # 不把编码分隔符、点路径或反斜线当成可安全发布的路径。
            if '\\' in path or any(ord(char) < 32 for char in path) or any(part in {'.', '..'} for part in path.split('/')) or '%' in path:
                continue
            if not base or path == base or path.startswith(base + '/'):
                return True
        return False
    except (ValueError, UnicodeError):
        return False


class ScopedAssetPublisher:
    def __init__(self, convergence, authorized: Scope, delegated: Scope):
        self.convergence = convergence
        self.authorized = authorized.model_copy(deep=True)
        self.delegated = delegated.model_copy(deep=True)

    def publish(self, asset):
        try:
            if asset.asset_type == 'domain':
                parsed = urlsplit('//' + asset.value)
                if parsed.port is not None or parsed.path or parsed.query or parsed.fragment or parsed.username is not None:
                    return None
            elif asset.asset_type == 'ip':
                ipaddress.ip_address(asset.value)
            elif asset.asset_type in {'url', 'endpoint', 'script'} and urlsplit(asset.value).scheme not in {'http', 'https'}:
                return None
        except ValueError:
            return None
        if not _within(asset.value, self.authorized) or not _within(asset.value, self.delegated):
            return None
        return self.convergence.merge_asset(asset, AgentType.SCANNER)
