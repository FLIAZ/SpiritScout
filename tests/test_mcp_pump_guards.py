"""MCP 调用泵的回填护栏：等待方中途取消不拆掉整条常驻会话。

``McpClient.call`` 的 Future 会被等待方（工具调用被取消/超时外层放弃）
在 ``session.call_tool`` 进行中取消；泵在回填时不检查 done 状态就会
抛 InvalidStateError 逃出 ``_pump`` → ``_serve`` 把那当成会话故障，
等待者被唤醒、后续调用全部拒绝——一次取消连坐整台服务器。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from spiritscout.tools.mcp_client import McpClient


def _text_result(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           isError=False)


class PumpFutureGuardTests(unittest.IsolatedAsyncioTestCase):
    async def _run_pump(self, *, remote_fails: bool) -> None:
        client = McpClient("t")
        loop = asyncio.get_running_loop()
        inflight: asyncio.Future = loop.create_future()
        alive: asyncio.Future = loop.create_future()
        inflight_tool = "boom" if remote_fails else "probe"

        async def call_tool(tool, arguments, read_timeout_seconds=None):
            if tool == inflight_tool and not inflight.done():
                inflight.cancel()   # 调用进行中，等待方被取消
            if tool == "boom":
                raise RuntimeError("remote boom")
            return _text_result()

        session = SimpleNamespace(call_tool=call_tool)
        await client._requests.put((inflight, inflight_tool, {}, 5.0))
        await client._requests.put((alive, "probe", {}, 5.0))
        await client._requests.put(None)
        await client._pump(session)   # 修复前：InvalidStateError 拆掉整条会话
        self.assertTrue(inflight.cancelled())
        self.assertEqual(alive.result()["text"], "ok")
        self.assertFalse(alive.cancelled())

    async def test_cancelled_future_success_path_keeps_pump(self) -> None:
        await self._run_pump(remote_fails=False)

    async def test_cancelled_future_exception_path_keeps_pump(self) -> None:
        await self._run_pump(remote_fails=True)


if __name__ == "__main__":
    unittest.main()
