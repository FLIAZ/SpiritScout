"""工具参数截断重发 + 页面观察保文案的回归测试。

场景一（网关层）：模型把单次工具参数写得超长，生成中途被掐断，litellm
在协议层抛「Failed to parse tool call arguments」——工具环收不到
parse_error，只能在后端调用层把截断事实回灌重发一次。
场景二（CTF 观察层）：HTML 摘要的段序曾是接口清单在前、可见文本垫底，
总帽之下题面文案最先被截掉——模型看到的"页面"只剩接口，直接暴力打。
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.config.settings import ModelEntry
from spiritscout.ctf.agents import _html_digest
from spiritscout.gateway.backends import LiteLLMBackend
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry

# litellm 协议层抛错原文（Anthropic 兼容端点，工具参数 JSON 被截断）
_TRUNCATION_ERROR = Exception(
    "litellm.BadRequestError: AnthropicException - Failed to parse tool "
    "call arguments for tool 'python' (Anthropic tool invoke). "
    "Error: Unterminated string starting at: line 1 column 29 (char 28). "
    'Arguments: {"action":"analysis","code":"import base64, hmac')


def _ok_response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=None),
            finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))


class TruncatedToolCallRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_args_retried_once_with_feedback(self) -> None:
        """截断错误 → 追加事实说明重发一次，模型重发后成功。"""
        complete = AsyncMock(side_effect=[_TRUNCATION_ERROR, _ok_response()])
        backend = LiteLLMBackend()
        entry = ModelEntry(model="glm-5.3-flash", provider="anthropic_compatible")
        messages = [{"role": "user", "content": "解这道题"}]
        with patch("litellm.acompletion", complete):
            result = await backend.complete(entry, messages, tools=[
                {"type": "function", "function": {"name": "python",
                 "parameters": {"type": "object", "properties": {}}}}])
        self.assertEqual(result["content"], "ok")
        self.assertEqual(complete.call_count, 2)
        resent = complete.call_args_list[1].kwargs["messages"]
        self.assertEqual(resent[-1]["role"], "user")
        self.assertIn("截断", resent[-1]["content"])
        # 原对话不被污染（重发用的是副本）
        self.assertEqual(len(messages), 1)

    async def test_truncated_args_raise_after_single_retry(self) -> None:
        """重发后仍截断 → 异常原样上抛（交给主备切换），不死循环。"""
        complete = AsyncMock(side_effect=[_TRUNCATION_ERROR, _TRUNCATION_ERROR])
        backend = LiteLLMBackend()
        entry = ModelEntry(model="glm-5.3-flash", provider="anthropic_compatible")
        with patch("litellm.acompletion", complete):
            with self.assertRaises(Exception) as ctx:
                await backend.complete(entry, [{"role": "user", "content": "x"}])
        self.assertIn("Failed to parse tool call arguments", str(ctx.exception))
        self.assertEqual(complete.call_count, 2)

    async def test_unrelated_errors_not_retried(self) -> None:
        """无关错误不触发截断重发（维持原有异常语义）。"""
        complete = AsyncMock(side_effect=[TimeoutError("upstream timeout")])
        with patch("litellm.acompletion", complete):
            with self.assertRaises(TimeoutError):
                await LiteLLMBackend().complete(
                    ModelEntry(model="m", provider="openai_compatible"),
                    [{"role": "user", "content": "x"}])
        self.assertEqual(complete.call_count, 1)


class HtmlDigestPriorityTests(unittest.TestCase):
    def test_page_copy_survives_and_precedes_link_list(self) -> None:
        """题面文案（可见文本）先于链接清单，且尾部提示不被截掉。"""
        filler = "很长的铺垫文案。" * 220           # >1600 字符触发保头尾
        hint = "提示：真正的入口藏在本页注释里，请留意表单的字段命名。"
        links = "".join(f'<a href="/api/v1/item/{i}">item{i}</a>'
                        + (" " * 30) for i in range(40))
        page = (
            "<html><head><title>算力查询台</title>"
            "<style>body{margin:0;padding:0}</style></head><body>"
            f"<p>{filler}</p><p>{hint}</p>{links}"
            "<!-- TODO: remove debug endpoint /debug/eval before final -->"
            "<form action='/calc'><input name='expr'></form>"
            "</body></html>")
        digest = _html_digest(page)
        self.assertIsNotNone(digest)
        # 文案优先：可见文本排在链接之前
        self.assertLess(digest.index("[可见文本]"), digest.index("[链接]"))
        # 尾部提示保住（此前垫底 + 1000 字符截断必然丢失）
        self.assertIn(hint, digest)
        # 注释与表单仍在（其他维度不牺牲）
        self.assertIn("[注释]", digest)
        self.assertIn("[表单] GET /calc", digest)

    def test_short_page_kept_whole(self) -> None:
        """短页面全量进摘要（常见 CTF 入口页，无需压缩）。"""
        digest = _html_digest(
            "<html><body><h1>签到题</h1><p>输入口令拿 Flag</p>"
            "<a href='/login'>登录</a></body></html>")
        self.assertIn("输入口令拿 Flag", digest)
        self.assertIn("[链接] /login", digest)


class OutputCeilingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserve_never_exceeds_window(self) -> None:
        """档案输出上限(128K)大于窗口(100K)的组合：历史压缩预算不得被钳到
        1024 字符压死现场——防御后预算按窗口减半给。"""
        entry = ModelEntry(model="m", provider="mock", context_window=100000,
                           max_output_tokens=131072)
        gateway = ModelGateway(ModelRegistry([entry]))
        messages = [{"role": "user", "content": "观察记录 " + "x" * 3000}
                    for _ in range(30)]
        captured: dict[str, int] = {}

        async def fake_complete(_entry, msgs, **_kw):
            captured["size"] = len(json.dumps(msgs, ensure_ascii=False))
            return {"content": "ok", "prompt_tokens": 1,
                    "completion_tokens": 1, "tool_calls": []}

        with patch("spiritscout.gateway.gateway.backend_for",
                   return_value=SimpleNamespace(complete=fake_complete)):
            await gateway.chat(messages)
        # 防御后预算 = 100000 - 50000 = 50000 字符量级；无防御时会被钳到 1024
        self.assertGreater(captured["size"], 40000)


if __name__ == "__main__":
    unittest.main()
