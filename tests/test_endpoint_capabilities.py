"""端点明确不支持参数的短期记忆，不改变成功请求的消息和采样参数。"""
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.config.settings import ModelEntry
from spiritscout.gateway.backends import LiteLLMBackend, backend_for
from spiritscout.gateway.capabilities import EndpointCapabilities


class UnsupportedParameter(Exception):
    status_code = 400


def response() -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}', tool_calls=None), finish_reason='stop')],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=5))


class EndpointCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_memory_never_suppresses_parameters(self) -> None:
        cache = EndpointCapabilities(enabled=False)
        entry = ModelEntry(model='fixture', provider='openai_compatible')
        cache.remember(entry, False, 'response_format', UnsupportedParameter('response_format unsupported'))
        self.assertFalse(cache.unsupported(entry, False, 'response_format'))

    async def test_successful_fallback_is_reused_across_backend_instances(self) -> None:
        cache = EndpointCapabilities()
        entry = ModelEntry(model='fixture', provider='openai_compatible', base_url='http://local/v1', api_key='private')
        messages = [{'role':'system','content':'完整规则'}, {'role':'user','content':'原题面'}]
        original = copy.deepcopy(messages)
        call = AsyncMock(side_effect=[UnsupportedParameter('response_format is not supported'), response(), response()])
        with patch('litellm.acompletion', call):
            first = backend_for(entry, capabilities=cache)
            second = backend_for(entry, capabilities=cache)
            await first.complete(entry, messages, response_format='json', max_tokens=1234, reasoning_effort='low')
            await second.complete(entry, messages, response_format='json', max_tokens=1234, reasoning_effort='low')
        self.assertEqual(call.await_count, 3)
        self.assertIn('response_format', call.call_args_list[0].kwargs)
        self.assertEqual(call.call_args_list[1].kwargs, call.call_args_list[2].kwargs)
        self.assertEqual(messages, original)
        self.assertNotIn('private', repr(cache))

    async def test_failed_fallback_does_not_teach_capability(self) -> None:
        cache = EndpointCapabilities()
        backend = LiteLLMBackend(capabilities=cache)
        entry = ModelEntry(model='fixture', provider='openai_compatible')
        call = AsyncMock(side_effect=[UnsupportedParameter('response_format unsupported'), TimeoutError('timeout'), response()])
        with patch('litellm.acompletion', call):
            with self.assertRaises(TimeoutError):
                await backend.complete(entry, [], response_format='json')
            await backend.complete(entry, [], response_format='json')
        self.assertIn('response_format', call.call_args_list[-1].kwargs)

    async def test_ambiguous_invalid_json_is_not_remembered(self) -> None:
        call = AsyncMock(side_effect=[UnsupportedParameter('response_format invalid for this input'), response(), response()])
        entry = ModelEntry(model='fixture', provider='openai_compatible')
        backend = LiteLLMBackend()
        with patch('litellm.acompletion', call):
            await backend.complete(entry, [], response_format='json')
            await backend.complete(entry, [], response_format='json')
        self.assertIn('response_format', call.call_args_list[-1].kwargs)

    async def test_stream_options_omission_keeps_streaming(self) -> None:
        backend = LiteLLMBackend()
        backend._consume_stream = AsyncMock(return_value={'content':'ok'})
        entry = ModelEntry(model='fixture', provider='openai_compatible')
        call = AsyncMock(side_effect=[UnsupportedParameter('unsupported parameter: stream_options'), object(), object()])
        with patch('litellm.acompletion', call):
            await backend.complete(entry, [], on_delta=lambda *_: None)
            await backend.complete(entry, [], on_delta=lambda *_: None)
        self.assertEqual(call.await_count, 3)
        self.assertTrue(call.call_args_list[-1].kwargs['stream'])
        self.assertNotIn('stream_options', call.call_args_list[-1].kwargs)

    def test_expiry_identity_and_capacity(self) -> None:
        now = [0.0]
        cache = EndpointCapabilities(ttl_seconds=10, capacity=2, clock=lambda: now[0])
        entry = ModelEntry(model='a', provider='openai_compatible', base_url='http://one/v1', api_key='first')
        error = UnsupportedParameter('response_format is not supported')
        cache.remember(entry, False, 'response_format', error)
        self.assertTrue(cache.unsupported(entry, False, 'response_format'))
        for changed in [{'model':'b'}, {'base_url':'http://two/v1'}, {'api_key':'second'}, {'api_format':'responses'}]:
            self.assertFalse(cache.unsupported(entry.model_copy(update=changed), False, 'response_format'))
        self.assertFalse(cache.unsupported(entry, True, 'response_format'))
        now[0] = 11
        self.assertFalse(cache.unsupported(entry, False, 'response_format'))
        for model in ['a','b','c']:
            cache.remember(entry.model_copy(update={'model':model}), False, 'response_format', error)
        self.assertFalse(cache.unsupported(entry, False, 'response_format'))
        transient = UnsupportedParameter('response_format unsupported')
        transient.status_code = 503
        cache.remember(entry, False, 'response_format', transient)
        self.assertFalse(cache.unsupported(entry, False, 'response_format'))
