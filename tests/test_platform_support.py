"""原生平台的真实锁竞争和 SQLite 并存，不依赖 Linux shell。"""

import asyncio
import contextlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from spiritscout.core.exceptions import ConfigurationError
from spiritscout.platform_support import chrome_executable, node_cli_command
from spiritscout.tools.run_lock import ExecutionRunLock


class PlatformLockTests(unittest.TestCase):
    def test_database_alias_lock_does_not_block_sqlite_or_grow_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "state.db"
            lock = ExecutionRunLock(root / "session", database)
            lock.acquire()
            try:
                with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute("CREATE TABLE sample(value TEXT)")
                    connection.execute("INSERT INTO sample VALUES ('persisted')")
                    self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], "persisted")
                self.assertLess(database.stat().st_size, 1024 * 1024)
                alias = root / "alias.db"
                os.link(database, alias)
                contender = ExecutionRunLock(root / "other", alias)
                try:
                    with self.assertRaises(ConfigurationError):
                        contender.acquire()
                    self.assertFalse(contender.owned)
                finally:
                    contender.close()
            finally:
                lock.close()

    def test_process_exit_releases_workspace_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "session"
            code = ("import sys; from pathlib import Path; "
                    "from spiritscout.tools.run_lock import ExecutionRunLock; "
                    "lock=ExecutionRunLock(Path(sys.argv[1])); lock.acquire(); "
                    "print('ready',flush=True); sys.stdin.readline()")
            with subprocess.Popen([sys.executable, "-c", code, str(workspace)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
                try:
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                    contender = ExecutionRunLock(workspace)
                    with self.assertRaises(ConfigurationError):
                        contender.acquire()
                finally:
                    child.kill()
                    child.wait(timeout=10)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        contender.acquire()
                        break
                    except ConfigurationError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
                contender.close()


class PlatformCommandTests(unittest.TestCase):
    def test_dashboard_timezone_is_available_on_native_platform(self):
        from datetime import datetime

        self.assertEqual(datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")).utcoffset().total_seconds(), 8 * 3600)

    def test_windows_venv_console_scripts_are_resolved(self):
        from spiritscout.tools.mcp_client import _resolve_command

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / ".venv/Scripts/idalib-mcp.exe"
            script.parent.mkdir(parents=True)
            script.touch()
            with patch("spiritscout.tools.mcp_client.sys.platform", "win32"), \
                    patch("spiritscout.tools.mcp_client._project_root", return_value=root):
                self.assertEqual(_resolve_command(".venv/bin/idalib-mcp"), str(script))

    def test_windows_node_wrapper_never_invokes_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrapper = root / "npx.cmd"
            cli = root / "node_modules/npm/bin/npx-cli.js"
            cli.parent.mkdir(parents=True)
            cli.write_text("// fixture", encoding="utf-8")
            with patch("spiritscout.platform_support.sys.platform", "win32"), \
                    patch("shutil.which", side_effect=lambda name: str(wrapper) if name == "npx" else "node"):
                self.assertEqual(node_cli_command("npx"), ["node", str(cli)])

    def test_mac_application_browser_and_explicit_override(self):
        with patch("spiritscout.platform_support.sys.platform", "darwin"), \
                patch("shutil.which", return_value=None), \
                patch.object(Path, "is_file", lambda path: str(path).replace("\\", "/") ==
                             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
            self.assertIn("Google Chrome.app", chrome_executable({}))
        self.assertEqual(chrome_executable({"SPIRITSCOUT_CHROME_EXECUTABLE": "custom-browser"}), "custom-browser")


class PlatformProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_cleans_up_descendants_on_native_platform(self):
        from spiritscout.tools.executor import LocalExecutor
        from spiritscout.tools.spec import ToolKind, ToolSpec

        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "unwanted descendant output.txt"
            child = "import time,sys; from pathlib import Path; time.sleep(2); Path(sys.argv[1]).write_text('leaked')"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); print('started',flush=True); time.sleep(60)"
            spec = ToolSpec(name="timeout-fixture", kind=ToolKind.CLI, timeout_seconds=0.7)
            result = await LocalExecutor().run_argv(spec, [sys.executable, "-c", parent, child, str(marker)])
            self.assertTrue(result.timed_out)
            self.assertIn("started", result.stdout)
            await asyncio.sleep(2.1)
            self.assertFalse(marker.exists(), "工具超时后不能留下继续写入的后代进程")
