"""本地模型连通性入口允许无密钥；远端通用条目仍遵守既有密钥契约。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.config.settings import SystemConfig
from spiritscout.web.model_routes import mount_model_routes
from spiritscout.web.modelcfg import write_config


class LocalModelConnectionTests(unittest.TestCase):
    def test_connection_check_reaches_gateway_for_configured_local_runtimes_without_dummy_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            models = root / 'models.yaml'
            write_config(models, [
                {'name': 'ollama', 'provider': 'ollama', 'base_url': 'http://127.0.0.1:11434'},
                {'name': 'llama', 'provider': 'openai_compatible', 'base_url': 'http://127.0.0.1:8081/v1'},
                {'name': 'remote', 'provider': 'openai_compatible', 'base_url': 'https://example.invalid/v1'}], [
                {'model': 'ollama', 'provider_ref': 'ollama'},
                {'model': 'llama', 'provider_ref': 'llama', 'local_runtime': 'llama_cpp'},
                {'model': 'remote', 'provider_ref': 'remote'}])
            app = FastAPI()
            mount_model_routes(app, SystemConfig(models_config_path=str(models),
                               audit_log_path=str(root / 'audit.jsonl')))
            response = SimpleNamespace(latency_ms=1, content='ok', reasoning='')
            with patch('spiritscout.web.vault.apply_keys', side_effect=lambda entries: entries), \
                    patch('spiritscout.gateway.ModelGateway.chat', new=AsyncMock(return_value=response)) as chat, \
                    TestClient(app) as client:
                for name in ('ollama', 'llama'):
                    result = client.post('/api/models/test', json={'model': name})
                    self.assertTrue(result.json()['ok'], result.text)
                self.assertEqual(chat.await_count, 2)
                self.assertIn('密钥未配置', client.post('/api/models/test', json={'model': 'remote'}).json()['error'])
                self.assertEqual(chat.await_count, 2)


if __name__ == '__main__':
    unittest.main()
