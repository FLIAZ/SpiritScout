"""加密代理池：只导入节点数据，固定链路按快照供会话使用。"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import ipaddress
import json
import re
import socket
import time
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx
import yaml

from spiritscout import __version__
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.ids import new_id
from spiritscout.traffic.mihomo import executable as mihomo_executable

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_NODES = 1000
_TYPES = {"http", "socks5", "ss", "vmess", "vless", "trojan", 'anytls', 'hysteria2', 'tuic'}
_FIELDS = {
    "name", "type", "server", "port", "username", "password", "cipher",
    "uuid", "alterId", "tls", "servername", "sni", "network", "udp",
    "skip-cert-verify", "client-fingerprint", "alpn", "ws-opts", "grpc-opts",
    "reality-opts", "flow", "packet-encoding",
    'idle-session-check-interval', 'idle-session-timeout', 'min-idle-session',
    'up', 'down', 'obfs', 'obfs-password', 'ports', 'hop-interval', 'fingerprint',
    'name-cert-verify', 'heartbeat-interval', 'disable-sni', 'reduce-rtt', 'request-timeout',
    'udp-relay-mode', 'congestion-controller', 'max-udp-relay-packet-size', 'fast-open', 'max-open-streams',
}

_NOTICE = re.compile(r'剩余流量|距离下次|套餐到期|客户端.*(?:不支持|版本|更新)|请用官网|建议每天|到期时间|流量重置|traffic remaining|expire', re.I)


class SubscriptionContentError(ValueError):
    """可公开展示、不含订阅地址或认证信息的订阅内容诊断。"""


class SubscriptionPayload(str):
    """订阅正文及响应头中的公开流量额度；字符串兼容现有解析入口。"""

    def __new__(cls, content: str, usage: dict[str, int] | None = None):
        value = super().__new__(cls, content)
        value.usage = usage
        return value


def parse_subscription_usage(headers) -> dict[str, int] | None:
    """解析 Clash 生态的 subscription-userinfo 响应头。"""
    value = next((item for key, item in headers.items()
                  if key.lower() == 'subscription-userinfo'
                  or key.lower().endswith('-subscription-userinfo')), '')
    values = {key.lower(): int(number) for key, number in re.findall(
        r'(?:^|;)\s*(upload|download|total|expire)\s*=\s*(\d+)', value, re.I)}
    if not values:
        return None
    usage = {key: max(0, values.get(key, 0)) for key in ('upload', 'download', 'total', 'expire')}
    usage['used'] = usage['upload'] + usage['download']
    usage['remaining'] = max(0, usage['total'] - usage['used']) if usage['total'] else 0
    return usage


def node_kind(node):
    try:
        local = ipaddress.ip_address(node['server']).is_loopback
    except ValueError:
        local = node['server'].lower() == 'localhost'
    return 'notice' if local and _NOTICE.search(node['name']) else 'proxy'


class PublicSubscriptionTransport(httpx.AsyncBaseTransport):
    """每跳校验公网解析并固定连接地址，避免订阅 URL 访问控制面。"""

    def __init__(self):
        self.transport = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request):
        host = request.url.host
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            host, request.url.port or 443, type=socket.SOCK_STREAM), 10)
        addresses = list(dict.fromkeys(item[4][0] for item in infos))
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            raise ValueError("订阅地址必须解析为公网 IP")
        pinned = httpx.Request(request.method, request.url.copy_with(host=addresses[0]),
                               headers=request.headers, stream=request.stream,
                               extensions={**request.extensions, "sni_hostname": host})
        return await self.transport.handle_async_request(pinned)

    async def aclose(self):
        await self.transport.aclose()


async def fetch_subscription(url: str) -> SubscriptionPayload:
    try:
        async with httpx.AsyncClient(transport=PublicSubscriptionTransport(), trust_env=False,
                                     timeout=20, follow_redirects=False) as client:
            for _ in range(4):
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("订阅需要不含用户认证段的 HTTPS 地址")
                async with client.stream("GET", url, headers={'User-Agent': f'mihomo SpiritScout/{__version__}',
                        'Accept': 'application/yaml, text/yaml, text/plain', "Accept-Encoding": "identity"}) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers.get("location", "")))
                        continue
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError("订阅响应不能使用压缩编码")
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        body.extend(chunk)
                        if len(body) > MAX_IMPORT_BYTES:
                            raise ValueError("订阅内容超过 2 MiB")
                    return SubscriptionPayload(body.decode("utf-8-sig"), parse_subscription_usage(response.headers))
            raise ValueError("订阅重定向次数过多")
    except (httpx.HTTPError, OSError, TimeoutError, UnicodeError) as exc:
        # HTTP 异常会携带含令牌的订阅 URL，不能直接返回异常文本。
        raise ValueError("订阅获取失败，请检查地址和网络") from exc


def _decode(value: str) -> str:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4),
                                altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("节点 Base64 内容无效") from exc


def _node_id(node, source='manual'):
    identity = {key: node[key] for key in ('name', 'type', 'server', 'port')}
    identity['source'] = source
    return 'node-' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def node_fingerprint(node):
    return hashlib.sha256(json.dumps(node, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _node(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - _FIELDS:
        raise ValueError("节点包含不支持的配置字段")
    # 部分服务商用 null 表示未启用的可选参数（例如 VLESS flow）。
    node = {key: copy.deepcopy(value) for key, value in raw.items() if value is not None}
    if not isinstance(node.get('type'), str) or node['type'] not in _TYPES:
        raise ValueError("节点协议暂不支持")
    for key in ("name", "server"):
        value = node.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError("节点名称或服务器无效")
    server = node["server"]
    if any(c in server for c in "/\\@?# "):
        raise ValueError("节点服务器必须为主机名或 IP")
    if type(node.get("port")) is not int or not 1 <= node["port"] <= 65535:
        raise ValueError("节点端口无效")
    # 嵌套配置仅接受内联连接参数，禁止证书路径、脚本及远程资源。
    allowed = {"ws-opts": {"path", "headers", "max-early-data", "early-data-header-name"},
               "grpc-opts": {"grpc-service-name"},
               "reality-opts": {"public-key", "short-id"}}
    for key, keys in allowed.items():
        if key in node and (not isinstance(node[key], dict) or set(node[key]) - keys):
            raise ValueError("节点传输参数包含不支持的字段")
    for key in ('tls', 'udp', 'skip-cert-verify'):
        if key in node and type(node[key]) is not bool:
            raise ValueError('节点开关必须为布尔值')
    for key in ('username', 'password', 'uuid', 'cipher', 'sni', 'servername', 'flow', 'client-fingerprint'):
        if key in node and (not isinstance(node[key], str) or any(ord(char) < 32 for char in node[key])):
            raise ValueError('节点认证或传输参数格式无效')
    return node


def parse_nodes(content: str) -> list[dict[str, Any]]:
    """解析 Clash/Mihomo YAML 或 Base64 URI 列表；不执行导入配置。"""
    if len(content.encode()) > MAX_IMPORT_BYTES:
        raise ValueError("代理配置超过 2 MiB")
    content = content.strip()
    if not content:
        raise ValueError("代理配置为空")
    if "://" not in content and not content.startswith(("proxies:", "{")):
        try:
            content = _decode(content)
        except ValueError:
            pass
    if content.startswith(("http://", "https://", "socks5://", "trojan://", "vless://", "ss://", "vmess://", 'anytls://', 'hysteria2://', 'hy2://', 'tuic://')):
        nodes = []
        for line in content.splitlines():
            if not line.strip():
                continue
            parsed = urlsplit(line.strip())
            if parsed.scheme == 'ss' and '@' not in parsed.netloc:
                decoded = _decode(parsed.netloc + parsed.path)
                parsed = urlsplit('ss://' + decoded + ('#' + parsed.fragment if parsed.fragment else ''))
            if parsed.scheme == "vmess":
                try:
                    data = json.loads(_decode(line.strip()[8:]))
                    if not isinstance(data, dict):
                        raise ValueError('VMess 节点必须为对象')
                    node = {"type": "vmess", "name": data.get("ps") or "VMess",
                            "server": data["add"], "port": int(data["port"]),
                            "uuid": data["id"], "alterId": int(data.get("aid", 0)),
                            "cipher": data.get("scy", "auto"), "tls": data.get("tls") == "tls"}
                    if data.get('sni'):
                        node['servername'] = data['sni']
                    if data.get('net') == 'ws':
                        node.update(network='ws', **{'ws-opts': {'path': data.get('path') or '/',
                            'headers': {'Host': data['host']} if data.get('host') else {}}})
                    elif data.get('net') == 'grpc':
                        node.update(network='grpc', **{'grpc-opts': {'grpc-service-name': data.get('path') or ''}})
                    elif data.get("net") not in (None, "", "tcp"):
                        raise ValueError('VMess 传输暂不支持，请用 YAML 导入')
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("VMess 节点无效") from exc
            else:
                if parsed.query and parsed.scheme not in {'vless', 'trojan', 'anytls', 'hysteria2', 'hy2', 'tuic'}:
                    raise ValueError("带传输参数的 URI 请改用 Clash YAML 导入")
                node = {"type": "http" if parsed.scheme == "https" else parsed.scheme,
                        "name": unquote(parsed.fragment) or "导入节点",
                        "server": parsed.hostname or "", "port": parsed.port or {'http': 80, 'https': 443, 'socks5': 1080}.get(parsed.scheme)}
                if parsed.scheme == "https":
                    node["tls"] = True
                if parsed.scheme in {'anytls', 'hysteria2', 'hy2', 'tuic'}:
                    node['type'] = 'hysteria2' if parsed.scheme == 'hy2' else parsed.scheme
                    node['password'] = unquote((parsed.password or '') if parsed.scheme == 'tuic' else (parsed.username or ''))
                    if parsed.scheme == 'tuic':
                        node['uuid'] = unquote(parsed.username or '')
                    pairs = parse_qsl(parsed.query, keep_blank_values=True)
                    options = dict(pairs)
                    allowed = {'sni', 'peer', 'alpn', 'obfs', 'obfs-password', 'up', 'down', 'congestion_control', 'udp_relay_mode'}
                    if len(options) != len(pairs) or set(options) - allowed:
                        raise ValueError('URI 包含不支持或不安全的协议参数，请使用 YAML')
                    for old, new in (('sni', 'sni'), ('peer', 'sni'), ('obfs', 'obfs'), ('obfs-password', 'obfs-password'),
                                     ('up', 'up'), ('down', 'down'), ('congestion_control', 'congestion-controller'), ('udp_relay_mode', 'udp-relay-mode')):
                        if old in options:
                            node[new] = options[old]
                    if options.get('alpn'):
                        node['alpn'] = options['alpn'].split(',')
                    nodes.append(_node(node))
                    continue
                if parsed.scheme in {"vless", "trojan"}:
                    node["uuid" if parsed.scheme == "vless" else "password"] = unquote(parsed.username or "")
                    pairs = parse_qsl(parsed.query, keep_blank_values=True)
                    options = dict(pairs)
                    if len(options) != len(pairs) or set(options) - {'type', 'security', 'encryption', 'sni', 'peer', 'host', 'path', 'serviceName', 'flow', 'fp', 'pbk', 'sid'}:
                        raise ValueError('URI 包含重复或不支持的传输字段')
                    if options.get('encryption', 'none') != 'none':
                        raise ValueError('URI encryption 暂不支持')
                    security = options.get('security', 'tls' if parsed.scheme == 'trojan' else 'none')
                    if security not in {'none', 'tls', 'reality'}:
                        raise ValueError('URI security 暂不支持')
                    if security != 'none':
                        node['tls'] = True
                    if options.get('sni') or options.get('peer'):
                        node['servername'] = options.get('sni') or options['peer']
                    if security == 'reality':
                        node['reality-opts'] = {'public-key': options.get('pbk', ''), 'short-id': options.get('sid', '')}
                    for old, new in (('flow', 'flow'), ('fp', 'client-fingerprint')):
                        if options.get(old):
                            node[new] = options[old]
                    network = options.get('type', 'tcp')
                    if network == 'ws':
                        node.update(network='ws', **{'ws-opts': {'path': options.get('path', '/'),
                            'headers': {'Host': options['host']} if options.get('host') else {}}})
                    elif network == 'grpc':
                        node.update(network='grpc', **{'grpc-opts': {'grpc-service-name': options.get('serviceName', '')}})
                    elif network != 'tcp':
                        raise ValueError('URI type 暂不支持')
                elif parsed.scheme == "ss":
                    credentials = unquote(parsed.username or "")
                    if parsed.password is None:
                        credentials = _decode(credentials)
                    else:
                        credentials += ":" + unquote(parsed.password)
                    cipher, separator, password = credentials.partition(":")
                    if not separator:
                        raise ValueError("SS 节点认证无效")
                    node.update(cipher=cipher, password=password)
                else:
                    node.update(username=unquote(parsed.username or ""), password=unquote(parsed.password or ""))
            nodes.append(_node(node))
    else:
        try:
            # 别名不需要参与节点导入，拒绝以避免递归/指数展开。
            if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(content)):
                raise ValueError("代理配置不能包含 YAML 别名")
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError("代理 YAML 格式无效") from exc
        if not isinstance(data, dict):
            raise ValueError("需要 Clash/Mihomo proxies 列表")
        blocked = {"proxy-providers", "rule-providers", "script", "tun", "external-controller",
                   "external-controller-unix", "external-controller-pipe", "external-ui", "external-ui-url"}
        if blocked.intersection(data):
            raise ValueError("导入只接受内联节点，请移除远程引用、脚本、TUN 和外部控制器")
        values = data.get("proxies")
        if not isinstance(values, list) or len(values) > MAX_NODES:
            raise ValueError("节点列表无效或超过 1000 个")
        nodes = [_node(item) for item in values]
    if not nodes or len(nodes) > MAX_NODES:
        raise ValueError("节点列表为空或超过 1000 个")
    return nodes


def parse_subscription(content: str) -> list[dict[str, Any]]:
    """只构造订阅的内联节点，忽略客户端全局设置，绝不加载远程 provider。"""
    if len(content.encode()) > MAX_IMPORT_BYTES:
        raise SubscriptionContentError('订阅内容超过 2 MiB')
    loader = yaml.SafeLoader(content)
    try:
        document = loader.get_single_node()
        if not isinstance(document, yaml.MappingNode):
            return parse_nodes(content)
        matches = [value for key, value in document.value if isinstance(key, yaml.ScalarNode) and key.value == 'proxies']
        if len(matches) != 1 or not isinstance(matches[0], yaml.SequenceNode):
            raise SubscriptionContentError('订阅没有内联 proxies 节点，请获取 Mihomo 完整节点订阅；不加载远程 provider。')
        value = matches[0]
        if len(value.value) > MAX_NODES:
            raise SubscriptionContentError('订阅节点超过 1000 个')
        # 全局规则中的别名不参与构造；节点内部仍禁止共享/循环引用和过深结构。
        seen = set()
        def visit(node, depth=0):
            if depth > 16 or id(node) in seen:
                raise SubscriptionContentError('订阅节点包含别名或过深的结构，请使用内联节点配置')
            seen.add(id(node))
            if isinstance(node, yaml.MappingNode):
                for key, child in node.value:
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            elif isinstance(node, yaml.SequenceNode):
                for child in node.value:
                    visit(child, depth + 1)
        visit(value)
        values = loader.construct_object(value, deep=True)
        if not values:
            raise SubscriptionContentError('订阅没有返回节点')
        return [_node(item) for item in values]
    except yaml.YAMLError:
        raise SubscriptionContentError('订阅格式无效，请检查是否为 Mihomo YAML 或节点列表') from None
    finally:
        loader.dispose()


class ProxyPool:
    """一个加密文档保证节点、链路和删除操作原子更新。"""

    def __init__(self, store):
        self.store = store

    def _read(self):
        row = next((r for r in self.store.settings("proxy_pool") if r["id"] == "pool"), None)
        data, version = (copy.deepcopy(row["data"]), row["version"]) if row else ({"nodes": {}, "plans": {}, "subscriptions": {}}, 0)
        sources = data.setdefault('node_sources', {})
        for key, node in data['nodes'].items():
            if key not in sources:
                sources[key] = next((source for source in data['subscriptions'] if _node_id(node, source) == key),
                                    'manual' if _node_id(node) == key else 'legacy')
        return data, version

    def view(self):
        data, version = self._read()
        nodes = [{**self.public_node(k, v), 'source_id': data['node_sources'][k]} for k, v in data['nodes'].items()]
        measurements = {row['id']: row['data'] for row in self.store.settings('proxy_measurements')}
        for node in nodes:
            saved = measurements.get(node['id'])
            node['benchmark'] = saved['result'] if saved and saved['fingerprint'] == node_fingerprint(data['nodes'][node['id']]) else None
        subscriptions = []
        for key, sub in data['subscriptions'].items():
            owned = [node for node in nodes if node['source_id'] == key]
            count = sum(node['selectable'] for node in owned)
            subscriptions.append({'id': key, 'name': sub['name'], 'node_count': count,
                                  'notice_count': len(owned) - count, 'updated_at': sub.get('updated_at'),
                                  'usage': copy.deepcopy(sub.get('usage')),
                                  'diagnostic': '订阅仅返回流量或客户端兼容提示，没有真实节点。请刷新订阅，或从服务商获取 Mihomo 订阅。' if owned and not count else ''})
        return {"version": version, "nodes": nodes,
                "plans": [{"id": k, **v} for k, v in data["plans"].items()],
                "subscriptions": subscriptions,
                "mihomo_available": bool(mihomo_executable())}

    @staticmethod
    def public_node(key, node):
        return {"id": key, "name": node["name"], "type": node["type"],
                "server": node["server"], "port": node["port"],
                "verification": "unverified", 'kind': node_kind(node), 'selectable': node_kind(node) == 'proxy',
                'tls_verification': 'disabled' if node.get('skip-cert-verify') else 'default'}

    def import_content(self, content: str, expected: int):
        nodes = parse_nodes(content)
        data, version = self._read()
        if version != expected:
            raise SpiritScoutError("代理池已更新，请刷新")
        for node in nodes:
            data['nodes'][_node_id(node)] = node
            data['node_sources'][_node_id(node)] = 'manual'
        if len(data["nodes"]) > MAX_NODES:
            raise ValueError("代理池最多保存 1000 个节点")
        self.store.save("proxy_pool", "pool", data, expected)
        return self.view()

    def save_plan(self, name: str, nodes: list[str], expected: int):
        data, version = self._read()
        if version != expected:
            raise SpiritScoutError("代理池已更新，请刷新")
        if not name.strip() or len(name) > 80 or not 1 <= len(nodes) <= 8 or len(set(nodes)) != len(nodes):
            raise ValueError("请填写方案名并选择 1 至 8 个不重复节点")
        if any(key not in data["nodes"] for key in nodes):
            raise ValueError("方案引用的节点不存在")
        if any(node_kind(data['nodes'][key]) != 'proxy' for key in nodes):
            raise SubscriptionContentError('订阅提示条目不能用作代理节点，请先刷新订阅获取真实节点')
        data["plans"][new_id("proxy")] = {"name": name.strip(), "nodes": nodes}
        self.store.save("proxy_pool", "pool", data, expected)
        return self.view()

    async def subscribe(self, name: str, url: str, expected: int, key: str = ""):
        if not name.strip() or len(name) > 80 or len(url) > 4096:
            raise ValueError("订阅名称或地址无效")
        response = await fetch_subscription(url)
        nodes = parse_subscription(str(response))
        if not any(node_kind(node) == 'proxy' for node in nodes):
            raise SubscriptionContentError('服务商仅返回流量或客户端兼容提示，没有真实节点。请使用 Mihomo 订阅链接；原有节点已保留。')
        data, version = self._read()
        if version != expected:
            raise SpiritScoutError("代理池已更新，请刷新")
        key = key or new_id("sub")
        data["subscriptions"][key] = {"name": name.strip(), "url": url, 'updated_at': time.time(),
                                      'usage': copy.deepcopy(getattr(response, 'usage', None))}
        incoming = {_node_id(node, key) for node in nodes}
        referenced = {node for plan in data['plans'].values() for node in plan['nodes']}
        for old in list(data['nodes']):
            if data['node_sources'].get(old) == key and old not in incoming:
                if old in referenced:
                    # 保留已有方案的节点，不把已下架的节点冒充本次订阅结果。
                    data['node_sources'][old] = 'legacy'
                else:
                    del data['nodes'][old]
                    data['node_sources'].pop(old, None)
        for node in nodes:
            data['nodes'][_node_id(node, key)] = node
            data['node_sources'][_node_id(node, key)] = key
        if len(data["nodes"]) > MAX_NODES:
            raise ValueError("代理池最多保存 1000 个节点")
        self.store.save("proxy_pool", "pool", data, expected)
        return self.view()

    async def refresh(self, key: str, expected: int):
        data, version = self._read()
        if version != expected:
            raise SpiritScoutError("代理池已更新，请刷新")
        subscription = data["subscriptions"].get(key)
        if subscription is None:
            raise KeyError(key)
        return await self.subscribe(subscription["name"], subscription["url"], expected, key)

    def snapshot(self, plan_id: str):
        if plan_id == "direct":
            return {"id": "direct", "name": "直连", "nodes": []}
        data, _ = self._read()
        plan = data["plans"].get(plan_id)
        if plan is None:
            raise ValueError("代理方案不存在")
        if any(node_kind(data['nodes'][key]) != 'proxy' for key in plan['nodes']):
            raise SubscriptionContentError('方案包含订阅提示条目，请重新选择真实节点')
        return {"id": plan_id, "name": plan["name"],
                "nodes": [copy.deepcopy(data["nodes"][key]) for key in plan["nodes"]]}

    def delete(self, kind: str, key: str, expected: int):
        data, _ = self._read()
        if kind not in {"nodes", "plans", "subscriptions"} or key not in data[kind]:
            raise KeyError(key)
        if kind == "nodes" and any(key in plan["nodes"] for plan in data["plans"].values()):
            raise ValueError("请先删除使用此节点的方案")
        del data[kind][key]
        if kind == 'nodes':
            data['node_sources'].pop(key, None)
        elif kind == 'subscriptions':
            data['node_sources'] = {node: 'legacy' if source == key else source for node, source in data['node_sources'].items()}
        self.store.save("proxy_pool", "pool", data, expected)
        return self.view()
