"""运行时状态的增量消息。

系统提示保持稳定；授权与环境放入 world_state，模型侧规则仅携带黑板引用。
授权与规则变化按顶层字段整体发送；保留原始片段的 CTF 会话可增量同步近期事实目录。"""

from __future__ import annotations

import json
import platform as _platform
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from spiritscout.core.exceptions import VersionConflict
from spiritscout.core.models import BlackboardLayer

_ASSET_SAMPLE_LIMIT = 1000
_CLUE_SAMPLE_LIMIT = 500
_VULN_SAMPLE_LIMIT = 500
_ENTRYPOINT_TYPES = {"endpoint", "url", "service", "script", "app"}
_NEGATIVE_STATES = {
    "blocked", "cancelled", "failed", "false_positive", "inconclusive", "interrupted",
    "out_of_rules", "refuted", "rejected", "stopped", "timeout",
}
_FRONTIER_LIMIT = 8
_TARGET_ORIGIN_MARKERS = {
    "browser", "har", "page_network", "target_response", "traffic_capture",
}
_DERIVED_ORIGIN_MARKERS = {"js_static", "source_map", "model_observed"}
_GENERATED_ORIGIN_MARKERS = {
    "bruteforce", "dictionary", "fuzz", "guessed", "model_generated",
    "model_generated_test", "scanner_generated", "wordlist",
}
_OBSERVED_API_SOURCES = {
    "business_observation", "har", "page_network", "target_response", "traffic_capture",
}
_ASSET_STAGES = ("external_candidate", "in_scope", "dns_verified", "live_verified",
                 "fingerprinted")


def _ref(layer: BlackboardLayer, row: dict[str, Any]) -> dict[str, Any]:
    return {"layer": layer.value, "record_id": row["record_id"], "version": row["version"]}


