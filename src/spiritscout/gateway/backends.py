"""模型后端：LiteLLM（真实云端）+ Mock（确定性测试后端）。

统一能力面（Agent 工具环的地基）：
- 原生工具调用：``tools`` 传 OpenAI 形态函数 schema，响应归一化为
  ``tool_calls``（``{"id", "name", "arguments"(dict), "raw"(str)}``）；
- 流式增量：``on_delta(kind, text)`` 回调（kind ∈ text/reasoning/tool_call/reset），
  chat_completions 与 anthropic_messages 协议支持；responses 协议仅非流式。

Mock 后端用于启动冒烟与离线演练（G10：研发验证不触碰真实 API）：
- 规则化应答：按 system prompt 中的角色标记返回确定性 JSON；
- 支持故障注入（fail_times），验证主备切换（4.9.3-5）。
"""

from __future__ import annotations

import abc
import asyncio
import copy
import inspect
import json
import time
from collections.abc import Callable
from typing import Any

from spiritscout.config.settings import ModelEntry

# 流式增量回调：(kind, text) -> None | Awaitable[None]
DeltaCallback = Callable[[str, str], Any]

# Anthropic Messages 协议必填输出上限；模型/档案都未声明时的保底值
# （对照同类 Agent 实践：按模型最大输出给足，思考型模型不再被小顶截断）
_ANTHROPIC_MIN_MAX_TOKENS = 32000

# 工具参数被截断时回灌给模型的事实说明（重发一次时追加进对话）
_TRUNCATED_TOOL_ARGS_NOTE = (
    "上一条回复中的工具调用参数在生成中途被截断（输出超长），该调用未执行。"
    "请重新发起这次调用；单次参数要明显缩短——大段代码拆成多次小步完成，"
    "或先分步写入文件再执行。")


