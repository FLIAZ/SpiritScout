"""真实 LiteLLM SDK → 本地 HTTP 端点，核对实际请求而非 mock kwargs。"""
from __future__ import annotations

import asyncio
import json
import unittest

from spiritscout.config.settings import GatewayConfig, ModelEntry
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry


class EffortTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_effort_reaches_chat_and_anthropic_wire(self):
        received = []

        async def handler(reader, writer):
            try:
                head = await reader.readuntil(b'\r\n\r\n')
                size = next(int(line.split(b':', 1)[1]) for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:'))
                body = json.loads(await reader.readexactly(size))
                received.append(body)
                if b'/messages ' in head.split(b'\r\n')[0]:
                    reply = {'id': 'msg-test', 'type': 'message', 'role': 'assistant', 'model': 'glm-5.3',
                             'content': [{'type': 'text', 'text': 'fixture'}], 'stop_reason': 'end_turn',
                             'usage': {'input_tokens': 2, 'output_tokens': 1}}
                else:
                    reply = {'id': 'chat-test', 'object': 'chat.completion', 'created': 1, 'model': 'glm-5.3',
                             'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'fixture'}}],
                             'usage': {'prompt_tokens': 2, 'completion_tokens': 1}}
                raw = json.dumps(reply).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '
                             + str(len(raw)).encode() + b'\r\n\r\n' + raw)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        for protocol in ('chat_completions', 'anthropic_messages'):
            entry = ModelEntry(model='glm-5.3', provider_ref=protocol, api_key='local-fixture',
                base_url=f'http://127.0.0.1:{port}', api_format=protocol, reasoning_effort_levels=['low', 'high', 'max'])
            gateway = ModelGateway(ModelRegistry([entry]), GatewayConfig(request_timeout_seconds=5))
            response = await gateway.chat([{'role': 'user', 'content': 'fixture'}], model=entry.key, reasoning_effort='max', max_tokens=100)
            self.assertEqual(response.content, 'fixture')
            self.assertEqual(received[-1].get('reasoning_effort'), 'max', (protocol, received[-1]))
