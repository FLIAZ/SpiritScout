"""资产候选的可解释排序；分数只决定调查顺序，不触发执行。"""

from __future__ import annotations

from typing import Any

from spiritscout.assets.models import STAGE_RANK, AssetCandidate, AssetStage


def score_candidate(candidate: AssetCandidate) -> tuple[int, list[dict[str, Any]]]:
    components: list[dict[str, Any]] = []

    def add(name: str, points: int, basis: str) -> None:
        components.append({"name": name, "points": points, "basis": basis})

    rank = STAGE_RANK[candidate.stage]
    if rank >= STAGE_RANK[AssetStage.IN_SCOPE]:
        add("scope_alignment", 30, "候选值与用户声明的结构化授权范围一致")
    if rank >= STAGE_RANK[AssetStage.DNS_VERIFIED]:
        add("dns_evidence", 20, "本会话已完成 DNS 核验")
    if rank >= STAGE_RANK[AssetStage.LIVE_VERIFIED]:
        add("live_evidence", 25, "本会话已获得可达 HTTP 响应")
    if rank >= STAGE_RANK[AssetStage.FINGERPRINTED]:
        add("fingerprint_evidence", 10, "响应提供了可复核的技术或服务指纹")
    source_points = min(15, candidate.source_count * 5)
    if source_points:
        add("source_diversity", source_points,
            f"来自 {candidate.source_count} 类相互独立的来源")
    if rank < STAGE_RANK[AssetStage.DNS_VERIFIED]:
        add("unverified_penalty", -15, "仍仅是外部或范围候选，尚无本地 DNS 实证")

    score = max(0, min(100, sum(int(item["points"]) for item in components)))
    return score, components
