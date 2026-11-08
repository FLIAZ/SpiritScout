"""火山个人套餐只读 OpenAPI：查询凭据与推理 Key 分开，固定主机与 Action。"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import httpx

from spiritscout.config.settings import ProviderEntry
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.web import vault


def sign_headers(access_key: str, secret: str, host: str, query: str, body: bytes,
                 now: datetime | None = None) -> dict[str, str]:
    now = now or datetime.now(timezone.utc)
    date = now.strftime('%Y%m%dT%H%M%SZ')
    digest = hashlib.sha256(body).hexdigest()
    scope = f'{date[:8]}/cn-beijing/ark/request'
    signed = 'content-type;host;x-content-sha256;x-date'
    canonical = (f'POST\n/\n{query}\ncontent-type:application/json\nhost:{host}\n'
                 f'x-content-sha256:{digest}\nx-date:{date}\n\n{signed}\n{digest}')
    to_sign = f'HMAC-SHA256\n{date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    key = secret.encode()
    for item in (date[:8], 'cn-beijing', 'ark', 'request'):
        key = hmac.new(key, item.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {'Content-Type': 'application/json', 'Host': host, 'X-Date': date, 'X-Content-Sha256': digest,
            'Authorization': f'HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}'}


async def fetch_ark(client: httpx.AsyncClient, endpoint: str, provider: ProviderEntry) -> httpx.Response:
    credentials = vault.get_quota_credentials(provider.name)
    if not credentials.get('access_key_id') or not credentials.get('secret_access_key'):
        raise SpiritScoutError('火山套餐查询需单独配置 Access Key ID / Secret，推理 API Key 不具备查询权限')
    action = 'GetCodingPlanUsage' if '/api/coding' in urlsplit(provider.base_url).path else 'GetAFPUsage'
    query = urlencode({'Action': action, 'Version': '2024-01-01'})
    body = b'{}'
    headers = sign_headers(credentials['access_key_id'], credentials['secret_access_key'],
                           urlsplit(endpoint).netloc, query, body)
    response = await client.post(endpoint + '?' + query, content=body, headers=headers)
    if response.is_success and len(response.content) < 1_000_000:
        error = (response.json().get('ResponseMetadata') or {}).get('Error') or {}
        if any(word in str(error.get('Code')) for word in ('Auth', 'AccessDenied', 'Signature', 'Credential')):
            raise SpiritScoutError('火山用量查询凭据无效或缺少权限，请检查 Access Key 配置')
    return response
