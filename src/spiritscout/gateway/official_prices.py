"""从官方公开价目表读取价格；不发送密钥，不访问账户或购买接口，不执行远端脚本。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import time
from functools import lru_cache
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

SOURCES = {
    'glm_api': 'https://docs.bigmodel.cn/cn/guide/start/pricing.md',
    'glm_token_plan': 'https://docs.bigmodel.cn/cn/coding-plan/overview.md',
    'kimi_api': 'https://platform.kimi.com/docs/pricing/chat',
    'kimi_token_plan': 'https://www.kimi.com/resources/kimi-k2-7-code-pricing',
    'deepseek_api': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
}
GLM_CHECKOUT = 'https://www.bigmodel.cn/glm-coding'
_HOSTS = {urlsplit(url).hostname for url in (*SOURCES.values(), GLM_CHECKOUT)} | {'static.bigmodel.cn'}
TTL = 24 * 3600


class Tables(HTMLParser):
    """读取单元格原文；不把 rowspan 复制为新的价格。"""
    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.table = []
        elif tag == 'tr':
            self.row = []
        elif tag in {'td', 'th'}:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {'td', 'th'} and self.cell is not None:
            if self.row is not None:
                self.row.append(''.join(self.cell).strip())
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.table is not None:
                self.table.append(self.row)
            self.row = None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def _number(value: str) -> float:
    value = value.strip().replace(',', '')
    if value in {'免费', '限时免费'}:
        return 0.0
    if not re.fullmatch(r'[¥￥]?\d+(?:\.\d+)?(?:元)?', value):
        raise ValueError('价格单元格格式变化，未采用新价格')
    number = float(value.strip('¥￥元'))
    if not math.isfinite(number):
        raise ValueError('价格数值无效')
    return number


def _tables(text: str) -> list[list[list[str]]]:
    parser = Tables()
    parser.feed(text)
    return parser.tables


def parse_prices(scheme: str, text: str) -> dict[str, Any]:
    """只解析明确的币种/列顺序，缺失或格式漂移不回退到猜测的费率。"""
    out: dict[str, Any] = {'currency': 'CNY', 'models': {}, 'plans': {},
        'kind': 'subscription' if scheme.endswith('_token_plan') else 'api'}
    models = out['models']
    if scheme == 'glm_api':
        if '输入单价（元/百万 Tokens）' not in text or '缓存命中（元/百万 Tokens）' not in text:
            raise ValueError('GLM 价格表单位或表头变化')
        for line in text.splitlines():
            cells = [s.strip() for s in line.lstrip('> ').strip('|').split('|')]
            if len(cells) < 6 or not re.fullmatch(r'GLM-[\w.-]+', cells[0], re.I):
                continue
            try:
                price = {'input': _number(cells[2]), 'output': _number(cells[3]),
                    'cached_input': None if cells[5] == '不支持' else _number(cells[5]),
                    'cache_write': _number(cells[2]), 'condition': cells[1],
                    'cache_storage_per_mtok_hour': _number(cells[4])}
            except ValueError:
                continue  # 多模态/按次计费不混入文本 token 费用。
            models.setdefault(cells[0].lower(), []).append(price)
    elif scheme == 'kimi_api':
        # Mintlify 的导出 Markdown 不含动态表；读取公开 HTML 中的字面量，不执行 JS。
        text = unescape(text)
        if '缓存写入（5min）' not in text or '¥' not in text or '/ MTok' not in text:
            raise ValueError('Kimi 价格表单位或表头变化')
        pattern = r'children:`(kimi-[\w.-]+)`\}\),((?:`¥[\d.,]+ / MTok`\s*,?\s*){3,5})\]'
        for match in re.finditer(pattern, text):
            values = [_number(v) for v in re.findall(r'`(¥[\d.,]+) / MTok`', match[2])]
            rate = dict(zip(('input', 'output', 'cached_input', 'cache_write', 'cache_write_1h'), values))
            rate.setdefault('cache_write', rate['input'])
            if rate not in models.setdefault(match[1].lower(), []):
                models[match[1].lower()].append(rate)
    elif scheme == 'deepseek_api':
        for table in _tables(text):
            names = [re.sub(r'\(\d+\)$', '', s) for s in table[0][1:]] if table else []
            if not names or not all(re.fullmatch(r'deepseek-[\w.-]+', n) for n in names):
                continue
            kind = ''
            rates = {name: {'off_peak': {}, 'peak': {}} for name in names}
            for row in table:
                label = ''.join(row[:-len(names)])
                if '缓存命中' in label:
                    kind = 'cached_input'
                elif '缓存未命中' in label:
                    kind = 'input'
                elif '百万tokens输出' in label.lower():
                    kind = 'output'
                period = 'off_peak' if '空闲时段' in label else 'peak' if '高峰时段' in label else ''
                if kind and period:
                    for name, value in zip(names, row[-len(names):], strict=True):
                        rates[name][period][kind] = _number(value)
            for name, periods in rates.items():
                if all(set(row) == {'input', 'output', 'cached_input'} for row in periods.values()):
                    models[name] = [{**row, 'cache_write': row['input'], 'period': period}
                                    for period, row in periods.items()]
        plain = re.sub(r'<[^>]*>', '', text)
        if re.search(r'周一至周五.*9:00\s*-\s*12:00、14:00\s*-\s*18:00', plain):
            out['schedule'] = 'deepseek_cn_workday_peak'
    elif scheme == 'glm_token_plan':
        for match in re.finditer(r'\|\s*(Lite|Pro|Max) 套餐\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|', text):
            out['plans'][match[1]] = {'quota_5h': _number(match[2]), 'quota_week': _number(match[3])}
        for table in _tables(text):
            for row in table:
                index = next((i for i, cell in enumerate(row) if cell.startswith('GLM-')), None)
                if index is not None and len(row[index + 1:]) == 3:
                    name = row[index].split('（')[0].lower()
                    models[name] = [dict(zip(('input', 'cached_input', 'output'), map(_number, row[index + 1:])))]
        out.update(unit='credits_per_10k_tokens', fee_status='checkout_required',
            note='个人套餐公开额度；适用客户端和账号权益由供应商决定，套餐选择不代表授权或已经订阅。')
        if '50%' in text and '周一至周五的 14:00～18:00' in text:
            out['schedule'] = 'glm_weekday_afternoon_half_offpeak'
    elif scheme == 'kimi_token_plan':
        for table in _tables(text):
            if not table or table[0] != ['方案 / 权益', 'Plus', 'Pro', 'Max', 'Ultra']:
                continue
            for row in table[1:]:
                period = {'连续包月': 'month', '连续包年（折算月价）': 'year_monthly_equivalent'}.get(row[0])
                if period and len(row) == 5:
                    for name, amount in zip(table[0][1:], row[1:], strict=True):
                        out['plans'].setdefault(name, {})[period] = _number(amount)
        out.update(region='CN', fee_status='public_reference',
            note='官方公开文章的人民币参考价；年付栏为折算月价。实际优惠、续费与跨产品额度以账户页为准。')
    else:
        raise ValueError('不支持的官方计费方案')
    if not models and not out['plans']:
        raise ValueError('未识别到可靠的官方价格，保留旧快照')
    return out


def glm_plan_fees(script: str) -> dict[str, Any]:
    """购买页公开代码内的最新套餐字面量（从不执行），历史 V1/V2 不能冒充新价。"""
    versions = list(re.finditer(r'="V(\d+)"', script))
    if not versions:
        raise ValueError('未找到公开套餐版本')
    latest = max(int(m[1]) for m in versions)
    blocks = [script[m.end():versions[i + 1].start() if i + 1 < len(versions) else len(script)]
              for i, m in enumerate(versions) if int(m[1]) == latest]
    fees = {}
    for block in blocks:
        for match in re.finditer(r'\{type:"(lite|pro|max)",unitKey:"(month|quarter|year)",[^{}]*?renewAmount:([\d.]+)[^{}]*\}', block):
            fees.setdefault(match[1].title(), {})[match[2]] = _number(match[3])
    if set(fees) != {'Lite', 'Pro', 'Max'} or any(set(p) != {'month', 'quarter', 'year'} for p in fees.values()):
        raise ValueError('公开套餐价格不完整')
    return {'version': f'V{latest}', 'fees': fees}


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    for _ in range(4):
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in _HOSTS or parts.username or parts.port not in (None, 443):
            raise ValueError('价格来源不是预设的官方 HTTPS 地址')
        async with client.stream('GET', url) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get('location', ''))
                continue
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 3_000_000:
                    raise ValueError('官方价格页面过大')
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8')
    raise ValueError('官方价格页面重定向过多')


async def _glm_checkout(client: httpx.AsyncClient) -> tuple[dict[str, Any], list[str]]:
    page = await _fetch(client, GLM_CHECKOUT)
    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)', page)
    app_url = next((u for u in scripts if '/js/app.' in u), '')
    runtime_url = next((u for u in scripts if '/js/runtime.' in u), '')
    if not app_url or not runtime_url:
        raise ValueError('购买页公开资源结构变化')
    app, runtime = await asyncio.gather(_fetch(client, app_url), _fetch(client, runtime_url))
    route = re.search(r'path:"/glm-coding".{0,1200}?Promise\.all\(\[(.*?)\]\)', app)
    if not route:
        raise ValueError('购买页套餐路由结构变化')
    ids = re.findall(r'\.e\((\d+)\)', route[1])
    if not ids or len(ids) > 12:
        raise ValueError('购买页资源列表变化')
    # Webpack 的 JS 映射位于 CSS 映射前；名称表和散列分别提取。
    urls = []
    for number in ids:
        values = re.findall(r'(?:[,{])' + number + r':"([\w.-]+)"', runtime)
        digest = next((v for v in values if re.fullmatch('[a-f0-9]{8,}', v)), '')
        name = values[0] if values and values[0] != digest else number
        if not digest:
            raise ValueError('购买页资源版本无法解析')
        urls.append(urljoin(runtime_url, f'{name}.{digest}.js'))
    results = await asyncio.gather(*(_fetch(client, u) for u in urls), return_exceptions=True)
    for url, script in zip(urls, results, strict=True):
        if isinstance(script, str):
            try:
                return glm_plan_fees(script), [GLM_CHECKOUT, url]
            except ValueError:
                pass
    raise ValueError('未找到当前套餐公开价格；请查看官方结算页')


class PriceCatalog:
    """24 小时缓存；失败退避，旧价保留展示但不当作新调用的最新报价。"""
    def __init__(self, path: Path | None = None):
        self.path = path
        self.data: dict[str, Any] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        if path:
            try:
                data = json.loads(path.read_text())
                if data.get('schema') == 1 and isinstance(data.get('catalogs'), dict):
                    self.data = data['catalogs']
            except (OSError, ValueError, TypeError):
                pass

    def _save(self) -> None:
        if not self.path:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, delete=False, encoding='utf-8') as handle:
                temporary = Path(handle.name)
                json.dump({'schema': 1, 'catalogs': self.data}, handle, ensure_ascii=False)
            os.replace(temporary, self.path)
        except OSError:
            pass  # 价格查询不因只读缓存目录而阻断任务。
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    async def get(self, scheme: str, *, force: bool = False) -> dict[str, Any]:
        if scheme not in SOURCES:
            raise ValueError('不支持的官方计费方案')
        async with self.locks.setdefault(scheme, asyncio.Lock()):
            old, now = self.data.get(scheme, {}), time.time()
            age = now - old.get('fetched_at', 0)
            retry = 60 if force else 300
            should_fetch = (force or age >= TTL or old.get('error')) and now - old.get('attempted_at', 0) >= retry
            if should_fetch:
                try:
                    async with asyncio.timeout(25), httpx.AsyncClient(timeout=8, headers={'User-Agent': 'SpiritScout-price-catalog'}) as client:
                        page = await _fetch(client, SOURCES[scheme])
                        quote = parse_prices(scheme, page)
                        sources = [SOURCES[scheme]]
                        if scheme == 'glm_token_plan':
                            try:
                                fees, extra = await _glm_checkout(client)
                                quote['plan_version'] = fees['version']
                                for name, values in fees['fees'].items():
                                    quote['plans'].setdefault(name, {}).update(values)
                                quote['fee_status'] = 'public_reference'
                                sources += extra
                            except (httpx.HTTPError, ValueError, TimeoutError):
                                quote['fee_status'] = 'checkout_required'
                        quote.update(scheme=scheme, fetched_at=time.time(), attempted_at=now, sources=sources,
                                     version=hashlib.sha256(json.dumps(quote, sort_keys=True).encode()).hexdigest()[:16])
                        self.data[scheme] = quote
                except (httpx.HTTPError, ValueError, TimeoutError, UnicodeError):
                    self.data[scheme] = {**old, 'scheme': scheme, 'attempted_at': now,
                        'error': '官方页面不可用或格式变化，未采用未经核实的新价格', 'sources': old.get('sources', [SOURCES[scheme]])}
                self._save()
            result = copy.deepcopy(self.data.get(scheme, {}))
            result['status'] = ('fresh' if result.get('fetched_at') and not result.get('error')
                and time.time() - result['fetched_at'] < TTL else 'stale' if result.get('fetched_at') else 'unavailable')
            return result


@lru_cache(maxsize=1)
def price_catalog() -> PriceCatalog:
    root = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return PriceCatalog(root / 'spiritscout' / 'official-prices.json')
