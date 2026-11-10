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
    if not any((asset_total, clue_total, vuln_total)):
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
        "negative_evidence": {"total_in_clue_window": negative_total,
                              "samples": negative_samples,
                              "note": "固定失败分类；完整原文按 blackboard_ref 读取"},
        "vulnerabilities": {"total": vuln_total, "by_status": dict(sorted(vuln_status.items())),
                            "samples": vuln_samples},
        "truncated": {"assets": asset_total > len(asset_rows),
                      "clues": clue_total > len(clue_rows),
                      "vulnerabilities": vuln_total > len(vuln_rows)},
    }
    try:
        from spiritscout.resources.session_secrets import redact_known

        return redact_known(blackboard.database_path.parent, summary)
    except (AttributeError, OSError, ValueError):
        return summary


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
            assets = blackboard.query(BlackboardLayer.ASSET, limit=_ASSET_SAMPLE_LIMIT)
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
