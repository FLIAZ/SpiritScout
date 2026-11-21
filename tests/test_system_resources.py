"""资源口径、进程树和共享采样缓存；不启动模型或后台任务。"""
from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.web.system_resources import ResourceMonitor, mount_system_resources


class SystemResourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.proc = Path(temporary.name)
        self.monitor = ResourceMonitor(self.proc, pid=10)
        self.monitor._page_size = 4096
        self.monitor._ticks = 100
        self.counters(100, 100, 10)
        self.process(10, 1, 0, 10, 2, 'server (worker)')
        self.process(11, 10, 100, 5, 3)
        self.process(12, 11, 200, 5, 4)
        self.process(99, 1, 0, 1000, 100)

    def counters(self, used: int, idle: int, uptime: float) -> None:
        (self.proc / 'stat').write_text(f'cpu {used} 0 0 {idle} 0 0 0 0 999 999\n')
        (self.proc / 'uptime').write_text(f'{uptime} 0\n')
        (self.proc / 'meminfo').write_text(
            'MemTotal: 1000 kB\nMemAvailable: 400 kB\nMemFree: 100 kB\n')

    def process(self, pid: int, parent: int, started: int, cpu: int, rss: int,
                name: str = 'worker') -> None:
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        fields = ['0'] * 22
        fields[0], fields[1] = 'S', str(parent)
        fields[11], fields[19], fields[21] = str(cpu), str(started), str(rss)
        (folder / 'stat').write_text(f'{pid} ({name}) ' + ' '.join(fields))

    def sample(self, now: float) -> dict:
        with patch('spiritscout.web.system_resources.time.monotonic', return_value=now):
            return self.monitor.snapshot()

    def test_available_memory_and_process_tree_are_not_free_memory_or_host_processes(self) -> None:
        data = self.sample(0)
        self.assertIsNone(data['system']['cpu_percent'])
        self.assertIsNone(data['application']['cpu_percent'])
        self.assertEqual(data['system']['memory_available'], 400 * 1024)
        self.assertEqual(data['system']['memory_used'], 600 * 1024)
        self.assertEqual(data['system']['memory_percent'], 60)
        self.assertEqual(data['application']['process_count'], 3)
        self.assertEqual(data['application']['memory_rss'], 9 * 4096)

    def test_cpu_is_normalized_to_host_and_new_children_count_only_recent_work(self) -> None:
        self.sample(0)
        self.counters(200, 400, 15)
        self.process(10, 1, 0, 30, 2)
        self.process(11, 10, 100, 15, 3)
        self.process(12, 11, 200, 15, 4)
        self.process(13, 10, 1200, 20, 2)
        self.process(14, 10, 500, 900, 1)  # 先前就存在，不能计入全部历史 CPU。
        data = self.sample(5)
        self.assertEqual(data['system']['cpu_percent'], 25)
        self.assertEqual(data['application']['cpu_percent'], 15)
        (self.proc / '11' / 'stat').unlink()
        data = self.sample(10)
        self.assertEqual(data['application']['process_count'], 3)

    def test_pid_reuse_and_counter_reset_do_not_create_cpu_spikes(self) -> None:
        self.sample(0)
        self.counters(200, 400, 15)
        self.process(11, 10, 1200, 8, 1)
        data = self.sample(5)
        self.assertEqual(data['application']['cpu_percent'], 2)
        self.counters(1, 2, 20)
        data = self.sample(10)
        self.assertIsNone(data['system']['cpu_percent'])
        self.assertIsNone(data['application']['cpu_percent'])

    def test_simultaneous_readers_share_one_nonblocking_capture_and_expire(self) -> None:
        with patch.object(self.monitor, '_capture', wraps=self.monitor._capture) as capture:
            with patch('spiritscout.web.system_resources.time.monotonic', return_value=0):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    snapshots = list(pool.map(lambda _: self.monitor.snapshot(), range(8)))
            self.assertEqual(capture.call_count, 1)
            self.assertTrue(all(data is snapshots[0] for data in snapshots))
            self.sample(4.99)
            self.assertEqual(capture.call_count, 1)
            self.sample(5)
            self.assertEqual(capture.call_count, 2)

    def test_unreadable_and_partial_data_are_explicit_and_failure_is_cached(self) -> None:
        with patch.object(Path, 'read_text', side_effect=PermissionError):
            data = self.sample(0)
        self.assertFalse(data['available'])
        self.assertIsNone(data['system'])
        self.assertIs(self.sample(1), data)
        self.assertTrue(self.sample(5)['available'])
        (self.proc / '12' / 'stat').write_text('malformed')
        self.assertTrue(self.sample(10)['application']['partial'])
        (self.proc / '10' / 'stat').unlink()
        self.assertIsNone(self.sample(15)['application'])

    def test_route_is_read_only_no_store_and_reports_failure_as_503(self) -> None:
        app = FastAPI()
        monitor = mount_system_resources(app)
        with TestClient(app) as client, patch.object(monitor, '_capture', self.monitor._capture):
            response = client.get('/api/system/resources')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(client.post('/api/system/resources').status_code, 405)
            monitor._last_read = float('-inf')
            with patch.object(monitor, '_capture', side_effect=PermissionError):
                response = client.get('/api/system/resources')
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json()['available'])