def _safe_location(value: Any, *, limit: int = 300, allow_plain: bool = True) -> str:
    """上下文只保留定位所需的 URL 形态；凭据、片段与查询值不进入摘要。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text[:limit] if allow_plain else ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return text[:limit] if allow_plain else ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    if parts.port:
        host += f":{parts.port}"
    query = urlencode([(name[:80], "…") for name, _ in parse_qsl(
        parts.query, keep_blank_values=True)[:30]])
    return urlunsplit((parts.scheme, host, parts.path, query, ""))[:limit]


def _technology_names(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    names = []
    for key in ("server", "x_powered_by"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            names.append(value.strip()[:160])
    rows = data.get("technologies")
    if isinstance(rows, list):
        for row in rows:
            value = row.get("name") if isinstance(row, dict) else row
            if isinstance(value, str) and value.strip():
                names.append(value.strip()[:160])
    if isinstance(data.get("fingerprint"), dict):
        names.extend(_technology_names(data["fingerprint"]))
    return list(dict.fromkeys(names))


def _failure_mode(data: dict[str, Any]) -> str:
    """负证据只投影固定分类，不把可能含凭据的错误原文灌入模型上下文。"""
    detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
    status = str(data.get("state") or data.get("status") or data.get("execution_status")
                 or detail.get("state") or detail.get("status") or "").lower()
    observation = detail if "ok" in detail or "error" in detail else (
        detail.get("observation") if isinstance(detail.get("observation"), dict) else {})
    text = " ".join(str(value) for value in (
        data.get("reason"), data.get("error"), data.get("value"), detail.get("reason"),
        detail.get("error"), observation.get("error"), observation.get("summary")) if value).lower()
    if "waf" in text or "firewall" in text or "防火墙" in text:
        return "waf_or_filter"
    if any(marker in text for marker in ("403", "forbidden", "unauthor", "denied", "拒绝", "鉴权")):
        return "access_denied"
    if "timeout" in text or "timed out" in text or "超时" in text:
        return "timeout"
    if "policy" in text or "scope" in text or "授权" in text or "策略" in text:
        return "policy_or_scope"
    if status in _NEGATIVE_STATES:
        return status
    if observation and (observation.get("ok") is False or observation.get("error")
                        or observation.get("returncode") not in (None, 0)):
        return "tool_error"
    if data.get("clue_type") == "scanner_finding" and data.get("verification_status") == "unverified":
        return "unverified_scanner_hit"
    return ""


def _expert_action_signals(blackboard: Any, asset_rows: list[dict[str, Any]],
                           clue_rows: list[dict[str, Any]],
                           vuln_rows: list[dict[str, Any]],
                           task_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """把已记录事实压成规划信号；只描述证据成熟度，不替模型选择固定流程。"""
    candidate_stages = Counter(
        str(row["data"].get("stage") or "external_candidate")
        for row in clue_rows if row["data"].get("clue_type") == "asset_candidate")
    scripts = [row["data"] for row in asset_rows
               if row["data"].get("asset_type") == "script"]
    endpoints = [row["data"] for row in asset_rows
                 if row["data"].get("asset_type") == "endpoint"]
    services = [row["data"] for row in asset_rows
                if row["data"].get("asset_type") == "service"]

    observed_endpoints = 0
    parameterized_endpoints = 0
    authenticated_endpoints = 0
    for endpoint in endpoints:
        attrs = endpoint.get("attrs") if isinstance(endpoint.get("attrs"), dict) else {}
        sources = {str(value).lower() for value in attrs.get("sources", [])
                   if isinstance(value, str)}
        source = str(attrs.get("source") or "").lower()
        if sources & _OBSERVED_API_SOURCES or source == "model_observed":
            observed_endpoints += 1
        if any(isinstance(value, str) and value.strip() for value in attrs.get("params", [])):
            parameterized_endpoints += 1
        if attrs.get("auth_required") is True or attrs.get("authenticated") is True:
            authenticated_endpoints += 1

    route_count = sum(
        max(0, int((script.get("attrs") or {}).get("route_count") or 0))
        for script in scripts if isinstance(script.get("attrs"), dict))
    source_map_count = sum(
        len((script.get("attrs") or {}).get("source_maps", []))
        for script in scripts
        if isinstance(script.get("attrs"), dict)
        and isinstance((script.get("attrs") or {}).get("source_maps"), list))

    read = getattr(blackboard, "read", None)
    business = (read(BlackboardLayer.TOPOLOGY, "business-context")
                if callable(read) else None) or {}
    roles = len(business.get("roles") or [])
    objects = len(business.get("objects") or [])
    workflows = len(business.get("workflows") or [])
    boundaries = len(business.get("trust_boundaries") or [])
    business_evidence = roles + objects + workflows + boundaries
    surface = business.get("surface_assessment")
    surface = surface if isinstance(surface, dict) else {}
    surface_type = str(surface.get("type") or "unknown")
    complexity = str(surface.get("business_complexity") or "unknown")
    if complexity not in {"low", "medium", "high", "unknown"}:
        complexity = "unknown"
    if complexity == "unknown":
        complexity = ("high" if workflows >= 2 and roles >= 2 and objects >= 2
                      else "medium" if business_evidence >= 3 and workflows
                      else "low" if business and business_evidence == 0 else "unknown")

    verified_web_assets = 0
    fingerprinted_assets = 0
    for row in asset_rows:
        data = row["data"]
        attrs = data.get("attrs") if isinstance(data.get("attrs"), dict) else {}
        sources = {str(value).lower() for value in attrs.get("sources", [])
                   if isinstance(value, str)}
        if (data.get("asset_type") in {"url", "app", "endpoint", "script"}
                and (sources & _OBSERVED_API_SOURCES or attrs.get("status_code") is not None)):
            verified_web_assets += 1
        if _technology_names(data.get("fingerprint")):
            fingerprinted_assets += 1
    if fingerprinted_assets:
        maturity = "fingerprinted"
    elif verified_web_assets or services:
        maturity = "live_verified"
    elif asset_rows:
        maturity = "formal_asset"
    elif candidate_stages:
        maturity = "candidate_only"
    else:
        maturity = "unknown"

    business_tasks = []
    repeated_unknowns: Counter[str] = Counter()
    structured_blockers: list[dict[str, Any]] = []
    for row in (task_rows if task_rows is not None else
                blackboard.query(BlackboardLayer.TASK_STATE, limit=80)):
        data = row["data"]
        task = data.get("task") if isinstance(data.get("task"), dict) else {}
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        handoff = summary.get("handoff") if isinstance(summary.get("handoff"), dict) else {}
        for blocker in handoff.get("blockers", []):
            if isinstance(blocker, dict) and blocker not in structured_blockers:
                structured_blockers.append(blocker)
        if task.get("agent_type") != "business_context":
            continue
        business_tasks.append({"status": str(task.get("status") or summary.get("status") or ""),
                               "task_ref": _ref(BlackboardLayer.TASK_STATE, row)})
        repeated_unknowns.update(str(value)[:300] for value in
                                 handoff.get("remaining_unknowns", []) if isinstance(value, str))
    repeated = [value for value, count in repeated_unknowns.items() if count > 1][:6]
    low_value_business = bool(business_tasks and business and observed_endpoints == 0
                              and not business.get("recorded_transactions")
                              and not business.get("har_transactions")
                              and complexity == "low")
    login_page_stall = bool(
        (low_value_business and surface_type == "login_wall")
        or any(blocker.get("type") in {"authentication_required", "sso_required",
                                       "captcha_required", "credentials_required"}
               for blocker in structured_blockers))
    stall_reasons = []
    if login_page_stall:
        stall_reasons.append("登录墙走访已完成但没有新增交易、接口或权限边界证据")
    elif low_value_business:
        stall_reasons.append("业务走访已完成但没有形成可测试的流程、接口或边界")
    if repeated:
        stall_reasons.append("多个任务交接重复保留相同信息缺口")

    candidates_only = bool(candidate_stages and not asset_rows)
    web_test_ready = bool((observed_endpoints and (parameterized_endpoints or business_evidence))
                          or vuln_rows)
    return {
        "asset_maturity": {
            "level": maturity,
            "formal_assets": len(asset_rows),
            "verified_web_assets": verified_web_assets,
            "candidate_stages": {stage: candidate_stages.get(stage, 0)
                                 for stage in _ASSET_STAGES if candidate_stages.get(stage)},
            "candidate_only": candidates_only,
        },
        "business_complexity": {
            "level": complexity, "surface_type": surface_type,
            "roles": roles, "objects": objects, "workflows": workflows,
            "trust_boundaries": boundaries,
            "deep_analysis_supported": complexity in {"medium", "high"},
        },
        "js_surface": {
            "scripts": len(scripts), "extracted_routes": route_count,
            "source_maps": source_map_count,
            "density": ("high" if route_count >= 10 or len(scripts) >= 4
                        else "medium" if route_count or scripts else "none"),
        },
        "api_surface": {
            "endpoints": len(endpoints), "observed_endpoints": observed_endpoints,
            "parameterized_endpoints": parameterized_endpoints,
            "web_test_ready": web_test_ready,
        },
        "auth_surface": {
            "login_wall": surface_type == "login_wall",
            "authenticated_endpoints": authenticated_endpoints,
            "role_or_account_evidence": bool(roles or surface_type == "account_workflow"),
            "observed": bool(authenticated_endpoints or roles
                             or surface_type in {"login_wall", "account_workflow"}),
        },
        "service_evidence": {
            "services": len(services),
            "ports": sorted({int(item["port"]) for item in services
                             if isinstance(item.get("port"), int)})[:32],
            "port_scan_supported": bool(services),
        },
        "route_stall": {
            "detected": bool(stall_reasons), "login_page_stall": login_page_stall,
            "reasons": stall_reasons, "repeated_unknowns": repeated,
            "blockers": structured_blockers[:12],
            "user_resolvable": any(blocker.get("user_resolvable") is True
                                   for blocker in structured_blockers),
            "recommended_pivot": ("js_api_or_asset_surface" if login_page_stall
                                  else "alternate_capability_or_asset" if stall_reasons
                                  else ""),
        },
        "dispatch_evidence": {
            "continue_asset_verification": candidates_only,
            "business_surface_observed": bool(verified_web_assets or observed_endpoints),
            "business_context_supported": complexity in {"medium", "high"},
            "js_reverse_supported": bool(scripts or route_count or verified_web_assets),
            "web_vuln_supported": web_test_ready,
            "port_scan_supported": bool(services),
        },
        "contract": (
            "这些是证据摘要而非固定流程：模型结合任务目标决定派工；candidate_only 不足以支持"
            "业务或脚本分析需要已验证 Web 表面，漏洞或端口测试需要更强证据；端口角色需要"
            "已观测服务证据，Web 漏洞角色需要已验证接口及"
            "参数/权限/输入证据或已有漏洞候选。"),
    }


def intelligence_summary(blackboard: Any) -> dict[str, Any]:
    """统一资产、扫描、JS、负证据和漏洞候选的紧凑观察面。"""
    assets = blackboard.query(BlackboardLayer.ASSET, limit=_ASSET_SAMPLE_LIMIT + 1)
    clues = blackboard.query(BlackboardLayer.RECON_CLUE, limit=_CLUE_SAMPLE_LIMIT + 1)
    vulns = blackboard.query(BlackboardLayer.VULN, limit=_VULN_SAMPLE_LIMIT + 1)
    asset_rows, clue_rows, vuln_rows = (
        assets[:_ASSET_SAMPLE_LIMIT], clues[:_CLUE_SAMPLE_LIMIT], vulns[:_VULN_SAMPLE_LIMIT])
    count = getattr(blackboard, "count", None)
    asset_total = count(BlackboardLayer.ASSET) if callable(count) else len(assets)
    clue_total = count(BlackboardLayer.RECON_CLUE) if callable(count) else len(clues)
    vuln_total = count(BlackboardLayer.VULN) if callable(count) else len(vulns)
    read = getattr(blackboard, "read", None)
    business_context = (read(BlackboardLayer.TOPOLOGY, "business-context")
                        if callable(read) else None)
    task_rows = blackboard.query(BlackboardLayer.TASK_STATE, limit=80)
    has_action_handoff = any(
        isinstance((row.get("data") or {}).get("summary"), dict)
        and isinstance(((row.get("data") or {}).get("summary") or {}).get("handoff"), dict)
        and any((((row.get("data") or {}).get("summary") or {}).get("handoff") or {}).get(key)
                for key in ("blockers", "remaining_unknowns", "resolved_unknowns"))
        for row in task_rows
    )
    if not any((asset_total, clue_total, vuln_total, business_context, has_action_handoff)):
        return {}

    assets_by_type = Counter(str(row["data"].get("asset_type") or "unknown")
                             for row in asset_rows)
    entrypoints = []
    for row in asset_rows:
        data = row["data"]
        asset_type = str(data.get("asset_type") or "unknown")
        if asset_type not in _ENTRYPOINT_TYPES or len(entrypoints) >= 36:
            continue
        attrs = data.get("attrs") if isinstance(data.get("attrs"), dict) else {}
        item: dict[str, Any] = {
            "blackboard_ref": _ref(BlackboardLayer.ASSET, row),
            "asset_type": asset_type,
            "value": _safe_location(data.get("value")),
        }
        if asset_type == "endpoint":
            item.update(
                method=str(attrs.get("method") or "UNKNOWN").upper()[:16],
                params=[str(value)[:80] for value in attrs.get("params", [])[:30]
                        if isinstance(value, str)],
                sources=[str(value)[:60] for value in attrs.get("sources", [])[:12]
                         if isinstance(value, str)],
            )
        if asset_type == "script":
            item.update(
                route_count=int(attrs.get("route_count") or 0),
                source_map_count=len(attrs.get("source_maps", []))
                if isinstance(attrs.get("source_maps"), list) else 0,
                secret_reference_count=len(attrs.get("secrets", []))
                if isinstance(attrs.get("secrets"), list) else 0,
            )
        if data.get("port") is not None:
            item["port"] = data["port"]
        if data.get("service"):
            item["service"] = str(data["service"])[:80]
        entrypoints.append(item)

    technology_refs: dict[str, list[dict[str, Any]]] = {}
    for row in asset_rows:
        for name in _technology_names(row["data"].get("fingerprint")):
            refs = technology_refs.setdefault(name, [])
            if len(refs) < 4:
                refs.append(_ref(BlackboardLayer.ASSET, row))
    for row in clue_rows:
        data = row["data"]
        if data.get("clue_type") != "tech_stack":
            continue
        for name in str(data.get("value") or "").split(","):
            name = name.strip()[:160]
            if name:
                refs = technology_refs.setdefault(name, [])
                if len(refs) < 4:
                    refs.append(_ref(BlackboardLayer.RECON_CLUE, row))

    clue_types = Counter(str(row["data"].get("clue_type") or "unknown") for row in clue_rows)
    asset_candidates = []
    for row in clue_rows:
        data = row["data"]
        if data.get("clue_type") != "asset_candidate":
            continue
        asset_candidates.append({
            "blackboard_ref": _ref(BlackboardLayer.RECON_CLUE, row),
            "value": _safe_location(data.get("value")),
            "asset_type": str(data.get("asset_type") or "domain")[:30],
            "stage": str(data.get("stage") or "external_candidate")[:40],
            "priority_score": int(data.get("priority_score") or 0),
            "priority_components": [
                {"name": str(item.get("name") or "")[:80],
                 "points": int(item.get("points") or 0),
                 "basis": str(item.get("basis") or "")[:240]}
                for item in data.get("priority_components", [])[:8]
                if isinstance(item, dict)
            ],
            "providers": [str(value)[:60] for value in data.get("providers", [])[:8]
                          if isinstance(value, str)],
            "source_count": int(data.get("source_count") or 0),
            "unknowns": [str(value)[:240] for value in data.get("unknowns", [])[:6]
                        if isinstance(value, str)],
        })
    asset_candidates.sort(key=lambda item: (-item["priority_score"], item["value"]))
    scanner_samples = []
    negative_samples = []
    negative_total = 0
    for row in clue_rows:
        data = row["data"]
        clue_type = str(data.get("clue_type") or "unknown")
        if clue_type != "user_reply" and len(scanner_samples) < 20:
            detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
            observation = detail.get("observation") if isinstance(detail.get("observation"), dict) else detail
            scanner_samples.append({
                "blackboard_ref": _ref(BlackboardLayer.RECON_CLUE, row),
                "clue_type": clue_type,
                "scanner": str(data.get("scanner") or observation.get("tool") or row.get("updated_by") or "")[:80],
                "target": _safe_location(data.get("value"), allow_plain=False),
                "status": str(data.get("status") or data.get("execution_status")
                              or data.get("verification_status") or "observed")[:60],
                **({"name": str(data.get("name"))[:160]} if data.get("name") else {}),
                **({"severity": str(data.get("severity"))[:30]} if data.get("severity") else {}),
            })
        mode = _failure_mode(data)
        if mode:
            negative_total += 1
        if mode and len(negative_samples) < 16:
            detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
            observation = detail.get("observation") if isinstance(detail.get("observation"), dict) else detail
            negative_samples.append({
                "blackboard_ref": _ref(BlackboardLayer.RECON_CLUE, row),
                "clue_type": clue_type,
                "failure_mode": mode,
                "tool": str(data.get("scanner") or observation.get("tool") or "")[:80],
                "target": _safe_location(data.get("value") or
                    (observation.get("arguments") or {}).get("url")
                    if isinstance(observation.get("arguments"), dict) else data.get("value"),
                    allow_plain=False),
            })

    status_counts = getattr(blackboard, "status_counts", None)
    vuln_status = (Counter(status_counts(BlackboardLayer.VULN)) if callable(status_counts)
                   else Counter(str(row["data"].get("status") or "candidate")
                                for row in vuln_rows))
    vuln_samples = []
    for row in vuln_rows[:20]:
        data = row["data"]
        verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
        vuln_samples.append({
            "blackboard_ref": _ref(BlackboardLayer.VULN, row),
            "title": str(data.get("title") or row["record_id"])[:180],
            "vuln_type": str(data.get("vuln_type") or "unknown")[:80],
            "target": _safe_location(data.get("endpoint") or data.get("target")),
            "status": str(data.get("status") or "candidate")[:60],
            "severity": str(data.get("severity") or "info")[:30],
            "verification_verdict": str(verification.get("verdict") or "")[:60],
        })

    summary = {
        "assets_total": asset_total,
        "assets_by_type": dict(sorted(assets_by_type.items())),
        "entrypoints": entrypoints,
        "technologies": [{"name": name, "source_refs": refs}
                         for name, refs in list(technology_refs.items())[:24]],
        "scanner_clues": {"total": clue_total, "by_type": dict(sorted(clue_types.items())),
                           "samples": scanner_samples},
        "asset_candidates": {
            "total_in_clue_window": len(asset_candidates),
            "prioritized": asset_candidates[:24],
            "note": "外部候选不代表授权、归属、存活或正式资产；分数只用于调查排序",
        },
        "negative_evidence": {"total_in_clue_window": negative_total,
                              "samples": negative_samples,
                              "note": "固定失败分类；完整原文按 blackboard_ref 读取"},
        "vulnerabilities": {"total": vuln_total, "by_status": dict(sorted(vuln_status.items())),
                            "samples": vuln_samples},
        "action_signals": _expert_action_signals(
            blackboard, asset_rows, clue_rows, vuln_rows, task_rows),
        "truncated": {"assets": asset_total > len(asset_rows),
                      "clues": clue_total > len(clue_rows),
                      "vulnerabilities": vuln_total > len(vuln_rows)},
    }
    try:
        from spiritscout.resources.session_secrets import redact_known

        return redact_known(blackboard.database_path.parent, summary)
    except (AttributeError, OSError, ValueError):
        return summary


def _origin_markers(data: dict[str, Any], row: dict[str, Any]) -> list[str]:
    """提取记录已经声明的来源；不根据 URL 形态猜测真实性。"""
    attrs = data.get("attrs") if isinstance(data.get("attrs"), dict) else {}
    values: list[Any] = []
    for source in (data, attrs):
        raw = source.get("sources")
        if isinstance(raw, list):
            values.extend(raw)
        values.extend(source.get(key) for key in ("source", "origin", "scanner")
                      if source.get(key))
    provenance = data.get("provenance")
    if isinstance(provenance, dict) and provenance.get("origin"):
        values.append(provenance["origin"])
    if row.get("updated_by"):
        values.append(row["updated_by"])
    return list(dict.fromkeys(str(value).strip()[:80] for value in values
                              if str(value).strip()))[:8]


def _frontier_subject(layer: BlackboardLayer, row: dict[str, Any]) -> str:
    data = row["data"]
    if layer == BlackboardLayer.VULN:
        return str(data.get("title") or data.get("endpoint") or data.get("target")
                   or row["record_id"])[:240]
    return (_safe_location(data.get("value") or data.get("endpoint") or data.get("target"))
            or str(data.get("name") or data.get("clue_type") or row["record_id"])[:240])


def _frontier_status(layer: BlackboardLayer, data: dict[str, Any], origins: list[str]) -> str:
    state = str(data.get("status") or data.get("verification_status")
                or data.get("execution_status") or "").lower()
    evidence = str(data.get("evidence_state") or "unverified").lower()
    origin_words = {part.lower() for value in origins for part in value.replace("-", "_").split("/")}
    if state in _NEGATIVE_STATES:
        return "exhausted_leads"
    if _failure_mode(data):
        return "blocked_leads"
    if (layer == BlackboardLayer.VULN and state in {"accepted", "confirmed"}) \
            or evidence == "independently_verified":
        return "breakthroughs"
    if origin_words & _GENERATED_ORIGIN_MARKERS or evidence in {"conflicting", "expired"}:
        return "needs_disambiguation"
    if evidence == "unverified" and not origin_words & _TARGET_ORIGIN_MARKERS:
        return "needs_disambiguation"
    return "actionable_leads"


def investigation_frontier(blackboard: Any) -> dict[str, Any]:
    """将现有事实整理成可排序的调查候选，不替模型判断漏洞价值。"""
    groups: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("breakthroughs", "actionable_leads", "needs_disambiguation",
                            "blocked_leads", "exhausted_leads")
    }
    changed: list[dict[str, Any]] = []
    lead_subjects: set[str] = set()
    for row in blackboard.query(BlackboardLayer.TOPOLOGY, limit=160):
        data = row["data"]
        if data.get("kind") != "lead_card":
            continue
        subject = _safe_location(data.get("subject")) or str(data.get("subject") or "")[:240]
        if subject:
            lead_subjects.add(subject)
        status = str(data.get("status") or "needs_disambiguation")
        group = {
            "breakthrough": "breakthroughs", "actionable": "actionable_leads",
            "investigating": "actionable_leads", "new": "needs_disambiguation",
            "needs_disambiguation": "needs_disambiguation", "blocked": "blocked_leads",
            "exhausted": "exhausted_leads", "refuted": "exhausted_leads",
        }.get(status, "needs_disambiguation")
        if len(groups[group]) >= _FRONTIER_LIMIT:
            continue
        card = {
            "ref": _ref(BlackboardLayer.TOPOLOGY, row),
            "source_kind": "lead_card",
            "lead_id": str(data.get("lead_id") or row["record_id"]),
            "subject": subject,
            "origin": [str(value)[:80] for value in data.get("origins", [])
                       if isinstance(value, str)],
            "origin_class": str(data.get("origin_class") or "unspecified"),
            "evidence_state": str(data.get("evidence_state") or "unverified")[:40],
            "authenticity": data.get("authenticity") or {},
            "unknowns": [str(value)[:300] for value in data.get("unknowns", [])
                         if isinstance(value, str)][:8],
            "next_minimal_experiment": str(data.get("next_minimal_experiment") or "")[:500],
            "novelty": "new_record" if row["version"] == 1 else "updated_record",
            "repetition": {"record_versions": row["version"],
                           "observations": int(data.get("observation_count") or 0),
                           "reobserved": int(data.get("observation_count") or 0) > 1},
            "status": group,
        }
        groups[group].append(card)
        if len(changed) < _FRONTIER_LIMIT:
            changed.append({"ref": card["ref"], "updated_at": row.get("updated_at", ""),
                            "status": group})
    for layer, limit in ((BlackboardLayer.ASSET, 120), (BlackboardLayer.RECON_CLUE, 120),
                         (BlackboardLayer.VULN, 80)):
        for row in blackboard.query(layer, limit=limit):
            data = row["data"]
            subject = _frontier_subject(layer, row)
            if subject in lead_subjects:
                continue
            origins = _origin_markers(data, row)
            group = _frontier_status(layer, data, origins)
            if len(groups[group]) >= _FRONTIER_LIMIT:
                continue
            origin_words = {part.lower() for value in origins
                            for part in value.replace("-", "_").split("/")}
            card = {
                "ref": _ref(layer, row),
                "source_kind": str(
                    data.get("asset_type") or data.get("clue_type")
                    or data.get("vuln_type") or layer.value
                )[:80],
                "subject": subject,
                "origin": origins,
                "origin_class": (
                    "target_observation" if origin_words & _TARGET_ORIGIN_MARKERS
                    else "derived_observation" if origin_words & _DERIVED_ORIGIN_MARKERS
                    else "generated_test" if origin_words & _GENERATED_ORIGIN_MARKERS
                    else "unspecified"),
                "evidence_state": str(data.get("evidence_state") or "unverified")[:40],
                "novelty": "new_record" if row["version"] == 1 else "updated_record",
                "repetition": {"record_versions": row["version"],
                               "reobserved": row["version"] > 1},
                "status": group,
            }
            groups[group].append(card)
            if len(changed) < _FRONTIER_LIMIT:
                changed.append({"ref": card["ref"], "updated_at": row.get("updated_at", ""),
                                "status": group})

    handoffs = []
    for row in blackboard.query(BlackboardLayer.TASK_STATE, limit=80):
        handoff = (row["data"].get("summary") or {}).get("handoff")
        if isinstance(handoff, dict):
            handoffs.append({"task_ref": _ref(BlackboardLayer.TASK_STATE, row), **handoff})
        if len(handoffs) >= 12:
            break
    if not any(groups.values()) and not handoffs:
        return {}
    frontier = {
        **groups,
        "recently_changed": changed,
        "recent_handoffs": handoffs,
        "ranking_contract": (
            "这些字段只描述来源、证据状态、重复和交接缺口；结合任务目标判断预期信息增益，"
            "不要把 generated_test 或单一 HTTP 状态直接当成真实入口。"),
    }
    try:
        from spiritscout.resources.session_secrets import redact_known

        return redact_known(blackboard.database_path.parent, frontier)
    except (AttributeError, OSError, ValueError):
        return frontier


def _asset_intelligence(assets: list[dict[str, Any]], legacy: list[dict[str, Any]]) -> dict[str, Any]:
    """旧字段继续投影 JS 子集；新调度统一读取 intelligence_summary。"""
    scripts = [row for row in assets if row["data"].get("asset_type") == "script"]
    endpoints = [row for row in assets if row["data"].get("asset_type") == "endpoint"
                 and ("js_static" in (row["data"].get("attrs") or {}).get("sources", [])
                      or (row["data"].get("attrs") or {}).get("source") == "js_static")]
    if scripts or endpoints:
        return {
            "scripts_total": len(scripts),
            "js_endpoints_total": len(endpoints),
            "scripts": [{"blackboard_ref": _ref(BlackboardLayer.ASSET, row),
                "url": _safe_location(row["data"].get("value", "")),
                "route_count": (row["data"].get("attrs") or {}).get("route_count", 0),
                "secret_count": len((row["data"].get("attrs") or {}).get("secrets", []))}
                for row in scripts[:12]],
            "endpoints": [{"blackboard_ref": _ref(BlackboardLayer.ASSET, row),
                "url": _safe_location(row["data"].get("value", "")),
                "method": (row["data"].get("attrs") or {}).get("method", "UNKNOWN"),
                "params": (row["data"].get("attrs") or {}).get("params", [])}
                for row in endpoints[:20]],
        }
    if legacy:
        return {
            "legacy_projection": True,
            "scripts_total": len(legacy),
            "js_endpoints_total": sum(len(row["data"].get("routes", [])) for row in legacy),
            "scripts": [{"blackboard_ref": _ref(BlackboardLayer.JS_INTEL, row),
                "url": _safe_location(row["data"].get("script_url", "")),
                "route_count": len(row["data"].get("routes", [])),
                "secret_count": len(row["data"].get("secrets", []))} for row in legacy[:12]],
        }
    return {}


def authorization(guard: Any) -> dict[str, Any]:
    """模型上下文中的用户声明：初始范围、批准答复与排除规则，不做硬校验。"""
    scope = getattr(guard, "scope", None) if guard is not None else None
    if scope is None:
        return {}
    approved = getattr(guard, "approved_targets", None)
    return {
        "allowed_domains": list(getattr(scope, "allowed_domains", []) or []),
        "allowed_cidrs": list(getattr(scope, "allowed_cidrs", []) or []),
        "allowed_urls": list(getattr(scope, "allowed_urls", []) or []),
        "approved_by_user": sorted(approved()) if callable(approved) else [],
        "denied": list(getattr(scope, "denied", []) or []),
        "testing_rules": str(getattr(scope, "testing_rules", "") or ""),
        "notes": str(getattr(scope, "notes", "") or ""),
        "boundary_enforcement": "model_context",
        "local_analysis": bool(getattr(scope, "local_analysis", False)),
    }


def environment(*, workspace: str = "", artifacts: str = "") -> dict[str, Any]:
    """沙箱运行态事实：只陈述位置与平台，不枚举可用的命令或库。"""
    facts: dict[str, Any] = {
        "platform": _platform.system().lower() or "unknown",
        "date": datetime.now().date().isoformat(),
    }
    if workspace:
        facts["workspace"] = workspace
    if artifacts:
        facts["artifacts"] = artifacts
    return facts


def record_user_statement(blackboard: Any, statement: dict[str, Any]) -> None:
    """用户消息/询问答复原文留存；模型自行理解边界含义，后端不分类。"""
    if blackboard is None:
        return
    while True:
        row = blackboard.read_versioned(BlackboardLayer.TARGET_AUTH, "user-context")
        current, version = row if row is not None else ({}, 0)
        statements = list(current.get("statements") or [])
        statement_id = statement.get("statement_id")
        if statement_id and any(item.get("statement_id") == statement_id for item in statements):
            return
        statements.append(statement)
        try:
            blackboard.write(BlackboardLayer.TARGET_AUTH, "user-context",
                             {**current, "statements": statements}, updated_by="user-context",
                             expected_version=version)
        except VersionConflict:
            # 多 Agent 的问题可并发作答，重读后追加，不能用旧列表覆盖其他用户原文。
            continue
        return


def record_user_statement_at_path(path: str | Path, statement: dict[str, Any]) -> None:
    """未开工或运行态已关闭时仍立即留存用户原话，不依赖主 Agent 消费。"""
    from spiritscout.blackboard import Blackboard

    blackboard = Blackboard(path)
    try:
        record_user_statement(blackboard, statement)
    finally:
        blackboard.close()


def sync_user_context(blackboard: Any, *,
                      dialogue_history: list[dict[str, str]] | None = None,
                      mission: str | None = None, standing_rules: str | None = None) -> None:
    """恢复可用的旧对话与带 ID 的新消息，原子更新任务，不覆盖并发答复。"""
    while True:
        row = blackboard.read_versioned(BlackboardLayer.TARGET_AUTH, "user-context")
        current, version = row if row is not None else ({}, 0)
        statements = list(current.get("statements") or [])
        ids = {item["statement_id"] for item in statements if item.get("statement_id")}
        for message in dialogue_history or []:
            if message.get("role") != "user":
                continue
            statement_id = message.get("statement_id")
            # 无 ID 的旧历史只在事实块缺失时回填；相同文本的不同消息不能去重。
            if statement_id in ids or (not statement_id and current):
                continue
            statement = {"kind": "message", "text": str(message.get("content") or "")}
            if message.get("attachments"):
                statement["attachments"] = message["attachments"]
            if statement_id:
                statement["statement_id"] = statement_id
                ids.add(statement_id)
            statements.append(statement)
        updated = {**current}
        if statements:
            updated["statements"] = statements
        if mission is not None:
            updated["mission"] = mission
        if standing_rules is not None:
            updated["standing_rules"] = standing_rules
        if updated == current:
            return
        try:
            blackboard.write(BlackboardLayer.TARGET_AUTH, "user-context", updated,
                             updated_by="user-context", expected_version=version)
        except VersionConflict:
            continue
        return


def snapshot(*, guard: Any = None, env: dict[str, Any] | None = None,
             blackboard: Any = None, references: bool = False) -> dict[str, Any]:
    """当前世界状态：空字段不占位，片段只承载运行时事实。"""
    out: dict[str, Any] = {}
    auth = authorization(guard)
    if auth:
        out["authorization"] = auth
    if env:
        out["environment"] = env
    if blackboard is not None:
        context = blackboard.read(BlackboardLayer.TARGET_AUTH, "user-context")
        if context:
            out["user_context"] = context
        project = blackboard.read(BlackboardLayer.TOPOLOGY, "project-memory") or {}
        if project.get("description"):
            out["project_rules"] = project["description"]
        if callable(getattr(blackboard, 'query', None)):
            summary = intelligence_summary(blackboard)
            if summary:
                out["intelligence_summary"] = summary
            from spiritscout.blackboard.hypotheses import summary as hypothesis_summary

            hypotheses = hypothesis_summary(blackboard)
            if hypotheses:
                out["vulnerability_hypotheses"] = hypotheses
            frontier = investigation_frontier(blackboard)
            if frontier:
                out["investigation_frontier"] = frontier
            from spiritscout.blackboard.opportunities import build_opportunity_pool

            opportunities = build_opportunity_pool(
                frontier, hypotheses,
                blackboard.query(BlackboardLayer.TASK_STATE, limit=300),
                action_signals=summary.get("action_signals") or {},
            )
            if opportunities.get("total") or opportunities["action_yield"]["completed_tasks"]:
                out["testing_opportunities"] = opportunities
            assets = blackboard.query(BlackboardLayer.ASSET, limit=_ASSET_SAMPLE_LIMIT)
            from spiritscout.blackboard.expert_scorecard import build_scorecard

            scorecard_assets = blackboard.query(BlackboardLayer.ASSET, limit=10000)
            scorecard_vulns = blackboard.query(BlackboardLayer.VULN, limit=10000)
            asset_count = (blackboard.count(BlackboardLayer.ASSET)
                           if callable(getattr(blackboard, "count", None))
                           else len(scorecard_assets))
            vuln_counts = (blackboard.status_counts(BlackboardLayer.VULN)
                           if callable(getattr(blackboard, "status_counts", None))
                           else dict(Counter(str(row.get("data", {}).get("status") or "candidate")
                                             for row in scorecard_vulns)))
            scorecard = build_scorecard(
                asset_count=asset_count,
                asset_rows=scorecard_assets,
                vuln_rows=scorecard_vulns,
                opportunities=opportunities, hypotheses=hypotheses,
                vuln_counts=vuln_counts,
            )
            if assets or opportunities.get("total") or hypotheses:
                out["expert_scorecard"] = scorecard
            legacy = blackboard.query(BlackboardLayer.JS_INTEL, limit=100)
            js_projection = _asset_intelligence(assets, legacy)
            if js_projection:
                out["asset_intelligence"] = js_projection
        from spiritscout.blackboard.exchange import fact_index

        facts = fact_index(blackboard) if references else []
        if facts:
            out["shared_facts"] = facts
        if references and callable(getattr(blackboard, 'query', None)):
            from spiritscout.blackboard.exploration import directory

            # 仅广播小目录；详细图与失败分支由模型按需查询。
            recent = directory(blackboard, 'exploration', limit=8, recent=True)
            if recent['items']:
                out['exploration_graph'] = recent
    if references and blackboard is not None:
        from spiritscout.blackboard.exchange import project_rules
        return project_rules(blackboard, out)
    return out


def render(values: dict[str, Any], *, update: bool = False) -> str:
    attribute = ' update="true"' if update else ""
    body = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return f"<world_state{attribute}>\n{body}\n</world_state>"


def update(previous: dict[str, Any] | None, current: dict[str, Any], *,
           incremental_facts: bool = False, incremental_exploration: bool = False) -> str | None:
    """返回与上次不同的顶层字段（整值重发）；无变化返回 None。

    不发送 JSON Patch：旧片段可能被上下文压缩折叠，后续片段必须能独立
    读懂。整值重发让最新片段始终包含该字段的完整现状（与 Codex 的
    environment_context 按区块重绘同语义）。incremental_facts 仅供完整保留
    初始片段和后续更新的 CTF 对话启用，按记录身份同步近期目录，不变更授权更新契约。
    探索目录增量另行协商；仅在补丁明显更小时发送，旧会话继续接收完整字段。
    """
    if not current:
        return None
    if not previous:
        return render(current)
    changed: dict[str, Any] = {}
    for key, value in current.items():
        if previous.get(key) != value:
            changed[key] = value
    for key in previous.keys() - current.keys():
        changed[key] = None
    if 'exploration_graph' in changed and isinstance(current.get('exploration_graph'), dict):
        before, after = previous.get('exploration_graph') or {}, current['exploration_graph']
        def graph_index(value: Any) -> dict[tuple[str, str], dict[str, Any]] | None:
            if not isinstance(value, dict) or not isinstance(value.get('items', []), list):
                return None
            result = {}
            for item in value.get('items', []):
                ref = item.get('blackboard_ref') if isinstance(item, dict) else None
                if (not isinstance(ref, dict) or not isinstance(ref.get('layer'), str)
                        or not isinstance(ref.get('record_id'), str) or not ref['layer'] or not ref['record_id']):
                    return None
                identity = ref['layer'], ref['record_id']
                if identity in result:
                    return None
                result[identity] = item
            return result

        old, new = graph_index(before), graph_index(after)
        if old is not None and new is not None:
            old_meta = {key: value for key, value in before.items() if key != 'items'}
            new_meta = {key: value for key, value in after.items() if key != 'items'}
            upsert = [row for key, row in new.items() if old.get(key) != row]
            retired = [{'layer': key[0], 'record_id': key[1]} for key in old if key not in new]
            if not upsert and not retired and old_meta == new_meta:
                changed.pop('exploration_graph')  # 仅展示次序变化不产生模型消息。
            elif incremental_exploration:
                delta = {'upsert': upsert, 'retired': retired}
                if old_meta != new_meta:
                    delta['meta'] = new_meta
                compact = render({'exploration_graph_delta': delta}, update=True)
                full = render({'exploration_graph': after}, update=True)
                from spiritscout.gateway.context import estimate_input_tokens

                # 传输保留全部字段；补丁膨胀时回退整块，避免全图变化反而更贵。
                if (len(compact.encode()) * 5 <= len(full.encode()) * 4
                        and estimate_input_tokens([{'role': 'user', 'content': compact}])
                        < estimate_input_tokens([{'role': 'user', 'content': full}])):
                    changed.pop('exploration_graph')
                    changed['exploration_graph_delta'] = delta
    if incremental_facts and "shared_facts" in changed:
        # 仅用于原始世界片段完整保留的追加式会话；授权/规则仍按原契约整字段更新。
        before, after = previous.get("shared_facts", []), current.get("shared_facts", [])
        def indexed(rows: Any) -> dict[tuple[str, str], dict[str, Any]] | None:
            if not isinstance(rows, list):
                return None
            result = {}
            for row in rows:
                ref = row.get("blackboard_ref") if isinstance(row, dict) else None
                if (not isinstance(ref, dict) or not isinstance(ref.get("layer"), str)
                        or not isinstance(ref.get("record_id"), str) or not ref["layer"] or not ref["record_id"]):
                    return None
                identity = (ref["layer"], ref["record_id"])
                if identity in result:
                    return None
                result[identity] = row
            return result
        old_index, new_index = indexed(before), indexed(after)
        if old_index is not None and new_index is not None:
            changed.pop("shared_facts")
            upsert = [row for key, row in new_index.items() if old_index.get(key) != row]
            retired = [{"layer": key[0], "record_id": key[1]} for key in old_index if key not in new_index]
            if upsert or retired:
                changed["shared_facts_delta"] = {"upsert": upsert, "retired": retired}
    if not changed:
        return None
    return render(changed, update=True)
