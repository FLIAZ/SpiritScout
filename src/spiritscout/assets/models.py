"""资产候选模型；外部情报与正式资产保持隔离。"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from spiritscout.core.models import utcnow


class AssetStage(str, enum.Enum):
    EXTERNAL_CANDIDATE = "external_candidate"
    OWNERSHIP_SUPPORTED = "ownership_supported"
    IN_SCOPE = "in_scope"
    DNS_VERIFIED = "dns_verified"
    LIVE_VERIFIED = "live_verified"
    FINGERPRINTED = "fingerprinted"
    PRIORITIZED = "prioritized"
    TESTING = "testing"
    EXHAUSTED = "exhausted"
    DEFERRED = "deferred"


STAGE_RANK = {
    AssetStage.EXTERNAL_CANDIDATE: 0,
    AssetStage.OWNERSHIP_SUPPORTED: 1,
    AssetStage.IN_SCOPE: 2,
    AssetStage.DNS_VERIFIED: 3,
    AssetStage.LIVE_VERIFIED: 4,
    AssetStage.FINGERPRINTED: 5,
    AssetStage.PRIORITIZED: 2,
    AssetStage.TESTING: 2,
    AssetStage.EXHAUSTED: 2,
    AssetStage.DEFERRED: 2,
}


class AssetCandidate(BaseModel):
    """外部来源提出的调查候选，不代表授权、归属、存活或正式资产。"""

    candidate_id: str
    value: str
    asset_type: Literal["domain", "ip", "url", "service"] = "domain"
    stage: AssetStage = AssetStage.EXTERNAL_CANDIDATE
    providers: list[str] = Field(default_factory=list)
    source_count: int = Field(default=0, ge=0)
    source_refs: list[str] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    ownership_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    liveness_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    priority_score: int = Field(default=0, ge=0, le=100)
    priority_components: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
