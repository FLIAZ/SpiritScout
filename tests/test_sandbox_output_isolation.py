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
