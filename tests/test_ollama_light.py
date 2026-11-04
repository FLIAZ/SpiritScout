"""Ollama 原生本机协议、并发槽与独立轻量配置回归。"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest

from spiritscout.config.settings import (
    ModelEntry,
    SystemConfig,
    load_model_config,
    load_system_config,
)
from spiritscout.gateway.backends import LiteLLMBackend, backend_for
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.ollama import OllamaBackend, native_messages
from spiritscout.gateway.registry import ModelRegistry


class OllamaBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests = []
        self.active = self.peak = 0

        async def handle(reader, writer):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                            if line.lower().startswith(b'content-length:'))
                body = json.loads(await reader.readexactly(size))
                self.requests.append((header, body))
                await asyncio.sleep(0.02)
                result = {'model': 'fixture', 'message': {'content': '{"ok":true}', 'thinking': 'fixture reasoning',
                    'tool_calls': [{'function': {'name': 'local_probe', 'arguments': {'value': 'evidence'}}}]},
                    'done': True, 'done_reason': 'stop', 'prompt_eval_count': 10, 'eval_count': 5,
                    'load_duration': 1000000, 'prompt_eval_duration': 10000000, 'eval_duration': 20000000}
                if body.get('stream'):
                    chunks = [
                        {'message': {'content': 'ok', 'thinking': 'reason'}, 'done': False},
                        {**result, 'message': {'tool_calls': result['message']['tool_calls']}}]
                    payload = ('\n'.join(json.dumps(chunk) for chunk in chunks)+'\n').encode()
                    mime = b'application/x-ndjson'
                else:
                    payload = json.dumps(result).encode()
                    mime = b'application/json'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: '+mime+b'\r\nContent-Length: '
                             +str(len(payload)).encode()+b'\r\nConnection: close\r\n\r\n'+payload)
                await writer.drain()
            finally:
                self.active -= 1
                writer.close()
                await writer.wait_closed()
        self.server = await asyncio.start_server(handle, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.entry = ModelEntry(model='fixture', provider='ollama', base_url=f'http://127.0.0.1:{port}',
            context_window=16384, ollama_think=False, ollama_options={'temperature': 0.3})

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def test_native_request_without_key_preserves_tools_and_metrics(self) -> None:
        gateway = ModelGateway(ModelRegistry([self.entry]))
        tools = [{'type': 'function', 'function': {'name': 'local_probe', 'parameters': {'type': 'object'}}}]
        result = await gateway.chat([{'role': 'user', 'content': 'fixture'}], tools=tools, reasoning_effort='')
        header, body = self.requests[0]
        self.assertIn(b'/api/chat ', header)
        self.assertNotIn(b'Authorization:', header)
        self.assertEqual(body['model'], 'fixture')
        self.assertFalse(body['think'])
        self.assertEqual(body['options']['num_ctx'], 16384)
        self.assertEqual(body['options']['temperature'], 0.3)
        self.assertEqual(body['tools'], tools)
        self.assertEqual(result.prompt_tokens, 10)
        self.assertEqual(result.completion_tokens, 5)
        self.assertEqual(result.tool_calls[0]['arguments'], {'value': 'evidence'})
        self.assertEqual(gateway.budget.used_tokens, 15)
        self.assertEqual(gateway.budget.call_log[0]['sdk_attempts'], 1)
        self.assertEqual(gateway.budget.call_log[0]['eval_tokens_per_second'], 250)

    async def test_streaming_preserves_content_thinking_and_tool_call(self) -> None:
        deltas = []
        result = await OllamaBackend().complete(self.entry, [{'role': 'user', 'content': 'fixture'}],
            on_delta=lambda kind, text: deltas.append((kind, text)))
        self.assertEqual(result['content'], 'ok')
        self.assertEqual(result['reasoning'], 'reason')
        self.assertEqual(len(result['tool_calls']), 1)
        self.assertIn(('text', 'ok'), deltas)
        self.assertIn(('reasoning', 'reason'), deltas)

    async def test_separate_gateway_instances_share_one_inference_slot(self) -> None:
        first, second = ModelGateway(ModelRegistry([self.entry])), ModelGateway(ModelRegistry([self.entry]))
        await asyncio.gather(first.chat([{'role': 'user', 'content': 'one'}]),
                             second.chat([{'role': 'user', 'content': 'two'}]))
        self.assertEqual(self.peak, 1)
        self.assertEqual(len(self.requests), 2)

    async def test_high_effort_enables_thinking_and_json_uses_native_format(self) -> None:
        await OllamaBackend().complete(self.entry, [{'role': 'user', 'content': 'fixture'}],
            response_format='json', reasoning_effort='high')
        body = self.requests[0][1]
        self.assertTrue(body['think'])
        self.assertEqual(body['format'], 'json')

    def test_history_is_deeply_detached_and_tool_response_uses_name(self) -> None:
        source = [{'role': 'assistant', 'reasoning_content': 'reason', 'tool_calls': [
            {'id': 'c', 'function': {'name': 'local_probe', 'arguments': '{"value":1}'}}]},
            {'role': 'tool', 'tool_call_id': 'c', 'content': 'result'}]
        before = copy.deepcopy(source)
        result = native_messages(source)
        self.assertEqual(result[0]['thinking'], 'reason')
        self.assertEqual(result[0]['tool_calls'][0]['function']['arguments'], {'value': 1})
        self.assertEqual(result[1]['tool_name'], 'local_probe')
        self.assertEqual(source, before)


class OllamaProfileTests(unittest.TestCase):
    def test_light_profile_is_independent_and_normal_defaults_unchanged(self) -> None:
        profile = load_system_config('configs/system.ollama-light.example.yaml')
        self.assertEqual(profile.web.max_concurrent_sessions, 1)
        self.assertFalse(profile.web.auto_name_sessions)
        self.assertFalse(profile.gateway.automatic_platform_extraction)
        self.assertFalse(profile.experience.auto_distill)
        self.assertFalse(profile.secretary.enabled)
        self.assertNotEqual(profile.workspace.root, SystemConfig().workspace.root)
        normal = SystemConfig()
        self.assertTrue(normal.web.auto_name_sessions)
        self.assertTrue(normal.gateway.automatic_platform_extraction)
        self.assertTrue(normal.experience.auto_distill)
        self.assertTrue(normal.secretary.enabled)
        entry = load_model_config('configs/models.ollama-light.example.yaml')[0]
        self.assertEqual(entry.context_window, 16384)
        self.assertEqual(entry.ollama_options['presence_penalty'], 0)
        self.assertFalse(entry.ollama_think)
        self.assertIsInstance(backend_for(entry), OllamaBackend)
        self.assertIsInstance(backend_for(ModelEntry(model='normal')), LiteLLMBackend)


if __name__ == '__main__':
    unittest.main()
