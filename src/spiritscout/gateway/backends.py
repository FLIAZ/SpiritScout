"""模型后端：LiteLLM（真实云端）+ Mock（确定性测试后端）。

Mock 后端用于全部单测与启动冒烟（合规自查 G10：研发验证不触碰真实 API）：
- 规则化应答：按 system prompt 中的角色标记返回确定性 JSON；
- 支持故障注入（fail_times），验证主备切换（4.9.3-5）。
"""

from __future__ import annotations

import abc
import asyncio
import json
from typing import Any

from spiritscout.config.settings import ModelEntry


class ChatBackend(abc.ABC):
    @abc.abstractmethod
    async def complete(
        self,
        entry: ModelEntry,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: int = 120,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        """返回 {"content": str, "prompt_tokens": int, "completion_tokens": int}"""


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
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: int = 120,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        api_format = entry.api_format
        if api_format == "auto":
            api_format = ("anthropic_messages" if entry.provider == "anthropic_compatible"
                          else "chat_completions")
        if api_format == "responses":
            return await self._complete_responses(
                entry, messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        return await self._complete_chat(
            entry, messages,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
            response_format=response_format,
            anthropic=api_format == "anthropic_messages")

    # ------------------------------------------------------------ chat completions / anthropic

    async def _complete_chat(
        self,
        entry: ModelEntry,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        timeout: int,
        response_format: str | None,
        anthropic: bool,
    ) -> dict[str, Any]:
        import litellm  # 延迟导入

        kwargs: dict[str, Any] = {
            "model": entry.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        if entry.base_url:
            kwargs["api_base"] = entry.base_url
        if entry.api_key:
            kwargs["api_key"] = entry.api_key
        if anthropic and not entry.model.startswith("anthropic/"):
            kwargs["model"] = f"anthropic/{entry.model}"
        elif (not anthropic and entry.provider == "openai_compatible" and entry.base_url
                and not entry.model.startswith("openai/")):
            # OpenAI 兼容端点：走 openai/ 前缀 + 自定义 base
            kwargs["model"] = f"openai/{entry.model}"
        if response_format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = await litellm.acompletion(**kwargs)
        except Exception as exc:
            # 部分端点（推理模型/国产兼容层）不支持 response_format：
            # 回退无 JSON 模式重试一次（不静默——审计可经 calls 观察，且解析端有 extract_json 兜底）
            if response_format == "json" and self._json_mode_unsupported(exc):
                kwargs.pop("response_format", None)
                resp = await litellm.acompletion(**kwargs)
            else:
                raise
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return {
            "content": choice.message.content or "",
            "reasoning": str(getattr(choice.message, "reasoning_content", "") or ""),
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        }

    # ------------------------------------------------------------ OpenAI Responses 协议

    async def _complete_responses(
        self,
        entry: ModelEntry,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        timeout: int,
    ) -> dict[str, Any]:
        import litellm  # 延迟导入

        # messages 协议 → Responses 协议：system 提升为 instructions，其余入 input
        instructions = "\n".join(m.get("content", "") for m in messages
                                 if m.get("role") == "system") or None
        input_items = [{"role": m["role"], "content": m.get("content", "")}
                       for m in messages if m.get("role") != "system"]
        kwargs: dict[str, Any] = {
            "model": entry.model if entry.model.startswith("openai/") else f"openai/{entry.model}",
            "input": input_items,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "timeout": timeout,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if entry.base_url:
            kwargs["api_base"] = entry.base_url
        if entry.api_key:
            kwargs["api_key"] = entry.api_key

        resp = await litellm.aresponses(**kwargs)
        # 响应结构：output items（message/reasoning/tool_call…），文本在
        # message 项的 content output_text 块；output_text 为便捷聚合属性
        content = str(getattr(resp, "output_text", "") or "")
        reasoning_parts: list[str] = []
        if not content:
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
            content = "".join(texts)
        usage = getattr(resp, "usage", None)
        return {
            "content": content,
            "reasoning": "".join(reasoning_parts),
            # Responses 协议 usage 字段名是 input_tokens/output_tokens
            "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }

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
    - ROLE: ctf_solver      → 固定提议 http_request 定向 /flag.txt；已见 Flag 则收尾
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
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: int = 120,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"alias": entry.alias, "messages": messages})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TimeoutError("MockBackend 故障注入：模拟主模型超时")
        await asyncio.sleep(0)  # 保持异步语义

        if self.script:
            content = self.script.pop(0)
        else:
            content = self._rule_based(messages)

        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        return {
            "content": content,
            "prompt_tokens": max(1, prompt_chars // 4),
            "completion_tokens": max(1, len(content) // 4),
        }

    @staticmethod
    def _rule_based(messages: list[dict[str, str]]) -> str:
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
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
            if '"direction_brief"' in user:
                # CTF 调查角度提议（按题给 K 个角度；mock 无语义能力，给固定角度）
                return json.dumps(
                    {"angles": [
                        {"name": "page_surface", "angle": "入口页与响应表面的线索"},
                        {"name": "hidden_paths", "angle": "常见隐藏路径与源码泄露"},
                    ]},
                    ensure_ascii=False,
                )
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
