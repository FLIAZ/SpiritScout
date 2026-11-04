"""轻量执行者的重复观察记忆；只提供证据，不封禁动作或决定解题方法。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def review_row(row: dict[str, Any]) -> dict[str, Any]:
    """发送视图只含紧凑摘要与原文引用，完整观察仍留在状态和执行记录中。"""
    arguments = str(row.get("arguments_preview") or "")
    preview = str(row.get("preview") or "")
    return {"tool": row.get("name"), "ok": row.get("ok"), "empty": row.get("empty"),
            "arguments_preview": arguments[:300], "result_preview": preview if row.get("structured") else preview[:400],
            "partial": True, "same_result_count": row.get("same_result_count"),
            "message_index": row.get("message_index"), "invocation_id": row.get("invocation_id")}


def observe(state: dict[str, Any], name: str, arguments: dict[str, Any], outcome: Any) -> dict[str, Any] | None:
    if outcome.stop or name in {"history_read", "steering_feedback", "ask_user"}:
        return None
    if outcome.ok and isinstance(arguments, dict) and arguments.get("action") not in (None, "analysis"):
        # 无标准输出的写入也可能改变现场，不能仅凭输出相同推断没有推进。
        return None
    observation = outcome.metadata.get("observation")
    empty = False
    structured = False
    if isinstance(observation, dict) and isinstance(observation.get("digest"), str):
        digest = observation["digest"]
        preview = observation.get("preview", "")
        empty = observation.get("empty", False)
        structured = observation.get("structured", False)
    else:
        text = str(outcome.text or "")
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            # 只识别查询接口的空集合，不把空结果当作没有执行或没有价值。
            empty = (("items" in data and data["items"] == [])
                     or (name == "blackboard_read" and "data" in data and data["data"] is None))
            text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(text.encode()).hexdigest()
        preview = text[:900]
    request = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False, sort_keys=True)
    key = hashlib.sha256((name + ":" + digest + ":" + str(outcome.ok)).encode()).hexdigest()
    seen = state.setdefault("evidence_seen", {})
    entry = seen.setdefault(key, {"count": 0, "first_request": request[:500]})
    entry["count"] += 1
    rows = state.setdefault("evidence_rows", [])
    row = {"name": name, "arguments_preview": request[:500], "ok": outcome.ok, "empty": empty,
           "preview": preview, "structured": structured, "same_result_count": entry["count"],
           "first_request": entry["first_request"],
           "message_index": max(0, len(state.get("messages") or []) - 1),
           "invocation_id": outcome.metadata.get("invocation_id", "")}
    entry.setdefault("first_observation", row)
    rows.append(row)
    del rows[:-12]
    if entry["count"] < 2:
        return None
    return {"tool": name, "same_result_count": entry["count"], "empty": empty,
            "failed": not outcome.ok,
            "reason": "重复空查询" if empty else "重复失败结果" if not outcome.ok else "返回已观察过的相同结果"}
