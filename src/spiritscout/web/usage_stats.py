"""共享/会话用量的只读聚合，不复算重试用量，不用配置猜测历史供应商。"""
from __future__ import annotations

import base64
import json
import math
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from spiritscout.config.model_identity import model_key
from spiritscout.web.dashboard import start_of_period, timestamp, usage_metrics

PERIODS = {"hour", "today", "7d", "30d", "all"}
COUNT_FIELDS = ("tokens", "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens",
                "sdk_attempts", "unreported_attempts")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _row(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    row = dict(value)
    for key in COUNT_FIELDS:
        row[key] = _count(row.get(key))
    cached = value.get("cached_input_tokens")
    row["cached_input_tokens"] = cached if type(cached) is int and cached >= 0 else None
    for key in ("model", "model_key", "provider", "provider_ref", "actor", "task_type", "cache_scope"):
        row[key] = str(value.get(key) or "")
    parts = value.get("context_parts")
    row["context_parts"] = {str(key): _count(count) for key, count in parts.items()} if isinstance(parts, dict) else {}
    row["cost_usd"] = _number(row.get("cost_usd")) or 0.0
    for key in ("estimated_cost_cny_min", "estimated_cost_cny_max"):
        row[key] = _number(row.get(key))
    if (row["estimated_cost_cny_min"] is not None and row["estimated_cost_cny_max"] is not None
            and row["estimated_cost_cny_min"] > row["estimated_cost_cny_max"]):
        row["estimated_cost_cny_min"] = row["estimated_cost_cny_max"] = None
    row["ts"] = timestamp(row.get("ts"))
    return row


def _identity(row: dict[str, Any]) -> tuple[str, str, str]:
    reference = str(row.get("model_key") or row.get("model") or "")
    provider = str(row.get("provider_ref") or row.get("provider") or "")
    name = str(row.get("model") or reference)
    if reference.startswith("@") and len(reference) < 16384:
        try:
            encoded = reference[1:]
            pair = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(v, str) and v for v in pair):
                provider, name = pair
                return reference, provider, name
        except (ValueError, UnicodeDecodeError):
            pass
    if provider:
        reference = model_key({"model": name, "provider_ref": provider})
    return reference, provider, name


def _read_snapshot(legacy: Path) -> dict[str, Any] | None:
    ledger = legacy.with_suffix(".db")
    if ledger.is_file():
        # 不构造 UsageLedger，不执行迁移/DDL；在一个只读事务中读取 WAL 可见快照。
        with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.execute("BEGIN")
            baseline = conn.execute("SELECT data FROM baseline WHERE id=1").fetchone()
            base = json.loads(baseline[0]) if baseline else {"used_tokens": 0}
            if not isinstance(base, dict):
                raise ValueError("Invalid baseline")
            rows = [json.loads(row[0]) for row in conn.execute("SELECT data FROM calls ORDER BY seq")]
        original = base.get("call_series") or []
        if not isinstance(original, list):
            raise ValueError("Invalid call series")
        appended = [r for value in rows if (r := _row(value)) is not None]
        base = {**base, "call_series": original + appended,
                "used_tokens": _count(base.get("used_tokens")) + sum(r["tokens"] for r in appended),
                "used_usd": (_number(base.get("used_usd")) or 0) + sum(r["cost_usd"] for r in appended),
                "calls": max(_count(base.get("calls")), len(original)) + len(appended)}
        per_model = dict(base.get("per_model_tokens") or {})
        for row in appended:
            key = str(row.get("model_key") or row.get("model") or "")
            per_model[key] = _count(per_model.get(key)) + row["tokens"]
        base["per_model_tokens"] = per_model
        return base
    if legacy.is_file():
        data = json.loads(legacy.read_bytes())
        if not isinstance(data, dict):
            raise ValueError("Invalid usage snapshot")
        return data
    return None


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = usage_metrics(rows)
    # 与 gateway 一致：tokens/cost_usd 已经包含已报告的失败/重试，不再相加 attempts。
    result["metered_usd"] = sum(r["cost_usd"] for r in rows)
    result["failed_calls"] = sum(r.get("call_status") == "failed" for r in rows)
    result["cancelled_calls"] = sum(r.get("call_status") == "cancelled" for r in rows)
    result["undated_calls"] = sum(r["ts"] is None for r in rows)
    return result


