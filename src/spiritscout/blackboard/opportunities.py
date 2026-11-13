"""统一测试机会投影与行动收益指标。

机会分数只表达当前证据下的执行就绪度、信息增益和重复成本，不代表漏洞
成立，也不替主 Agent 选择固定渗透流程。机会本身由已有黑板事实实时投影，
不另建一套会漂移的持久化结论。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any


def _clamp(value: int) -> int:
    return max(0, min(100, value))


def _identity(ref: Any) -> str:
    if isinstance(ref, dict):
        layer = str(ref.get("layer") or "")
        record_id = str(ref.get("record_id") or "")
        return f"{layer}:{record_id}" if layer and record_id else ""
    text = str(ref or "").strip()
    name, separator, version = text.rpartition("@")
    return name if separator and version.isdigit() else text


def opportunity_ref(kind: str, source_ref: Any) -> str:
    identity = _identity(source_ref)
    digest = hashlib.sha256(f"{kind}|{identity}".encode()).hexdigest()[:24]
    return f"opportunity:{digest}"


def _revision(kind: str, source_ref: Any, *signals: Any) -> str:
    material = json.dumps([kind, source_ref, *signals], ensure_ascii=False,
                          sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _component(components: list[dict[str, Any]], name: str, points: int, basis: str) -> int:
    if points:
        components.append({"name": name, "points": points, "basis": basis[:240]})
    return points


def _yield_bucket() -> dict[str, Any]:
    return {
        "attempts": 0, "productive": 0, "failed": 0, "empty_completed": 0,
        "consecutive_empty": 0, "blockers": [], "active_blockers": [], "outcomes": [],
        "tool_calls": 0, "duration_ms": 0.0, "detection_signals": 0,
    }


def _selected_capability(task: dict[str, Any], contract: dict[str, Any]) -> tuple[str, str]:
    selected = contract.get("selected_capability")
    selected = selected if isinstance(selected, dict) else {}
    tactic = str(selected.get("tactic") or task.get("payload", {}).get("tactic") or "").split("@", 1)[0]
    capability = str(selected.get("capability") or tactic or task.get("agent_type")
                     or task.get("task_type") or "unknown")
    return capability[:120], tactic[:80]


def _candidate_capability(contract: dict[str, Any], tactic: str) -> str:
    candidates = contract.get("capability_candidates")
    candidates = candidates if isinstance(candidates, list) else []
    match = next((item for item in candidates
                  if isinstance(item, dict)
                  and str(item.get("tactic") or "").split("@", 1)[0] == tactic), None)
    return str((match or {}).get("capability") or "")[:120]


def _task_target(task: dict[str, Any]) -> str:
    return " ".join(str(task.get("target") or "session-target").split()).strip()[:300]


def _record_yield(bucket: dict[str, Any], *, useful: bool, failed: bool,
                  status: str, blockers: list[dict[str, Any]],
                  execution: dict[str, Any] | None = None) -> None:
    bucket["attempts"] += 1
    bucket["productive"] += int(useful)
    bucket["failed"] += int(failed)
    bucket["empty_completed"] += int(status == "done" and not useful)
    bucket["outcomes"].append(bool(useful))
    execution = execution or {}
    bucket["tool_calls"] += int(execution.get("tool_calls") or 0)
    bucket["duration_ms"] += float(execution.get("duration_ms") or 0)
    bucket["detection_signals"] += int(execution.get("detection_signals") or 0)
    bucket["active_blockers"] = list(blockers)
    for blocker in blockers:
        if blocker not in bucket["blockers"]:
            bucket["blockers"].append(blocker)


def _finalize_yield(buckets: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    for key, value in buckets.items():
        outcomes = list(value.pop("outcomes", []))
        consecutive = 0
        for useful in reversed(outcomes):
            if useful:
                break
            consecutive += 1
        value["consecutive_empty"] = consecutive
        value["effective_rate"] = round(value["productive"] / value["attempts"], 3) \
            if value["attempts"] else None
        value["duration_ms"] = round(value["duration_ms"], 3)
        value["productive_per_100_tool_calls"] = round(
            value["productive"] * 100 / value["tool_calls"], 3
        ) if value["tool_calls"] else None
        value["blockers"] = value["blockers"][:12]
        value["active_blockers"] = value["active_blockers"][:12]
        finalized[key] = dict(value)
    return finalized


def _task_yield(task_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    completed = productive = empty_completed = failed = 0
    repeated_unknowns: Counter[str] = Counter()
    by_opportunity: dict[str, dict[str, Any]] = defaultdict(_yield_bucket)
    by_opportunity_capability: dict[str, dict[str, Any]] = defaultdict(_yield_bucket)
    by_target: dict[str, dict[str, Any]] = defaultdict(_yield_bucket)
    by_capability: dict[str, dict[str, Any]] = defaultdict(_yield_bucket)
    ordered_rows = sorted(enumerate(task_rows), key=lambda item: (
        str(item[1].get("updated_at") or ""), -item[0]))
    for _, row in ordered_rows:
        if row.get("record_id") == "dag-snapshot":
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        task = data.get("task") if isinstance(data.get("task"), dict) else {}
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        if summary.get("live") is True:
            continue
        status = str(task.get("status") or summary.get("status") or "")
        if status not in {"done", "failed", "cancelled", "pruned"}:
            continue
        completed += 1
        if status in {"failed", "cancelled", "pruned"}:
            failed += 1
        handoff = summary.get("handoff") if isinstance(summary.get("handoff"), dict) else {}
        resolved = [value for value in handoff.get("resolved_unknowns", []) if str(value).strip()]
        findings = [value for value in summary.get("findings", []) if isinstance(value, dict)]
        # 路线结论文本可能只是“已完成”一类自述；收益只认结构化发现或明确消除未知项。
        useful = bool(resolved or findings)
        productive += int(useful)
        empty_completed += int(status == "done" and not useful)
        for unknown in handoff.get("remaining_unknowns", []):
            text = " ".join(str(unknown).split()).strip().lower()
            if text:
                repeated_unknowns[text[:300]] += 1
        contract = task.get("payload", {}).get("investigation_contract", {}) \
            if isinstance(task.get("payload"), dict) else {}
        blockers = [blocker for blocker in handoff.get("blockers", [])
                    if isinstance(blocker, dict)]
        execution = summary.get("execution_feedback") \
            if isinstance(summary.get("execution_feedback"), dict) else {}
        capability, tactic = _selected_capability(task, contract)
        if tactic and capability == tactic:
            capability = _candidate_capability(contract, tactic) or capability
        target = _task_target(task)
        failed_action = status in {"failed", "cancelled", "pruned"}
        _record_yield(by_target[target], useful=useful, failed=failed_action,
                      status=status, blockers=blockers, execution=execution)
        _record_yield(by_capability[capability], useful=useful, failed=failed_action,
                      status=status, blockers=blockers, execution=execution)
        by_capability[capability]["tactic"] = tactic
        ref = str(contract.get("opportunity_ref") or "").strip() \
            if isinstance(contract, dict) else ""
        if ref:
            revision = str(contract.get("opportunity_revision") or "legacy")[:40]
            key = f"{ref}@{revision}"
            _record_yield(by_opportunity[key], useful=useful, failed=failed_action,
                          status=status, blockers=blockers, execution=execution)
            _record_yield(by_opportunity_capability[f"{key}|{capability}"],
                          useful=useful, failed=failed_action,
                          status=status, blockers=blockers, execution=execution)
    repeated = [{"unknown": unknown, "count": count}
                for unknown, count in repeated_unknowns.most_common(12) if count > 1]
    metrics = {
        "completed_tasks": completed,
        "productive_tasks": productive,
        "effective_action_rate": round(productive / completed, 3) if completed else None,
        "empty_completion_count": empty_completed,
        "failed_or_interrupted_count": failed,
        "repeated_unknowns": repeated,
        "tool_calls": sum(int((row.get("data", {}).get("summary", {}).get(
            "execution_feedback", {}) or {}).get("tool_calls") or 0)
                          for row in task_rows),
        "duration_ms": round(sum(float((row.get("data", {}).get("summary", {}).get(
            "execution_feedback", {}) or {}).get("duration_ms") or 0)
                                 for row in task_rows), 3),
        "detection_signals": sum(int((row.get("data", {}).get("summary", {}).get(
            "execution_feedback", {}) or {}).get("detection_signals") or 0)
                                 for row in task_rows),
        "note": "有效动作指任务形成结构化发现、既定事实或明确消除未知项；不等于确认漏洞",
    }
    return metrics, {
        "by_opportunity": _finalize_yield(by_opportunity),
        "by_opportunity_capability": _finalize_yield(by_opportunity_capability),
        "by_target": _finalize_yield(by_target),
        "by_capability": _finalize_yield(by_capability),
    }


def _stats(attempt_map: dict[str, Any], ref: str,
           revision: str) -> dict[str, Any]:
    return attempt_map.get("by_opportunity", {}).get(f"{ref}@{revision}", {})


def _capability(name: str, tactic: str, basis: str, *, preferred: bool = False,
                execution: str = "dag") -> dict[str, Any]:
    return {
        "capability": name,
        "tactic": tactic,
        "basis": basis[:240],
        "preferred": preferred,
        "execution": execution,
    }


def _opportunity_strategy(item: dict[str, Any], attempt_map: dict[str, Any]) -> None:
    """按机会×能力的实际收益给出换路信号，不把单一角色空转等同于机会穷尽。"""
    ref = str(item.get("opportunity_ref") or "")
    revision = str(item.get("opportunity_revision") or "")
    key = f"{ref}@{revision}"
    attempts = item.get("attempts") if isinstance(item.get("attempts"), dict) else {}
    candidates = [candidate for candidate in item.get("capability_candidates", [])
                  if isinstance(candidate, dict)]
    per_capability = attempt_map.get("by_opportunity_capability", {})
    candidate_yields: list[dict[str, Any]] = []
    for candidate in candidates:
        capability = str(candidate.get("capability") or candidate.get("tactic") or "unknown")
        stats = per_capability.get(f"{key}|{capability}", {})
        count = int(stats.get("attempts") or 0)
        productive = int(stats.get("productive") or 0)
        consecutive = int(stats.get("consecutive_empty") or 0)
        marginal = (1.0 if not count else
                    max(0.0, (productive / count) - min(0.75, consecutive * 0.25)))
        candidate["yield"] = {
            "attempts": count, "productive": productive,
            "consecutive_empty": consecutive,
            "marginal_yield": round(marginal, 3),
        }
        candidate_yields.append({
            "capability": capability, "tactic": str(candidate.get("tactic") or ""),
            "active_blockers": stats.get("active_blockers", []),
            **candidate["yield"],
        })

    blockers: list[dict[str, Any]] = []
    for row in candidate_yields:
        for blocker in row.get("active_blockers", []):
            if isinstance(blocker, dict) and blocker not in blockers:
                blockers.append(blocker)
    if not candidate_yields:
        blockers = [blocker for blocker in attempts.get("active_blockers", [])
                    if isinstance(blocker, dict)]
    attempted = [row for row in candidate_yields if row["attempts"]]
    untried = [row for row in candidate_yields if not row["attempts"]]
    productive = any(row["productive"] for row in candidate_yields)
    diminishing = int(attempts.get("consecutive_empty") or 0) >= 2
    user_resolvable = any(blocker.get("user_resolvable") is True for blocker in blockers)
    exhausted = bool(candidate_yields and not untried and not productive
                     and all(row["consecutive_empty"] for row in candidate_yields)
                     and not user_resolvable)
    if blockers and not productive:
        direction_state = "blocked"
    elif exhausted:
        direction_state = "exhausted"
    elif diminishing:
        direction_state = "diminishing"
    else:
        direction_state = "productive"

    pivot_parts: list[str] = []
    alternatives = list(dict.fromkeys(
        str(capability) for blocker in blockers
        for capability in blocker.get("alternative_capabilities", [])
        if isinstance(capability, str) and capability
    ))
    if user_resolvable:
        pivot_parts.append("向用户索取可立即解除阻塞的必要输入")
    if alternatives:
        untried.sort(key=lambda row: (row["capability"] not in alternatives,
                                      alternatives.index(row["capability"])
                                      if row["capability"] in alternatives else len(alternatives)))
    if untried:
        best = untried[0]
        pivot_parts.append(f"同时改用未尝试能力 {best['capability']}（{best['tactic']}）")
    elif diminishing and not exhausted:
        pivot_parts.append("换到其他资产或独立机会，避免继续投入同质实验")
    elif exhausted:
        pivot_parts = ["关闭当前方向；仅在新证据或阻塞条件变化后重新进入"]

    aggregate_attempts = int(attempts.get("attempts") or 0)
    aggregate_productive = int(attempts.get("productive") or 0)
    aggregate_margin = (1.0 if not aggregate_attempts else max(
        0.0, aggregate_productive / aggregate_attempts
        - min(0.75, int(attempts.get("consecutive_empty") or 0) * 0.25)))
    subject = " ".join(str(item.get("subject") or "").split()).strip()[:300]
    target_yield = attempt_map.get("by_target", {}).get(subject, {}) if subject else {}
    item.update({
        "blockers": blockers[:12],
        "capability_yield": candidate_yields,
        "marginal_yield": round(aggregate_margin, 3),
        "direction_state": direction_state,
        "pivot_recommendation": "；".join(pivot_parts),
        "target_yield": target_yield,
        "stalled": diminishing,
        "exhausted": exhausted,
        "untried_capabilities": [row["capability"] for row in untried],
        "attempted_capabilities": [row["capability"] for row in attempted],
        "recommended_capability": untried[0]["capability"] if untried else "",
    })


def _portfolio_review(opportunities: list[dict[str, Any]], attempt_map: dict[str, Any]) -> dict[str, Any]:
    targets = attempt_map.get("by_target", {})
    capabilities = attempt_map.get("by_capability", {})
    total_attempts = sum(int(stats.get("attempts") or 0) for stats in targets.values())
    concentrated = max(targets.items(), key=lambda item: int(item[1].get("attempts") or 0),
                       default=("", {}))
    concentration = (int(concentrated[1].get("attempts") or 0) / total_attempts
                     if total_attempts else 0.0)
    low_yield = [
        {"capability": capability, **{key: stats.get(key) for key in (
            "attempts", "productive", "consecutive_empty", "effective_rate")}}
        for capability, stats in capabilities.items()
        if int(stats.get("consecutive_empty") or 0) >= 2
    ]
    blockers = [
        {"opportunity_ref": item.get("opportunity_ref"), "subject": item.get("subject"),
         **blocker}
        for item in opportunities for blocker in item.get("blockers", [])
        if blocker.get("user_resolvable") is True
    ]
    pivots = [
        {"opportunity_ref": item.get("opportunity_ref"), "subject": item.get("subject"),
         "state": item.get("direction_state"),
         "recommendation": item.get("pivot_recommendation")}
        for item in opportunities
        if item.get("pivot_recommendation")
    ]
    return {
        "active_opportunities": len(opportunities),
        "active_targets": len(targets),
        "target_concentration": {
            "target": concentrated[0], "share": round(concentration, 3),
            "over_concentrated": bool(total_attempts >= 3 and concentration >= 0.67),
        },
        "low_yield_capabilities": low_yield[:12],
        "user_resolvable_blockers": blockers[:12],
        "pivot_candidates": pivots[:12],
        "contract": (
            "复盘用于比较继续、补条件、换能力、换资产或关闭方向；耗时本身不是停止依据，"
            "新证据与阻塞条件变化会重置当前机会修订的衰减。"),
    }


def _hypothesis_capabilities(sample: dict[str, Any], *, review: bool,
                             action_signals: dict[str, Any]) -> list[dict[str, Any]]:
    if review:
        return [_capability(
            "independent_verification", "VERIFICATION-QUEUE",
            "已有支持信号，必须由独立复核队列确认实际危害或排除误报",
            preferred=True, execution="verification_queue",
        )]
    dispatch = action_signals.get("dispatch_evidence") \
        if isinstance(action_signals, dict) else {}
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    basis = sample.get("technical_basis") \
        if isinstance(sample.get("technical_basis"), dict) else {}
    capabilities: list[dict[str, Any]] = []
    if dispatch.get("web_vuln_supported"):
        capabilities.append(_capability(
            "web_hypothesis_test", "TA0001",
            "已有可测试接口、输入或权限边界证据，适合做单变量最小实验",
            preferred=True,
        ))
    if dispatch.get("port_scan_supported"):
        capabilities.append(_capability(
            "service_hypothesis_test", "HOST-VULN",
            "已有服务证据，适合核验暴露面、弱配置或服务级假设",
            preferred=not capabilities,
        ))
    if dispatch.get("js_reverse_supported"):
        capabilities.append(_capability(
            "script_evidence_enrichment", "JS-REVERSE",
            "公开脚本或提取路由可补齐接口来源、认证和参数依据",
            preferred=not capabilities,
        ))
    if any(basis.get(key) for key in ("cve_ids", "template_ids", "tags", "versions")):
        capabilities.append(_capability(
            "targeted_template_validation", "SCANNER-CONTROL",
            "存在具体技术栈、版本、CVE 或模板依据；仅可定向验证当前假设",
            execution="scanner_control",
        ))
    if not capabilities:
        capabilities.append(_capability(
            "evidence_enrichment", "TA0043",
            "当前假设尚缺少足以派专项测试的现场证据，先补齐可核对事实",
            preferred=True,
        ))
    return capabilities


def _lead_capabilities(card: dict[str, Any], state: str,
                       action_signals: dict[str, Any]) -> list[dict[str, Any]]:
    dispatch = action_signals.get("dispatch_evidence") \
        if isinstance(action_signals, dict) else {}
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    source_kind = str(card.get("source_kind") or "")
    capabilities: list[dict[str, Any]] = []
    if state in {"needs_disambiguation", "blocked"}:
        capabilities.append(_capability(
            "authenticity_or_blocker_resolution", "TA0043",
            "线索仍需真实性消歧或解除前置阻塞，侦察角色负责补最小事实",
            preferred=True,
        ))
    if source_kind in {"endpoint", "vuln"} and dispatch.get("web_vuln_supported"):
        capabilities.append(_capability(
            "web_boundary_test", "TA0001",
            "线索对应已观测接口或漏洞候选，可构造最小输入/权限对照实验",
            preferred=state == "actionable" and not capabilities,
        ))
    if source_kind == "service" or dispatch.get("port_scan_supported"):
        capabilities.extend([
            _capability(
                "service_mapping", "TA0007",
                "已有服务事实，可围绕现场证据补充协议与端口假设",
                preferred=state == "actionable" and not capabilities,
            ),
            _capability(
                "service_exposure_review", "HOST-VULN",
                "已观测服务可进一步核验管理面、调试面和弱配置",
            ),
        ])
    if source_kind == "script" or dispatch.get("js_reverse_supported"):
        capabilities.append(_capability(
            "script_and_api_extraction", "JS-REVERSE",
            "已有脚本或公开 Web 表面，可从真实代码补充接口和认证线索",
            preferred=state == "actionable" and not capabilities,
        ))
    if dispatch.get("business_context_supported"):
        capabilities.append(_capability(
            "business_boundary_modeling", "BUSINESS-CONTEXT",
            "已有交易、角色、对象或状态流证据，业务建模可能改变测试决策",
        ))
    if not capabilities:
        capabilities.append(_capability(
            "evidence_enrichment", "TA0043",
            "当前线索尚无专项角色所需的成熟证据，先补齐可验证入口事实",
            preferred=True,
        ))
    return capabilities


def _hypothesis_opportunity(sample: dict[str, Any], *, review: bool,
                            attempt_map: dict[str, dict[str, int]],
                            action_signals: dict[str, Any]) -> dict[str, Any]:
    source_ref = str(sample.get("hypothesis_ref") or "")
    state = str(sample.get("state") or "open")
    revision = _revision("hypothesis", source_ref, state, sample.get("evidence_refs"),
                         sample.get("technical_basis"), review)
    ref = opportunity_ref("hypothesis", source_ref)
    attempt_stats = _stats(attempt_map, ref, revision)
    components: list[dict[str, Any]] = []
    score = 0
    base = 86 if review else {"investigating": 68, "open": 58, "blocked": 28}.get(state, 45)
    score += _component(components, "lifecycle_readiness", base,
                        "已有支持信号待独立复核" if review else f"假设状态为 {state}")
    basis = sample.get("technical_basis") if isinstance(sample.get("technical_basis"), dict) else {}
    if basis:
        score += _component(components, "technical_basis", 7, "已有技术栈、CVE、模板或标签依据")
    if sample.get("expected_signal") or sample.get("counter_signal"):
        score += _component(components, "falsifiability", 6, "已声明支持信号或反证信号")
    if sample.get("next_minimal_experiment"):
        score += _component(components, "minimal_experiment", 7, "已有下一步最小实验")
    if sample.get("stop_condition"):
        score += _component(components, "stop_condition", 5, "已有停止条件，便于及时收口")
    if sample.get("evidence_refs"):
        score += _component(components, "evidence", 5, "已有可追溯证据引用")
    attempts = int(attempt_stats.get("attempts") or 0)
    productive = int(attempt_stats.get("productive") or 0)
    if attempts > 1 and productive == 0:
        score += _component(components, "unproductive_repetition", -min(24, attempts * 6),
                            f"已有 {attempts} 次任务未形成新事实或消除未知项")
    recommended = ("independent_verification" if review else
                   "resolve_blocker" if state == "blocked" else "minimal_hypothesis_test")
    capabilities = _hypothesis_capabilities(
        sample, review=review, action_signals=action_signals)
    return {
        "opportunity_ref": ref, "opportunity_revision": revision,
        "kind": "vulnerability_hypothesis", "subject": str(sample.get("subject") or "")[:300],
        "state": "review_pending" if review else state,
        "priority_score": _clamp(score), "priority_components": components,
        "hypothesis_ref": source_ref, "source_refs": [source_ref] if source_ref else [],
        "claim": str(sample.get("claim") or "")[:300],
        "expected_information": str(sample.get("expected_signal") or sample.get("claim") or "")[:500],
        "decision_impact": ("决定该候选能否进入确认漏洞或误报结论" if review
                            else "决定该假设是否值得继续、证伪或关闭"),
        "next_minimal_experiment": str(sample.get("next_minimal_experiment") or "")[:500],
        "stop_condition": str(sample.get("stop_condition") or "")[:500],
        "unknowns": ([str(sample.get("reason"))[:300]] if state == "blocked"
                     and sample.get("reason") else []),
        "recommended_work": recommended,
        "capability_candidates": capabilities,
        "attempts": attempt_stats,
        "stalled": attempts > 1 and productive == 0,
    }


def _lead_opportunity(card: dict[str, Any], state: str,
                      attempt_map: dict[str, dict[str, int]],
                      action_signals: dict[str, Any]) -> dict[str, Any]:
    source_ref = card.get("ref") or {}
    revision = _revision("lead", source_ref, state, card.get("evidence_state"),
                         card.get("authenticity"), card.get("unknowns"))
    ref = opportunity_ref("lead", source_ref)
    attempt_stats = _stats(attempt_map, ref, revision)
    components: list[dict[str, Any]] = []
    score = 0
    base = {"actionable": 50, "needs_disambiguation": 42, "blocked": 20}.get(state, 30)
    score += _component(components, "frontier_state", base, f"调查前沿状态为 {state}")
    origin = str(card.get("origin_class") or "unspecified")
    origin_points = {"target_observation": 18, "derived_observation": 10,
                     "derived_candidate": 10, "generated_test": -12,
                     "generated_candidate": -12}.get(origin, 0)
    score += _component(components, "origin_quality", origin_points,
                        f"来源类别为 {origin}")
    evidence = str(card.get("evidence_state") or "unverified")
    evidence_points = {"independently_verified": 16, "verified": 13,
                       "supported": 10, "conflicting": -8, "expired": -10}.get(evidence, 0)
    score += _component(components, "evidence_maturity", evidence_points,
                        f"证据状态为 {evidence}")
    if card.get("next_minimal_experiment"):
        score += _component(components, "minimal_experiment", 8, "已有最小真实性或差异性实验")
    unknowns = [str(value)[:300] for value in card.get("unknowns", []) if str(value).strip()][:8]
    if unknowns:
        score += _component(components, "decision_unknown", 5, "存在可由最小实验消除的明确未知项")
    repetition = card.get("repetition") if isinstance(card.get("repetition"), dict) else {}
    versions = max(int(repetition.get("record_versions") or 1),
                   int(repetition.get("observations") or 1))
    if versions > 1:
        score += _component(components, "reobservation", -min(18, (versions - 1) * 4),
                            f"相同对象已有 {versions} 个版本或观察")
    attempts = int(attempt_stats.get("attempts") or 0)
    productive = int(attempt_stats.get("productive") or 0)
    if attempts > 1 and productive == 0:
        score += _component(components, "unproductive_repetition", -min(24, attempts * 6),
                            f"已有 {attempts} 次任务未形成新事实或消除未知项")
    recommended = ("resolve_blocker" if state == "blocked" else
                   "authenticity_disambiguation" if state == "needs_disambiguation"
                   else "evidence_followup")
    capabilities = _lead_capabilities(card, state, action_signals)
    return {
        "opportunity_ref": ref, "opportunity_revision": revision,
        "kind": "investigation_lead",
        "subject": str(card.get("subject") or "")[:300], "state": state,
        "priority_score": _clamp(score), "priority_components": components,
        "source_refs": [source_ref] if source_ref else [],
        "origin_class": origin, "evidence_state": evidence,
        "expected_information": ("消除：" + "；".join(unknowns))[:500] if unknowns
                                else "确认该对象是否能形成新的可测试攻击面",
        "decision_impact": "决定该线索应晋级为正式测试入口、继续补证或关闭",
        "next_minimal_experiment": str(card.get("next_minimal_experiment") or "")[:500],
        "stop_condition": ("真实性或差异性实验没有形成独立入口证据时关闭"
                           if state == "needs_disambiguation" else "达到交接目标或证据不再变化时收口"),
        "unknowns": unknowns, "recommended_work": recommended,
        "capability_candidates": capabilities,
        "attempts": attempt_stats, "stalled": attempts > 1 and productive == 0,
    }


def build_opportunity_pool(frontier: dict[str, Any], hypotheses: dict[str, Any],
                           task_rows: list[dict[str, Any]], *,
                           action_signals: dict[str, Any] | None = None) -> dict[str, Any]:
    """将线索、假设和历史行动收益统一投影成可解释的动态机会池。"""
    signals = action_signals if isinstance(action_signals, dict) else {}
    metrics, attempt_map = _task_yield(task_rows)
    opportunities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sample in hypotheses.get("review_pending", []) if isinstance(hypotheses, dict) else []:
        ref = opportunity_ref("hypothesis", sample.get("hypothesis_ref"))
        if ref not in seen:
            seen.add(ref)
            opportunities.append(_hypothesis_opportunity(
                sample, review=True, attempt_map=attempt_map,
                action_signals=signals))
    for sample in hypotheses.get("active", []) if isinstance(hypotheses, dict) else []:
        ref = opportunity_ref("hypothesis", sample.get("hypothesis_ref"))
        if ref not in seen:
            seen.add(ref)
            opportunities.append(_hypothesis_opportunity(
                sample, review=False, attempt_map=attempt_map,
                action_signals=signals))
    for group, state in (("actionable_leads", "actionable"),
                         ("needs_disambiguation", "needs_disambiguation"),
                         ("blocked_leads", "blocked")):
        for card in frontier.get(group, []) if isinstance(frontier, dict) else []:
            ref = opportunity_ref("lead", card.get("ref"))
            if ref in seen:
                continue
            seen.add(ref)
            opportunities.append(_lead_opportunity(
                card, state, attempt_map=attempt_map,
                action_signals=signals))
    for opportunity in opportunities:
        _opportunity_strategy(opportunity, attempt_map)
    opportunities.sort(key=lambda item: (-item["priority_score"], item["opportunity_ref"]))
    by_kind = Counter(item["kind"] for item in opportunities)
    by_state = Counter(item["state"] for item in opportunities)
    stalled = [item for item in opportunities if item["stalled"]]
    return {
        "total": len(opportunities), "prioritized": opportunities[:24],
        "by_kind": dict(sorted(by_kind.items())), "by_state": dict(sorted(by_state.items())),
        "stalled": stalled[:12], "action_yield": metrics,
        "portfolio_review": _portfolio_review(opportunities, attempt_map),
        "ranking_contract": (
            "priority_score 只表示当前证据下的信息收益、执行就绪度和重复成本；"
            "主 Agent 仍需结合用户目标判断，分数不是漏洞结论，也不授予测试范围。"),
    }


def find_opportunity(pool: dict[str, Any], ref: str) -> dict[str, Any] | None:
    return next((item for item in pool.get("prioritized", [])
                 if item.get("opportunity_ref") == ref), None)
