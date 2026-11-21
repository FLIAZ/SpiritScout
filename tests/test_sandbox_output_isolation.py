"""同题同名并行调用必须保留各自的完整输出证据。"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from spiritscout.tools.sandbox import sandbox_exec


@unittest.skipUnless(shutil.which('bwrap'), '需要 bubblewrap 验证实际并行日志')
class SandboxOutputIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_scope_and_label_do_not_overwrite_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            results = await asyncio.gather(*(sandbox_exec(
                f'printf "proof-{i}\\n"', workspace_root=temporary,
                workspace_scope='same-challenge',log_name='output_solve') for i in range(4)))
            paths = [Path(result.parsed['output_file']) for result in results]
            self.assertEqual(len(set(paths)),4)
            for i,(result,path) in enumerate(zip(results,paths)):
                self.assertTrue(result.ok,result.error)
                self.assertEqual(path.read_text(),f'proof-{i}\n')
                self.assertEqual(path.parent,Path(temporary)/'sandbox/same-challenge')

    async def test_live_output_is_context_local_and_does_not_change_result_window(self):
        from spiritscout.tools.output import command_output

        with tempfile.TemporaryDirectory() as temporary:
            async def execute(label):
                chunks = []
                token = command_output.set(lambda channel, data: chunks.append((channel, data)))
                try:
                    result = await sandbox_exec(f"printf '{label}-out'; printf '{label}-err' >&2",
                        workspace_root=temporary, workspace_scope=label)
                finally:
                    command_output.reset(token)
                return result, chunks
            first, second = await asyncio.gather(execute('first'), execute('second'))
            for label, (result, chunks) in zip(('first', 'second'), (first, second)):
                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.stdout, label + '-out')
                self.assertEqual(result.stderr, label + '-err')
                self.assertEqual(b''.join(data for channel, data in chunks if channel == 'stdout'), (label + '-out').encode())
                self.assertEqual(b''.join(data for channel, data in chunks if channel == 'stderr'), (label + '-err').encode())
            self.assertIsNone(command_output.get())


class ExecutorLiveOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_callback_keeps_full_stream_but_same_capped_model_result(self):
        from spiritscout.tools.executor import _read_capped
        from spiritscout.tools.output import command_output

        data = b'head' + b'x' * 5000 + b'tail'
        async def read(callback=None):
            stream = asyncio.StreamReader()
            stream.feed_data(data)
            stream.feed_eof()
            token = command_output.set(callback)
            try:
                return await _read_capped(stream, 1000, 'stderr')
            finally:
                command_output.reset(token)
        expected = await read()
        chunks = []
        actual = await read(lambda channel, value: chunks.append((channel, value)))
        self.assertEqual(actual, expected)
        self.assertEqual(b''.join(value for _, value in chunks), data)
        self.assertTrue(all(channel == 'stderr' for channel, _ in chunks))
