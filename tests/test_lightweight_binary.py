"""轻量结构化二进制读取的参数校验和真实沙箱解析回归。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.binary_tools import inspect
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec

R2_AVAILABLE = bool(shutil.which("r2") or (Path(__file__).resolve().parents[1] / ".venv/bin/r2").exists())


class BinaryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "attachments").mkdir()
        shutil.copyfile("/usr/bin/true", self.root / "attachments/sample.bin")
        registry = ToolRegistry(None)
        registry.register_builtin("sandbox_exec", sandbox_exec, ToolSpec(
            name="sandbox_exec", kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
        ctx = SimpleNamespace(mode=RunMode.CTF, shared_cache=None, tools=registry, search_enabled=False,
            gateway=SimpleNamespace(config=SimpleNamespace(lightweight_local=True)),
            builtin_ctx={"workspace_root": str(self.root), "_run_mode": RunMode.CTF, "_lightweight_local": True})
        ctx.for_agent = lambda role: ctx
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))
        self.task = Task(task_id="one", agent_type=self.agent.agent_type, task_type="ctf_single", target="",
                         payload={"challenge": {"id": "one"}})

    async def test_malformed_arguments_are_tool_errors_not_runtime_failures(self):
        for arguments in ("not-json-object", {"path": "/missing", "operation": "unknown"}):
            result = await self.agent._run_tool_call_inner(self.task, None, {"messages": []},
                {"name": "binary_inspect", "arguments": arguments})
            self.assertFalse(result.ok)

    @unittest.skipUnless(sandbox_available() and R2_AVAILABLE, "需要现有 Linux/bwrap/r2")
    async def test_unrecognized_zip_and_source_are_not_successful_binary_information(self):
        archive = self.root / 'attachments/source.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('app.rb', 'puts "fixture"\n')
        source = self.root / 'attachments/app.rb'
        source.write_text('puts "fixture"\n')
        for path in (archive, source):
            with self.subTest(path=path.name):
                result = await inspect(self.agent, self.task, None, {'path': str(path), 'operation': 'info'})
                self.assertFalse(result.ok, result.text)
                self.assertIn('未识别', json.loads(result.text)['error'])
                self.assertTrue(result.metadata['observation']['empty'])

    async def test_invalid_operation_address_and_external_paths_are_rejected(self):
        path = str(self.root / "attachments/sample.bin")
        for arguments in ({"path": path, "operation": "unknown"},
                          {"path": path, "operation": "disasm", "address": "main;!id"},
                          {"path": "/etc/passwd", "operation": "info"}):
            result = await inspect(self.agent, self.task, None, arguments)
            self.assertFalse(result.ok)

    @unittest.skipUnless(sandbox_available() and R2_AVAILABLE, "需要现有 Linux/bwrap/r2")
    async def test_live_info_functions_and_disassembly_are_complete_json(self):
        path = str(self.root / "attachments/sample.bin")
        info = await inspect(self.agent, self.task, None, {"path": path, "operation": "info"})
        self.assertTrue(info.ok, info.text)
        self.assertIn("ELF", json.loads(info.text)["info"]["class"])
        functions = await inspect(self.agent, self.task, None, {"path": path, "operation": "functions", "limit": 32})
        self.assertTrue(functions.ok, functions.text)
        data = json.loads(functions.text)
        self.assertTrue(data["items"])
        self.assertLess(len(functions.text.encode()), 4000)
        address = data["items"][0]["offset"]
        disasm = await inspect(self.agent, self.task, None,
            {"path": path, "operation": "disasm", "address": str(address), "limit": 12})
        self.assertTrue(disasm.ok, disasm.text)
        self.assertTrue(json.loads(disasm.text)["items"])

    @unittest.skipUnless(sandbox_available() and R2_AVAILABLE, "需要现有 Linux/bwrap/r2")
    async def test_large_requested_page_is_paginated_instead_of_rejected(self):
        path = str(self.root / "attachments/sample.bin")
        functions = await inspect(self.agent, self.task, None, {"path": path, "operation": "functions", "limit": 200})
        self.assertTrue(functions.ok, functions.text)
        data = json.loads(functions.text)
        self.assertEqual(data["requested_limit"], 200)
        self.assertLess(len(functions.text.encode()), 4000)
        address = data["items"][0]["offset"]
        result = await inspect(self.agent, self.task, None, {"path": path, "operation": "disasm", "address": str(address), "limit": 200})
        self.assertTrue(result.ok, result.text)
        decoded = json.loads(result.text)
        self.assertEqual(decoded["requested_limit"], 200)
        self.assertIn("name", decoded["function"])
        self.assertTrue(all("disasm" in item for item in decoded["items"]))

    @unittest.skipUnless(sandbox_available() and R2_AVAILABLE, "需要现有 Linux/bwrap/r2")
    async def test_inside_function_addresses_and_page_cursors_advance(self):
        path = str(self.root / "attachments/sample.bin")
        functions = await inspect(self.agent, self.task, None, {"path": path, "operation": "functions", "limit": 64})
        page = json.loads(functions.text)
        rows = list(page['items'])
        while page['has_more']:
            page = json.loads((await inspect(self.agent, self.task, None, {
                "path": path, "operation": "functions", "limit": 64, "offset": page['next_offset']})).text)
            rows.extend(page['items'])
        largest = max(rows, key=lambda row: row.get("size", 0))
        first = json.loads((await inspect(self.agent, self.task, None, {
            "path": path, "operation": "disasm", "address": str(largest["offset"]), "limit": 3})).text)
        self.assertTrue(first["has_more"])
        inside = first["items"][1]["offset"]
        shifted = json.loads((await inspect(self.agent, self.task, None, {
            "path": path, "operation": "disasm", "address": hex(inside), "limit": 3})).text)
        self.assertEqual(shifted["items"][0]["offset"], inside)
        self.assertEqual(shifted["items"][0]["address"], hex(inside))
        self.assertEqual(first['next_call']['name'], 'binary_inspect')
        self.assertEqual(first['next_call']['arguments']['offset'], first['next_offset'])
        self.assertEqual(first['next_call']['arguments']['address'], hex(largest['offset']))
        following = json.loads((await inspect(self.agent, self.task, None, first['next_call']['arguments'])).text)
        self.assertEqual(following["items"][0]["address"], first["next_address"])
        self.assertGreater(following["items"][0]["offset"], first["items"][-1]["offset"])

    @unittest.skipUnless(sandbox_available() and R2_AVAILABLE, "需要现有 Linux/bwrap/r2")
    async def test_binary_inspect_can_read_current_persistent_tmp_without_copying(self):
        result = await sandbox_exec(f'cp "{self.root}/attachments/sample.bin" /tmp/sample.bin',
            workspace_root=str(self.root), workspace_scope="one", _run_mode=RunMode.CTF, _lightweight_local=True)
        self.assertTrue(result.ok, result.error)
        result = await inspect(self.agent, self.task, None, {"path": "/tmp/sample.bin", "operation": "info"})
        self.assertTrue(result.ok, result.text)
        self.assertIn("ELF", json.loads(result.text)["info"]["class"])

    @unittest.skipUnless(sandbox_available() and R2_AVAILABLE, "需要现有 Linux/bwrap/r2")
    async def test_live_pagination_does_not_split_json(self):
        path = str(self.root / "attachments/sample.bin")
        result = await inspect(self.agent, self.task, None, {"path": path, "operation": "strings", "limit": 32})
        self.assertTrue(result.ok, result.text)
        data = json.loads(result.text)
        self.assertLess(len(result.text.encode()), 4000)
        self.assertEqual(data["next_offset"], len(data["items"]))
