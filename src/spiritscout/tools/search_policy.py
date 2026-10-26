"""网页搜索的能力边界；智能体竞赛禁止出网检索，独立于操作预授权。"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from spiritscout.core.models import RunMode

_SEARCH_SERVERS = {'exa', 'tavily', 'brave', 'brave_search', 'brave-search', 'serper', 'searxng'}
_SEARCH_TOOLS = {'web_search', 'web_search_exa', 'web_search_advanced_exa', 'get_code_context_exa',
                 'brave_web_search', 'tavily_search', 'tavily-search', 'search_web', 'internet_search'}


def is_search_tool(name: str) -> bool:
    parts = str(name).lower().split('__')
    return parts[-1] in _SEARCH_TOOLS or (len(parts) >= 3 and parts[1] in _SEARCH_SERVERS)


def is_search_server(server: Any) -> bool:
    if getattr(server, 'internet_search', False) or server.name.lower() in _SEARCH_SERVERS:
        return True
    host = (urlsplit(server.url).hostname or '').lower()
    return any(host == domain or host.endswith('.' + domain) for domain in (
        'exa.ai', 'tavily.com', 'search.brave.com', 'serper.dev'))


def search_allowed(ctx: Any) -> bool:
    mode = getattr(ctx, 'mode', None) or getattr(getattr(ctx, 'policy', None), 'mode', None)
    return mode != RunMode.COMPETITION
