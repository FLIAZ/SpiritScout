"""真实 SDK 经本机 HTTP 兼容端点验证参数回退；不连接任何付费供应商。"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest

from spiritscout.config.settings import GatewayConfig, ModelEntry
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry


class LocalHttpGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_with_tools_over_real_http(self) -> None:
        """真实 LiteLLM 流式路径也必须兼容项目 MCP SDK；不访问付费模型。"""
        requests: list[dict] = []
        authorization: list[bytes] = []
        chunks: list[tuple[str, str]] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                headers = dict(line.split(b':', 1) for line in header.split(b'\r\n')[1:] if b':' in line)
                authorization.append(next((value.strip() for key, value in headers.items()
                                           if key.lower() == b'authorization'), b''))
                size = int(next(value for key, value in headers.items() if key.lower() == b'content-length'))
                requests.append(json.loads(await reader.readexactly(size)))
                events = [
                    {'id': 'local-stream', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                     'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': None}]},
                    {'id': 'local-stream', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                     'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 10, 'completion_tokens': 0, 'total_tokens': 10,
                               'prompt_tokens_details': {'cached_tokens': 8}}},
                    {'id': 'local-stream', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                     'choices': [], 'usage': {'prompt_tokens': 0, 'completion_tokens': 1, 'total_tokens': 1}},
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
                base_url=f'http://127.0.0.1:{port}/v1', local_runtime='llama_cpp')]),
                config=GatewayConfig(request_timeout_seconds=5))
            from spiritscout.awdp.editor import SCHEMA

            tools = [SCHEMA]
            malformed = '{"tests":[{"code":"assert True\n"}]}'
            messages = [{'role': 'user', 'content': 'fixture'},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'broken', 'type': 'function', 'function': {'name': 'verify_patch', 'arguments': malformed}},
                    {'id': 'valid', 'type': 'function', 'function': {'name': 'edit_source', 'arguments': '{"edits":[]}'}}]},
                {'role': 'tool', 'tool_call_id': 'broken', 'content': 'Invalid JSON; not executed'},
                {'role': 'tool', 'tool_call_id': 'valid', 'content': 'No edits; not executed'}]
            original = copy.deepcopy(messages)
            response = await gateway.chat(messages, max_tokens=16, preserve_context=True,
                                          tools=tools, preserve_tool_order=True,
                                          on_delta=lambda kind, text: chunks.append((kind, text)))
            self.assertEqual(response.content, 'ok')
            self.assertEqual(response.prompt_tokens, 10)
            self.assertEqual(response.completion_tokens, 1)
            self.assertEqual(response.cached_input_tokens, 8)
            self.assertEqual(gateway.budget.used_tokens, 11)
            self.assertIn(('text', 'ok'), chunks)
            self.assertEqual(len(requests), 1)
            self.assertEqual(authorization, [b'Bearer no-key'])
            self.assertTrue(requests[0]['stream'])
            self.assertEqual(requests[0]['tools'], tools)
            self.assertEqual(messages, original)
            calls = requests[0]['messages'][1]['tool_calls']
            self.assertEqual(json.loads(calls[0]['function']['arguments']), {'_invalid_json_not_executed': malformed})
            self.assertEqual(calls[1]['function']['arguments'], '{"edits":[]}')
            properties = requests[0]['tools'][0]['function']['parameters']['properties']
            self.assertEqual(list(properties), ['source_ref', 'path', 'sha256', 'edits'])
            self.assertEqual(list(properties['edits']['items']['properties']), ['search', 'search_ref', 'replace'])
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

    async def test_responses_and_anthropic_cache_usage_over_real_http(self) -> None:
        for api_format in ('responses', 'anthropic_messages'):
            with self.subTest(api_format=api_format):
                async def handle(reader, writer):
                    try:
                        header = await reader.readuntil(b'\r\n\r\n')
                        size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                                    if line.lower().startswith(b'content-length:'))
                        await reader.readexactly(size)
                        if api_format == 'responses':
                            result = {'id': 'resp_fixture', 'object': 'response', 'created_at': 1,
                                      'status': 'completed', 'model': 'fixture',
                                      'output': [{'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                                                  'status': 'completed', 'content': [{'type': 'output_text', 'text': 'ok', 'annotations': []}]}],
                                      'usage': {'input_tokens': 110, 'output_tokens': 20, 'total_tokens': 130,
                                                'input_tokens_details': {'cached_tokens': 60}}}
                        else:
                            result = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'model': 'fixture',
                                      'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn',
                                      'usage': {'input_tokens': 40, 'output_tokens': 20,
                                                'cache_read_input_tokens': 60, 'cache_creation_input_tokens': 10}}
                        data = json.dumps(result).encode()
                        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                                     + str(len(data)).encode() + b'\r\nConnection: close\r\n\r\n' + data)
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                server = await asyncio.start_server(handle, '127.0.0.1', 0)
                try:
                    port = server.sockets[0].getsockname()[1]
                    gateway = ModelGateway(ModelRegistry([ModelEntry(model='fixture', provider='openai_compatible',
                        api_format=api_format, base_url=f'http://127.0.0.1:{port}/v1', api_key='local-test-only')]),
                        config=GatewayConfig(request_timeout_seconds=5))
                    response = await gateway.chat([{'role': 'user', 'content': 'fixture'}], max_tokens=32)
                    self.assertEqual(response.content, 'ok')
                    self.assertEqual(response.prompt_tokens, 110, '缓存只计入输入总量一次')
                    self.assertEqual(response.completion_tokens, 20)
                    self.assertEqual(response.cached_input_tokens, 60)
                    self.assertEqual(gateway.budget.used_tokens, 130)
                    if api_format == 'anthropic_messages':
                        self.assertEqual(response.cache_write_tokens, 10)
                finally:
                    server.close()
                    await server.wait_closed()