def cache_usage(usage: Any) -> dict[str, int | None]:
    """兼容 SDK 对象和字典；未提供的指标保留 None，不能伪造零命中。"""
    def get(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    details = get(usage, "prompt_tokens_details") or get(usage, "input_tokens_details")
    read = get(details, "cached_tokens")
    if read is None:
        read = get(usage, "cache_read_input_tokens")
    if read is None:
        read = get(usage, "prompt_cache_hit_tokens")
    write = get(usage, "cache_creation_input_tokens")
    return {"cached_input_tokens": read if type(read) is int and read >= 0 else None,
            "cache_write_tokens": write if type(write) is int and write >= 0 else None}


async def emit_delta(on_delta: DeltaCallback | None, kind: str, text: str) -> None:
    """投递一个流式增量（回调失败不影响主链——增量只是观感，不是数据）。"""
    if on_delta is None or not text:
        return
    try:
        out = on_delta(kind, text)
        if inspect.isawaitable(out):
            await out
    except Exception:  # noqa: BLE001, S110 —— 前端断连等不应中断模型调用
        pass


def parse_tool_arguments(raw: Any) -> tuple[dict[str, Any], str]:
    """工具入参解析：返回 (dict, 错误说明)。模型偶发产出非法 JSON——
    不抛异常，把错误作为工具结果回灌给模型自纠（成熟客户端的通用做法）。"""
    if isinstance(raw, dict):
        return raw, ""
    text = str(raw or "").strip()
    if not text:
        return {}, ""
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, f"arguments 不是合法 JSON: {exc}"
    if not isinstance(data, dict):
        return {"value": data}, ""
    return data, ""


def normalize_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """归一化各协议的工具调用：``[{"id", "name", "arguments", "raw"}]``。"""
    out: list[dict[str, Any]] = []
    for idx, tc in enumerate(raw or []):
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            call_id = str(tc.get("id") or "")
            name = str(fn.get("name") or "")
            raw_args = fn.get("arguments")
        else:
            fn = getattr(tc, "function", None)
            call_id = str(getattr(tc, "id", "") or "")
            name = str(getattr(fn, "name", "") or "") if fn is not None else ""
            raw_args = getattr(fn, "arguments", None) if fn is not None else None
        args, err = parse_tool_arguments(raw_args)
        call: dict[str, Any] = {
            "id": call_id or f"call_{idx}",
            "name": name,
            "arguments": args,
            "raw": raw_args if isinstance(raw_args, str) else json.dumps(
                raw_args or {}, ensure_ascii=False),
        }
        if err:
            call["parse_error"] = err
        out.append(call)
    return out


class ChatBackend(abc.ABC):
    @abc.abstractmethod
    async def complete(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: int = 120,
        response_format: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_delta: DeltaCallback | None = None,
        reasoning_effort: str = "",
    ) -> dict[str, Any]:
        """返回 {"content", "prompt_tokens", "completion_tokens",
        "tool_calls": [{"id","name","arguments","raw"}], "finish_reason"}

        reasoning_effort：思考强度取值（该模型自己声明的档位，见 ModelEntry）；
        空 = 不下发，由端点用默认档。
        max_tokens：输出上限；None = 请求不带该字段（端点按模型自身设定，
        Anthropic Messages 协议必填、此时给 32000 保底）。"""


def extract_json(text: str) -> Any:
    """从模型输出稳健提取 JSON。

    推理模型常把 JSON 包在思考散文或 ```json fence 里，裸 json.loads 脆。
    顺序：整体解析 → 剥 fence → 截取首个平衡的 {...} / [...] 块。
    """
    import json as _json
    import re as _re

    text = (text or "").strip()
    if not text:
        raise ValueError("模型输出为空，无法提取 JSON")
    try:
        return _json.loads(text)
    except ValueError:
        pass
    fence = _re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, _re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for cand in candidates:
        try:
            return _json.loads(cand)
        except ValueError:
            continue
    raise ValueError(f"模型输出中未找到可解析的 JSON: {text[:160]!r}")


class LiteLLMBackend(ChatBackend):
    """LiteLLM 统一网关：OpenAI/Anthropic 兼容 API（base_url + api_key 启用）。

    三协议差异处理（entry.api_format 选择，auto 按 provider 推导）：
    - chat_completions：经典 messages 协议（DeepSeek/通义/GLM/方舟等绝大多数
      OpenAI 兼容端点）；响应取 choices[0].message.content；
    - responses：OpenAI Responses 协议（o 系/gpt-5 原生端点）；请求为
      input/instructions，响应取 output items 的 output_text，usage 字段名为
      input_tokens/output_tokens（与 chat 的 prompt_tokens/completion_tokens 不同）；
    - anthropic_messages：Anthropic Messages 协议（litellm 负责翻译并归一化
      回 choices 形状）。
    推理模型的思考内容（reasoning_content / thinking）单独提取进 "reasoning"，
    不混入最终回答 content；JSON 模式被端点拒绝时自动回退无 response_format 重试。
    """

    async def complete(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: int = 120,
        response_format: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_delta: DeltaCallback | None = None,
        reasoning_effort: str = "",
    ) -> dict[str, Any]:
        api_format = entry.api_format
        if api_format == "auto":
            api_format = ("anthropic_messages" if entry.provider == "anthropic_compatible"
                          else "chat_completions")
        if api_format == "responses":
            return await self._complete_responses(
                entry, messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
                tools=tools, tool_choice=tool_choice, reasoning_effort=reasoning_effort)
        return await self._complete_chat(
            entry, messages,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            response_format=response_format,
            anthropic=api_format == "anthropic_messages",
            tools=tools, tool_choice=tool_choice, on_delta=on_delta,
            reasoning_effort=reasoning_effort)

    # ------------------------------------------------------------ chat completions / anthropic

    async def _complete_chat(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int | None,
        timeout: int,
        response_format: str | None,
        anthropic: bool,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_delta: DeltaCallback | None = None,
        reasoning_effort: str = "",
        _retry_truncated: bool = False,
    ) -> dict[str, Any]:
        import litellm  # 延迟导入
        # litellm 认不出供应商时往终端打红色 Provider List 横幅——纯提示
        # 噪音（真实错误仍会抛 BadRequestError），官方开关一次关掉
        litellm.suppress_debug_info = True

        messages = copy.deepcopy(messages)
        tools = copy.deepcopy(tools)
        if entry.prompt_caching == "anthropic_ephemeral":
            if not anthropic:
                raise ValueError("显式 Anthropic 缓存需要 anthropic_messages 协议")
            # tools → system → 共享事实 → 最近消息，最多四个断点；
            # 不能只缓存短 system 而让整个工具环和大事实块每次重算。
            if tools:
                tools[-1]["cache_control"] = {"type": "ephemeral"}
            system = next((m for m in messages if m.get("role") == "system"), None)
            if system and isinstance(system.get("content"), str):
                system["content"] = [{"type": "text", "text": system["content"],
                                      "cache_control": {"type": "ephemeral"}}]
            eligible = [m for m in messages if m.get("role") in {"user", "tool"}
                        and isinstance(m.get("content"), str) and m["content"]]
            for message in ([eligible[0], eligible[-1]] if len(eligible) > 1 else eligible):
                if message["role"] == "tool":
                    message["cache_control"] = {"type": "ephemeral"}
                else:
                    message["content"] = [{"type": "text", "text": message["content"],
                                           "cache_control": {"type": "ephemeral"}}]
        kwargs: dict[str, Any] = {
            "model": entry.model,
            "messages": messages,
            "temperature": temperature,
            "timeout": timeout,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        elif anthropic:
            # Anthropic Messages 协议必填输出上限：档案未声明时按模型最大
            # 输出给足保底，思考预算也从中扣除（_apply_effort）
            kwargs["max_tokens"] = _ANTHROPIC_MIN_MAX_TOKENS
        if entry.base_url:
            kwargs["api_base"] = entry.base_url
        if entry.api_key:
            kwargs["api_key"] = entry.api_key
        # 发给 litellm 的模型名必须带可路由前缀：裸名解析不出供应商时
        # litellm 打红字横幅且调用必败（前缀规则见 _routed_model_name）
        kwargs["model"] = self._routed_model_name(
            entry.model, entry.base_url, anthropic=anthropic)
        self._apply_effort(kwargs, reasoning_effort, anthropic=anthropic)
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        elif response_format == "json":
            # 工具环与 JSON 模式互斥：有工具时由 schema 约束结构
            kwargs["response_format"] = {"type": "json_object"}
        streaming = on_delta is not None
        if streaming:
            kwargs["stream"] = True
            # Anthropic 兼容端点也需要 usage：不设 stream_options 时
            # 流式响应可能不含 usage 块，token 计数会归零
            kwargs["stream_options"] = {"include_usage": True}

        requested_at = time.perf_counter()
        try:
            resp = await litellm.acompletion(**kwargs)
        except Exception as exc:
            # 端点能力差异回退重试一次：stream_options 不被支持（部分兼容层）、
            # response_format 不被支持（推理模型/国产兼容层）。重试是显式降级，
            # 解析端有 extract_json 兜底，不静默。
            retried = False
            if kwargs.get("stream_options") and self._opt_unsupported(
                    exc, "stream_options"):
                kwargs.pop("stream_options", None)
                retried = True
            if not retried and response_format == "json" and self._json_mode_unsupported(exc):
                kwargs.pop("response_format", None)
                retried = True
            if not retried and self._tool_args_truncated(exc) and not _retry_truncated:
                return await self._reissue_truncated_call(
                    entry, messages, temperature=temperature, max_tokens=max_tokens,
                    timeout=timeout, response_format=response_format, anthropic=anthropic,
                    tools=tools, tool_choice=tool_choice, on_delta=on_delta,
                    reasoning_effort=reasoning_effort)
            if not retried:
                raise
            resp = await litellm.acompletion(**kwargs)

        if streaming:
            try:
                return await self._consume_stream(resp, on_delta, started=requested_at)
            except Exception as exc:
                # 流式的截断错误在增量消费时才抛出（非流式在 acompletion 就抛）
                if self._tool_args_truncated(exc) and not _retry_truncated:
                    return await self._reissue_truncated_call(
                        entry, messages, temperature=temperature, max_tokens=max_tokens,
                        timeout=timeout, response_format=response_format, anthropic=anthropic,
                        tools=tools, tool_choice=tool_choice, on_delta=on_delta,
                        reasoning_effort=reasoning_effort)
                raise

        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return {
            "content": choice.message.content or "",
            "reasoning": str(getattr(choice.message, "reasoning_content", "") or ""),
            **cache_usage(usage),
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "tool_calls": normalize_tool_calls(
                getattr(choice.message, "tool_calls", None)),
            "finish_reason": str(getattr(choice, "finish_reason", "") or ""),
        }

    async def _reissue_truncated_call(
        self, entry: ModelEntry, messages: list[dict[str, Any]], **params: Any,
    ) -> dict[str, Any]:
        """工具参数截断后的重发（只重发一次）。

        模型把单次工具参数写得超长、生成在中途被掐断时，残缺 JSON 过不了
        litellm 的严格解析直接抛错——工具环收不到 parse_error 无从回灌。
        这里把「截断、未执行」作为事实追加进对话重发一次，怎么缩短参数
        由模型自己决定（与 parse_tool_arguments 把解析错误回灌模型自纠是
        同一语义，只是故障点在更底层的协议转换）。
        """
        retry = copy.deepcopy(messages)
        retry.append({"role": "user", "content": _TRUNCATED_TOOL_ARGS_NOTE})
        return await self._complete_chat(entry, retry, **params, _retry_truncated=True)

    @staticmethod
    def _tool_args_truncated(exc: Exception) -> bool:
        """litellm 协议层工具参数解析失败的特征串（Anthropic tool invoke）。"""
        return "Failed to parse tool call arguments" in str(exc)

    # ------------------------------------------------------------ 流式累积

    async def _consume_stream(
        self, stream: Any, on_delta: DeltaCallback, *, started: float | None = None,
    ) -> dict[str, Any]:
        """累积流式增量：文本/思考逐块投递回调，工具调用按 index 拼接分片。

        流式只影响观感与首字延迟，最终产物与非流式路径同构（content /
        tool_calls / usage / finish_reason），上层工具环无感知。
        """
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict[str, Any]] = {}
        usage: Any = None
        finish_reason = ""
        first_delta_ms = None
        started = started if started is not None else time.perf_counter()
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = str(choice.finish_reason)
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            if first_delta_ms is None and any(getattr(delta, k, None) for k in ("content", "reasoning_content", "reasoning", "tool_calls")):
                first_delta_ms = round((time.perf_counter() - started) * 1000, 1)
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                await emit_delta(on_delta, "text", text)
            think = (getattr(delta, "reasoning_content", None)
                     or getattr(delta, "reasoning", None))
            if think:
                reasoning_parts.append(str(think))
                await emit_delta(on_delta, "reasoning", str(think))
            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = int(getattr(tc, "index", 0) or 0)
                slot = tool_slots.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""})
                if getattr(tc, "id", None):
                    slot["id"] = str(tc.id)
                fn = getattr(tc, "function", None)
                if fn is None:
                    continue
                if getattr(fn, "name", None):
                    slot["name"] = str(fn.name)
                frag = getattr(fn, "arguments", None)
                if frag:
                    slot["arguments"] += str(frag)
                    await emit_delta(on_delta, "tool_call", json.dumps(
                        {"index": idx, "name": slot["name"], "fragment": str(frag)},
                        ensure_ascii=False))
        # 还原 OpenAI 工具调用外层结构，统一交给 normalize_tool_calls 解析
        # （分片累积出的是扁平槽位；name/arguments 的解析与报错只此一处）
        ordered = [
            {"id": tool_slots[i]["id"],
             "function": {"name": tool_slots[i]["name"],
                          "arguments": tool_slots[i]["arguments"]}}
            for i in sorted(tool_slots)
        ]
        return {
            "first_delta_ms": first_delta_ms,
            "content": "".join(content_parts),
            "reasoning": "".join(reasoning_parts),
            **cache_usage(usage),
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "tool_calls": normalize_tool_calls(ordered),
            "finish_reason": finish_reason,
        }

    # ------------------------------------------------------------ OpenAI Responses 协议

    async def _complete_responses(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int | None,
        timeout: int,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        reasoning_effort: str = "",
    ) -> dict[str, Any]:
        import litellm  # 延迟导入

        # messages 协议 → Responses 协议：system 提升为 instructions，其余入
        # input。多轮工具对话必须显式转 function_call / function_call_output
        # 项并回填 call_id——只映射 role/content 会把"谁调了什么、结果对应
        # 哪次调用"全部抹掉（本地回显端点实测证实），端点侧无法续上工具轮。
        instructions = "\n".join(m.get("content", "") for m in messages
                                 if m.get("role") == "system") or None
        input_items: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                continue
            if role == "assistant" and m.get("tool_calls"):
                text = str(m.get("content", "") or "")
                if text:
                    input_items.append({"role": "assistant", "content": text})
                for tc in m["tool_calls"]:
                    fn = tc.get("function") or {}
                    input_items.append({
                        "type": "function_call",
                        "call_id": str(tc.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or "{}"),
                    })
            elif role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": str(m.get("tool_call_id") or ""),
                    "output": str(m.get("content", "") or ""),
                })
            else:
                input_items.append({"role": role, "content": m.get("content", "")})
        # 工具清单同步转 Responses 形状：扁平 {name,description,parameters}，
        # 不带 Chat Completions 的 function 包装层
        responses_tools = [
            {"type": "function",
             "name": str(t["function"].get("name") or ""),
             "description": str(t["function"].get("description") or ""),
             "parameters": t["function"].get("parameters") or {"type": "object"}}
            for t in (tools or [])
            if isinstance(t, dict) and isinstance(t.get("function"), dict)
        ]
        kwargs: dict[str, Any] = {
            "model": entry.model if entry.model.startswith("openai/") else f"openai/{entry.model}",
            "input": input_items,
            "temperature": temperature,
            "timeout": timeout,
        }
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens
        if instructions:
            kwargs["instructions"] = instructions
        if entry.base_url:
            kwargs["api_base"] = entry.base_url
        if entry.api_key:
            kwargs["api_key"] = entry.api_key
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        if responses_tools:
            kwargs["tools"] = responses_tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        if reasoning_effort:
            # Responses 协议的思考强度走 reasoning.effort
            kwargs["reasoning"] = {"effort": reasoning_effort}
            kwargs.pop("temperature", None)  # 推理档与采样温度互斥

        resp = await litellm.aresponses(**kwargs)
        # 响应结构：output items（message/reasoning/function_call…），文本在
        # message 项的 content output_text 块；output_text 为便捷聚合属性
        content = str(getattr(resp, "output_text", "") or "")
        reasoning_parts: list[str] = []
        raw_calls: list[dict[str, Any]] = []
        texts: list[str] = []
        for item in (getattr(resp, "output", None) or []):
            itype = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
            if itype == "message":
                for block in (getattr(item, "content", None)
                              or (item.get("content") if isinstance(item, dict) else []) or []):
                    btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
                    if btype == "output_text":
                        texts.append(str(getattr(block, "text", None)
                                       or (block.get("text") if isinstance(block, dict) else "") or ""))
            elif itype == "reasoning":
                for block in (getattr(item, "summary", None)
                              or (item.get("summary") if isinstance(item, dict) else []) or []):
                    reasoning_parts.append(str(getattr(block, "text", None)
                                           or (block.get("text") if isinstance(block, dict) else "") or ""))
            elif itype == "function_call":
                raw_calls.append({
                    "id": str(getattr(item, "call_id", None)
                              or (item.get("call_id") if isinstance(item, dict) else "") or ""),
                    "function": {
                        "name": str(getattr(item, "name", None)
                                    or (item.get("name") if isinstance(item, dict) else "") or ""),
                        "arguments": str(getattr(item, "arguments", None)
                                         or (item.get("arguments") if isinstance(item, dict) else "") or ""),
                    },
                })
        if not content:
            content = "".join(texts)
        usage = getattr(resp, "usage", None)
        return {
            "content": content,
            "reasoning": "".join(reasoning_parts),
            # Responses 协议 usage 字段名是 input_tokens/output_tokens
            **cache_usage(usage),
            "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "tool_calls": normalize_tool_calls(raw_calls),
            "finish_reason": str(getattr(resp, "status", "") or ""),
        }

    @staticmethod
    def _apply_effort(kwargs: dict[str, Any], effort: str, *, anthropic: bool) -> None:
        """思考强度下发（取值只来自该模型条目自己声明的档位）。

        - chat completions：``reasoning_effort`` 原样透传，端点自行裁决——litellm
          对未收录的模型名就是透传，端点不认会明确报错，不会被悄悄吞掉；
        - anthropic messages：litellm 只对它模型表里收录的 Claude 模型放行
          ``thinking`` / ``reasoning_effort``，第三方兼容端点的自有模型名（glm-5.3
          等）会被它整单拒绝，所以先问 litellm 认不认这个模型名：认，走它的原生
          翻译；不认，用 ``allowed_openai_params`` 把参数按 Anthropic 协议原样
          送进请求体，由端点自行裁决。
        纯数字取值一律按原生 token 预算发 ``thinking``（Anthropic 开思考时
        temperature 必须为 1、max_tokens 必须大于预算，这里一并满足）；档位名
        （如 high）发 ``reasoning_effort``——它是端点自己的词汇，端点怎么解释由
        端点决定，端点的思考开关一并打开。端点不认会明确报错，不静默降级。
        """
        if not effort:
            return
        if not anthropic:
            kwargs["reasoning_effort"] = effort
            return
        kwargs["temperature"] = 1
        thinking: dict[str, Any] | None = None
        if effort.isdigit():
            budget = int(effort)
            thinking = {"type": "enabled", "budget_tokens": budget}
            kwargs["max_tokens"] = max(int(kwargs.get("max_tokens") or 0), budget + 1024)
        if LiteLLMBackend._anthropic_native_effort(str(kwargs.get("model") or "")):
            if thinking is not None:
                kwargs["thinking"] = thinking
            else:
                kwargs["reasoning_effort"] = effort
            return
        # litellm 不认识的模型名（第三方兼容端点的自有模型）：带 allowed_openai_params
        # 原样透传，参数不经过 litellm 的模型表校验，直接进请求体。
        kwargs["allowed_openai_params"] = ["thinking", "reasoning_effort"]
        kwargs["thinking"] = thinking if thinking is not None else {"type": "enabled"}
        if thinking is None:
            kwargs["reasoning_effort"] = effort

    @staticmethod
    def _routed_model_name(model: str, base_url: str, *, anthropic: bool) -> str:
        """发给 litellm 的模型名：带协议前缀才有可路由的供应商。

        Anthropic 协议走 anthropic/ 前缀；其余带自定义 base_url 的端点一律
        openai/ 前缀（自定义端点即 OpenAI 兼容传输）。此前 openai/ 前缀只认
        openai_compatible 一种字面值——手写 YAML 带其它 provider 值的条目会
        以裸模型名落进 litellm，解析不出供应商即终端刷红字横幅且调用必败。
        无 base_url 的裸名保持原样：留给 litellm 按模型名原生路由。
        """
        if anthropic:
            return model if model.startswith("anthropic/") else f"anthropic/{model}"
        if base_url and not model.startswith(("openai/", "anthropic/")):
            return f"openai/{model}"
        return model

    @staticmethod
    def _anthropic_native_effort(model: str) -> bool:
        """litellm 是否认识这个模型名、肯替它翻译思考强度参数。

        判据就是 litellm 自己放行参数时用的那张表（``get_supported_openai_params``）：
        收录的 Claude 模型支持 ``reasoning_effort``/``thinking``，其余模型名一律
        不支持。探测失败按不支持处理——走原样透传，由端点裁决。
        """
        try:
            import litellm

            params = litellm.get_supported_openai_params(
                model=model, custom_llm_provider="anthropic") or []
        except Exception:  # noqa: BLE001 —— 探测失败不阻断调用，按透传处理
            return False
        return "reasoning_effort" in params

    @staticmethod
    def _opt_unsupported(exc: Exception, opt: str) -> bool:
        msg = str(exc).lower()
        return opt in msg and any(k in msg for k in (
            "not support", "unsupported", "unknown", "invalid", "unexpected"))

    @staticmethod
    def _json_mode_unsupported(exc: Exception) -> bool:
        msg = str(exc).lower()
        return ("response_format" in msg or "json_object" in msg
                or ("json" in msg and ("not support" in msg or "unsupported" in msg
                                       or "invalid" in msg)))


