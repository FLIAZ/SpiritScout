"""控制台通知只读视图：只展示已保存的判定，不提取或重新判断答案。"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from spiritscout.gateway.usage import file_version
from spiritscout.web.dashboard import timestamp

MAX_ITEMS = 60
CACHE_FILES = 128


def _text(value: Any, limit: int = 240) -> str:
    return str(value or "")[:limit]


def _event_id(kind: str, *parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()
    return kind + ":" + hashlib.sha256(raw).hexdigest()


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return default


def _outcomes(flags: Any, completions: Any, ts: float | None) -> dict[str, Any]:
    flags = [row for row in flags if isinstance(row, dict)] if isinstance(flags, list) else []
    events: dict[str, dict[str, Any]] = {}
    for row in flags:
        if row.get("accepted") is not True:
            continue
        challenge = _text(row.get("challenge_id"))
        identity = row.get("flag_id") or _event_id("stored-flag", challenge, row.get("value"))
        key = _event_id("flag_accepted", identity)
        events[key] = {"id": key, "type": "flag_accepted", "challenge_id": challenge,
                       "title": "Flag 已通过", "message": challenge or "已保存平台接受结果",
                       "ts": ts, "time_source": "file_modified"}
    if isinstance(completions, dict):
        for challenge, row in completions.items():
            if not isinstance(row, dict) or row.get("completed") is not True:
                continue
            ids = row.get("answer_ids")
            if (not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids)
                    or not str(row.get("reason") or "").strip()):
                continue
            # 只核对持久化确认仍指向当前登记集合；不判断答案内容或证据好坏。
            disputed = row.get("disputed_answer_ids") or []
            if not isinstance(disputed, list):
                continue
            current = {f.get("flag_id") for f in flags if f.get("challenge_id") == challenge
                       and f.get("flag_id") and f.get("flag_id") not in disputed}
            if set(ids) != current:
                continue
            key = _event_id("local_answer_completion", challenge, sorted(set(ids)))
            events[key] = {"id": key, "type": "local_answer_completion",
                           "challenge_id": _text(challenge), "title": "本地答案已确认完成",
                           "message": _text(row.get("reason")), "ts": ts,
                           "time_source": "file_modified"}
    # 每文件只缓存响应上限的精简事件，保留未裁剪计数，不缓存 Flag 原文。
    return {"items": sorted(events.values(), key=lambda e: e["id"])[:MAX_ITEMS], "total": len(events)}


class NotificationReader:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[Any, dict[str, Any]]] = OrderedDict()

    def _stored(self, path: Path, fallback: Any) -> dict[str, Any]:
        completion = path.with_suffix(".completion.json")
        versions = file_version(path), file_version(completion)
        key = str(path.resolve())
        fallback_version = _event_id("summary", fallback) if not versions[1] else ""
        version = (*versions, fallback_version)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] == version:
                self._cache.move_to_end(key)
                return cached[1]
            flags = _json(path, [])
            # 已存在的完成文件（包括撤回、空文件或损坏文件）优先，不能复活旧摘要。
            confirmations = _json(completion, {}) if versions[1] else fallback
            modified = max((v[3] / 1e9 for v in versions if v), default=None)
            result = _outcomes(flags, confirmations, modified)
            self._cache[key] = (version, result)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_FILES:
                self._cache.popitem(last=False)
            return result

    def read(self, sessions: list[Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        total = 0
        seen_sessions: set[str] = set()
        for session in sessions:
            sid = str(session.session_id)
            if sid in seen_sessions:
                continue
            seen_sessions.add(sid)
            summary = session.summary if isinstance(session.summary, dict) else {}
            run = summary.get("run") or {}
            fallback = run.get("local_answer_completion", {}) if isinstance(run, dict) else {}
            outcomes = self._stored(Path(session.flags_path), fallback)
            total += outcomes["total"]
            for event in outcomes["items"]:
                items.append({**event, "id": _event_id(event["type"], sid, event["id"]),
                              "session_id": sid, "session_title": _text(session.display_title())})
            questions = {str(q.get("question_id")): q for q in session.questions.view()
                         if isinstance(q, dict) and q.get("question_id")}
            total += len(questions)
            for qid, question in questions.items():
                items.append({"id": _event_id("question_pending", sid, qid),
                              "type": "question_pending", "session_id": sid,
                              "session_title": _text(session.display_title()), "question_id": qid,
                              "title": "等待你的回答", "message": _text(question.get("question"), 500),
                              "ts": timestamp(question.get("requested_at")), "time_source": "requested_at"})
        items.sort(key=lambda e: (e["type"] == "question_pending", e["ts"] or 0, e["id"]), reverse=True)
        return {"items": items[:MAX_ITEMS], "total": total, "truncated": total > MAX_ITEMS}
