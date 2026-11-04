"""轻量结构化二进制读取的参数校验和真实沙箱解析回归。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
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
    async def test_live_pagination_does_not_split_json(self):
        path = str(self.root / "attachments/sample.bin")
        result = await inspect(self.agent, self.task, None, {"path": path, "operation": "strings", "limit": 32})
        self.assertTrue(result.ok, result.text)
        data = json.loads(result.text)
        self.assertLess(len(result.text.encode()), 4000)
        self.assertEqual(data["next_offset"], len(data["items"]))
