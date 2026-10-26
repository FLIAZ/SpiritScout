"""真实 SDK 经本机 HTTP 兼容端点验证参数回退；不连接任何付费供应商。"""
from __future__ import annotations

import asyncio
import json
import unittest

from spiritscout.config.settings import GatewayConfig, ModelEntry
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry


class LocalHttpGatewayTests(unittest.IsolatedAsyncioTestCase):
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
