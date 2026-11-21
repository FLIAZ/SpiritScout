import unittest

from spiritscout.assets.providers.oneforall import OneForAllProvider


class OneForAllProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_installation_is_reported_without_network_access(self) -> None:
        result = await OneForAllProvider("/definitely/missing/oneforall").collect("example.test")
        self.assertEqual(result.status, "not_configured")
        self.assertEqual(result.candidates, [])


    async def test_real_subprocess_streams_before_finishing_and_parses_large_json(self):
        import asyncio
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            release = path / "release"
            (path / "oneforall.py").write_text('''
import time, sys
from pathlib import Path
class OneForAll:
    def __init__(self, **kwargs): pass
    def run(self):
        print("progress-before-result", flush=True)
        print("stderr-progress", file=sys.stderr, flush=True)
        while not Path("release").exists(): time.sleep(.01)
        return [{"subdomain": f"host{i}.example.com", "title": "x" * 2000} for i in range(100)]
''')
            entered = asyncio.Event()
            output = []
            def on_output(message):
                output.append(message)
                if "progress-before-result" in message:
                    entered.set()
            task = asyncio.create_task(OneForAllProvider(root).collect("example.com", on_output=on_output))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                self.assertFalse(task.done())
            finally:
                release.touch()
            result = await asyncio.wait_for(task, 5)
            self.assertEqual(result.status, "ok")
            self.assertEqual(len(result.candidates), 100)
            self.assertTrue(any("stderr-progress" in line for line in output))
            self.assertFalse(any("__SPIRITSCOUT_JSON__" in line or '"subdomain"' in line for line in output))

    async def test_cancellation_reaps_the_real_subprocess(self):
        import asyncio
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "oneforall.py").write_text('''
import os, time
class OneForAll:
    def __init__(self, **kwargs): pass
    def run(self):
        print(f"pid:{os.getpid()}", flush=True)
        time.sleep(60)
        return []
''')
            entered = asyncio.Event()
            pids = []
            def on_output(message):
                if "pid:" in message:
                    pids.append(int(message.rsplit(":", 1)[1]))
                    entered.set()
            task = asyncio.create_task(OneForAllProvider(root).collect("example.com", on_output=on_output))
            try:
                await asyncio.wait_for(entered.wait(), 5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            with self.assertRaises(ProcessLookupError):
                os.kill(pids[0], 0)
