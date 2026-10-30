"""模型注册表与主备关系。

优先使用显式角色指派；未指派时按注册表顺序选择能力匹配的模型。"""

from __future__ import annotations

from spiritscout.config.model_identity import resolve_model
from spiritscout.config.settings import ModelEntry
from spiritscout.core.exceptions import ConfigurationError


class ModelRegistry:
    def __init__(self, entries: list[ModelEntry]):
        if not entries:
            raise ConfigurationError("模型注册表为空：请先在 Web 控制台「模型」页配置至少一个模型")
        if len({e.key for e in entries}) != len(entries):
            raise ConfigurationError("同一供应商下模型 ID 存在重复")
        self._by_model: dict[str, ModelEntry] = {e.key: e for e in entries}

    def get(self, model: str) -> ModelEntry:
        return resolve_model(self.all(), model)

    def has(self, model: str) -> bool:
        try:
            self.get(model)
            return True
        except ConfigurationError:
            return False

    def fallback_of(self, model: str) -> list[ModelEntry]:
        """主备关系：返回声明为该模型备机的条目（4.9.3-5 主备切换）。"""
        # 旧裸名备用关系必须唯一，不能把另一供应商的同名模型也当作主机。
        target = self.get(model).key
        return [e for e in self._by_model.values() if any(
            self.has(ref) and self.get(ref).key == target for ref in e.is_fallback_for)]

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
