"""可追溯漏洞假设生命周期；记录复用现有探索图，不另建存储层。"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from spiritscout.core.exceptions import VersionConflict
from spiritscout.core.models import AgentType, BlackboardLayer

STATUSES = {"open", "investigating", "supported", "refuted", "exhausted", "blocked"}
TERMINAL_STATUSES = {"supported", "refuted", "exhausted"}
ALLOWED_TRANSITIONS = {
    "open": STATUSES,
    "investigating": STATUSES,
    "blocked": STATUSES,
    # supported 只是支持信号；独立复核仍可推翻，但执行失败不能把它降级成 blocked。
    "supported": {"supported", "refuted"},
    "refuted": {"refuted"},
    "exhausted": {"exhausted"},
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _identity(value: str) -> str:
    name, separator, version = str(value).rpartition("@")
    return name if separator and version.isdigit() else str(value)


def _version(value: str) -> int | None:
    _name, separator, version = str(value).rpartition("@")
    return int(version) if separator and version.isdigit() else None


def merge_versioned_refs(*groups: list[Any], limit: int = 32) -> list[str]:
    """按稳定身份去重引用，并保留已观察到的最高版本。"""
    merged: dict[str, tuple[str, int | None]] = {}
    for values in groups:
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            clean = value.strip()[:200]
            identity, version = _identity(clean), _version(clean)
            previous = merged.get(identity)
            if (previous is None or version is not None
                    and (previous[1] is None or version >= previous[1])):
                merged[identity] = (clean, version)
    return [value for value, _version_no in merged.values()][:limit]


def _latest_refs(old: list[Any], incoming: list[Any]) -> list[str]:
    return merge_versioned_refs(old, incoming)


def _merge_basis(old: Any, incoming: dict[str, Any]) -> dict[str, Any]:
    previous = old if isinstance(old, dict) else {}
    merged = dict(previous)
    for key, value in incoming.items():
        if isinstance(value, list):
            old_value = previous.get(key)
            earlier = old_value if isinstance(old_value, list) else ([old_value] if old_value else [])
            merged[key] = list(dict.fromkeys([*earlier, *value]))[:20]
        elif value:
            old_value = previous.get(key)
            if isinstance(old_value, list):
                merged[key] = list(dict.fromkeys([*old_value, value]))[:20]
            else:
                merged[key] = value
    return merged


def _record_id(subject: str, test_type: str, claim: str) -> str:
    normalized = re.sub(r"\s+", " ", claim).strip().lower()
    digest = hashlib.sha256(
        f"{test_type.strip().lower()}|{subject.strip().lower()}|{normalized}".encode()
    ).hexdigest()[:24]
    return f"hypothesis-{digest}"


def hypothesis_ref(record_id: str, version: int) -> str:
    return f"topology:{record_id}@{version}"


def resolve(blackboard: Any, ref: str) -> tuple[str, dict[str, Any], int] | None:
    """解析当前会话中的假设引用；旧版本引用可定位同一身份但返回最新版本。"""
    text = str(ref or "").strip()
    name, separator, version = text.rpartition("@")
    base = name if separator else text
    layer, colon, record_id = base.partition(":")
    if not colon or layer != BlackboardLayer.TOPOLOGY.value or not record_id:
        return None
    row = blackboard.read_versioned(BlackboardLayer.TOPOLOGY, record_id)
    if not row or row[0].get("kind") != "exploration" \
            or row[0].get("node_type") != "hypothesis":
        return None
    if separator and (not version.isdigit() or int(version) > row[1]):
        return None
    return record_id, row[0], row[1]


def upsert(blackboard: Any, *, claim: str, subject: str, test_type: str,
           asset_refs: list[str] | None = None, evidence_refs: list[str] | None = None,
           expected_signal: str = "", counter_signal: str = "",
           next_minimal_experiment: str = "", stop_condition: str = "",
           technical_basis: dict[str, Any] | None = None,
           actor: str, agent_type: AgentType,
           _allow_retry: bool = True) -> dict[str, Any]:
    """按 subject+test_type+claim 创建稳定假设，重复发现合并证据而非复制节点。"""
    clean_claim = re.sub(r"\s+", " ", str(claim or "")).strip()[:2000]
    clean_subject = str(subject or "").strip()[:500]
    clean_type = str(test_type or "unknown").strip()[:100]
    if not clean_claim or not clean_subject:
        raise ValueError("漏洞假设需要非空 claim 与 subject")
    record_id = _record_id(clean_subject, clean_type, clean_claim)
    current = blackboard.read_versioned(BlackboardLayer.TOPOLOGY, record_id)
    old, version = current if current else ({}, 0)
    basis = technical_basis if isinstance(technical_basis, dict) else {}
    clean_basis = {
        key: ([str(item)[:160] for item in value[:20] if isinstance(item, str)]
              if isinstance(value, list) else str(value)[:500])
        for key, value in basis.items()
        if key in {"technologies", "versions", "cve_ids", "template_ids", "tags", "rationale"}
        and isinstance(value, (str, list))
    }
    card = {
        "kind": "exploration", "node_type": "hypothesis",
        "hypothesis_contract": True, "hypothesis_id": record_id,
        "text": clean_claim, "subject": clean_subject, "test_type": clean_type,
        "state": str(old.get("state") or "open"),
        "reason": str(old.get("reason") or "")[:500],
        "asset_refs": _latest_refs(old.get("asset_refs", []), asset_refs or []),
        "evidence_refs": _latest_refs(old.get("evidence_refs", []), evidence_refs or []),
        "parent_refs": list(old.get("parent_refs") or [])[:16],
        "expected_signal": str(expected_signal or old.get("expected_signal") or "")[:500],
        "counter_signal": str(counter_signal or old.get("counter_signal") or "")[:500],
        "next_minimal_experiment": str(
            next_minimal_experiment or old.get("next_minimal_experiment") or "")[:500],
        "stop_condition": str(stop_condition or old.get("stop_condition") or "")[:500],
        "technical_basis": _merge_basis(old.get("technical_basis"), clean_basis),
        "assigned_tasks": list(old.get("assigned_tasks") or [])[-20:],
        "attempts": list(old.get("attempts") or [])[-20:],
        "created_at": old.get("created_at") or _now(),
        "updated_at": _now(),
    }
    stable = {key: value for key, value in card.items() if key != "updated_at"}
    if old and all(old.get(key) == value for key, value in stable.items()):
        return {**old, "hypothesis_ref": hypothesis_ref(record_id, version)}
    try:
        new_version = blackboard.write(
            BlackboardLayer.TOPOLOGY, record_id, card, agent_type=agent_type,
            updated_by=actor, dedup_key=f"hypothesis|{record_id}", expected_version=version,
        )
    except VersionConflict:
        if not _allow_retry:
            raise
        return upsert(
            blackboard, claim=claim, subject=subject, test_type=test_type,
            asset_refs=asset_refs, evidence_refs=evidence_refs,
            expected_signal=expected_signal, counter_signal=counter_signal,
            next_minimal_experiment=next_minimal_experiment, stop_condition=stop_condition,
            technical_basis=technical_basis, actor=actor, agent_type=agent_type,
            _allow_retry=False,
        )
    return {**card, "hypothesis_ref": hypothesis_ref(record_id, new_version)}


def transition(blackboard: Any, ref: str, *, state: str, reason: str,
               task_id: str = "", evidence_refs: list[str] | None = None,
               outcome: str = "", actor: str, agent_type: AgentType,
               _allow_retry: bool = True) -> dict[str, Any]:
    """将一次最小实验写回同一假设；supported 仍表示有支持信号，不等于漏洞终审。"""
    if state not in STATUSES:
        raise ValueError("未知假设状态")
    resolved = resolve(blackboard, ref)
    if resolved is None:
        raise ValueError("hypothesis_ref 不存在或不是漏洞假设")
    record_id, old, version = resolved
    old_state = str(old.get("state") or "open")
    if state not in ALLOWED_TRANSITIONS.get(old_state, set()):
        raise ValueError(f"漏洞假设不能从 {old_state} 转换为 {state}")
    now = _now()
    attempts = list(old.get("attempts") or [])
    attempt = {
        "task_id": str(task_id)[:100], "state": state,
        "outcome": str(outcome)[:120], "reason": str(reason)[:500], "at": now,
        "evidence_refs": _latest_refs([], evidence_refs or []),
    }
    if not attempts or attempts[-1] != attempt:
        attempts.append(attempt)
    assigned = list(old.get("assigned_tasks") or [])
    if task_id and task_id not in assigned:
        assigned.append(str(task_id)[:100])
    data = {
        **old, "state": state, "reason": str(reason)[:500],
        "evidence_refs": _latest_refs(old.get("evidence_refs", []), evidence_refs or []),
        "assigned_tasks": assigned[-20:], "attempts": attempts[-20:], "updated_at": now,
    }
    if state == "supported" and not data.get("first_supported_at"):
        data["first_supported_at"] = now
    try:
        new_version = blackboard.write(
            BlackboardLayer.TOPOLOGY, record_id, data, agent_type=agent_type,
            updated_by=actor, dedup_key=f"hypothesis|{record_id}", expected_version=version,
        )
    except VersionConflict:
        if not _allow_retry:
            raise
        return transition(
            blackboard, ref, state=state, reason=reason, task_id=task_id,
            evidence_refs=evidence_refs, outcome=outcome, actor=actor,
            agent_type=agent_type, _allow_retry=False,
        )
    return {**data, "hypothesis_ref": hypothesis_ref(record_id, new_version)}


def summary(blackboard: Any, *, limit: int = 200) -> dict[str, Any]:
    rows = [row for row in blackboard.query(
        BlackboardLayer.TOPOLOGY, limit=limit, data_filter=("kind", "exploration"))
        if row["data"].get("node_type") == "hypothesis"
        and row["data"].get("hypothesis_contract") is True]
    if not rows:
        return {}
    counts: dict[str, int] = {}
    attempts = 0
    samples = []
    review_pending = []
    first_supported_at = ""
    first_created_at = ""
    first_supported_seconds: list[float] = []
    for row in rows:
        data = row["data"]
        state = str(data.get("state") or "open")
        counts[state] = counts.get(state, 0) + 1
        attempts += len(data.get("attempts") or [])
        supported_at = str(data.get("first_supported_at") or "")
        created_at = str(data.get("created_at") or "")
        if created_at and (not first_created_at or created_at < first_created_at):
            first_created_at = created_at
        if supported_at and (not first_supported_at or supported_at < first_supported_at):
            first_supported_at = supported_at
        if supported_at and data.get("created_at"):
            try:
                elapsed = (datetime.fromisoformat(supported_at)
                           - datetime.fromisoformat(str(data["created_at"]))).total_seconds()
                if elapsed >= 0:
                    first_supported_seconds.append(elapsed)
            except (TypeError, ValueError):
                pass
        sample = {
                "hypothesis_ref": hypothesis_ref(row["record_id"], row["version"]),
                "claim": str(data.get("text") or "")[:300],
                "subject": str(data.get("subject") or "")[:300],
                "test_type": str(data.get("test_type") or "")[:100],
                "state": state,
                "reason": str(data.get("reason") or "")[:500],
                "expected_signal": str(data.get("expected_signal") or "")[:500],
                "counter_signal": str(data.get("counter_signal") or "")[:500],
                "next_minimal_experiment": str(data.get("next_minimal_experiment") or "")[:500],
                "stop_condition": str(data.get("stop_condition") or "")[:500],
                "technical_basis": data.get("technical_basis") or {},
                "asset_refs": list(data.get("asset_refs") or [])[:12],
                "evidence_refs": list(data.get("evidence_refs") or [])[:12],
            }
        if len(samples) < 16 and state not in TERMINAL_STATUSES:
            samples.append(sample)
        outcomes = {str(attempt.get("outcome") or "")
                    for attempt in data.get("attempts", []) if isinstance(attempt, dict)}
        last_outcome = str((data.get("attempts") or [{}])[-1].get("outcome") or "")
        if (len(review_pending) < 16 and state == "supported"
                and "independently_confirmed" not in outcomes):
            review_pending.append({**sample, "last_outcome": last_outcome})
    supported = counts.get("supported", 0)
    completed = supported + counts.get("refuted", 0) + counts.get("exhausted", 0)
    repeat_attempts = max(0, attempts - len(rows))
    time_to_first_supported = None
    if first_created_at and first_supported_at:
        try:
            time_to_first_supported = max(0.0, (datetime.fromisoformat(first_supported_at)
                - datetime.fromisoformat(first_created_at)).total_seconds())
        except (TypeError, ValueError):
            pass
    return {
        "total": len(rows), "by_state": dict(sorted(counts.items())),
        "active": samples, "review_pending": review_pending, "attempts": attempts,
        "supported": supported,
        "independently_confirmed": sum(
            1 for row in rows if any(
                attempt.get("outcome") == "independently_confirmed"
                for attempt in row["data"].get("attempts", []))),
        "conversion_rate": round(supported / completed, 3) if completed else None,
        "blocked_ratio": round(counts.get("blocked", 0) / len(rows), 3),
        "exhausted_ratio": round(counts.get("exhausted", 0) / len(rows), 3),
        "repeat_attempts": repeat_attempts,
        "repeat_attempt_rate": round(repeat_attempts / attempts, 3) if attempts else 0.0,
        "first_created_at": first_created_at or None,
        "first_supported_at": first_supported_at or None,
        "time_to_first_supported_seconds": time_to_first_supported,
        "fastest_support_seconds": min(first_supported_seconds) if first_supported_seconds else None,
        "note": "supported 表示最小实验获得支持信号，仍需漏洞候选与独立复核完成终审",
    }
