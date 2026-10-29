"""不同供应商的同名模型：配置、迁移、路由与 API 不串用。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from spiritscout.config import load_model_config
from spiritscout.config.model_identity import model_key
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.web import agentcfg, modelcfg
from spiritscout.web.app import create_app


class ProviderModelIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = self.root / 'models.yaml'
        self.agents = self.root / 'agents.yaml'
        modelcfg.write_config(self.models, [
            {'name': '供应商 A', 'provider': 'mock', 'base_url': 'http://a.invalid'},
            {'name': 'supplier-b', 'provider': 'mock', 'base_url': 'http://b.invalid'}], [
            {'model': 'org/shared/model', 'provider_ref': '供应商 A'}])
        self.key_a = model_key({'provider_ref': '供应商 A', 'model': 'org/shared/model'})
        self.key_b = model_key({'provider_ref': 'supplier-b', 'model': 'org/shared/model'})

    def add_b(self) -> None:
        modelcfg.add_entries(self.models, 'supplier-b', ['org/shared/model'], assignments_path=self.agents)

    def test_batch_add_keeps_same_id_under_other_provider_and_pins_existing_references(self) -> None:
        agentcfg.write_assignments(self.agents, {'master': 'org/shared/model'})
        agentcfg.set_naming_chain(self.agents, ['org/shared/model'], {'org/shared/model'})
        self.add_b()
        self.assertEqual(agentcfg.read_assignments(self.agents)['master'], self.key_a)
        self.assertEqual(agentcfg.read_naming_chain(self.agents), [self.key_a])
        registry = ModelRegistry(load_model_config(self.models))
        self.assertEqual(len(registry.all()), 2)
        self.assertEqual(registry.get(self.key_a).base_url, 'http://a.invalid')
        self.assertEqual(registry.get(self.key_b).base_url, 'http://b.invalid')
        self.assertEqual(registry.get(self.key_b).model, 'org/shared/model')
        with self.assertRaisesRegex(SpiritScoutError, '多个供应商'):
            registry.get('org/shared/model')
        _, added = modelcfg.add_entries(self.models, 'supplier-b', ['org/shared/model'])
        self.assertEqual(added, [])

    def test_existing_fallback_is_pinned_to_original_supplier(self) -> None:
        modelcfg.upsert_entry(self.models, {'model': 'backup', 'provider_ref': 'supplier-b',
                             'is_fallback_for': ['org/shared/model']})
        self.add_b()
        registry = ModelRegistry(load_model_config(self.models))
        self.assertEqual([e.model for e in registry.fallback_of(self.key_a)], ['backup'])
        self.assertEqual(registry.fallback_of(self.key_b), [])

    def test_edit_delete_and_provider_rename_only_affect_selected_identity(self) -> None:
        self.add_b()
        agentcfg.write_assignments(self.agents, {'master': self.key_a})
        modelcfg.upsert_entry(self.models, {'model': 'org/shared/model', 'provider_ref': 'supplier-b',
                             'context_window': 12345}, original_model=self.key_b)
        registry = ModelRegistry(load_model_config(self.models))
        self.assertEqual(registry.get(self.key_b).context_window, 12345)
        self.assertNotEqual(registry.get(self.key_a).context_window, 12345)
        modelcfg.upsert_provider(self.models, {'name': 'renamed-a', 'provider': 'mock'},
                                 original_name='供应商 A', assignments_path=self.agents)
        renamed = model_key({'provider_ref': 'renamed-a', 'model': 'org/shared/model'})
        self.assertEqual(agentcfg.read_assignments(self.agents)['master'], renamed)
        modelcfg.delete_entry(self.models, self.key_b, assignments_path=self.agents)
        self.assertEqual([m.key for m in load_model_config(self.models)], [renamed])

    def test_legacy_unique_names_and_duplicate_rejection(self) -> None:
        self.assertEqual(ModelRegistry(load_model_config(self.models)).get('org/shared/model').key, self.key_a)
        with self.assertRaises(SpiritScoutError):
            modelcfg.upsert_entry(self.models, {'model': 'org/shared/model', 'provider_ref': '供应商 A'})

    def test_api_can_edit_bind_and_delete_same_name_independently(self) -> None:
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)),
                           models_config_path=str(self.models), agent_models_path=str(self.agents),
                           audit_log_path=str(self.root / 'audit.jsonl'))
        with TestClient(create_app(cfg, []), base_url='http://127.0.0.1') as client:
            response = client.post('/api/models/batch', json={
                'provider_ref': 'supplier-b', 'models': ['org/shared/model']})
            self.assertEqual(response.status_code, 201, response.text)
            models = client.get('/api/models').json()['models']
            self.assertEqual({m['key'] for m in models}, {self.key_a, self.key_b})
            self.assertEqual(client.put('/api/agents/master', json={'model': self.key_b}).status_code, 200)
            from urllib.parse import quote
            response = client.put('/api/models/' + quote(self.key_a, safe=''), json={
                'model': 'org/shared/model', 'provider_ref': '供应商 A', 'context_window': 64000})
            self.assertEqual(response.status_code, 200, response.text)
            response = client.delete('/api/models/' + quote(self.key_a, safe=''))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get('/api/agents').json()['assignments']['master'], self.key_b)


class SameNameGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_local_http_routes_same_model_id_to_selected_provider(self) -> None:
        import asyncio
        import json

        from spiritscout.config.settings import GatewayConfig, ModelEntry
        from spiritscout.gateway.gateway import ModelGateway

        received = []
        servers = []
        entries = []
        for label in ('A', 'B'):
            async def handle(reader, writer, supplier=label):
                try:
                    header = await reader.readuntil(b'\r\n\r\n')
                    size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                                if line.lower().startswith(b'content-length:'))
                    body = json.loads(await reader.readexactly(size))
                    received.append((supplier, body['model']))
                    data = json.dumps({'id': 'fixture', 'object': 'chat.completion', 'created': 1,
                                       'model': 'same-id', 'choices': [{'index': 0, 'finish_reason': 'stop',
                                       'message': {'role': 'assistant', 'content': supplier}}],
                                       'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode()
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                                 + str(len(data)).encode() + b'\r\nConnection: close\r\n\r\n' + data)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(handle, '127.0.0.1', 0)
            servers.append(server)
            port = server.sockets[0].getsockname()[1]
            entries.append(ModelEntry(model='same-id', provider_ref=label,
                                      base_url=f'http://127.0.0.1:{port}/v1', api_key='local-fixture-only'))
        try:
            gateway = ModelGateway(ModelRegistry(entries), GatewayConfig(request_timeout_seconds=5))
            for entry in entries:
                response = await gateway.chat([{'role': 'user', 'content': 'fixture'}], model=entry.key, max_tokens=16)
                self.assertEqual(response.content, entry.provider_ref)
            self.assertEqual(received, [('A', 'same-id'), ('B', 'same-id')])
            self.assertEqual(set(gateway._rate_state), {entry.key for entry in entries})
        finally:
            for server in servers:
                server.close()
                await server.wait_closed()
