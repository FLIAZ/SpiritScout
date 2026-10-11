"""模型注册表（4.9.4）：能力画像、成本、速率、质量评分。"""

from __future__ import annotations

from spiritscout.config.settings import ModelEntry
from spiritscout.core.exceptions import ConfigurationError

# 任务类型 → 推荐档位（4.9.2 路由表）
TASK_TIER_MAP: dict[str, str] = {
    "meta_planning": "flagship",     # 元规划 / DAG 决策 / 全局复盘
    "execution": "fast",             # 子Agent执行 / 工具调用 / 结果解析
    "codegen": "code",               # PoC / EXP 代码生成
    "verification": "rigorous",      # 漏洞核验 / 报告审查 / SRC 合规判定
    "ctf_special": "flagship",       # CTF 逆向 / 密码破解（特化模型 + 工具）
    "simple": "light",               # 简单任务 / 高吞吐
}

# 预算压力下的自动降档链（4.9.4 超预算自动降级到低档模型）
TIER_DOWNGRADE: dict[str, str] = {
    "flagship": "fast",
    "code": "fast",
    "rigorous": "fast",
    "fast": "light",
    "light": "light",                # 已是最低档，无法再降
}


class ModelRegistry:
    def __init__(self, entries: list[ModelEntry]):
        if not entries:
            raise ConfigurationError("模型注册表为空：请先在 Web 控制台「模型」页配置至少一个模型")
        self._by_model: dict[str, ModelEntry] = {e.model: e for e in entries}
        self._by_tier: dict[str, list[ModelEntry]] = {}
        for e in entries:
            self._by_tier.setdefault(e.tier, []).append(e)

    def get(self, model: str) -> ModelEntry:
        try:
            return self._by_model[model]
        except KeyError as exc:
            raise ConfigurationError(f"未注册的模型: {model}") from exc

    def has(self, model: str) -> bool:
        return model in self._by_model

    def find_by_tier(self, tier: str) -> list[ModelEntry]:
        return list(self._by_tier.get(tier, []))

    def fallback_of(self, model: str) -> list[ModelEntry]:
        """主备关系：返回声明为该模型备机的条目（4.9.3-5 主备切换）。"""
        return [e for e in self._by_model.values() if model in e.is_fallback_for]

    def all(self) -> list[ModelEntry]:
        return list(self._by_model.values())

    def route(
        self, task_type: str, *, prefer_model: str | None = None,
        required_capability: str | None = None,
    ) -> ModelEntry:
        """路由模式（4.9.3-1 + 6.10 模型选择公式的工程化简化）：

        候选集 = 任务推荐档位（缺档则全表）∩ 能力约束；
        选择 = argmax( quality_score / max(单位成本, ε) )，质量优先、成本调节。
        （预算降级走 route_degraded，不在此混入档位强制。）
        """
        if prefer_model:
            return self.get(prefer_model)
        tier = TASK_TIER_MAP.get(task_type, "fast")
        candidates = self.find_by_tier(tier) or self.all()
        if required_capability:
            # 能力约束是硬要求：档位内无候选时扩到全表，而非静默忽略（避免路由到不胜任的模型）
            capable = [c for c in candidates if required_capability in c.capabilities]
            if not capable:
                capable = [c for c in self.all() if required_capability in c.capabilities]
            candidates = capable or candidates

        def score(e: ModelEntry) -> float:
            unit_cost = max(e.cost_input_per_mtok + e.cost_output_per_mtok, 1e-6)
            return e.quality_score / unit_cost

        return max(candidates, key=score)

    @staticmethod
    def _unit_cost(e: ModelEntry) -> float:
        return e.cost_input_per_mtok + e.cost_output_per_mtok

    def cheapest_for(self, task_type: str = "simple") -> ModelEntry:
        """级联模式首棒：任务推荐档位内单位成本最低者（4.9.3-4 便宜模型先答）。"""
        candidates = self.find_by_tier(TASK_TIER_MAP.get(task_type, "fast")) or self.all()
        return min(candidates, key=lambda e: (self._unit_cost(e), -e.quality_score))

    def top_models(self, n: int, *, task_type: str = "verification") -> list[ModelEntry]:
        """投票/辩论候选：推荐档位优先、全表补足，按质量评分降序取 n 个不同模型。"""
        tier_first = self.find_by_tier(TASK_TIER_MAP.get(task_type, "fast"))
        rest = [e for e in self.all() if e not in tier_first]
        ordered = sorted(tier_first, key=lambda e: -e.quality_score) + sorted(
            rest, key=lambda e: -e.quality_score)
        return ordered[: max(n, 0)]

    def downgraded_tier(self, task_type: str) -> str:
        """预算降级后的目标档位（4.9.4）。"""
        return TIER_DOWNGRADE.get(TASK_TIER_MAP.get(task_type, "fast"), "light")

    def route_degraded(self, task_type: str,
                       *, required_capability: str | None = None) -> ModelEntry:
        """预算降级路由（4.9.4 超预算自动降级到低档模型）。

        降档后的档位有候选则在其内按质量/成本择优；**档位为空不得回退到
        全表择优**（那会把请求升级回旗舰，违背降级本意）——改为全表取
        单位成本最低者（成本并列时取质量高者）。
        """
        candidates = self.find_by_tier(self.downgraded_tier(task_type))
        if required_capability:
            capable = [c for c in candidates if required_capability in c.capabilities]
            candidates = capable or candidates
        if candidates:
            return max(candidates,
                       key=lambda e: e.quality_score / max(self._unit_cost(e), 1e-6))
        pool = self.all()
        if required_capability:
            capable = [c for c in pool if required_capability in c.capabilities]
            pool = capable or pool
        return min(pool, key=lambda e: (self._unit_cost(e), -e.quality_score))