class MockBackend(ChatBackend):
    """确定性测试后端。

    应答规则（按 messages 中 system 的 ``ROLE: <name>`` 标记分发）：
    - ROLE: meta_planner    → 返回固定 DAG 规划 JSON
    - ROLE: route_planner   → 固定路线计划 JSON（CTF 调查角度提议另按载荷识别）
    - ROLE: vuln_analyst    → 按用户消息中是否含漏洞线索返回判定 JSON
    - ROLE: mode_router     → 回显用户消息中的 fallback_guess 模式
    - ROLE: route_budget    → 每任务固定预算 6 的 JSON
    - ROLE: ctf_solver      → 有工具时按工具环应答（先 http_request /flag.txt，
                              见到工具结果即收尾）；无工具时回退旧 JSON 动作
    - 默认                  → 回显摘要文本
    可通过 ``script`` 注入精确应答队列（先进先出），用于精细化测试。
    """

    def __init__(self, script: list[str] | None = None, fail_times: int = 0):
        self.script = list(script or [])
        self.fail_times = fail_times
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: int = 120,
        response_format: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        on_delta: DeltaCallback | None = None,
        reasoning_effort: str = "",
    ) -> dict[str, Any]:
        self.calls.append({"model": entry.model, "messages": messages,
                           "effort": reasoning_effort,
                           "tools": [t.get("function", {}).get("name") for t in tools or []]})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TimeoutError("MockBackend 故障注入：模拟主模型超时")
        await asyncio.sleep(0)  # 保持异步语义

        tool_calls: list[dict[str, Any]] = []
        if self.script:
            content = self.script.pop(0)
        elif tools:
            content, tool_calls = self._rule_based_tools(messages)
        else:
            content = self._rule_based(messages)
        if on_delta is not None and content:
            await emit_delta(on_delta, "text", content)

        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        return {
            "content": content,
            "tool_calls": tool_calls,
            "finish_reason": "tool_calls" if tool_calls else "stop",
            "prompt_tokens": max(1, prompt_chars // 4),
            "completion_tokens": max(1, len(content) // 4),
        }

    @staticmethod
    def _rule_based_tools(
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """工具环的确定性应答：无工具结果 → 定向 /flag.txt；有结果 → 收尾。"""
        seen_result = any(m.get("role") == "tool" for m in messages)
        if seen_result:
            return "mock：已获取工具结果，调查结束。", []
        return "", [{"id": "call_mock_1", "name": "http_request",
                     "arguments": {"method": "GET", "path": "/flag.txt"},
                     "raw": '{"method": "GET", "path": "/flag.txt"}'}]

    @staticmethod
    def _rule_based(messages: list[dict[str, Any]]) -> str:
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        # 测试后端还原追加式现场，语义与真实模型接收的补丁相同。
        latest = extract_json(user)
        if isinstance(latest, dict) and ("snapshot" in latest or "patch" in latest):
            state: dict[str, Any] = {}
            for message in messages:
                if message.get("role") != "user":
                    continue
                item = extract_json(message.get("content", ""))
                if not isinstance(item, dict):
                    continue
                if "snapshot" in item:
                    state = copy.deepcopy(item["snapshot"])
                for change in item.get("patch", []):
                    keys = [key.replace("~1", "/").replace("~0", "~")
                            for key in change["path"].lstrip("/").split("/")]
                    parent = state
                    for key in keys[:-1]:
                        parent = parent[key]
                    if change["op"] == "remove":
                        parent.pop(keys[-1], None)
                    else:
                        parent[keys[-1]] = copy.deepcopy(change["value"])
            user = json.dumps(state, ensure_ascii=False)
        role = ""
        for line in system.splitlines():
            if line.startswith("ROLE:"):
                role = line.split(":", 1)[1].strip()
                break

        if role == "meta_planner":
            return json.dumps(
                {
                    "analysis": "mock 全局分析：按 ATT&CK 战术拆解为侦察→初始访问两阶段",
                    "dag_nodes": [
                        {"tactic": "TA0043", "title": "侦察与资产测绘", "depends_on": []},
                        {"tactic": "TA0001", "title": "Web 漏洞探测", "depends_on": ["TA0043"]},
                    ],
                    "tasks": [
                        {"task_type": "recon", "agent_type": "recon", "priority": 3,
                         "payload": {"depth": "standard"}},
                        {"task_type": "web_probe", "agent_type": "web_vuln", "priority": 4,
                         "payload": {"vuln_classes": ["sqli", "xss", "ssrf"]}},
                    ],
                    "continue_iteration": False,
                    "reason": "mock：核心目标已覆盖",
                },
                ensure_ascii=False,
            )
        if role == "route_planner":
            if '"existing_routes"' in user:
                return json.dumps(
                    {
                        "routes": [
                            {"name": "baseline", "strategy": "标准流水线", "tools": ["http_probe"]},
                            {"name": "deep_params", "strategy": "参数深度探测", "tools": ["http_probe"]},
                            {"name": "alt_vectors", "strategy": "替代攻击面", "tools": ["http_probe"]},
                        ]
                    },
                    ensure_ascii=False,
                )
            # CTF 调查角度提议（按题给 K 个角度；mock 无语义能力，给固定角度）
            return json.dumps(
                {"angles": [
                    {"name": "page_surface", "angle": "入口页与响应表面的线索"},
                    {"name": "hidden_paths", "angle": "常见隐藏路径与源码泄露"},
                ]},
                ensure_ascii=False,
            )
        if role == "payload_planner":
            # 步骤决策：mock 透传骨架候选（等价称职 LLM 的保守裁剪），
            # 保证离线 e2e 走 LLM 主路径且行为与骨架一致
            candidates: list[str] = []
            try:
                candidates = list(json.loads(user).get("candidate_payloads", []))
            except ValueError:
                pass
            return json.dumps(
                {"payloads": [{"payload": p, "purpose": "mock 透传骨架候选"}
                              for p in candidates],
                 "rationale": "mock：骨架候选全量采用"},
                ensure_ascii=False,
            )
        if role == "response_analyst":
            # 步骤分析：mock 不做幻觉信号（保守空集）；LLM 信号路径由单测覆盖
            return json.dumps({"signals": []}, ensure_ascii=False)
        if role == "business_analyst":
            try:
                evidence = list(json.loads(user).get("evidence") or [])
            except ValueError:
                evidence = []
            page_refs = [item.get("id") for item in evidence
                         if item.get("kind") == "page" and item.get("id")]
            tx_refs = [item.get("id") for item in evidence
                       if item.get("kind") == "har_transaction" and item.get("id")]
            facts = ([{"statement": "已观测到公开入口与页面导航",
                       "evidence_refs": page_refs[:3]}] if page_refs else [])
            workflows = ([{"name": "公开页面访问路径",
                           "steps": ["按页面中实际导航访问公开功能"],
                           "evidence_refs": page_refs[:8]}] if page_refs else [])
            if tx_refs:
                workflows.append({
                    "name": "请求记录中已观测的 HTTP 交易顺序",
                    "steps": ["按请求记录首次出现顺序访问端点"],
                    "evidence_refs": tx_refs[:8],
                })
            return json.dumps({
                "roles": [], "objects": [], "workflows": workflows,
                "trust_boundaries": [], "facts": facts, "hypotheses": [],
                "unknowns": ["离线 mock 不推断未观测的账号角色与权限"],
            }, ensure_ascii=False)
        if role == "path_planner":
            return json.dumps({"paths": [], "rationale": "mock：无新增"},
                              ensure_ascii=False)
        if role == "vuln_analyst":
            suspicious = any(k in user.lower() for k in ("sqli", "xss", "rce", "ssrf", "error", "flag{"))
            return json.dumps(
                {
                    "is_candidate": suspicious,
                    "vuln_type": "sqli" if "sqli" in user.lower() else "unknown",
                    "confidence": 0.6 if suspicious else 0.1,
                    "reason": "mock 规则判定",
                },
                ensure_ascii=False,
            )
        if role == "mode_router":
            # 智能模式路由：mock 无语义能力，按装配层兜底判定回显，
            # 保证离线 e2e 走 LLM 路由主路径且行为确定（G10）
            guess = "src"
            try:
                guess = str(json.loads(user).get("fallback_guess") or "src")
            except ValueError:
                pass
            return json.dumps({"mode": guess, "rationale": "mock：按兜底判定"},
                              ensure_ascii=False)
        if role == "route_budget":
            # 路线预算决策：mock 无复杂度判断能力，统一给足量预算 6
            # （不触发截断、增广由 route_planner 角色决定），保证离线 e2e
            # 行为确定且走通「LLM 决策 → 规则钳制 → 任务携带预算」链路
            budgets = []
            try:
                count = len(json.loads(user).get("tasks") or [])
            except ValueError:
                count = 0
            for i in range(count):
                budgets.append({"index": i, "route_budget": 6,
                                "rationale": "mock：足量预算"})
            return json.dumps({"budgets": budgets}, ensure_ascii=False)
        if role == "ctf_solver":
            # CTF 调查环：mock 固定提议 http_request 定向 /flag.txt（确定性，
            # 覆盖「LLM 提议 → 规则校验 → 执行 → 观察记录」全链路）；
            # 已见到 Flag 则收尾（等价称职 LLM 的完成判定）
            flags_found: list = []
            try:
                flags_found = list(json.loads(user).get("flags_found") or [])
            except ValueError:
                pass
            if flags_found:
                return json.dumps(
                    {"analysis": "mock：Flag 已取得，调查完成",
                     "action": {"type": "conclude",
                                "summary": f"已取得 Flag：{flags_found[0]}"},
                     "rationale": "mock 完成判定"},
                    ensure_ascii=False)
            return json.dumps(
                {"analysis": "mock：无真实推理，定向常见 flag 路径",
                 "action": {"type": "http_request", "method": "GET",
                            "path": "/flag.txt"},
                 "rationale": "mock 固定动作"},
                ensure_ascii=False)
        if role == "collab_judge":
            # 协作判定（投票/辩论/级联复判）：议题含误报线索 → reject，否则 pass
            negative = any(k in user for k in ("误报", "无法复现", "证据不足", "假阳性"))
            return json.dumps(
                {
                    "verdict": "reject" if negative else "pass",
                    "confidence": 0.9,
                    "reason": "mock 规则判定：议题"
                              + ("含误报线索" if negative else "无否决线索"),
                },
                ensure_ascii=False,
            )
        return f"MOCK-RESPONSE[{entry_role_safe(role)}]: {user[:200]}"

    @staticmethod
    def available() -> bool:
        return True


def entry_role_safe(role: str) -> str:
    return role or "default"


def backend_for(entry: ModelEntry, mock_backend: MockBackend | None = None) -> ChatBackend:
    """按 provider 选择后端。provider=mock 时共用注入的 MockBackend 实例。"""
    if entry.provider == "mock":
        return mock_backend or MockBackend()
    return LiteLLMBackend()
