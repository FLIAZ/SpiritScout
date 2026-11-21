"""MCP 调用泵的回填护栏：等待方中途取消不拆掉整条常驻会话。

``McpClient.call`` 的 Future 会被等待方（工具调用被取消/超时外层放弃）
在 ``session.call_tool`` 进行中取消；泵在回填时不检查 done 状态就会
抛 InvalidStateError 逃出 ``_pump`` → ``_serve`` 把那当成会话故障，
等待者被唤醒、后续调用全部拒绝——一次取消连坐整台服务器。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spiritscout.tools.mcp_client import McpClient, McpError, McpStdioClient


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


class ClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def client(self):
        started, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
        executed = []
        owners = []

        async def call_tool(tool, arguments, read_timeout_seconds=None):
            executed.append(tool)
            if tool == 'blocked':
                started.set()
                await release.wait()
            return _text_result()

        @asynccontextmanager
        async def session(*args, **kwargs):
            yield SimpleNamespace(initialize=initialize, list_tools=list_tools, call_tool=call_tool)

        async def initialize():
            return None

        async def list_tools():
            return SimpleNamespace(tools=[])

        class FixtureClient(McpClient):
            @asynccontextmanager
            async def _connect(self):
                owners.append(asyncio.current_task())
                try:
                    yield (None, None)
                finally:
                    owners.append(asyncio.current_task())
                    exited.set()

        self.enterContext(patch('spiritscout.tools.mcp_client.ClientSession', side_effect=session))
        client = FixtureClient('fixture')
        await client.start()
        self.addAsyncCleanup(client.close)
        return client, started, release, exited, executed, owners

    async def test_service_cancellation_and_forced_close_settle_all_callers(self):
        for mode in ('service_cancel', 'close_timeout', 'close_cancel', 'concurrent_close'):
            with self.subTest(mode=mode):
                client, started, release, exited, executed, owners = await self.client()
                first = asyncio.create_task(client.call('blocked'))
                await started.wait()
                queued = asyncio.create_task(client.call('queued'))
                await asyncio.sleep(0)
                if mode == 'service_cancel':
                    client._task.cancel()
                    await asyncio.gather(client._task, return_exceptions=True)
                elif mode == 'concurrent_close':
                    with patch('spiritscout.tools.mcp_client._CLOSE_TIMEOUT', .01):
                        closed = await asyncio.gather(client.close(), client.close(), return_exceptions=True)
                    self.assertEqual(closed, [None, None], '并发关闭共享同一次资源收口')
                elif mode == 'close_timeout':
                    with patch('spiritscout.tools.mcp_client._CLOSE_TIMEOUT', .01):
                        await client.close()
                else:
                    closing = asyncio.create_task(client.close())
                    await asyncio.sleep(0)
                    closing.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await closing
                results = await asyncio.wait_for(asyncio.gather(first, queued, return_exceptions=True), 1)
                self.assertTrue(all(isinstance(result, McpError) for result in results))
                self.assertIn('结果未知', str(results[0]))
                self.assertNotIn('结果未知', str(results[1]))
                self.assertEqual(executed, ['blocked'], '未执行的排队调用不能在故障后重放')
                self.assertTrue(exited.is_set())
                self.assertEqual(owners[0], owners[1], 'SDK 上下文由同一任务退出')
                self.assertFalse(client.connected)
                self.assertTrue(client._requests.empty())
                with self.assertRaises(McpError):
                    await client.call('after_close')

    async def test_real_stdio_sdk_call_and_forced_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / 'started'
            script = root / 'server.py'
            script.write_text("""import asyncio, json, sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP('lifecycle-fixture', log_level='ERROR')
@server.tool()
async def observe() -> str:
    print(json.dumps({'jsonrpc': '2.0', 'method': 'fixture/info',
                      'params': {'value': 'fixture-secret-must-not-be-logged'}}), flush=True)
    return 'ok'
@server.tool()
async def blocked() -> str:
    Path(sys.argv[1]).write_text('started')
    await asyncio.Event().wait()
    return 'unreachable'
server.run()
""")
            client = McpStdioClient('fixture', command=sys.executable,
                                    args=[str(script), str(marker)], timeout=30)
            self.addAsyncCleanup(client.close)
            tools = await asyncio.wait_for(client.start(), 10)
            self.assertEqual({tool['name'] for tool in tools}, {'observe', 'blocked'})
            with self.assertLogs(level='WARNING') as logs:
                self.assertEqual((await client.call('observe'))['text'], 'ok')
            self.assertIn('MCP 通知校验失败', '\n'.join(logs.output))
            self.assertNotIn('fixture-secret-must-not-be-logged', '\n'.join(logs.output))
            call = asyncio.create_task(client.call('blocked'))
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(.01)
            with patch('spiritscout.tools.mcp_client._CLOSE_TIMEOUT', .05):
                await asyncio.wait_for(client.close(), 5)
            with self.assertRaisesRegex(McpError, '结果未知'):
                await asyncio.wait_for(call, 1)
            self.assertFalse(client.connected)

    async def test_graceful_close_rejects_new_calls_and_finishes_existing_call(self):
        client, started, release, exited, executed, owners = await self.client()
        first = asyncio.create_task(client.call('blocked'))
        await started.wait()
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        with self.assertRaises(McpError):
            await client.call('late')
        release.set()
        await asyncio.wait_for(closing, 1)
        self.assertEqual((await first)['text'], 'ok')
        self.assertEqual(executed, ['blocked'])
        self.assertTrue(exited.is_set())
        self.assertEqual(owners[0], owners[1])
        await client.close()  # 重复关闭没有残留 Future 或重复执行。


if __name__ == "__main__":
    unittest.main()
