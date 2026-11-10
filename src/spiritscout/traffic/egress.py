"""会话固定出口的私有快照和运行生命周期。"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import secrets
import socket
import ssl
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.resources.proxy_pool import ProxyPool
from spiritscout.traffic.mihomo import executable as mihomo_executable
from spiritscout.traffic.proxy import _REQUIRED, SessionProxy


async def verify_exit(environment):
    """通过网关有界读取公网出口；诊断不包含认证信息。"""
    try:
        async with httpx.AsyncClient(proxy=environment['HTTPS_PROXY'], trust_env=False,
                verify=ssl.create_default_context(cafile=environment['SSL_CERT_FILE']), timeout=15,
                follow_redirects=False) as client:
            async with client.stream('GET', 'https://api.ipify.org?format=json',
                                     headers={'Accept-Encoding': 'identity'}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > 4096:
                        raise ValueError('出口响应超过上限')
                address = ipaddress.ip_address(json.loads(body)['ip'])
                if not address.is_global:
                    raise ValueError('出口不是公网地址')
                return str(address)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
        raise SpiritScoutError('无法通过选定代理验证公网出口，目标任务未开工') from None


def bind(store, session_id: str, plan_id: str, *, source_session: str = ''):
    if source_session:
        row = store.setting('session_egress', source_session)
        if row is None or row['data'].get('id') != plan_id:
            raise ValueError('父会话的固定出口快照不可用')
        snapshot = row['data']
    else:
        snapshot = ProxyPool(store).snapshot(plan_id)
    store.save('session_egress', session_id, snapshot, 0)
    return {'id': snapshot['id'], 'name': snapshot['name'],
            'nodes': [{k: node[k] for k in ('name', 'type', 'server', 'port')}
                      for node in snapshot['nodes']], 'verification': 'unverified'}


def upstream(node):
    scheme = 'https' if node.get('tls') and node['type'] == 'http' else node['type']
    host = node['server']
    if ':' in host:
        host = '[' + host + ']'
    credentials = ''
    if node.get('username') or node.get('password'):
        credentials = quote(node.get('username', ''), safe='') + ':' + quote(node.get('password', ''), safe='') + '@'
    return f"{scheme}://{credentials}{host}:{node['port']}"


def mihomo_config(nodes, port, authentication=None):
    proxies = []
    for index, node in enumerate(nodes):
        config = {**node, 'name': f'hop-{index}'}
        if index:
            config['dialer-proxy'] = f'hop-{index - 1}'
        proxies.append(config)
    return {'mixed-port': port, 'bind-address': '127.0.0.1', 'allow-lan': False,
            'mode': 'rule', 'log-level': 'silent', 'ipv6': False,
            'proxies': proxies, 'rules': [f'MATCH,hop-{len(nodes) - 1}'],
            **({'authentication': [authentication]} if authentication else {})}


@contextlib.asynccontextmanager
async def session_egress(store, session):
    binding = session.request.get('proxy_binding') or {}
    if binding.get('id', 'direct') == 'direct':
        yield
        return
    _REQUIRED.add(str(session.workspace.resolve()))
    rows = store.settings('session_egress')
    snapshot_row = next((row for row in rows if row['id'] == session.session_id), None)
    snapshot = snapshot_row['data'] if snapshot_row else None
    if snapshot is None or not snapshot.get('nodes'):
        raise SpiritScoutError('会话代理快照缺失，未建立目标出口')
    nodes = snapshot['nodes']
    process = None
    proxy = None
    temporary = None
    bridge = None
    try:
        if len(nodes) == 1 and nodes[0]['type'] in {'http', 'socks5'}:
            address = upstream(nodes[0])
        else:
            binary = mihomo_executable()
            if not binary:
                raise SpiritScoutError('此代理方案需要本机 Mihomo，请安装后重试')
            temporary = tempfile.TemporaryDirectory(prefix='spiritscout-egress-')
            root = Path(temporary.name)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            path = root / 'config.yaml'
            authentication = 'session:' + secrets.token_urlsafe(32)
            with path.open('x') as output:
                os.chmod(path, 0o600)
                yaml.safe_dump(mihomo_config(nodes, port, authentication), output)
            process = await asyncio.create_subprocess_exec(binary, '-d', str(root), '-f', str(path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)
            deadline = asyncio.get_running_loop().time() + 10
            while True:
                if process.returncode is not None:
                    raise SpiritScoutError('Mihomo 启动失败，请检查节点配置')
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise SpiritScoutError('Mihomo 启动超时') from None
                    await asyncio.sleep(.05)
            address = f'http://{authentication}@127.0.0.1:{port}'
        proxy = SessionProxy(session.workspace)
        proxy.task = asyncio.create_task(proxy.serve(address))
        await asyncio.wait_for(asyncio.shield(proxy.ready), 20)
        from spiritscout.traffic.bridge import EgressBridge

        bridge = EgressBridge(proxy.address)
        await bridge.start()
        proxy.egress_socket = bridge.path
        exit_ip = await verify_exit(await proxy.environment())
        if snapshot.get('exit_ip') and snapshot['exit_ip'] != exit_ip:
            raise SpiritScoutError('此代理方案的公网出口已改变；原会话未切换出口，请新建会话重新确认')
        if not snapshot.get('exit_ip'):
            snapshot['exit_ip'] = exit_ip
            store.save('session_egress', session.session_id, snapshot, snapshot_row['version'])
        binding.update(exit_ip=exit_ip, verification='verified_at_start')
        yield
    finally:
        if bridge:
            await bridge.close()
        if proxy:
            await proxy.stop()
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.kill()
                await process.wait()
        if temporary:
            temporary.cleanup()
