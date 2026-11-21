from __future__ import annotations

import json
import unittest

from spiritscout.gateway.backends import LiteLLMBackend, extract_json


class RoutedModelNameTests(unittest.TestCase):
    """发给 litellm 的模型名必须带可路由前缀：裸名解析不出供应商时
    litellm 打红字横幅且调用必败。"""

    def test_anthropic_protocol_gets_prefix(self) -> None:
        self.assertEqual(
            LiteLLMBackend._routed_model_name("glm-5.3", "https://x", anthropic=True),
            "anthropic/glm-5.3")
        # 已带前缀的不叠双前缀
        self.assertEqual(
            LiteLLMBackend._routed_model_name("anthropic/glm-5.3", "", anthropic=True),
            "anthropic/glm-5.3")

    def test_custom_base_any_provider_gets_openai_prefix(self) -> None:
        # 手写 YAML 带非常规 provider 值：只要指向自定义端点就走 openai 传输
        for model in ("glm-5.3", "some-internal-model"):
            self.assertEqual(
                LiteLLMBackend._routed_model_name(model, "https://api.example.com",
                                                  anthropic=False),
                f"openai/{model}")

    def test_no_base_url_stays_bare_for_native_routing(self) -> None:
        # 无自定义端点的名字留给 litellm 原生路由（如官方 deepseek 模型名）
        self.assertEqual(
            LiteLLMBackend._routed_model_name("deepseek-chat", "", anthropic=False),
            "deepseek-chat")

    def test_explicit_prefix_respected(self) -> None:
        self.assertEqual(
            LiteLLMBackend._routed_model_name("openai/gpt-x", "https://x",
                                              anthropic=False),
            "openai/gpt-x")
        self.assertEqual(
            LiteLLMBackend._routed_model_name("anthropic/claude-x", "https://x",
                                              anthropic=False),
            "anthropic/claude-x")


class ExtractJsonTests(unittest.TestCase):
    def test_complete_fenced_and_embedded_json_preserve_values(self) -> None:
        values = [{"x": "}", "y": 1}, {"x": "a{b", "nested": [1, {"v": "[\"\\"}]},
                  [1, 2, "}[]"], {"json": '{"embedded":true}', "unicode": "中文"}]
        for value in values:
            encoded = json.dumps(value, ensure_ascii=False)
            for text in (encoded, f"```json\n{encoded}\n```", f"前言 {encoded} 后语"):
                with self.subTest(text=text):
                    self.assertEqual(extract_json(text), value)

    def test_compatibility_priority_and_invalid_json(self) -> None:
        self.assertEqual(extract_json('前言 {"first":1} 后语 [2]'), {"first": 1})
        self.assertEqual(extract_json('前言 {"first":1} ```json\n[2]\n```'), [2])
        for text in ('', '无 JSON', '前言 {"x":[1,} 后语', '前言 {"x":"未结束'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                extract_json(text)


if __name__ == "__main__":
    unittest.main()
