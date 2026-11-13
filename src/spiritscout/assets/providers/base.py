"""外部资产 provider 的最小数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderCandidate:
    value: str
    asset_type: str = "domain"
    source_ref: str = ""
    observation: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResult:
    provider: str
    status: str
    candidates: list[ProviderCandidate] = field(default_factory=list)
    returned: int = 0
    truncated: bool = False
    error: str = ""
    cached: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "returned": self.returned,
            "candidate_count": len(self.candidates),
            "truncated": self.truncated,
            "cached": self.cached,
            **({"error": self.error} if self.error else {}),
        }
