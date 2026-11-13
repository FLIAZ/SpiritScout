"""资产候选写入、合并与阶段晋级。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from spiritscout.assets.models import STAGE_RANK, AssetCandidate, AssetStage
from spiritscout.assets.scoring import score_candidate
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, BlackboardLayer, utcnow


def _normalized(value: str, asset_type: str) -> str:
    value = value.strip()
    if asset_type == "domain":
        return value.rstrip(".").lower()
    if asset_type == "url":
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme.lower()}://{host}{port}{parsed.path or '/'}"
    return value


class AssetCandidateService:
    def __init__(self, blackboard, *, actor: str = "scanner") -> None:
        self.blackboard = blackboard
        self.actor = actor

    def upsert(
        self, value: str, *, asset_type: str = "domain",
        stage: AssetStage = AssetStage.EXTERNAL_CANDIDATE,
        provider: str = "", source_ref: str = "",
        observation: dict[str, Any] | None = None,
        unknowns: list[str] | None = None,
    ) -> AssetCandidate:
        normalized = _normalized(value, asset_type)
        dedup_key = f"asset_candidate|{asset_type}|{normalized}"
        existing = self.blackboard.find_by_dedup_key(
            BlackboardLayer.RECON_CLUE, dedup_key)
        if existing:
            candidate = AssetCandidate.model_validate(existing["data"])
            if STAGE_RANK[stage] > STAGE_RANK[candidate.stage]:
                candidate.stage = stage
            candidate.last_seen = utcnow()
        else:
            candidate = AssetCandidate(
                candidate_id=new_id("candidate"), value=normalized,
                asset_type=asset_type, stage=stage,
                unknowns=["外部情报尚未经过本会话核验"],
            )
        if provider and provider not in candidate.providers:
            candidate.providers.append(provider)
        candidate.source_count = len(set(candidate.providers))
        if source_ref and source_ref not in candidate.source_refs:
            candidate.source_refs.append(source_ref)
        if observation and observation not in candidate.observations:
            candidate.observations.append(observation)
            candidate.observations = candidate.observations[-12:]
        if unknowns is not None:
            candidate.unknowns = list(dict.fromkeys(str(item) for item in unknowns if str(item)))
        if STAGE_RANK[candidate.stage] >= STAGE_RANK[AssetStage.DNS_VERIFIED]:
            candidate.liveness_confidence = max(candidate.liveness_confidence, 0.35)
            candidate.unknowns = [item for item in candidate.unknowns
                                  if "DNS" not in item and "外部情报" not in item]
        if STAGE_RANK[candidate.stage] >= STAGE_RANK[AssetStage.LIVE_VERIFIED]:
            candidate.liveness_confidence = max(candidate.liveness_confidence, 0.8)
        score, components = score_candidate(candidate)
        candidate.priority_score = score
        candidate.priority_components = components
        record_id = existing["record_id"] if existing else candidate.candidate_id
        self.blackboard.write(
            BlackboardLayer.RECON_CLUE, record_id,
            {"clue_type": "asset_candidate", **candidate.model_dump(mode="json")},
            agent_type=AgentType.SCANNER, updated_by=self.actor,
            dedup_key=dedup_key,
            **({"expected_version": existing["version"]} if existing else {}),
        )
        return candidate
