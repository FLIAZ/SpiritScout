"""Exa 凭据控制台与智能体模式搜索禁用的端到端接线。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from spiritscout.agents.business.agent import BusinessContextAgent
from spiritscout.agents.profiles import agent_system_prompt
from spiritscout.agents.sub.base import SubAgentContext
from spiritscout.agents.verifier import VerifierAgent
from spiritscout.config.settings import (
    McpServerConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.exceptions import PolicyViolation
from spiritscout.core.models import AgentType, Authorization, RunMode, Scope
from spiritscout.ctf.agents import CTFWebAgent
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.resources.store import ResourceStore
from spiritscout.runtime import _build_infra
from spiritscout.tools.registry import AgentToolView, ToolRegistry
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec
from spiritscout.tools.web_search import SEARCH_SPEC, effective_search, web_search
from spiritscout.web.app import create_app


class ExaSettingsTests(unittest.TestCase):
    def test_store_update_clear_and_restart_do_not_return_key_or_write_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'EXA_API_KEY': ''}):
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            app = create_app(cfg, [])
            endpoint = '/api/resources/search/exa'
            secret = 'exa-ui-fixture-not-a-real-key'
            with TestClient(app, base_url='http://127.0.0.1') as client:
                self.assertFalse(client.get(endpoint).json()['key_configured'])
                response = client.put(endpoint, json={'api_key_value': secret, 'expected_version': 0})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(secret, response.text)
                self.assertEqual(response.json()['key_source'], 'saved')
                self.assertTrue(response.json()['enabled'])
                self.assertFalse(response.json()['competition_allowed'])
                for url in (endpoint, '/api/resources/mcp'):
                    self.assertNotIn(secret, client.get(url).text)
                # 留空保留，环境变量不被前端写入动作改变。
                response = client.put(endpoint, json={'api_key_value': '', 'expected_version': 1, 'enabled': False})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['stored_key'])
                selected, key = effective_search(cfg.search, app.state.resources)
                self.assertEqual(key, secret)
                self.assertFalse(selected.enabled)
                self.assertEqual(os.environ['EXA_API_KEY'], '')
                loaded = ResourceStore(Path(tmp) / 'resources')
                self.assertEqual(effective_search(cfg.search, loaded)[1], secret)
                stale = client.put(endpoint, json={'api_key_value': secret, 'expected_version': 0})
                self.assertEqual(stale.status_code, 409)
                self.assertNotIn(secret, stale.text)
                # 密文落盘，包括 SQLite WAL 中也不出现原密钥。
                for file in Path(tmp).rglob('*'):
                    if file.is_file():
                        self.assertNotIn(secret.encode(), file.read_bytes())
                cleared = client.put(endpoint, json={'clear_key': True, 'enabled': False, 'expected_version': 2})
                self.assertEqual(cleared.status_code, 200)
                self.assertFalse(cleared.json()['key_configured'])

    def test_invalid_payload_never_echoes_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(SystemConfig(workspace=WorkspaceConfig(root=tmp)), [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                for body in ({'api_key_value': {'private': 'never-echo-this'}},
                             {'api_key_value': 'never-echo-this', 'unexpected': 'never-echo-this'},
                             {'api_key_value': 'never-echo-this\nheader: bad'},
                             {'api_key_value': 'never-echo-this', 'enabled': 'false'}):
                    response = client.put('/api/resources/search/exa', json=body)
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn('never-echo-this', response.text)

    def test_free_search_can_be_enabled_without_paid_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'EXA_API_KEY': ''}):
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            app = create_app(cfg, [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                response = client.put('/api/resources/search/exa', json={'enabled': True})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['enabled'])
                self.assertFalse(response.json()['key_configured'])
                self.assertEqual(response.json()['billing_policy'], 'free_first_paid_only_for_blockers')

    def test_environment_key_can_enable_without_copying_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'EXA_API_KEY': 'environment-fixture'}):
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            app = create_app(cfg, [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                response = client.put('/api/resources/search/exa', json={'enabled': True})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['key_source'], 'environment')
                self.assertFalse(response.json()['stored_key'])
                self.assertNotIn('environment-fixture', response.text)
                self.assertNotIn('api_key', app.state.resources.settings('search')[0]['data'])


class CompetitionSearchTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def policy(mode: RunMode) -> PolicyEngine:
        return PolicyEngine(mode, ScopeGuard(Scope(allowed_urls=['https://challenge.test'])))

    async def test_native_and_mcp_search_are_denied_before_executor(self) -> None:
        registry = ToolRegistry(SimpleNamespace(), policy=self.policy(RunMode.COMPETITION))
        network = AsyncMock(return_value=ToolResult(tool='web_search', ok=True))
        for name, flag in (('web_search', False), ('mcp__exa__web_search_exa', False),
                           ('mcp__mixed__web_search_exa', False), ('custom_lookup', True)):
            spec = ToolSpec(name=name, kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY, internet_search=flag)
            registry.register_builtin(name, network, spec)
            result = await registry.call(name, {'query': 'test'})
            self.assertFalse(result.ok)
            self.assertIn('智能体模式', result.error)
            with self.assertRaises(PolicyViolation):
                registry.policy.check_tool(spec)
            self.assertNotIn(name, AgentToolView(registry, AgentType.CTF_WEB).names())
        network.assert_not_awaited()
        service = SimpleNamespace(config=SimpleNamespace(enabled=True), api_key='fixture', search=AsyncMock())
        direct = await web_search(search=service, mode=RunMode.COMPETITION, query='test', reason='test')
        self.assertFalse(direct.ok)
        service.search.assert_not_awaited()

    async def test_other_modes_keep_search_capability(self) -> None:
        for mode in (RunMode.SRC, RunMode.CTF):
            registry = ToolRegistry(SimpleNamespace(), policy=self.policy(mode))
            fn = AsyncMock(return_value=ToolResult(tool='web_search', ok=True))
            registry.register_builtin('web_search', fn, SEARCH_SPEC)
            result = await registry.call('web_search', {'query': 'test'})
            self.assertTrue(result.ok)
            fn.assert_awaited_once()

    async def test_schemas_and_prompts_remove_search_in_competition(self) -> None:
        ctx = SubAgentContext(bus=Mock(), blackboard=Mock(), convergence=Mock(), gateway=Mock(),
                              tools=ToolRegistry(SimpleNamespace(), policy=self.policy(RunMode.COMPETITION)),
                              policy=self.policy(RunMode.COMPETITION), mode=RunMode.COMPETITION)
        ctf = CTFWebAgent(ctx)
        business = BusinessContextAgent(ctx)
        self.assertNotIn('web_search', [x['function']['name'] for x in ctf.tool_schemas])
        self.assertNotIn('web_search', [x['function']['name'] for x in business._tool_schemas()])
        verifier = object.__new__(VerifierAgent)
        verifier.ctx = ctx
        self.assertNotIn('web_search', [x['function']['name'] for x in verifier.schemas()])
        prompt = agent_system_prompt(AgentType.CTF_WEB, allow_search=False)
        self.assertIn('禁止公开网页搜索', prompt)
        self.assertNotIn('按需调用 web_search', prompt)

    async def test_competition_runtime_never_constructs_search_or_connects_exa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            cfg.blackboard.sqlite_path = str(root / 'bb.db')
            cfg.knowledge.sqlite_path = str(root / 'kb.db')
            cfg.audit_log_path = str(root / 'audit.jsonl')
            cfg.search.enabled = True
            cfg.mcp_servers = [McpServerConfig(name='thirdparty', transport='http', url='https://mcp.exa.ai/mcp')]
            auth = Authorization(auth_id='competition-test', mode=RunMode.COMPETITION,
                                 human_confirmed=True, scope=Scope(allowed_urls=['https://challenge.test']))
            with patch('spiritscout.tools.mcp_client.build_client') as connect, \
                 patch('spiritscout.tools.web_search.search_service') as make_search:
                infra = await _build_infra(cfg, [ModelEntry(model='local', provider='mock')], auth)
                try:
                    self.assertIsNone(infra.builtin_ctx['search'])
                    self.assertFalse(infra.mcp_tools)
                    connect.assert_not_called()
                    make_search.assert_not_called()
                finally:
                    await infra.close()
