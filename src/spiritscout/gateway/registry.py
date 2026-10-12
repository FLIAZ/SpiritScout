"""模型注册表：能力画像与主备关系。

路由语义（档位设计已退场）：**编队页显式指派是主路径**（AgentBoundGateway
注入绑定模型）；未指派时在能力合格的候选中按**注册表顺序**取首个——
models.yaml 的条目顺序即用户声明的优先级。
"""

from __future__ import annotations

from spiritscout.config.settings import ModelEntry
from spiritscout.core.exceptions import ConfigurationError


class ModelRegistry:
    def __init__(self, entries: list[ModelEntry]):
        if not entries:
            raise ConfigurationError("模型注册表为空：请先在 Web 控制台「模型」页配置至少一个模型")
        self._by_model: dict[str, ModelEntry] = {e.model: e for e in entries}

    def get(self, model: str) -> ModelEntry:
        try:
            return self._by_model[model]
        except KeyError as exc:
            raise ConfigurationError(f"未注册的模型: {model}") from exc

    def has(self, model: str) -> bool:
        return model in self._by_model

    def fallback_of(self, model: str) -> list[ModelEntry]:
        """主备关系：返回声明为该模型备机的条目（4.9.3-5 主备切换）。"""
        return [e for e in self._by_model.values() if model in e.is_fallback_for]

    def all(self) -> list[ModelEntry]:
        return list(self._by_model.values())

    def route(
        self, task_type: str = "", *, prefer_model: str | None = None,
        required_capability: str | None = None,
    ) -> ModelEntry:
        """路由：绑定模型优先；否则能力过滤后按注册表顺序取首个。

        required_capability 是硬要求：候选中无具备该能力者时在全表内放宽
        匹配，仍无才回退原候选（避免路由到不胜任的模型）。
        """
        if prefer_model:
            return self.get(prefer_model)
        pool = self.all()
        if required_capability:
            capable = [c for c in pool if required_capability in c.capabilities]
            pool = capable or pool
        return pool[0]

    def top_models(self, n: int, *, task_type: str = "") -> list[ModelEntry]:
        """投票/辩论候选：按注册表顺序取 n 个不同模型。"""
        return self.all()[: max(n, 0)]

    def cheapest_for(self, task_type: str = "") -> ModelEntry:
        """级联首棒：注册表顺序首个（用户配置顺序即优先级）。"""
        return self.all()[0]