def usage_stats(cfg: Any, manager: Any, period: str = "today", *, now: float | None = None) -> dict[str, Any]:
    """金额是已有记账/估算，不是账单；有限时间段不分配无时间戳的历史总额。"""
    if period not in PERIODS:
        raise ValueError("Unsupported period")
    now = time.time() if now is None else now
    start = start_of_period(period, now)
    sources = [(Path(cfg.workspace.root) / "usage.json", None)]
    sources.extend((Path(s.workspace) / "usage.json", s) for s in manager.list())
    seen_paths: set[str] = set()
    seen_calls: set[str] = set()
    rows: list[dict[str, Any]] = []
    historical_tokens = historical_calls = undated_calls = 0
    historical_usd = 0.0
    residual_models: dict[tuple[str, str, str], int] = defaultdict(int)
    errors = source_count = 0
    for path, session in sources:
        key = str(path.resolve())
        if key in seen_paths:
            continue
        seen_paths.add(key)
        try:
            snapshot = _read_snapshot(path)
            if snapshot is None and session is not None:
                run = session.summary.get("run") if isinstance(session.summary, dict) else None
                snapshot = run.get("model_usage") if isinstance(run, dict) else None
            if snapshot is None:
                continue
            series = snapshot.get("call_series") or []
            if not isinstance(series, list):
                raise ValueError("Invalid call series")
            all_rows = [r for value in series if (r := _row(value)) is not None]
            source_count += 1
            token_rest = max(0, _count(snapshot.get("used_tokens")) - sum(r["tokens"] for r in all_rows))
            call_rest = max(0, _count(snapshot.get("calls")) - len(all_rows))
            usd_rest = max(0.0, (_number(snapshot.get("used_usd")) or 0) - sum(r["cost_usd"] for r in all_rows))
            undated_calls += call_rest
            if period == "all":
                historical_tokens += token_rest
                historical_calls += call_rest
                historical_usd += usd_rest
                recorded: dict[str, int] = defaultdict(int)
                for row in all_rows:
                    recorded[str(row.get("model_key") or row.get("model") or "")] += row["tokens"]
                remaining = token_rest
                for name, tokens in (snapshot.get("per_model_tokens") or {}).items():
                    rest = min(remaining, max(0, _count(tokens) - recorded[str(name)]))
                    residual_models[_identity({"model": name})] += rest
                    remaining -= rest
                if remaining:
                    residual_models[("", "", "")] += remaining
            for row in all_rows:
                call_id = str(row.get("call_id") or "")
                if call_id and call_id in seen_calls:
                    continue
                if call_id:
                    seen_calls.add(call_id)
                if row["ts"] is None:
                    undated_calls += 1
                if (period == "all" and row["ts"] is None) or (row["ts"] is not None and start <= row["ts"] <= now):
                    rows.append(row)
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
            errors += 1
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_identity(row)].append(row)
    models = []
    for identity in grouped.keys() | residual_models.keys():
        reference, provider, name = identity
        metrics = _metrics(grouped[identity])
        metrics["historical_tokens"] = residual_models.get(identity, 0)
        metrics["tokens"] += metrics["historical_tokens"]
        models.append({"model_key": reference, "provider": provider, "model": name, **metrics})
    models.sort(key=lambda m: (-m["tokens"], m["provider"], m["model"], m["model_key"]))
    summary = _metrics(rows)
    summary["tokens"] += historical_tokens
    summary["calls"] += historical_calls
    summary["metered_usd"] += historical_usd
    summary["usage_incomplete_calls"] += historical_calls
    summary["cost"]["unpriced_calls"] += historical_calls
    return {"period": period, "timezone": "Asia/Shanghai", "start": start, "end": now,
            "summary": summary, "models": models, "source_count": source_count, "source_errors": errors,
            "undated_calls": undated_calls,
            "historical": {"tokens": historical_tokens, "calls": historical_calls, "metered_usd": historical_usd},
            "cost_note": "已有记账及估算；未定价不等于免费，不是供应商结算账单。"}
