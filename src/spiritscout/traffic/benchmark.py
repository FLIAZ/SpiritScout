"""独立节点实测：所有测试请求经临时 Mihomo 固定出口，不影响会话。"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import secrets
import socket
import statistics
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.ids import new_id
from spiritscout.resources.proxy_pool import ProxyPool, node_fingerprint, node_kind
from spiritscout.traffic.egress import mihomo_config
from spiritscout.traffic.mihomo import executable

TEST_URL = 'https://speed.cloudflare.com/__down'
DOWNLOAD_BYTES = 10_000_000


@asynccontextmanager
async def node_proxy(node):
    binary = executable()
    if not binary:
        raise SpiritScoutError('Mihomo 运行时不可用')
    with tempfile.TemporaryDirectory(prefix='spiritscout-node-test-') as temporary:
        root = Path(temporary)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        authentication = 'benchmark:' + secrets.token_urlsafe(24)
        path = root / 'config.yaml'
        with path.open('x') as output:
            os.chmod(path, 0o600)
            yaml.safe_dump(mihomo_config([node], port, authentication), output)
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(binary, '-d', str(root), '-f', str(path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # 创建子进程期间取消，也要拿到进程句柄并回收。
            process = await spawning
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        try:
            async with asyncio.timeout(10):
                while True:
                    if process.returncode is not None:
                        raise SpiritScoutError('节点配置无法启动，请检查协议参数')
                    try:
                        _, writer = await asyncio.open_connection('127.0.0.1', port)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(.05)
            yield f'http://{authentication}@127.0.0.1:{port}'
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()


async def measure(proxy: str, mode: str, url: str = TEST_URL) -> dict:
    """延迟含代理握手、目标 TLS 和响应；下载为实际字节/请求总耗时。"""
    samples, timeouts = [], 0
    headers = {'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache'}
    for _ in range(3):
        start = time.perf_counter()
        try:
            # 每个样本重新建连接，避免把已有 keep-alive 请求时间当连接延迟。
            async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=5,
                    follow_redirects=False, headers=headers) as client:
                async with asyncio.timeout(6), client.stream('GET', url, params={'bytes': 1, 'nonce': secrets.token_hex(8)}) as response:
                    response.raise_for_status()
                    body = 0
                    async for chunk in response.aiter_raw():
                        body += len(chunk)
                        if body > 1:
                            raise ValueError('测速端点响应无效')
                    if body != 1 or response.status_code != 200:
                        raise ValueError('测速端点响应无效')
            samples.append((time.perf_counter() - start) * 1000)
        except (TimeoutError, httpx.TimeoutException):
            timeouts += 1
        except (httpx.HTTPError, OSError, ValueError):
            pass
    result = {'status': 'available' if samples else 'timeout' if timeouts == 3 else 'unavailable',
              'latency_ms': round(statistics.median(samples), 2) if samples else None,
              'samples_ms': [round(sample, 2) for sample in samples], 'sample_count': len(samples),
              'speed_mb_s': None, 'download_bytes': 0, 'download_seconds': None,
              'download_complete': False, 'speed_error': '', 'tested_at': time.time(),
              'endpoint': url, 'error': '' if samples else '无法通过节点完成测速请求（节点或测速端点不可达）'}
    if mode != 'download' or not samples:
        return result
    count, complete = 0, False
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=8,
                follow_redirects=False, headers=headers) as client:
            async with asyncio.timeout(10), client.stream('GET', url, params={'bytes': DOWNLOAD_BYTES, 'nonce': secrets.token_hex(8)}) as response:
                response.raise_for_status()
                if response.status_code != 200 or response.headers.get('content-encoding', 'identity') != 'identity':
                    raise ValueError('测速端点响应无效')
                async for chunk in response.aiter_raw():
                    if count + len(chunk) > DOWNLOAD_BYTES:
                        raise ValueError('测速端点响应超过约定大小')
                    count += len(chunk)
                complete = count == DOWNLOAD_BYTES
                if not complete:
                    raise ValueError('测速端点响应不完整')
    except (TimeoutError, httpx.TimeoutException):
        result['speed_error'] = '下载采样超时' if count >= 65536 else '下载超时，样本不足'
    except (httpx.HTTPError, OSError, ValueError):
        result['speed_error'] = '下载测试失败或测速端点响应异常'
        count = 0
    elapsed = time.perf_counter() - start
    result.update(download_bytes=count, download_seconds=round(elapsed, 3), download_complete=complete,
                  speed_mb_s=count / 1_000_000 / elapsed if count >= 65536 else None,
                  tested_at=time.time())
    return result


class ProxyBenchmarks:
    def __init__(self, store):
        self.store = store
        self.task = None
        self.job = None
        self.workers = {}

    def view(self):
        if self.job is None:
            return {'job': None}
        public = copy.deepcopy(self.job)
        public.pop('configs', None)
        fingerprints = public.pop('fingerprints', {})
        current, _ = ProxyPool(self.store)._read()
        for key, fingerprint in fingerprints.items():
            if key not in current['nodes'] or node_fingerprint(current['nodes'][key]) != fingerprint:
                public['results'][key] = {'status': 'stale', 'error': '节点配置已变化，请重新测速'}
        return {'job': public}

    def start(self, ids: list[str], mode: str, version: int):
        if mode not in {'latency', 'download'} or not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(not isinstance(key, str) for key in ids) or len(set(ids)) != len(ids):
            raise ValueError('测速参数无效')
        data, current = ProxyPool(self.store)._read()
        if current != version:
            raise SpiritScoutError('代理池已更新，请刷新')
        if any(key not in data['nodes'] or node_kind(data['nodes'][key]) != 'proxy' for key in ids):
            raise ValueError('测速节点不存在或是订阅提示')
        if not executable():
            raise SpiritScoutError('Mihomo 运行时不可用，请先准备内核')
        running = self.task is not None and not self.task.done()
        if running and (self.job['mode'] != 'latency' or mode != 'latency'):
            raise SpiritScoutError('已有节点测速正在进行，请等待或停止后再测')
        if not running:
            self.job = {'id': new_id('probe'), 'mode': mode, 'status': 'running', 'started_at': time.time(),
                        'configs': {}, 'fingerprints': {}, 'results': {}}
            self.workers = {}
        job = self.job
        active = {key for key, worker in self.workers.items() if not worker.done()}
        for key in ids:
            if key in active:
                continue
            node = copy.deepcopy(data['nodes'][key])
            job['configs'][key] = node
            job['fingerprints'][key] = node_fingerprint(node)
            job['results'][key] = {'status': 'queued'}
            self.workers[key] = asyncio.create_task(self._run_node(job, key, node))
        if not running:
            self.task = asyncio.create_task(self._run(job))
        return self.view()

    async def _run_node(self, job, key, node):
        # 每个节点独立完成和落盘，不等待同批其他节点。
        job['results'][key] = {'status': 'testing'}
        try:
            async with node_proxy(node) as proxy:
                result = await measure(proxy, job['mode'])
        except asyncio.CancelledError:
            raise
        except (SpiritScoutError, OSError, TimeoutError):
            result = {'status': 'error', 'latency_ms': None, 'speed_mb_s': None,
                      'tested_at': time.time(), 'error': '测速运行时启动失败或超时'}
        current, _ = ProxyPool(self.store)._read()
        if key not in current['nodes'] or node_fingerprint(node) != node_fingerprint(current['nodes'][key]):
            job['results'][key] = {'status': 'stale', 'error': '节点配置已变化，请重新测速'}
            return
        if job['mode'] == 'latency':
            previous = self.store.setting('proxy_measurements', key)
            if previous and previous['data']['fingerprint'] == node_fingerprint(node):
                old = previous['data']['result']
                if old.get('speed_mb_s') is not None:
                    result.update({field: old.get(field) for field in ('speed_mb_s', 'download_bytes', 'download_seconds', 'download_complete', 'speed_error')})
                    result['speed_tested_at'] = old.get('speed_tested_at', old.get('tested_at'))
        elif result.get('speed_mb_s') is not None:
            result['speed_tested_at'] = result['tested_at']
        previous = self.store.setting('proxy_measurements', key)
        self.store.save('proxy_measurements', key, {'fingerprint': node_fingerprint(node), 'result': result}, previous['version'] if previous else 0)
        job['results'][key] = result

    async def _run(self, job):
        try:
            while True:
                active = [worker for worker in self.workers.values() if not worker.done()]
                if not active:
                    break
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for worker in done:
                    if not worker.cancelled() and worker.exception() is not None:
                        key = next((item for item, task in self.workers.items() if task is worker), None)
                        if key and job['results'].get(key, {}).get('status') in {'queued', 'testing'}:
                            job['results'][key] = {'status': 'error', 'error': '测速任务执行失败'}
            job['status'] = 'completed'
        except asyncio.CancelledError:
            job['status'] = 'cancelled'
            raise
        except Exception:
            # 后台故障必须成为可见终态，不能留在永久“测速中”；不回显内核日志和凭据。
            job['status'] = 'error'
        finally:
            workers = list(self.workers.values())
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for key, value in job['results'].items():
                if value['status'] in {'queued', 'testing'}:
                    job['results'][key] = {'status': 'cancelled' if job['status'] == 'cancelled' else 'error'}
            job['finished_at'] = time.time()
            job.pop('configs', None)

    async def close(self):
        task, job = self.task, self.job
        if task is not None and not task.done():
            if not task.cancelling():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
            workers = list(self.workers.values())
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if task.done() and job['status'] == 'running':
                # 在后台协程首次执行之前停止，不会进入 _run 的 finally。
                job['status'] = 'cancelled'
                job['finished_at'] = time.time()
                job.pop('configs', None)
                job['results'] = {key: {'status': 'cancelled'} for key in job['results']}
