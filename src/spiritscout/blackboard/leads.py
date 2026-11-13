"""稳定调查线索卡片与 HTTP 真实性消歧。

这里只整理来源、响应差异和生命周期事实；是否值得继续投入仍由模型判断。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from spiritscout.core.exceptions import VersionConflict
from spiritscout.core.models import AgentType, BlackboardLayer

LEAD_STATUSES = {
    "new", "needs_disambiguation", "actionable", "investigating", "breakthrough",
    "blocked", "exhausted", "refuted",
}
_TARGET_ORIGINS = {
    "browser", "business_observation", "har", "model_navigated", "page_network",
    "target_response",
}
_DERIVED_ORIGINS = {"js_reverse", "js_static", "robots.txt", "source_map"}
_GENERATED_ORIGINS = {
    "dictionary", "fuzz", "guessed", "model_generated", "model_generated_test",
    "scanner_generated", "wordlist",
}
_INTERFACE_MARKERS = (
    '"openapi"', '"swagger"', "swagger-ui", "graphql", "__schema", "application/json",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lead_id(subject: str, method: str) -> str:
    digest = hashlib.sha256(f"{method.upper()}|{subject}".encode()).hexdigest()[:24]
    return f"lead-{digest}"


def _ref_identity(ref: str) -> str:
    name, separator, version = ref.rpartition("@")
    return name if separator and version.isdigit() else ref


def _latest_refs(old: list[Any], incoming: list[Any]) -> list[str]:
    """同一证据对象只保留最新版本；版本升级不伪装成独立新证据。"""
    merged: dict[str, str] = {}
    for value in [*old, *incoming]:
        if isinstance(value, str) and value:
            merged[_ref_identity(value)] = value[:200]
    return list(merged.values())[:32]


def origin_class(origins: list[str]) -> str:
    words = {part.lower() for value in origins for part in value.replace("-", "_").split("/")}
    if words & _TARGET_ORIGINS:
        return "target_observation"
    if words & _DERIVED_ORIGINS:
        return "derived_candidate"
    if words & _GENERATED_ORIGINS:
        return "generated_candidate"
    return "unspecified"


def response_profile(body: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """生成不含正文的响应轮廓，供软 200 与统一页面比较。"""
    headers = parsed.get("headers") if isinstance(parsed.get("headers"), dict) else {}
    content_type = str(headers.get("content-type") or parsed.get("content_type") or "").split(";", 1)[0].lower()
    text = str(body or "")
    normalized = re.sub(r"\b\d{2,}\b", "#", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    tokens = re.findall(r"[a-z_][a-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", normalized[:12000])
    token_hashes = sorted({hashlib.sha256(token.encode()).hexdigest()[:12] for token in tokens})[:256]
    return {
        "status_code": int(parsed.get("status_code") or 0),
        "content_type": content_type[:120],
        "content_length": int(parsed.get("content_length") or len(text)),
        "body_sha256": str(parsed.get("body_sha256") or hashlib.sha256(text.encode()).hexdigest()),
        "structure_sha256": hashlib.sha256(normalized[:12000].encode()).hexdigest(),
        "token_hashes": token_hashes,
        "final_url": str(parsed.get("final_url") or "")[:500],
        "redirect_count": len(parsed.get("redirect_chain") or []),
        "interface_markers": [marker for marker in _INTERFACE_MARKERS if marker in normalized][:8],
    }


def compact_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """持久化/模型上下文不需要 token 集合，只保留真实性复核所需摘要。"""
    return {key: profile[key] for key in (
        "status_code", "content_type", "content_length", "body_sha256",
        "structure_sha256", "final_url", "redirect_count", "interface_markers",
    ) if key in profile}


def _similarity(left: dict[str, Any] | None, right: dict[str, Any]) -> float | None:
    if not left:
        return None
    if left.get("body_sha256") == right.get("body_sha256"):
        return 1.0
    a, b = set(left.get("token_hashes") or []), set(right.get("token_hashes") or [])
    return round(len(a & b) / len(a | b), 3) if a or b else 0.0


def assess_authenticity(*, profile: dict[str, Any],
                        baseline: dict[str, Any] | list[dict[str, Any]] | None,
                        origin: str, marker_confirmed: bool = False) -> dict[str, Any]:
    """按可核对响应特征判定真实性状态，不推断漏洞或业务价值。"""
    status = int(profile.get("status_code") or 0)
    baselines = baseline if isinstance(baseline, list) else [baseline] if baseline else []
    similarities = [_similarity(item, profile) for item in baselines]
    similarity = max((value for value in similarities if value is not None), default=None)
    marker_confirmed = bool(marker_confirmed or profile.get("interface_markers"))
    html = "html" in str(profile.get("content_type") or "")
    same_shape = any(
        profile.get("structure_sha256") == item.get("structure_sha256")
        or (_similarity(item, profile) or 0.0) >= 0.92
        for item in baselines
    )
    same_landing = any(
        profile.get("final_url") and profile.get("final_url") == item.get("final_url")
        for item in baselines
    )
    generated = origin_class([origin]) == "generated_candidate"

    if status in {404, 410}:
        verdict, lead_status = "authenticity_refuted", "refuted"
        reason = f"HTTP {status}"
    elif status >= 400 or status == 0:
        verdict, lead_status = "unreachable_or_blocked", "blocked"
        reason = f"HTTP {status or 'unknown'}"
    elif status == 200 and html and (same_shape or same_landing) and not marker_confirmed:
        verdict, lead_status = "soft_200_suspected", "needs_disambiguation"
        reason = "响应与首页基线高度相似或落到同一地址，且缺少独立接口标记"
    elif marker_confirmed:
        verdict, lead_status = "authenticity_confirmed", "actionable"
        reason = "响应包含候选类型的独立内容标记"
    elif not generated and origin_class([origin]) in {"target_observation", "derived_candidate"}:
        verdict, lead_status = "source_supported", "actionable"
        reason = "候选来自目标行为或目标内容派生"
    else:
        verdict, lead_status = "needs_disambiguation", "needs_disambiguation"
        reason = "状态码本身不足以证明候选是独立真实入口"
    return {
        "verdict": verdict,
        "lead_status": lead_status,
        "reason": reason,
        "baseline_similarity": similarity,
        "same_landing_url": same_landing,
        "marker_confirmed": marker_confirmed,
    }


def upsert_lead(blackboard: Any, *, subject: str, method: str = "GET",
                origins: list[str] | None = None, source_refs: list[str] | None = None,
                evidence_state: str = "", status: str = "new",
                authenticity: dict[str, Any] | None = None,
                observation: dict[str, Any] | None = None,
                unknowns: list[str] | None = None,
                replace_unknowns: bool = False,
                next_minimal_experiment: str = "", actor: str = "",
                agent_type: AgentType = AgentType.MASTER,
                _allow_retry: bool = True) -> dict[str, Any]:
    """创建或更新稳定 Lead Card；同一 method+subject 始终复用同一卡片。"""
    clean_subject = str(subject).strip()[:500]
    lead_id = _lead_id(clean_subject, method)
    current = blackboard.read_versioned(BlackboardLayer.TOPOLOGY, lead_id)
    old, version = current if current else ({}, 0)
    clean_origins = list(dict.fromkeys([
        *[str(value)[:80] for value in old.get("origins", []) if isinstance(value, str)],
        *[str(value)[:80] for value in (origins or []) if isinstance(value, str)],
    ]))[:16]
    old_refs = [value for value in old.get("source_refs", []) if isinstance(value, str)]
    refs = _latest_refs(old_refs, source_refs or [])
    observations = list(old.get("observations") or [])
    if observation:
        compact_observation = {key: observation[key] for key in (
            "status_code", "content_type", "content_length", "body_sha256",
            "structure_sha256", "final_url", "redirect_count",
        ) if key in observation}
        if compact_observation not in observations:
            observations.append(compact_observation)
    observations = observations[-8:]
    saved_authenticity = authenticity if authenticity is not None else old.get("authenticity") or {}
    new_evidence = {_ref_identity(value) for value in refs} - {
        _ref_identity(value) for value in old_refs}
    material_update = not old or bool(new_evidence) \
        or observations != old.get("observations", []) \
        or saved_authenticity != old.get("authenticity", {})
    clean_status = status if status in LEAD_STATUSES else "new"
    if old.get("authenticity") and authenticity is None:
        clean_status = str(old.get("status") or clean_status)
        unknowns = None
    old_unknowns = [] if replace_unknowns else old.get("unknowns", [])
    card = {
        "kind": "lead_card",
        "lead_id": lead_id,
        "subject": clean_subject,
        "method": method.upper()[:16],
        "status": clean_status,
        "origins": clean_origins,
        "origin_class": origin_class(clean_origins),
        "source_refs": refs,
        "evidence_refs": refs,
        "evidence_state": str(evidence_state or old.get("evidence_state") or "unverified")[:40],
        "authenticity": saved_authenticity,
        "unknowns": list(dict.fromkeys([
            *[str(value)[:300] for value in old_unknowns if isinstance(value, str)],
            *[str(value)[:300] for value in (unknowns or []) if isinstance(value, str)],
        ]))[:16],
        "next_minimal_experiment": str(next_minimal_experiment
                                       or old.get("next_minimal_experiment") or "")[:500],
        "observations": observations,
        "observation_count": int(old.get("observation_count") or 0) + int(material_update),
        "created_at": old.get("created_at") or _now(),
        "updated_at": _now() if material_update or not old else old.get("updated_at") or _now(),
    }
    stable_keys = tuple(card.keys())
    if old and all(old.get(key) == card.get(key) for key in stable_keys):
        return {**card, "blackboard_ref": {
            "layer": BlackboardLayer.TOPOLOGY.value, "record_id": lead_id,
            "version": version,
        }}
    try:
        new_version = blackboard.write(
            BlackboardLayer.TOPOLOGY, lead_id, card, agent_type=agent_type,
            updated_by=actor, dedup_key=f"lead|{method.upper()}|{clean_subject}",
            expected_version=version,
        )
    except VersionConflict:
        if not _allow_retry:
            raise
        # 并行角色可能同时升级同一线索；回读最新版本后合并重试一次。
        return upsert_lead(
            blackboard, subject=subject, method=method, origins=origins,
            source_refs=source_refs, evidence_state=evidence_state, status=status,
            authenticity=authenticity, observation=observation, unknowns=unknowns,
            replace_unknowns=replace_unknowns,
            next_minimal_experiment=next_minimal_experiment,
            actor=actor, agent_type=agent_type,
            _allow_retry=False,
        )
    return {**card, "blackboard_ref": {
        "layer": BlackboardLayer.TOPOLOGY.value, "record_id": lead_id, "version": new_version,
    }}


def sync_asset_lead(blackboard: Any, *, record_id: str, version: int,
                    data: dict[str, Any], actor: str,
                    agent_type: AgentType) -> dict[str, Any] | None:
    """仅为需要消歧的端点候选建卡；普通资产继续走既有摘要。"""
    if data.get("asset_type") != "endpoint":
        return None
    attrs = data.get("attrs") if isinstance(data.get("attrs"), dict) else {}
    origins = [str(value) for value in attrs.get("sources", []) if isinstance(value, str)]
    if attrs.get("source"):
        origins.append(str(attrs["source"]))
    if not origins and actor:
        origins.append(actor)
    classification = origin_class(origins)
    # 真实业务流量已由资产层与业务上下文承载；Lead Card 只处理尚需鉴真的
    # 派生/生成候选，避免普通端点重复占用模型上下文。
    if classification not in {"derived_candidate", "generated_candidate"}:
        return None
    return upsert_lead(
        blackboard, subject=str(data.get("value") or record_id),
        method=str(attrs.get("method") or "GET"), origins=origins,
        source_refs=[f"asset:{record_id}@{version}"],
        evidence_state=str(data.get("evidence_state") or "unverified"),
        status="needs_disambiguation",
        unknowns=["该候选是否对应独立真实入口"],
        next_minimal_experiment="比较响应类型、正文摘要、重定向和目标特征与已有基线",
        actor=actor, agent_type=agent_type,
    )
