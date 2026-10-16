"""取消初始化、日志轮转与并发证据写入回归。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import (
    McpServerConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.exceptions import VersionConflict
from spiritscout.core.models import (
    Authorization,
    BlackboardLayer,
    RunMode,
    Scope,
    Vulnerability,
    VulnStatus,
)
from spiritscout.runtime import _build_infra
from spiritscout.tools.sandbox import _RotatingLog
from spiritscout.verify.pipeline import VerifyPipeline


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

    async def test_stale_pipeline_write_preserves_concurrent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            stale = Vulnerability(vuln_id="v", title="audit", vuln_type="xss", target="audit",
                                  status=VulnStatus.SCREENED, evidence=[{"source": "old"}])
            bb.write(BlackboardLayer.VULN, "v", stale.model_dump(mode="json"))
            latest = stale.model_copy(deep=True)
            latest.evidence.append({"source": "peer"})
            bb.write(BlackboardLayer.VULN, "v", latest.model_dump(mode="json"), expected_version=1)
            pipeline = object.__new__(VerifyPipeline)
            pipeline.bb = bb
            pipeline.audit = NullAuditLogger()
            stale.status = VulnStatus.REPRODUCED
            with self.assertRaises(VersionConflict):
                pipeline._save("v", stale, 1)
            saved = bb.read(BlackboardLayer.VULN, "v")
            self.assertEqual(saved["evidence"], latest.model_dump(mode="json")["evidence"])
            self.assertEqual(saved["status"], "screened")
            # 中间态下轮仍会重新读取核验，不因放弃过期迁移而永久漏掉。
            pipeline.process = AsyncMock(return_value=latest)
            await pipeline.run()
            pipeline.process.assert_awaited_once_with("v", 2)

    async def test_terminal_verdict_is_not_reprocessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            verdict = Vulnerability(vuln_id="v", title="audit", vuln_type="xss", target="audit",
                                    status=VulnStatus.REJECTED_FP)
            bb.write(BlackboardLayer.VULN, "v", verdict.model_dump(mode="json"))
            pipeline = object.__new__(VerifyPipeline)
            pipeline.bb = bb
            result = await pipeline.process("v", 0)
            self.assertEqual(result.status, VulnStatus.REJECTED_FP)
            self.assertEqual(bb.read_versioned(BlackboardLayer.VULN, "v")[1], 1)


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
