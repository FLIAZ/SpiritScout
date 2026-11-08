"""只读关联任务、实际成果与用量；执行结束不冒充解题成功，公共成本不强行摊派。"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from spiritscout.gateway.cache import cache_summary
from spiritscout.gateway.costs import cost_per_result, cost_summary


def _metrics(rows: list[dict[str, Any]], *, history_complete: bool = True) -> dict[str, Any]:
    costs = cost_summary(rows)
    if not history_complete:
        for currency in ("usd", "cny"):
            costs[currency]["complete"] = False
    return {"calls": len(rows), "input_tokens": sum(r.get("input_tokens", 0) for r in rows),
            "output_tokens": sum(r.get("output_tokens", 0) for r in rows),
            "failed_calls": sum(r.get("call_status") == "failed" for r in rows),
            "cancelled_calls": sum(r.get("call_status") == "cancelled" for r in rows),
            "history_complete": history_complete, "cost": costs,
            "cache_hit_rate": cache_summary(rows)["hit_rate"]}


def session_outcome_costs(workspace: Path, snapshot: dict[str, Any], *,
                          version: str | None = None) -> dict[str, Any]:
    """成果为当前持久化快照。按版本筛选费用后，不把部分费用当作完整任务成本。"""
    rows = []
    seen: set[str] = set()
    for row in snapshot.get("call_series", []):
        identifier = row.get("call_id")
        if identifier and identifier in seen:
            continue
        if identifier:
            seen.add(identifier)
        rows.append(row)
    selected = [r for r in rows if version is None or r.get("runtime_version") == version]
    residual_calls = max(0, snapshot.get("calls", 0) - len(rows))
    residual_tokens = max(0, snapshot.get("used_tokens", 0) - sum(r.get("tokens", 0) for r in rows))
    complete = len(selected) == len(rows) and not residual_calls and not residual_tokens
    errors: list[dict[str, str]] = []

    def read_json(name: str, expected: type) -> Any:
        path = workspace / name
        if not path.exists():
            return expected()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, expected):
                raise ValueError("unexpected shape")
            return data
        except (OSError, ValueError) as exc:
            errors.append({"source": name, "error": type(exc).__name__})
            return expected()

    tasks: dict[str, dict[str, Any]] = {}
    vulnerabilities: dict[str, dict[str, Any]] = {}
    database = workspace / "bb.db"
    if database.is_file():
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                conn.execute("BEGIN")
                for layer, key, raw in conn.execute(
                        "SELECT layer,record_id,data FROM bb_records WHERE layer IN ('task_state','vuln')"):
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("unexpected record")
                    if layer == "task_state":
                        task = data.get("task")
                        if isinstance(task, dict):
                            tasks[str(task.get("task_id") or key)] = task
                    else:
                        vulnerabilities[key] = data
        except (OSError, ValueError, sqlite3.Error) as exc:
            errors.append({"source": "bb.db", "error": type(exc).__name__})

    flags = [row for row in read_json("flags.json", list) if isinstance(row, dict)]
    completions = read_json("flags.completion.json", dict)
    fixes = read_json("flags.fixes.json", dict)
    accepted: dict[str, set[str]] = defaultdict(set)
    answers: dict[str, set[str]] = defaultdict(set)
    for flag in flags:
        cid, identifier = str(flag.get("challenge_id") or ""), str(flag.get("flag_id") or "")
        if not cid or not identifier:
            continue
        answers[cid].add(identifier)
        if flag.get("accepted") is True and flag.get("submitted") is True:
            accepted[cid].add(identifier)
    completed_answers, completed_fixes = set(), set()
    for cid, record in completions.items():
        if not isinstance(record, dict) or record.get("completed") is not True:
            continue
        declared = record.get("answer_ids")
        if not isinstance(declared, list) or not declared or not all(isinstance(i, str) for i in declared):
            continue
        disputed = record.get("disputed_answer_ids") or []
        actual = answers[cid] - {i for i in disputed if isinstance(i, str)}
        patch_ids = {fid for fid, row in fixes.items() if isinstance(row, dict) and row.get("challenge_id") == cid}
        if set(declared) == actual:
            completed_answers.add(cid)
        elif patch_ids and set(declared) == patch_ids:
            completed_fixes.add(cid)

    confirmed_vulns = {key for key, row in vulnerabilities.items()
        if row.get("status") == "accepted" and row.get("poc_verified") is True
        and row.get("exploitable") is True and (row.get("verification") or {}).get("verdict") == "confirmed"
        and row.get("assessment") not in {"expired", "conflicting"}}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        grouped[str(row.get("task_id") or "")].append(row)
    task_reports = []
    challenge_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tid in sorted(set(grouped) | set(tasks)):
        if not tid:
            continue
        calls = grouped[tid]
        task = tasks.get(tid, {})
        challenge = (task.get("payload") or {}).get("challenge") or {}
        cid = str(challenge.get("id") or "")
        if cid:
            challenge_rows[cid].extend(calls)
        task_complete = not residual_calls and not residual_tokens and len(calls) == sum(r.get("task_id") == tid for r in rows)
        task_reports.append({"task_id": tid, "challenge_id": cid,
            "execution_status": task.get("status", "unknown"),
            "verified_vulnerability": tid.removeprefix("verify-") in confirmed_vulns if tid.startswith("verify-") else False,
            **_metrics(calls, history_complete=task_complete)})
    challenge_reports = [{"challenge_id": cid, "accepted_flags": len(accepted[cid]),
        "local_answer_complete": cid in completed_answers, "patch_delivered": cid in completed_fixes,
        "direct_execution": _metrics(challenge_rows[cid], history_complete=complete)}
        for cid in sorted(set(challenge_rows) | set(accepted) | completed_answers | completed_fixes)]
    counts = {"platform_accepted_flags": sum(map(len, accepted.values())),
              "local_completed_challenges": len(completed_answers),
              "delivered_patches": len(completed_fixes),
              "independently_verified_vulnerabilities": len(confirmed_vulns)}
    summary = _metrics(selected, history_complete=complete)
    # 成果来源读失败时保留已知计数，但不能发布均摊成本。
    per_result = {name: cost_per_result(summary["cost"], count if not errors else 0)
                  for name, count in counts.items()}
    return {"version_filter": version, "outcome_scope": "current_session_snapshot",
            "summary": summary, "outcomes": counts, "cost_per_result": per_result,
            "tasks": task_reports, "challenges": challenge_reports,
            "execution_status_counts": dict(Counter(t["execution_status"] for t in task_reports)),
            "shared_or_unattributed": _metrics(grouped[""], history_complete=complete),
            "excluded_version_calls": len(rows) - len(selected),
            "historical_unattributed_calls": residual_calls, "historical_unattributed_tokens": residual_tokens,
            "read_errors": errors,
            "note": "task done 仅表示执行结束；本地答案完成不等于独立验证。平台按已判对 Flag 计数，"
                    "多 Flag 题不据此推断整题完成。补丁交付不等于修复通过验证。"
                    "均摊成本含本会话失败尝试和公共调用；币种分列，缺用量/价格/历史时为 null。"}
