"""MCP 卡片目录筛选、时间来源与移除回归。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_resource_management import cfg_at

from spiritscout.config.settings import McpServerConfig
from spiritscout.resources.store import ResourceStore
from spiritscout.web.app import create_app


class McpCatalogTests(unittest.TestCase):
    def test_filters_counts_pagination_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = cfg_at(Path(directory))
            cfg.mcp_servers = [McpServerConfig(name='base', command='echo')]
            app = create_app(cfg, [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                for name, enabled, transport in [('alpha', True, 'http'), ('beta', False, 'http'),
                                                 ('gamma', True, 'sse')]:
                    response = client.put('/api/resources/mcp/' + name, json={'data': {
                        'transport': transport, 'url': 'https://example.test/mcp',
                        'headers': {'Authorization': 'secret-value'}, 'enabled': enabled,
                        'default_risk': 'readonly', 'allowed_agents': ['ctf_web']}})
                    self.assertEqual(response.status_code, 200)
                result = client.get('/api/resources/mcp', params={'transport': 'http',
                    'enabled': 'false', 'risk': 'readonly', 'agent': 'ctf_web',
                    'source': 'managed', 'date_state': 'known'}).json()
                self.assertEqual([row['id'] for row in result['items']], ['beta'])
                self.assertEqual(result['counts'], {'enabled': 1, 'disabled': 1})
                self.assertEqual(result['facets']['transports'], ['http', 'sse', 'stdio'])
                self.assertNotIn('secret-value', json.dumps(result))
                result = client.get('/api/resources/mcp?sort=name_asc&limit=1&offset=1').json()
                self.assertEqual(result['items'][0]['id'], 'base')
                self.assertTrue(result['has_more'])
                self.assertEqual(client.get('/api/resources/mcp?q=ctf_web&limit=1').json()['total'], 3)
                self.assertEqual(client.get('/api/resources/mcp?added_from=2026-10-29&added_to=2026-10-01').status_code, 422)
                self.assertEqual(client.get('/api/resources/mcp?sort=bad').status_code, 422)
                self.assertIn('thread', client.get('/api/resources/mcp/chat').json())
                detail = client.get('/api/resources/mcp/alpha/config').json()
                self.assertEqual(detail['version'], 1)
                self.assertNotIn('secret-value', json.dumps(detail))
                self.assertNotIn('created_at', detail['data'])

    def test_added_time_and_legacy_config_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = cfg_at(Path(directory))
            cfg.mcp_servers = [McpServerConfig(name='base', command='echo')]
            app = create_app(cfg, [])
            store = app.state.resources
            legacy = {'name': 'legacy', 'command': 'echo'}
            with store.connection() as conn:
                conn.execute('INSERT INTO settings VALUES(?,?,?,?,?)',
                    ('mcp', 'legacy', 1, store.cipher().encrypt(json.dumps(legacy).encode()), 99))
            with TestClient(app, base_url='http://127.0.0.1') as client:
                with patch('spiritscout.resources.store.time.time', return_value=100):
                    client.put('/api/resources/mcp/new', json={'data': {'command': 'echo'}})
                with patch('spiritscout.resources.store.time.time', return_value=200):
                    client.put('/api/resources/mcp/new', json={'data': {'enabled': False}, 'expected_version': 1})
                client.put('/api/resources/mcp/base', json={'data': {'enabled': False}})
                client.put('/api/resources/mcp/legacy', json={'data': {'enabled': False}, 'expected_version': 1})
                rows = {row['id']: row for row in client.get('/api/resources/mcp').json()['items']}
                self.assertEqual(rows['new']['created_at'], 100)
                self.assertEqual(rows['new']['updated_at'], 200)
                self.assertIsNone(rows['legacy']['created_at'])
                self.assertIsNone(rows['base']['created_at'])
                self.assertEqual(len(store.effective_mcp(cfg.mcp_servers)), 3)
                self.assertEqual(client.get('/api/resources/mcp?date_state=unknown').json()['total'], 2)

    def test_delete_version_and_base_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cfg = cfg_at(Path(directory))
            cfg.mcp_servers = [McpServerConfig(name='base', command='echo')]
            app = create_app(cfg, [])
            with TestClient(app, base_url='http://127.0.0.1') as client:
                client.put('/api/resources/mcp/new', json={'data': {'command': 'echo'}})
                self.assertEqual(client.delete('/api/resources/mcp/new?expected_version=0').status_code, 409)
                self.assertEqual(client.delete('/api/resources/mcp/new?expected_version=1').status_code, 200)
                self.assertEqual(client.delete('/api/resources/mcp/base?expected_version=0').status_code, 200)
                self.assertEqual(client.get('/api/resources/mcp').json()['total'], 0)
                self.assertEqual(ResourceStore(app.state.resources.root).effective_mcp(cfg.mcp_servers), [])
                self.assertEqual(cfg.mcp_servers[0].name, 'base')
                self.assertEqual(client.get('/api/resources/mcp/base/config').status_code, 404)
                self.assertEqual(client.delete('/api/resources/mcp/base?expected_version=1').status_code, 404)
                self.assertEqual(client.delete('/api/resources/mcp/missing?expected_version=0').status_code, 404)
