"""Ollama 原生协议：共享端点推理槽，保留思考、工具调用与原生耗时。"""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
import weakref
from typing import Any

import httpx

from spiritscout.config.settings import ModelEntry
from spiritscout.gateway.attempts import current_attempt_trace
from spiritscout.gateway.backends import ChatBackend, emit_delta

_SLOTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _transient(exc: BaseException) -> bool:
    """允许轻量调用重试的传输故障；不推断故障成因。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    if isinstance(exc, ValueError):
        text = str(exc)
        return ('流提前结束' in text or '未包含完成回执' in text
                or 'slot' in text.lower() or 'server error' in text.lower())
    return False


def endpoint(entry: ModelEntry) -> str:
    base = entry.base_url.rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    if not base:
        raise ValueError('Ollama 原生接入必须配置 base_url')
    return base


def native_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = copy.deepcopy(messages)
    names: dict[str, str] = {}
    for message in result:
        if 'reasoning_content' in message:
            message['thinking'] = message.pop('reasoning_content')
        for call in message.get('tool_calls') or []:
            function = call.get('function') or {}
            if call.get('id'):
                names[call['id']] = function.get('name', '')
            arguments = function.get('arguments', {})
            if isinstance(arguments, str):
                function['arguments'] = json.loads(arguments)
        identifier = message.pop('tool_call_id', None)
        if identifier in names:
            message['tool_name'] = names[identifier]
    return result


class OllamaBackend(ChatBackend):
    async def complete(self, entry: ModelEntry, messages: list[dict[str, Any]], *,
                       temperature: float = 0.2, max_tokens: int | None = None,
                       timeout: int = 120, response_format: str | None = None,
                       tools: list[dict[str, Any]] | None = None,
                       tool_choice: str | dict[str, Any] | None = None, on_delta=None,
                       reasoning_effort: str = '', _lightweight_local: bool = False) -> dict[str, Any]:
        base = endpoint(entry)
        # 按名工具请求只透传；模型恰好调用该工具不证明端点强制生效。
        # 聚合 required 等取值尚不接受。
        forced_tool = (isinstance(tool_choice, dict)
                       and isinstance((tool_choice.get('function') or {}).get('name'), str))
        if tool_choice not in (None, 'auto', 'none') and not forced_tool:
            raise ValueError('Ollama 原生协议仅支持 auto/none 或按名强制（dict），请使用 auto')
        options = copy.deepcopy(entry.ollama_options)
        options.setdefault('temperature', temperature)
        if entry.context_window:
            options.setdefault('num_ctx', entry.context_window)
        if max_tokens is not None:
            options['num_predict'] = max_tokens
        payload: dict[str, Any] = {'model': entry.model, 'messages': native_messages(messages),
                                   'stream': on_delta is not None, 'options': options,
                                   'keep_alive': entry.ollama_keep_alive}
        think = entry.ollama_think
        if reasoning_effort:
            if reasoning_effort not in {'low', 'high'}:
                raise ValueError('Ollama 轻量接入只支持 low/high：关闭/开启思考')
            think = reasoning_effort == 'high'
        if think is not None:
            payload['think'] = think
        if tools and tool_choice != 'none':
            payload['tools'] = copy.deepcopy(tools)
            if forced_tool:
                payload['tool_choice'] = copy.deepcopy(tool_choice)
        elif response_format == 'json':
            payload['format'] = 'json'
        loop = asyncio.get_running_loop()
        slots = _SLOTS.setdefault(loop, {})
        resource = entry.ollama_resource or base
        slot = slots.setdefault(resource, asyncio.Semaphore(1))
        queued = time.perf_counter()
        async with slot:
            queue_ms = (time.perf_counter() - queued) * 1000
            headers = {'Authorization': f'Bearer {entry.api_key}'} if entry.api_key else {}
            trace = current_attempt_trace.get()
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), trust_env=False) as client:
                # 仅合格轻量调用可在未输出任何增量时多尝试一次。
                # 故障原因不能仅凭截断或 5xx 推断；每次请求单独留审计。
                text, thinking, calls = '', '', []
                lightweight = _lightweight_local and entry.provider == 'ollama' and entry.ollama_lightweight
                for retry in range(2 if lightweight else 1):
                    if trace is not None:
                        trace.start(streaming=on_delta is not None)
                    try:
                        if on_delta is None:
                            response = await client.post(base + '/api/chat', json=payload, headers=headers)
                            response.raise_for_status()
                            data = response.json()
                            if data.get('error'):
                                raise ValueError(str(data['error']))
                            if not data.get('done'):
                                raise ValueError('Ollama 响应未包含完成回执')
                            message = data.get('message') or {}
                            text = str(message.get('content') or '')
                            thinking = str(message.get('thinking') or '')
                            calls = message.get('tool_calls') or []
                            first_ms = None
                        else:
                            text, thinking, calls, first_ms = '', '', [], None
                            started = time.perf_counter()
                            data = {}
                            async with client.stream('POST', base + '/api/chat', json=payload, headers=headers) as response:
                                response.raise_for_status()
                                async for line in response.aiter_lines():
                                    if not line.strip():
                                        continue
                                    chunk = json.loads(line)
                                    if chunk.get('error'):
                                        raise ValueError(str(chunk['error']))
                                    message = chunk.get('message') or {}
                                    content, thought = str(message.get('content') or ''), str(message.get('thinking') or '')
                                    tool_calls = message.get('tool_calls') or []
                                    if first_ms is None and (content or thought or tool_calls):
                                        first_ms = (time.perf_counter() - started) * 1000
                                    text += content
                                    thinking += thought
                                    await emit_delta(on_delta, 'text', content)
                                    await emit_delta(on_delta, 'reasoning', thought)
                                    calls.extend(tool_calls)
                                    if chunk.get('done'):
                                        data = chunk
                            if not data.get('done'):
                                raise ValueError('Ollama 流提前结束，未收到完成回执')
                        break
                    except (httpx.HTTPStatusError, ValueError) as exc:
                        partial = bool(text or thinking or calls)
                        if trace is not None:
                            trace.finish('failed')
                        if lightweight and retry == 0 and not partial and _transient(exc):
                            await asyncio.sleep(2.5)
                            continue
                        raise
        normalized = []
        for call in calls:
            function = call.get('function') or {}
            arguments = function.get('arguments') or {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            normalized.append({'id': call.get('id') or f'ollama-{uuid.uuid4().hex}',
                               'name': function.get('name', ''), 'arguments': arguments,
                               'raw': json.dumps(arguments, ensure_ascii=False)})
        eval_count, prompt_count = int(data.get('eval_count') or 0), int(data.get('prompt_eval_count') or 0)
        if trace is not None:
            trace.usage({'prompt_tokens': prompt_count, 'completion_tokens': eval_count})
            trace.finish('completed')
        metrics = {'queue_duration_ms': queue_ms,
                   **{key + '_ms': float(data.get(key) or 0) / 1e6
                      for key in ('load_duration', 'prompt_eval_duration', 'eval_duration')}}
        if data.get('eval_duration'):
            metrics['eval_tokens_per_second'] = eval_count * 1e9 / data['eval_duration']
        return {'content': text, 'reasoning': thinking, 'tool_calls': normalized,
                'model': data.get('model') or entry.model, 'prompt_tokens': prompt_count,
                'completion_tokens': eval_count, 'cached_input_tokens': None,
                'finish_reason': data.get('done_reason') or ('tool_calls' if normalized else 'stop'),
                'first_delta_ms': first_ms, **metrics}
