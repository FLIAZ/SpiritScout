"""确定性输入校验，以及真实 CyberChef / bubblewrap 往返；不调用模型。"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from spiritscout.agents.business.agent import BusinessContextAgent
from spiritscout.agents.profiles import get_profile
from spiritscout.agents.recon.agent import ReconAgent
from spiritscout.agents.verifier import VerifierAgent
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, RunMode, Scope, Task
from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.policy import PolicyEngine, ScopeGuard
from spiritscout.tools.cyberchef import (
    NAMES,
    SCHEMAS,
    SPECS,
    _argv,
    _input_bytes,
    cyberchef_bake,
    cyberchef_operations,
    runtime_paths,
)
from spiritscout.tools.executor import ToolExecutor
from spiritscout.tools.registry import AgentToolView, ToolRegistry
from spiritscout.tools.sandbox import sandbox_available

DECODE = [{'op': 'From Base64', 'args': ['A-Za-z0-9+/=', False, True]}]


class _NoCLI(ToolExecutor):
    async def run(self, spec, params):
        raise AssertionError('应调用真实 CyberChef builtin，而不是 CLI executor')


class CyberChefInputTests(unittest.TestCase):
    def test_file_ranges_and_scope_do_not_normalise_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'attachments').mkdir()
            file = root / 'attachments/input.bin'
            file.write_bytes(b'prefix\x00\xff\r\n suffix')
            self.assertEqual(_input_bytes(root, None, str(file), 6, 4), b'\x00\xff\r\n')
            self.assertEqual(_input_bytes(root, ' \r\n', None, 0, None), b' \r\n')
            (root / 'vault.key').write_bytes(b'private-fixture')
            link = root / 'attachments/link'
            link.symlink_to(root / 'vault.key')
            for path in ('vault.key', str(link), '/etc/passwd'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    _input_bytes(root, None, path, 0, None)
            with self.assertRaises(ValueError):
                _input_bytes(root, None, str(file), 6, 100)
            with self.assertRaises(ValueError):
                _input_bytes(root, 'inline', str(file), 0, None)

    def test_only_execution_roles_gain_tools_and_schemas(self):
        self.assertTrue(get_profile(AgentType.CTF_CRYPTO).allows_tool('cyberchef_bake'))
        self.assertFalse(get_profile(AgentType.MASTER).allows_tool('cyberchef_bake'))
        self.assertFalse(get_profile(AgentType.SECRETARY).allows_tool('cyberchef_bake'))
        self.assertEqual({s['function']['name'] for s in SCHEMAS}, NAMES)
        self.assertTrue(NAMES <= {s['function']['name'] for s in CTFBaseAgent.tool_schemas})
        for role in (AgentType.BUSINESS_CONTEXT, AgentType.RECON, AgentType.WEB_VULN,
                     AgentType.PORT_SCAN, AgentType.HOST_VULN, AgentType.POST_EXPLOIT, AgentType.VERIFIER):
            with self.subTest(role=role):
                self.assertTrue(all(get_profile(role).allows_tool(name) for name in NAMES))

    def test_local_conversion_is_not_independent_verifier_evidence(self):
        raw = {'verdict': 'confirmed', 'reason': 'decoded', 'harm': 'fixture harm',
               'rule_quote': 'fixture rule',
               'evidence': [{'execution_id': 'http', 'quote': 'fixture response'}],
               'controls': [{'execution_id': 'decode', 'quote': 'fixture decoded'}]}
        result = VerifierAgent.validate(raw, {
            'http': {'ok': True, 'text': 'fixture response', 'tool': 'http_probe'},
            'decode': {'ok': True, 'text': 'fixture decoded', 'tool': 'cyberchef_bake'},
        }, {'standing_rules': 'fixture rule'})
        self.assertEqual(result['verdict'], 'inconclusive')
        self.assertEqual(result['controls'], [])


class CyberChefLiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not sandbox_available():
            self.skipTest('需要 Linux/bubblewrap')
        try:
            self.runtime, self.node = runtime_paths()
        except ValueError as exc:
            self.skipTest(str(exc))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = ToolRegistry(_NoCLI())
        for fn in (cyberchef_operations, cyberchef_bake):
            self.registry.register_builtin(fn.__name__, fn, SPECS[fn.__name__])
        self.tools = AgentToolView(self.registry, AgentType.CTF_CRYPTO)

    async def bake(self, **kwargs):
        result = await self.tools.call('cyberchef_bake', kwargs, workspace_root=str(self.root))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.invocation_id, result.parsed['source_invocation_id'])
        return result

    async def test_exact_identifier_through_real_ctf_dispatch(self):
        agent = object.__new__(CTFBaseAgent)
        agent.ctx = SimpleNamespace(tools=self.tools, builtin_ctx={'workspace_root': str(self.root)},
                                    audit=NullAuditLogger())
        task = Task(task_id='decode-fixture', task_type='ctf_solve', target='',
                    agent_type=AgentType.CTF_CRYPTO)
        original = b'__verificatiionToken_0OIl\\"\r\n'
        outcome = await agent._run_tool_call_inner(
            task, SimpleNamespace(route_id='r1'), {}, {
                'name': 'cyberchef_bake', 'arguments': {
                    'input': base64.b64encode(original).decode(), 'recipe': DECODE}})
        self.assertTrue(outcome.ok, outcome.text)
        info = json.loads(outcome.text)
        self.assertEqual(Path(info['output_file']).read_bytes(), original)
        self.assertEqual(info['preview'], original.decode())
        self.assertEqual(info['output_sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(json.loads(Path(info['recipe_file']).read_text()), DECODE)

    async def test_multilayer_binary_and_file_result_reuse(self):
        original = b'\x00\xff\xfe\x80\r\n exact_nameI0l_'
        attachments = self.root / 'attachments'
        attachments.mkdir()
        source = attachments / 'captured.txt'
        encoded = base64.b64encode(gzip.compress(original))
        source.write_bytes(b'prefix' + encoded + b'suffix')
        first = await self.bake(input_file=str(source), offset=6, length=len(encoded),
                                recipe=[*DECODE, {'op': 'Gunzip'}])
        self.assertEqual(Path(first.parsed['output_file']).read_bytes(), original)
        self.assertEqual(first.parsed['preview_format'], 'hex')
        self.assertEqual(first.parsed['input_sha256'], hashlib.sha256(encoded).hexdigest())
        second = await self.bake(input_file=first.parsed['output_file'], recipe=[{'op': 'To Base64'}])
        self.assertEqual(Path(second.parsed['output_file']).read_bytes(), base64.b64encode(original))

    def src_context(self, role, **kwargs):
        self.registry.policy = PolicyEngine(RunMode.SRC, ScopeGuard(Scope(local_analysis=True)),
                                            audit=NullAuditLogger())
        return SimpleNamespace(tools=AgentToolView(self.registry, role),
            builtin_ctx={'workspace_root': str(self.root)}, audit=Mock(), blackboard=None,
            ring=None, mcp_tools={}, mode=RunMode.SRC, search_enabled=False, **kwargs)

    async def test_src_business_native_dispatch_has_real_cyberchef_access(self):
        agent = object.__new__(BusinessContextAgent)
        agent.agent_id = 'business-fixture'
        agent.ctx = self.src_context(AgentType.BUSINESS_CONTEXT)
        agent._explore = {}
        self.assertTrue(NAMES <= {s['function']['name'] for s in agent._tool_schemas(deferred=True)})
        task = Task(task_id='src-business', task_type='business_context',
                    agent_type=AgentType.BUSINESS_CONTEXT, target='https://fixture.invalid')
        text = await agent._run_tool_call(task, {'id': 'decode', 'name': 'cyberchef_bake',
            'arguments': {'input': 'X192ZXJpZmljYXRpaW9uVG9rZW4=', 'recipe': DECODE}})
        result = json.loads(text)
        self.assertEqual(Path(result['output_file']).read_bytes(), b'__verificatiionToken')
        verifier = object.__new__(VerifierAgent)
        verifier.ctx = self.src_context(AgentType.VERIFIER)
        self.assertTrue(NAMES <= {s['function']['name'] for s in verifier.schemas()})
        decoded = await verifier.ctx.tools.call('cyberchef_bake', {
            'input': 'YWJj', 'recipe': DECODE}, **verifier.ctx.builtin_ctx)
        self.assertTrue(decoded.ok, decoded.error)

    async def test_src_json_decision_queries_then_decodes_and_resumes(self):
        replies = [{'local_tool': {'name': 'cyberchef_operations', 'arguments': {'query': 'From Base64'}}},
                   {'local_tool': {'name': 'cyberchef_bake', 'arguments': {
                       'input': 'X192ZXJpZmljYXRpaW9uVG9rZW4=', 'recipe': DECODE}}},
                   {'paths': [], 'rationale': '已通过实际工具核对参数名'}]
        gateway = SimpleNamespace(chat=AsyncMock(side_effect=[
            SimpleNamespace(content=json.dumps(r), finish_reason='stop') for r in replies]))
        agent = object.__new__(ReconAgent)
        agent.agent_id = 'recon-fixture'
        agent.enable_llm_planning = True
        agent.ctx = self.src_context(AgentType.RECON, gateway=gateway)
        agent._world_state = lambda: {}
        agent._system_prompt = lambda: 'fixture rules'
        task = Task(task_id='src-json', task_type='recon', target='https://fixture.invalid',
                    agent_type=AgentType.RECON)
        result = await agent._llm_chat_json('path_planner', {'issue': '核对变量名'},
                                            task=task, _allow_ask=False)
        self.assertEqual(result, replies[-1])
        followup = gateway.chat.await_args_list[-1].args[0]
        self.assertIn('local_observations', followup[-1]['content'])
        self.assertIn('__verificatiionToken', followup[-1]['content'])
        self.assertEqual(len(list((self.root / 'artifacts').glob('cyberchef-*/result.bin'))), 1)

    async def test_src_local_bridge_cannot_dispatch_arbitrary_tools(self):
        gateway = SimpleNamespace(chat=AsyncMock(side_effect=[
            SimpleNamespace(content=json.dumps({'local_tool': {
                'name': 'sandbox_exec', 'arguments': {'code': 'echo forbidden'}}}), finish_reason='stop'),
            SimpleNamespace(content=json.dumps({'paths': []}), finish_reason='stop')]))
        agent = object.__new__(ReconAgent)
        agent.agent_id, agent.enable_llm_planning = 'recon-fixture', True
        agent.ctx = self.src_context(AgentType.RECON, gateway=gateway)
        agent._world_state = lambda: {}
        agent._system_prompt = lambda: 'fixture rules'
        with patch.object(agent.ctx.tools, 'call', new_callable=AsyncMock) as call:
            result = await agent._llm_chat_json('path_planner', {}, _allow_ask=False)
        self.assertEqual(result, {'paths': []})
        call.assert_not_awaited()
        feedback = json.loads(gateway.chat.await_args.args[0][-1]['content'])
        observation = feedback['patch'][0]['value'][0]
        self.assertEqual((observation['name'], observation['ok']), ('sandbox_exec', False))

    async def test_large_output_is_saved_in_full_and_not_presented_as_complete(self):
        original = ('a_variable_with_extra_ii_ 零\r\n' * 600).encode()
        result = await self.bake(input=base64.b64encode(original).decode(), recipe=DECODE)
        self.assertTrue(result.parsed['preview_truncated'])
        self.assertLess(len(result.stdout), 5500)
        self.assertEqual(Path(result.parsed['output_file']).read_bytes(), original)

    async def test_catalog_has_real_position_arguments_and_pagination(self):
        exact = await cyberchef_operations(query='From Base64', workspace_root=str(self.root))
        self.assertTrue(exact.ok, exact.error)
        args = exact.parsed['operations'][0]['args']
        self.assertEqual([arg['name'] for arg in args],
                         ['Alphabet', 'Remove non-alphabet chars', 'Strict mode'])
        page = await cyberchef_operations(query='base', limit=1, offset=1, workspace_root=str(self.root))
        self.assertTrue(page.ok, page.error)
        self.assertEqual(len(page.parsed['operations']), 1)
        self.assertTrue(page.parsed['has_more'])
        self.assertEqual(page.parsed['next_offset'], 2)

    async def test_strict_failure_unknown_operation_and_injected_context_do_not_succeed(self):
        for params in ({'input': 'YW!Jj', 'recipe': DECODE},
                       {'input': 'abc', 'recipe': [{'op': 'Imaginary Decoder'}]},
                       {'input': 'abc', 'recipe': [{'op': 'To Base64'}], 'workspace_root': '/etc'}):
            with self.subTest(params=params):
                result = await self.tools.call('cyberchef_bake', params, workspace_root=str(self.root))
                self.assertFalse(result.ok)
                self.assertTrue(result.error)
        self.assertEqual(list((self.root / 'artifacts').glob('cyberchef-*')), [])

    async def test_worker_cannot_read_host_workspace_or_environment_and_has_no_network(self):
        job = self.root / 'job'
        output = self.root / 'output'
        job.mkdir()
        output.mkdir()
        secret = self.root / 'vault.key'
        secret.write_text('fixture-private')
        connections = []

        def connected(reader, writer):
            connections.append(True)
            writer.close()

        server = await asyncio.start_server(connected, '127.0.0.1', 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        code = ('const fs=require("fs"), net=require("net"); '
                f'if(fs.existsSync({json.dumps(str(secret))}))process.exit(2); '
                'if(process.env.SPIRITSCOUT_TEST_SECRET)process.exit(3); '
                f'const s=net.connect({port},"127.0.0.1"); '
                's.on("error",()=>process.exit(0)); s.on("connect",()=>process.exit(4));')
        argv = _argv(self.runtime, self.node, job, output)
        argv[-1:] = ['-e', code]
        with patch.dict(os.environ, {'SPIRITSCOUT_TEST_SECRET': 'fixture-secret'}):
            proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
        self.assertEqual(proc.returncode, 0, (stdout + stderr).decode())
        self.assertFalse(connections)

    async def test_timeout_and_cancellation_remove_incomplete_results(self):
        def stalled(*args):
            argv = _argv(*args)
            argv[-1:] = ['-e', 'require("fs").writeFileSync("/output/started", "yes"); '
                         'setInterval(()=>{},1000);']
            return argv

        with patch('spiritscout.tools.cyberchef._argv', side_effect=stalled):
            result = await cyberchef_bake(input='abc', recipe=[{'op': 'To Base64'}],
                                          timeout=1, workspace_root=str(self.root))
            self.assertFalse(result.ok)
            self.assertTrue(result.timed_out)
            task = asyncio.create_task(cyberchef_bake(
                input='abc', recipe=[{'op': 'To Base64'}], workspace_root=str(self.root)))
            try:
                for _ in range(200):
                    if list((self.root / 'artifacts').glob('cyberchef-*/started')):
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(list((self.root / 'artifacts').glob('cyberchef-*/started')))
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(list((self.root / 'artifacts').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
