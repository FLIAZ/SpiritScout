"""控制台资源快照：Linux procfs 只读采样，不进入模型上下文或持久化。"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class _Process:
    parent: int
    started: int
    cpu: int
    rss: int


@dataclass(frozen=True)
class _Reading:
    cpu: int
    idle: int
    uptime: float
    memory: dict[str, int]
    processes: dict[int, _Process]
    partial: bool


class ResourceMonitor:
    interval = 5.0

    def __init__(self, proc: Path = Path('/proc'), pid: int | None = None) -> None:
        self.proc = proc
        self.pid = os.getpid() if pid is None else pid
        self._page_size = os.sysconf('SC_PAGE_SIZE') if os.name == 'posix' else 4096
        self._ticks = os.sysconf('SC_CLK_TCK') if os.name == 'posix' else 100
        self._lock = threading.Lock()
        self._previous: _Reading | None = None
        self._cached: dict[str, Any] | None = None
        self._last_read = float('-inf')

    def _capture(self) -> _Reading:
        values = self.proc.joinpath('stat').read_text().splitlines()[0].split()
        if values[0] != 'cpu' or len(values) < 9:
            raise ValueError('invalid CPU counters')
        # guest/guest_nice 已包含在 user/nice 内，不能再次计入总算力。
        cpu = list(map(int, values[1:9]))
        memory = {}
        for line in self.proc.joinpath('meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in {'MemTotal', 'MemAvailable', 'MemFree'}:
                memory[key] = int(value.split()[0]) * 1024
        if memory.get('MemTotal', 0) <= 0 or 'MemAvailable' not in memory:
            raise ValueError('invalid memory counters')
        uptime = float(self.proc.joinpath('uptime').read_text().split()[0])
        processes = {}
        partial = False
        for path in self.proc.iterdir():
            if not path.name.isdecimal():
                continue
            try:
                # comm 可包含空格和括号；从最后的右括号之后解析固定字段。
                fields = path.joinpath('stat').read_text().rsplit(')', 1)[1].split()
                processes[int(path.name)] = _Process(
                    parent=int(fields[1]), started=int(fields[19]),
                    cpu=int(fields[11]) + int(fields[12]),
                    rss=max(0, int(fields[21])) * self._page_size,
                )
            except (FileNotFoundError, ProcessLookupError):
                continue  # 采样期间正常退出的进程。
            except (PermissionError, IndexError, ValueError):
                partial = True
        children: dict[int, list[int]] = {}
        for pid, process in processes.items():
            children.setdefault(process.parent, []).append(pid)
        tree = {}
        pending = [self.pid]
        while pending:
            pid = pending.pop()
            if pid in tree or pid not in processes:
                continue
            tree[pid] = processes[pid]
            pending.extend(children.get(pid, []))
        return _Reading(sum(cpu), cpu[3] + cpu[4], uptime, memory, tree, partial)

    def snapshot(self) -> dict[str, Any]:
        # 同步路由由 FastAPI 在线程池执行；锁由采样线程持有，客户端取消不会提前释放。
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._last_read < self.interval:
                return self._cached
            sampled_at = time.time()
            try:
                current = self._capture()
            except (OSError, ValueError):
                self._previous = None
                result = {'available': False, 'sampled_at': sampled_at,
                          'detail': '当前环境无法读取 Linux 系统资源',
                          'system': None, 'application': None}
            else:
                previous = self._previous
                total_delta = current.cpu - previous.cpu if previous else 0
                system_cpu = application_cpu = None
                if previous and total_delta > 0 and current.idle >= previous.idle:
                    system_cpu = min(100.0, max(0.0, 100 * (
                        1 - (current.idle - previous.idle) / total_delta)))
                    if self.pid in previous.processes and self.pid in current.processes:
                        used = 0
                        for pid, process in current.processes.items():
                            old = previous.processes.get(pid)
                            if old and old.started == process.started:
                                used += max(0, process.cpu - old.cpu)
                            elif process.started >= previous.uptime * self._ticks:
                                used += process.cpu
                        application_cpu = min(100.0, 100 * used / total_delta)
                total = current.memory['MemTotal']
                available = min(total, max(0, current.memory['MemAvailable']))
                used_memory = total - available
                result = {
                    'available': True, 'sampled_at': sampled_at,
                    'system': {'cpu_percent': system_cpu, 'memory_total': total,
                               'memory_used': used_memory, 'memory_available': available,
                               'memory_percent': 100 * used_memory / total},
                    'application': {
                        'cpu_percent': application_cpu,
                        'memory_rss': sum(p.rss for p in current.processes.values()),
                        'process_count': len(current.processes),
                        'partial': current.partial,
                    } if self.pid in current.processes else None,
                }
                self._previous = current
            self._cached = result
            self._last_read = now
            return result


def mount_system_resources(app: FastAPI) -> ResourceMonitor:
    monitor = ResourceMonitor()

    @app.get('/api/system/resources')
    def system_resources() -> JSONResponse:
        snapshot = monitor.snapshot()
        return JSONResponse(snapshot, status_code=200 if snapshot['available'] else 503,
                            headers={'Cache-Control': 'no-store'})

    return monitor
