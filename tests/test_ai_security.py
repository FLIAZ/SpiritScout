"""离线 AI：实际文件/数值、真实沙箱与小模型按需目录，均不调用模型。"""
from __future__ import annotations

import hashlib
import json
import pickle
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_single_executor import context

from spiritscout.core.models import RunMode, Task
from spiritscout.ctf import ai_worker
from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.resources.runtime import SkillRuntime
from spiritscout.resources.store import ResourceStore
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def inspect(self, path, **kwargs):
        return ai_worker.run({'operation': 'artifact', 'path': str(path), 'offset': 0, 'limit': 2, **kwargs})

    def test_pytorch_zip_pages_are_disjoint_and_do_not_extract(self):
        path = self.root / 'unknown.bin'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('weights/data.pkl', pickle.dumps({'example': 2}))
            archive.writestr('../../must-not-extract', 'untrusted content')
            archive.writestr('weights/data/0', b'\0' * 8)
        first = self.inspect(path)
        second = self.inspect(path, offset=first['next_offset'])
        self.assertEqual(first['format'], 'pytorch_zip')
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])
        self.assertEqual(first['source']['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(len(first['items']) + len(second['items']), 3)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_pickle_reducer_is_only_disassembled(self):
        marker = self.root / 'should-not-exist'

        class Payload:
            def __reduce__(self):
                return eval, (f'open({str(marker)!r}, "w").write("executed")',)

        path = self.root / 'unknown.pt'
        path.write_bytes(pickle.dumps(Payload()))
        result = self.inspect(path, limit=32)
        self.assertIn('REDUCE', [row['opcode'] for row in result['items']])
        self.assertFalse(result['executed'])
        self.assertEqual(result['safety_verdict'], 'unknown')
        self.assertFalse(marker.exists())

    def test_safetensors_uses_real_header_and_rejects_invalid_offsets(self):
        path = self.root / 'misleading.pt'
        header = {'layer.weight': {'dtype': 'F32', 'shape': [2], 'data_offsets': [0, 8]}}
        raw = json.dumps(header).encode()
        path.write_bytes(struct.pack('<Q', len(raw)) + raw + bytes(8))
        self.assertEqual(self.inspect(path)['items'][0]['shape'], [2])
        path.write_bytes(struct.pack('<Q', len(raw)) + raw + bytes(4))
        with self.assertRaisesRegex(ValueError, '偏移'):
            self.inspect(path)

    def test_oversize_headers_and_unknown_formats_fail_explicitly(self):
        path = self.root / 'file'
        for data in (b'not a model', struct.pack('<Q', 2**40), b'\x93NUMPY\x01\x00\xff\xff'):
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                self.inspect(path)

    def test_npz_member_metadata_does_not_load_objects(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('本地未安装 NumPy')
        path = self.root / 'samples.npz'
        np.savez(path, clean=np.zeros((3, 2), dtype='uint8'), objects=np.array([{'data': 1}], dtype=object))
        result = self.inspect(path, member='objects.npy')
        self.assertFalse(result['values_loaded'])
        self.assertIn('O', result['dtype'])
        self.assertEqual(self.inspect(path, member='clean.npy')['shape'], (3, 2))


class NumericTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('本地未安装 NumPy')
        self.np = np
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = (Path(self.tmp.name) / name for name in ('a.npy', 'b.npy'))

    def compare(self, candidate, original):
        self.np.save(self.a, candidate)
        self.np.save(self.b, original)
        return ai_worker.run({'operation': 'compare', 'path': str(self.a), 'reference': str(self.b)})

    def test_uint8_difference_does_not_wrap(self):
        result = self.compare(self.np.array([0, 255], dtype='uint8'), self.np.array([255, 0], dtype='uint8'))
        self.assertEqual(result['linf'], 255)
        self.assertEqual(result['l1'], 510)
        self.assertAlmostEqual(result['l2'], (2 * 255**2)**0.5)
        self.assertEqual(result['changed_elements'], 2)
        self.assertTrue(result['complete'])

    def test_shape_object_nonfinite_and_int_precision_fail(self):
        np = self.np
        for a, b in ((np.zeros((2, 1)), np.zeros(2)), (np.array([float('nan')]), np.zeros(1)),
                     (np.array([{}], dtype=object), np.zeros(1)), (np.array([2**63], dtype='uint64'), np.zeros(1)),
                     (np.array([]), np.array([]))):
            with self.subTest(dtype=a.dtype, shape=a.shape), self.assertRaises(ValueError):
                self.compare(a, b)

    def test_fortran_and_large_arrays_are_compared_completely(self):
        np = self.np
        original = np.zeros((300, 300), dtype='float32')
        candidate = np.asfortranarray(original.copy())
        candidate[-1, -1] = 0.125
        result = self.compare(candidate, original)
        self.assertEqual(result['elements'], 90000)
        self.assertEqual(result['changed_elements'], 1)
        self.assertEqual(result['linf'], 0.125)


class OfflineKnowledgeTests(unittest.TestCase):
    def test_chinese_discovery_and_reference_reads_work_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = SkillRuntime(ResourceStore(directory))
            for query, key in [('PyTorch', 'ai-offline-pytorch-artifacts'), ('对抗样本', 'ai-offline-adversarial-inputs'),
                               ('后门', 'ai-offline-backdoor-evaluation'), ('成员推断', 'ai-offline-model-privacy')]:
                result = runtime.execute('skill_search', {'query': query}, {})
                self.assertIn(key, [row['skill_id'] for row in result['items']])
                self.assertNotIn('content', str(result))
                opened = runtime.execute('skill_open', {'skill_id': key}, {})
                self.assertTrue(opened['ok'])
                self.assertLess(len(opened['content']), 2600)
            script = runtime.execute('skill_read', {'skill_id': 'ai-offline-environment',
                'path': 'references/runtime_probe.py'}, {})
            compile(script['content'], 'runtime_probe.py', 'exec')

    def test_tool_is_available_in_normal_ctf_and_deferred_in_lightweight(self):
        for light in (False, True):
            ctx = context(RunMode.CTF, light)
            cls = SingleCTFAgent if light else CTFBaseAgent
            agent = cls(ctx, flag_manager=FlagManager(local_only=True))
            self.assertIn('ai_inspect', [s['function']['name'] for s in agent.tool_schemas])
            if light:
                self.assertNotIn('ai_inspect', [s['function']['name'] for s in agent._model_tool_schemas()])


@unittest.skipUnless(sandbox_available(), '需要现有 Linux/bwrap')
class SandboxAITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'attachments').mkdir()
        ctx = context(RunMode.CTF)
        ctx.builtin_ctx = {'workspace_root': str(self.root), '_run_mode': RunMode.CTF, '_lightweight_local': True}
        ctx.tools = ToolRegistry(None)
        ctx.tools.register_builtin('sandbox_exec', sandbox_exec, ToolSpec(
            name='sandbox_exec', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
        self.agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True))
        self.task = Task(task_id='ai-test', agent_type=self.agent.agent_type, task_type='ctf_single', target='',
                         payload={'challenge': {'id': 'ai-test'}})
        self.route = SimpleNamespace(name='ai', route_id='ai')

    async def invoke(self, args):
        out = await self.agent._run_tool_call(self.task, self.route, {'turn': 1},
            {'id': 'ai-call', 'name': 'tool_invoke', 'arguments': {'name': 'ai_inspect', 'arguments': args}})
        self.assertTrue(out.ok, out.text)
        return json.loads(out.text)

    async def test_actual_sandbox_environment_and_pagination(self):
        result = await self.invoke({'operation': 'environment'})
        self.assertIn('numpy', result['packages'])
        self.assertFalse(result['imports_tested'])
        path = self.root / 'attachments/model.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for index in range(4):
                archive.writestr(f'data/{index}', 'x')
        first = await self.invoke({'operation': 'artifact', 'path': str(path), 'limit': 2})
        second = await self.invoke(first['next_call']['arguments'])
        self.assertEqual(second['offset'], 2)
        self.assertFalse(second['has_more'])

    async def test_real_numeric_validation_matches_saved_arrays(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('本地未安装 NumPy')
        path, reference = self.root / 'attachments/a.npy', self.root / 'attachments/b.npy'
        np.save(path, np.array([0, 255], dtype='uint8'))
        np.save(reference, np.array([255, 0], dtype='uint8'))
        result = await self.invoke({'operation': 'compare', 'path': str(path), 'reference': str(reference)})
        self.assertEqual(result['linf'], 255)
        self.assertTrue(result['complete'])

    async def test_pickle_is_not_executed_in_real_sandbox(self):
        (self.root / 'artifacts').mkdir()
        marker = self.root / 'artifacts/executed'

        class Payload:
            def __reduce__(self):
                return eval, (f'open({str(marker)!r}, "w").write("executed")',)

        path = self.root / 'attachments/example.pkl'
        path.write_bytes(pickle.dumps(Payload()))
        result = await self.invoke({'operation': 'artifact', 'path': str(path)})
        self.assertFalse(result['executed'])
        self.assertFalse(marker.exists())

    async def test_outside_paths_and_invalid_parameters_do_not_execute(self):
        for args in ({'operation': 'artifact', 'path': '/etc/passwd'}, {'operation': 'environment', 'limit': True},
                     {'operation': 'artifact', 'path': '/tmp/../../etc/passwd'}):
            with patch.object(self.agent.ctx.tools, 'call') as call:
                out = await self.agent._run_tool_call(self.task, self.route, {'turn': 1},
                    {'id': 'bad', 'name': 'ai_inspect', 'arguments': args})
                self.assertFalse(out.ok)
                call.assert_not_called()
