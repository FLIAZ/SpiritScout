"""真实 SDK 经本机 HTTP 兼容端点验证参数回退；不连接任何付费供应商。"""
from __future__ import annotations

import asyncio
import json
import unittest

from spiritscout.config.settings import GatewayConfig, ModelEntry
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry


class LocalHttpGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_with_tools_over_real_http(self) -> None:
        """真实 LiteLLM 流式路径也必须兼容项目 MCP SDK；不访问付费模型。"""
        requests: list[dict] = []
        chunks: list[tuple[str, str]] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                headers = dict(line.split(b':', 1) for line in header.split(b'\r\n')[1:] if b':' in line)
                size = int(next(value for key, value in headers.items() if key.lower() == b'content-length'))
                requests.append(json.loads(await reader.readexactly(size)))
                events = [
                    {'id': 'local-stream', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                     'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': None}]},
                    {'id': 'local-stream', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                     'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 10, 'completion_tokens': 1, 'total_tokens': 11}},
                ]
                data = (''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
                        + 'data: [DONE]\n\n').encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: '
                             + str(len(data)).encode() + b'\r\nConnection: close\r\n\r\n' + data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        try:
            port = server.sockets[0].getsockname()[1]
            gateway = ModelGateway(ModelRegistry([ModelEntry(model='fixture', provider='openai_compatible',
                base_url=f'http://127.0.0.1:{port}/v1', api_key='local-test-only')]),
                config=GatewayConfig(request_timeout_seconds=5))
            tools = [{'type': 'function', 'function': {'name': 'local_probe', 'description': 'fixture',
                      'parameters': {'type': 'object', 'properties': {}}}}]
            response = await gateway.chat([{'role': 'user', 'content': 'fixture'}], max_tokens=16,
                                          tools=tools, on_delta=lambda kind, text: chunks.append((kind, text)))
            self.assertEqual(response.content, 'ok')
            self.assertIn(('text', 'ok'), chunks)
            self.assertEqual(len(requests), 1)
            self.assertTrue(requests[0]['stream'])
            self.assertEqual(requests[0]['tools'], tools)
        finally:
            server.close()
            await server.wait_closed()

    async def test_unsupported_parameter_is_only_rejected_once_over_real_http(self) -> None:
        requests: list[dict] = []
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                headers = dict(line.split(b':', 1) for line in header.split(b'\r\n')[1:] if b':' in line)
                size = int(next(value for key, value in headers.items() if key.lower() == b'content-length'))
                body = json.loads(await reader.readexactly(size))
                requests.append(body)
                if 'response_format' in body:
                    status = b'400 Bad Request'
                    result = {'error': {'message':'response_format is not supported', 'type':'invalid_request_error',
                                        'param':'response_format', 'code':'unsupported_parameter'}}
                else:
                    status = b'200 OK'
                    result = {'id':'local-completion','object':'chat.completion','created':1,'model':'fixture',
                              'choices':[{'index':0,'message':{'role':'assistant','content':'{"answer":"完整事实保留"}'},'finish_reason':'stop'}],
                              'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110,
                                       'prompt_tokens_details':{'cached_tokens':80}}}
                data = json.dumps(result, ensure_ascii=False).encode()
                writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(data)).encode() + b'\r\nConnection: close\r\n\r\n' + data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        try:
            port = server.sockets[0].getsockname()[1]
            gateway = ModelGateway(ModelRegistry([ModelEntry(model='fixture', provider='openai_compatible',
                base_url=f'http://127.0.0.1:{port}/v1', api_key='local-test-only')]), config=GatewayConfig(request_timeout_seconds=5))
            messages = [{'role':'system','content':'授权原文完整保留。'}, {'role':'user','content':'完整题面和证据。'}]
            for _ in range(2):
                response = await gateway.chat(messages, response_format='json', max_tokens=1234, preserve_context=True)
                self.assertEqual(response.content, '{"answer":"完整事实保留"}')
            self.assertEqual(len(requests), 3)
            self.assertEqual(requests[1], requests[2])
            self.assertEqual(requests[2]['messages'], messages)
            self.assertEqual(requests[2]['max_tokens'], 1234)
            first, second = gateway.budget.call_log
            self.assertEqual(first['sdk_attempts'], 2)
            self.assertEqual(first['unreported_attempts'], 1)
            self.assertEqual(second['sdk_attempts'], 1)
            self.assertEqual(second['cached_input_tokens'], 80)
        finally:
            server.close()
            await server.wait_closed()
