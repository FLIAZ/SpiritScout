"""MCP 后台续握：握手不再门控开工。

宽限期内就绪的服务器照常开工前并入；超时的转入后台续握，就绪一台
注册一台（活目录）；收口时在握任务取消回收——慢/卡死的服务器（实测
npx 解析 @latest 可挂满 60s 握手超时）不再连累整场白等。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
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
from spiritscout.core.models import Authorization, RunMode, Scope
from spiritscout.runtime import _build_infra


def _fake_client(delay: float):
    """握手耗时可控的假 MCP 客户端。"""
    async def start():
        await asyncio.sleep(delay)
        return [{"name": "probe", "inputSchema": {"type": "object", "properties": {}},
                 "annotations": {"readOnlyHint": True}}]
    return SimpleNamespace(start=start, close=AsyncMock(),
                           call=AsyncMock(return_value={"text": "ok", "is_error": False}))


def _cfg(root: Path) -> SystemConfig:
    cfg = SystemConfig(
        workspace=WorkspaceConfig(root=str(root)),
        audit_log_path=str(root / "audit.jsonl"),
        mcp_servers=[
            McpServerConfig(name="fast", default_risk="readonly"),
            McpServerConfig(name="slow", default_risk="readonly"),
        ])
    cfg.blackboard.sqlite_path = str(root / "bb.db")
    cfg.knowledge.sqlite_path = str(root / "kb.db")
    return cfg


def _auth() -> Authorization:
    return Authorization(auth_id="test-mcp-bg", mode=RunMode.SRC, human_confirmed=True,
                         scope=Scope(allowed_domains=["allowed.test"]))


class McpBackgroundStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_server_does_not_block_build_and_joins_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fast, slow = _fake_client(0.0), _fake_client(1.0)
            with patch("spiritscout.tools.mcp_client.build_client",
                       side_effect=lambda spec: fast if spec.name == "fast" else slow), \
                 patch("spiritscout.runtime._MCP_STARTUP_GRACE", 0.2):
                t0 = time.monotonic()
                infra = await _build_infra(_cfg(root), [ModelEntry(model="test", provider="mock")], _auth())
                try:
                    build_elapsed = time.monotonic() - t0
                    # 慢服务器还在握，开工不被门控：构建耗时贴着宽限期而非贴着握手时长
                    self.assertLess(build_elapsed, 0.8)
                    # 快服务器开工前已并入，慢服务器尚未
                    self.assertIn("mcp__fast__probe", infra.mcp_tools)
                    self.assertNotIn("mcp__slow__probe", infra.mcp_tools)
                    # 后台续握完成：工具中途并入活目录且经注册表可调
                    await asyncio.gather(*infra.mcp_startup_tasks)
                    self.assertIn("mcp__slow__probe", infra.mcp_tools)
                    res = await infra.tools.call("mcp__slow__probe", {},
                                                 **infra.builtin_ctx)
                    self.assertTrue(res.ok)
                finally:
                    await infra.close()

    async def test_close_cancels_pending_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stuck = _fake_client(60.0)   # 卡死：若收口不取消会挂满 60s
            with patch("spiritscout.tools.mcp_client.build_client",
                       return_value=stuck), \
                 patch("spiritscout.runtime._MCP_STARTUP_GRACE", 0.1):
                infra = await _build_infra(_cfg(root), [ModelEntry(model="test", provider="mock")], _auth())
                t0 = time.monotonic()
                await infra.close()
                # 收口即时：在握任务被取消，不等待手握超时
                self.assertLess(time.monotonic() - t0, 5.0)

    async def test_cancel_during_grace_reaps_pending_handshakes(self) -> None:
        """宽限期内被中断（用户停止/装配异常）：在握握手任务同样取消回收。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {"started": False, "cancelled": False}

            async def stuck_start():
                state["started"] = True
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    state["cancelled"] = True
                    raise

            stuck = SimpleNamespace(start=stuck_start, close=AsyncMock(),
                                    call=AsyncMock(return_value={"text": "ok", "is_error": False}))
            with patch("spiritscout.tools.mcp_client.build_client", return_value=stuck), \
                 patch("spiritscout.runtime._MCP_STARTUP_GRACE", 30.0):
                build = asyncio.ensure_future(
                    _build_infra(_cfg(root), [ModelEntry(model="test", provider="mock")], _auth()))
                for _ in range(200):
                    if state["started"]:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(state["started"])
                build.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await build
                # 修复前：wait 返回前任务未登记，除异常路径扑空，握手任务游离
                self.assertTrue(state["cancelled"])


if __name__ == "__main__":
    unittest.main()
