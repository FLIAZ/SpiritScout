"""仪表盘只读聚合：北京时间分桶、真实用量与跨会话执行态势。"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer
from spiritscout.gateway.cache import cache_summary
from spiritscout.gateway.usage import file_version

BJT = ZoneInfo("Asia/Shanghai")
ACTIVE = {"running", "dispatched"}


def timestamp(value: Any) -> float | None:
    """统一解析记录时间，拒绝无效数值；无时区的旧记录按北京时间解释。"""
    try:
        if isinstance(value, str):
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return date.replace(tzinfo=BJT).timestamp() if date.tzinfo is None else date.timestamp()
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def start_of_period(period: str, now: float) -> float:
    today = datetime.fromtimestamp(now, BJT).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "hour":
        return now - 3600
    if period == "all":
        return 0
    return (today - timedelta(days={"today": 0, "7d": 6, "30d": 29}[period])).timestamp()


def usage_metrics(calls: list[dict[str, Any]]) -> dict[str, Any]:
    cache = cache_summary(calls)
    priced_usd = [r for r in calls if float(r.get("cost_usd") or 0) > 0
                  and (r.get("estimated_cost_cny_min") is None
                       or r.get("estimated_cost_cny_max") is None)]
    cny = [r for r in calls if r.get("estimated_cost_cny_min") is not None
           and r.get("estimated_cost_cny_max") is not None]
    known = sum((r.get("estimated_cost_cny_min") is not None
                 and r.get("estimated_cost_cny_max") is not None) or float(r.get("cost_usd") or 0) > 0
                for r in calls)
    return {"calls": len(calls), "tokens": sum(r.get("tokens", 0) for r in calls),
            "cost": {"cny_min": sum(r["estimated_cost_cny_min"] for r in cny) if cny else None,
                     "cny_max": sum(r["estimated_cost_cny_max"] for r in cny) if cny else None,
                     "usd": sum(r["cost_usd"] for r in priced_usd) if priced_usd else None,
                     "priced_calls": known, "unpriced_calls": len(calls) - known,
                     "cny_calls": len(cny), "usd_calls": len(priced_usd), "kind": "estimate"},
            "cache": {k: v for k, v in cache.items() if k != "groups"}}


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return default


def _short(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


class DashboardReader:
    """文件版本不变就复用快照；审计从有界尾段起步，随后只读新增行。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._audits: dict[str, dict[str, Any]] = {}

    def _audit(self, path: Path) -> dict[str, Any]:
        key = str(path)
        version = file_version(path)
        previous = self._audits.get(key)
        if previous and previous["version"] == version and previous["offset"] >= version[2]:
            return previous
        if not version:
            return {"events": [], "pending": {}, "outcomes": [], "history_start": None, "complete": True}
        reset = not previous or previous["version"][:2] != version[:2] or version[2] < previous["offset"]
        state = ({"events": [], "pending": {}, "outcomes": [], "offset": 0, "complete": True,
                  "history_start": None, "last_score": None, "score": None, "accepted_flags": set()} if reset else previous)
        with path.open("rb") as stream:
            offset = state["offset"]
            # 巨型历史文件首次只取尾部，未知历史时间不伪装成“没有成果”。
            if reset and version[2] > 2 * 1024 * 1024:
                offset = version[2] - 2 * 1024 * 1024
                stream.seek(offset)
                stream.readline()
                offset = stream.tell()
                state["complete"] = False
            stream.seek(offset)
            # 突发增量也有读取上限，下一次刷新接续，绝不丢半行。
            remaining = 4 * 1024 * 1024
            while remaining > 0:
                line = stream.readline()
                if not line or not line.endswith(b"\n"):
                    break
                remaining -= len(line)
                offset = stream.tell()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                ts = timestamp(event.get("ts"))
                if ts is None:
                    continue
                d = event.get("detail") or {}
                if not isinstance(d, dict):
                    continue
                if state["history_start"] is None:
                    state["history_start"] = ts
                kind = event.get("type", "")
                compact = {"type": kind, "ts": ts, "actor": event.get("actor", ""),
                           "task_id": d.get("task_id", ""), "call_id": d.get("call_id", ""),
                           "seq": event.get("seq"), "kind": d.get("kind", ""),
                           "action": d.get("action", ""), "ok": d.get("ok"),
                           "text": _short(d.get("text") or d.get("summary") or d.get("error") or d.get("note") or d.get("title"))}
                if kind == "model.started":
                    state["pending"][str(d.get("call_id"))] = {**compact, "model": d.get("model"),
                                                               "task_type": d.get("task_type")}
                elif kind in {"model.call", "model.failed", "model.cancelled"}:
                    state["pending"].pop(str(d.get("call_id")), None)
                if kind == "flag.submit" and d.get("accepted") is True:
                    flag_id = d.get("flag_id") or d.get("challenge_id") or str(ts)
                    if flag_id not in state["accepted_flags"]:
                        state["accepted_flags"].add(flag_id)
                        state["last_score"] = ts
                        state["outcomes"].append({"ts": ts, "kind": "flag", "label": str(d.get("challenge_id") or "Flag 通过")})
                if kind == "competition.scoreboard" and isinstance(d.get("score"), (int, float)) and math.isfinite(d["score"]):
                    old_score = state["score"]
                    state["score"] = d["score"]
                    if old_score is not None and d["score"] > old_score:
                        state["last_score"] = ts
                        state["outcomes"].append({"ts": ts, "kind": "score", "label": f"得分 {d['score']}"})
                # 思考正文不进入总览，真实模型公开说明保留短片段。
                if kind != "agent.delta" or d.get("kind") == "text":
                    state["events"].append(compact)
                    state["events"] = state["events"][-120:]
                if kind == "platform.fact" and isinstance(d.get("profile"), dict):
                    state["capacity"] = (d["profile"].get("limits") or {}).get("max_active")
                if kind == "agent.delta" and d.get("kind") == "text":
                    stem = (event.get("actor"), d.get("call_id"), d.get("route_id") or d.get("route"), d.get("turn"))
                    text = (state.get("progress", "") if stem == state.get("progress_stem") else "") + str(d.get("text") or "")
                    state["progress"], state["progress_stem"] = text[-1200:], stem
                state["last_activity"] = ts
            state["offset"] = offset
        state["outcomes"] = state["outcomes"][-2000:]
        state["version"] = version
        self._audits[key] = state
        return state

    def _snapshot(self, session: Any) -> dict[str, Any]:
        root = session.workspace
        paths = [session.bb_path, Path(str(session.bb_path) + "-wal"), root / "flags.json",
                 root / "competition_progress.json", root / "ctf_progress.json"]
        version = tuple(file_version(p) for p in paths)
        key = str(root)
        old = self._snapshots.get(key)
        if old and old[0] == version:
            return old[1]
        data: dict[str, Any] = {"tasks": [], "nodes": [], "findings": [], "vulns": 0, "last_vuln": None}
        if session.bb_path.exists():
            bb = Blackboard(session.bb_path, readonly=True)
            try:
                records = bb.query(BlackboardLayer.TASK_STATE, limit=1000)
                data["nodes"] = next((r["data"]["dag_snapshot"] for r in records
                                      if isinstance(r["data"], dict) and "dag_snapshot" in r["data"]), [])
                ids = {tid for n in data["nodes"] for tid in n.get("task_ids", [])}
                node_ids = {n.get("node_id") for n in data["nodes"]}
                for r in records:
                    t = r["data"].get("task")
                    if t and (not data["nodes"] or t.get("task_id") in ids or t.get("parent_dag_node") in node_ids):
                        data["tasks"].append({"task_id": t.get("task_id"), "status": t.get("status"),
                                              "challenge_id": ((t.get("payload") or {}).get("challenge") or {}).get("id")})
                for r in bb.query(BlackboardLayer.TOPOLOGY, data_filter=("kind", "finding"), limit=20):
                    d = r["data"]
                    data["findings"].append({"type": d.get("type"), "text": _short(d.get("text")),
                                              "ts": timestamp(d.get("ts")) or timestamp(r.get("updated_at")),
                                              "record_id": r["record_id"]})
                data["vulns"] = bb.status_counts(BlackboardLayer.VULN).get("accepted", 0)
                accepted = bb.query(BlackboardLayer.VULN, data_filter=("status", "accepted"), limit=1)
                if accepted:
                    data["last_vuln"] = timestamp(accepted[0]["updated_at"])
            finally:
                bb.close()
        flags = _json(root / "flags.json", [])
        if not isinstance(flags, list):
            flags = []
        data["flags"] = ({"extracted": len(flags), "submitted": sum(bool(f.get("submitted")) for f in flags),
                          "accepted": sum(f.get("accepted") is True for f in flags)}
                         if (root / "flags.json").is_file() else {})
        progress = _json(root / "competition_progress.json", None) or _json(root / "ctf_progress.json", None)
        data["progress"] = progress if isinstance(progress, dict) else None
        self._snapshots[key] = version, data
        return data

    def build(self, sessions: list[Any], *, period: str = "today", now: float | None = None) -> dict[str, Any]:
        with self._lock:
            return self._build(sessions, period, time.time() if now is None else now)

    def _build(self, sessions: list[Any], period: str, now: float) -> dict[str, Any]:
        start = start_of_period(period, now)
        today_start = start_of_period("today", now)
        calls: list[dict[str, Any]] = []
        session_rows, findings, alerts, outcomes = [], [], [], []
        daily: dict[str, int] = defaultdict(int)
        per_model: dict[str, int] = defaultdict(int)
        total_tokens, total_cost = 0, 0.0
        for session in sessions:
            sid = session.session_id
            usage = session.model_usage()
            all_rows = [{**r, "session_id": sid} for r in usage.get("call_series", []) if timestamp(r.get("ts"))]
            calls.extend(all_rows)
            total_tokens += usage.get("used_tokens", 0)
            total_cost += usage.get("used_usd", 0)
            for model, tokens in (usage.get("per_model_tokens") or {}).items():
                per_model[model] += tokens
            for r in all_rows:
                daily[datetime.fromtimestamp(r["ts"], BJT).strftime("%Y-%m-%d")] += r.get("tokens", 0)
            try:
                snapshot, audit = self._snapshot(session), self._audit(session.audit_path)
                problem = ""
            except (OSError, ValueError, sqlite3.Error, TypeError, AttributeError, KeyError) as exc:
                snapshot = {"tasks": [], "nodes": [], "findings": [], "vulns": 0, "flags": {}, "progress": None}
                audit = {"events": [], "pending": {}, "outcomes": []}
                problem = f"部分记录暂不可读：{type(exc).__name__}"
            run = (session.summary or {}).get("run") or {}
            progress = snapshot["progress"] or run.get("platform") or {}
            flags = snapshot["flags"] or run.get("flag_stats") or (session.summary or {}).get("flags") or {}
            nodes, tasks = snapshot["nodes"], snapshot["tasks"]
            status_counts = Counter(t["status"] for t in tasks)
            running = sum(status_counts[s] for s in ACTIVE) if session.status == "running" else 0
            pending = sum(n.get("status") == "pending" for n in nodes)
            active_ids = {t.get("challenge_id") or t["task_id"] for t in tasks if t.get("status") in ACTIVE}
            platform = getattr(getattr(session, "plan", None), "platform_profile", None) or {}
            limits = platform.get("limits", {}) if isinstance(platform, dict) else getattr(platform, "limits", {})
            capacity = limits.get("max_active") or (run.get("platform") or {}).get("limits", {}).get("max_active") or audit.get("capacity")
            capacity = int(capacity) if isinstance(capacity, (int, float)) and capacity > 0 else None
            model_pending = list(audit.get("pending", {}).values()) if session.status == "running" else []
            last_result = max(filter(None, [audit.get("last_score"), snapshot.get("last_vuln")]), default=None)
            recent_calls = [r for r in all_rows if now - 3600 <= r["ts"] <= now]
            recent = usage_metrics(recent_calls)
            selected = usage_metrics([r for r in all_rows if start <= r["ts"] <= now])
            if period == "all":
                selected["tokens"] = usage.get("used_tokens", selected["tokens"])
            title = session.display_title()
            events = audit.get("events", [])
            latest = _short(audit.get("progress"))
            if latest.startswith(("{", "```")):
                latest = ""
            if not latest:
                latest = next((e["text"] for e in reversed(events) if e["type"] in {
                    "agent.note", "agent.conclude", "agent.error", "task.start", "model.failed"} and e["text"]), "尚未收到模型进展说明")
            waiting = ("已暂停，等待继续" if session.pause_gate.paused else
                       "等待用户回答" if session.questions.view() else
                       "等待人工审批" if session.approvals.view() else
                       "等待模型规划返回" if not running and model_pending else
                       "等待灵枢下发待发任务" if not running and pending else
                       "暂无在途任务，等待新的执行事件" if not running else "")
            row = {"session_id": sid, "title": title, "mode": session.mode, "status": session.status,
                   "phase": session.phase, "stage": "planning" if not running and any(
                       m.get("task_type") == "meta_planning" for m in model_pending) else session.phase,
                   "paused": session.pause_gate.paused, "project_id": session.project_id,
                   "started_at": session.started_at or session.created_at, "finished_at": session.finished_at,
                   "excluded_seconds": session.pause_gate.excluded_seconds, "tokens": usage.get("used_tokens", 0),
                   "selected": selected, "recent_hour": recent, "latest_progress": latest,
                   "latest_activity": audit.get("last_activity"), "last_result": last_result,
                   "tasks": {"running": running, "pending": pending, "completed": status_counts["done"]},
                   "capacity": {"limit": capacity, "occupied_tasks": len(active_ids) if running else 0,
                                "free": max(0, capacity - len(active_ids)) if capacity and running else capacity,
                                "source": "按独立题目任务统计，不等同于容器数"},
                   "waiting_reason": waiting, "pending_models": len(model_pending),
                   "rounds": run.get("rounds_executed") or max((n.get("round_created", 0) for n in nodes), default=0),
                   "outcomes": {"flags_extracted": flags.get("extracted", flags.get("total", 0)),
                                "flags_submitted": flags.get("submitted", 0), "flags_accepted": flags.get("accepted", 0),
                                "solved": len(progress.get("solved_ids", [])) if "solved_ids" in progress else None,
                                "unsolved": len(progress.get("unsolved_ids", [])) if "unsolved_ids" in progress else None,
                                "score": audit.get("score") if audit.get("score") is not None else run.get("score"),
                                "confirmed_vulns": snapshot["vulns"]},
                   "data_note": problem, "history_complete": audit.get("complete", False)}
            session_rows.append(row)
            for f in snapshot["findings"]:
                findings.append({**f, "session_id": sid, "session_title": title})
            for outcome in audit.get("outcomes", []):
                outcomes.append({**outcome, "session_id": sid, "session_title": title})
            if snapshot.get("last_vuln"):
                outcomes.append({"ts": snapshot["last_vuln"], "kind": "vuln", "label": "确认漏洞", "session_id": sid})
            if session.status != "running" or row["paused"]:
                continue
            def attention(kind: str, text: str, evidence: str, ts: float | None = None) -> None:
                alerts.append({"kind": kind, "session_id": sid, "session_title": title,
                               "text": text, "evidence": evidence, "ts": ts})
            for model in model_pending:
                if now - model["ts"] >= 120:
                    attention("model_wait", "模型调用已等待超过两分钟", f"{model.get('model') or '模型'} · {model.get('task_type') or '决策'} · call_id={model['call_id']}", model["ts"])
            if capacity and row["capacity"]["free"] and pending:
                attention("idle_slot", "有空闲任务名额，仍存在待发节点", f"独立题目任务 {row['capacity']['occupied_tasks']}/{capacity}，待发节点 {pending}；{waiting or '其余任务仍在执行'}", audit.get("last_activity"))
            failures = [e for e in events if e["ts"] >= now - 300 and (e["type"] in {"model.failed", "agent.error"} or (e["type"] == "agent.tool_result" and e["ok"] is False))]
            if len(failures) >= 3:
                attention("failures", "近五分钟多次调用失败", f"近期记录中有 {len(failures)} 次失败；{failures[-1]['text']}", failures[-1]["ts"])
            if last_result and now - last_result >= 1800 and recent["calls"]:
                attention("no_result", "距最近记录成果已超过三十分钟，调用仍有消耗", f"最近一小时 {recent['calls']} 次调用；最近成果 {datetime.fromtimestamp(last_result, BJT).strftime('%m-%d %H:%M')}（北京时间）", last_result)
            if problem:
                attention("data", "部分会话数据暂不可读", problem)
        valid_keys = {str(s.workspace) for s in sessions}
        self._snapshots = {k: v for k, v in self._snapshots.items() if k in valid_keys}
        self._audits = {k: v for k, v in self._audits.items() if str(Path(k).parent) in valid_keys}
        selected_calls = [r for r in calls if start <= r["ts"] <= now]
        windows = {str(m): usage_metrics([r for r in calls if now - m * 60 <= r["ts"] <= now]) for m in (5, 30, 60)}
        groups = {}
        for dimension in ("model", "actor", "session_id"):
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in selected_calls:
                grouped[str(r.get(dimension) or "未记录")].append(r)
            groups[dimension] = [{"name": name, **usage_metrics(rows)} for name, rows in grouped.items()]
        trend_start = start or min((r["ts"] for r in [*calls, *outcomes]), default=today_start)
        step = 300 if period == "hour" else 3600 if period == "today" else 86400
        step *= max(1, math.ceil((now - trend_start) / step / 96))
        # 加上 UTC+8 偏移再取整，确保日桶落在北京时间零点。
        bucket_start = math.floor((trend_start + 28800) / step) * step - 28800
        buckets = []
        grouped_calls: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in selected_calls:
            grouped_calls[int((r["ts"] - bucket_start) // step)].append(r)
        for index in range(max(1, int((now - bucket_start) // step) + 1)):
            ts = bucket_start + index * step
            metrics = usage_metrics(grouped_calls[index])
            buckets.append({"ts": ts, "label": datetime.fromtimestamp(ts, BJT).strftime("%Y-%m-%d" if step >= 86400 else "%m-%d %H:%M"), **metrics,
                            "results": [o for o in outcomes if ts <= o["ts"] < ts + step and start <= o["ts"] <= now]})
        summary = usage_metrics(selected_calls)
        if period == "all":
            summary["tokens"] = total_tokens
        unallocated = max(0, total_tokens - sum(r.get("tokens", 0) for r in calls))
        return {"timezone": "Asia/Shanghai", "unallocated_tokens": unallocated, "generated_at": now, "period": period, "range_start": start,
                "summary": summary, "today": usage_metrics([r for r in calls if today_start <= r["ts"] <= now]),
                "recent_hour": windows["60"], "previous_hour": usage_metrics([r for r in calls if now - 7200 <= r["ts"] < now - 3600]),
                "cache_windows": windows, "cache_groups": groups, "trend": buckets,
                "sessions": session_rows, "alerts": alerts,
                "findings": sorted(findings, key=lambda f: f.get("ts") or 0, reverse=True)[:20],
                "outcomes": sorted(outcomes, key=lambda f: f["ts"], reverse=True)[:20],
                "total_tokens": total_tokens, "total_cost": round(total_cost, 4),
                "total_sessions": len(sessions), "running": sum(s.status == "running" for s in sessions),
                "per_model": dict(per_model), "daily": dict(sorted(daily.items()))}
