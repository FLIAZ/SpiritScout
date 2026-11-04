"""真实 LiteLLM/OpenAI SDK 通过本机端点验证续聊读取工具往返。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiritscout.config.settings import (
    GatewayConfig,
    ModelEntry,
    SystemConfig,
    WorkspaceConfig,
)
from spiritscout.core.audit import AuditLogger
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.web.sessions import Session, _master_reply


class ConversationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_context_tool_roundtrip_preserves_evidence_over_real_http(self) -> None:
        requests = []

        async def handle(reader, writer):
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                            if line.lower().startswith(b'content-length:'))
                requests.append(json.loads(await reader.readexactly(size)))
                if len(requests) == 1:
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': 'context-read', 'type': 'function', 'function': {
                            'name': 'session_read', 'arguments': json.dumps({
                                'source': 'file', 'path': 'reports/source.md', 'limit': 12000})}}]}
                    reason = 'tool_calls'
                else:
                    message, reason = {'role': 'assistant', 'content': '原始证据已确认'}, 'stop'
                result = {'id': f'local-{len(requests)}', 'object': 'chat.completion', 'created': 1,
                          'model': 'fixture', 'choices': [{'index': 0, 'message': message, 'finish_reason': reason}],
                          'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
                data = json.dumps(result, ensure_ascii=False).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                             + str(len(data)).encode() + b'\r\nConnection: close\r\n\r\n' + data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        try:
            port = server.sockets[0].getsockname()[1]
            entry = ModelEntry(model='fixture', provider='openai_compatible',
                               base_url=f'http://127.0.0.1:{port}/v1', api_key='local-test-only')
            gateway = ModelGateway(ModelRegistry([entry]), config=GatewayConfig(request_timeout_seconds=5))
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / 'reports').mkdir()
                evidence = 'HTTP_NATIVE_EVIDENCE_' * 200
                (root / 'reports/source.md').write_text(evidence)
                session = Session('sess-http', 'ctf', 'fixture', root, status='completed')
                cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)),
                                   agent_models_path=str(root / 'agents.yaml'))
                with patch('spiritscout.web.sessions._conversation_gateway', return_value=gateway):
                    reply = await _master_reply(cfg, [entry], '读取证据再回答', AuditLogger(root / 'audit.jsonl'),
                                                kind='dialogue', session=session)
                self.assertEqual(reply, '原始证据已确认')
            self.assertEqual(len(requests), 2)
            self.assertTrue(any(tool['function']['name'] == 'session_read' for tool in requests[0]['tools']))
            result = requests[1]['messages'][-1]
            self.assertEqual(result['role'], 'tool')
            self.assertEqual(result['tool_call_id'], 'context-read')
            self.assertEqual(json.loads(result['content'])['content'], evidence)
        finally:
            server.close()
            await server.wait_closed()
