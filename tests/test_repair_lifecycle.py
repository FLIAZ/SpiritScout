"""取消初始化、日志轮转与并发证据写入回归。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.config.settings import (
    McpServerConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.models import (
    Authorization,
    RunMode,
    Scope,
)
from spiritscout.runtime import _build_infra
from spiritscout.tools.sandbox import _RotatingLog


class LifecycleRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_mcp_start_closes_all_created_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp),
                               audit_log_path=str(root / "audit.jsonl"),
                               mcp_servers=[McpServerConfig(name="fast"), McpServerConfig(name="slow")])
            cfg.blackboard.sqlite_path = str(root / "bb.db")
            cfg.knowledge.sqlite_path = str(root / "kb.db")
            entered = asyncio.Event()

            async def slow_start():
                entered.set()
                await asyncio.Event().wait()

            fast = SimpleNamespace(start=AsyncMock(return_value=[]), close=AsyncMock())
            slow = SimpleNamespace(start=slow_start, close=AsyncMock())
            auth = Authorization(auth_id="audit", mode=RunMode.CTF,
                                 scope=Scope(local_analysis=True), human_confirmed=True)
            with patch("spiritscout.tools.mcp_client.build_client",
                       side_effect=lambda spec: fast if spec.name == "fast" else slow):
                task = asyncio.create_task(_build_infra(
                    cfg, [ModelEntry(model="audit", provider="mock")], auth))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            fast.close.assert_awaited_once()
            slow.close.assert_awaited_once()


class LogRepairTests(unittest.TestCase):
    def test_legacy_oversized_log_is_reduced_to_a_tail_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "spiritscout.tools.sandbox._LOG_ROTATE_BYTES", 1024,
        ):
            path = Path(tmp) / "output.log"
            path.write_bytes(b"a" * 3000)
            writer = _RotatingLog(path)
            try:
                writer.write(b"new")
            finally:
                writer.close()
            self.assertEqual(path.read_bytes(), b"new")
            self.assertEqual(path.with_name("output.old.log").stat().st_size, 1024)

    def test_single_large_write_rotates_during_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "spiritscout.tools.sandbox._LOG_ROTATE_BYTES", 1024,
        ):
            path = Path(tmp) / "output.log"
            writer = _RotatingLog(path)
            try:
                data = bytes(range(256)) * 17
                writer.write(data)
                self.assertTrue(writer.rotated)
            finally:
                writer.close()
            previous = path.with_name("output.old.log")
            self.assertLessEqual(path.stat().st_size, 1024)
            self.assertLessEqual(previous.stat().st_size, 1024)
            retained = previous.read_bytes() + path.read_bytes()
            self.assertEqual(retained, data[-len(retained):])
