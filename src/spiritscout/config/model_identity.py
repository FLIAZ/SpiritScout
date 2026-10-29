"""配置身份与端点模型名分离；裸名仅在唯一匹配时兼容。"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from spiritscout.core.exceptions import ConfigurationError


def model_key(entry: Any) -> str:
    get = entry.get if isinstance(entry, Mapping) else lambda key, default='': getattr(entry, key, default)
    name, provider = str(get('model', '')), str(get('provider_ref', ''))
    if not provider:
        return name
    pair = json.dumps([provider, name], ensure_ascii=False, separators=(',', ':')).encode()
    return '@' + base64.urlsafe_b64encode(pair).decode().rstrip('=')


def resolve_model(entries: list[Any], reference: str) -> Any:
    exact = [entry for entry in entries if model_key(entry) == reference]
    if len(exact) == 1:
        return exact[0]
    matches = [entry for entry in entries if (entry.get('model') if isinstance(entry, Mapping)
                                            else entry.model) == reference]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 or len(exact) > 1:
        raise ConfigurationError(f'模型名存在多个供应商，请指定完整模型标识: {reference}')
    raise ConfigurationError(f'未注册的模型: {reference}')
