"""轻量执行者的重复观察记忆；只提供证据，不封禁动作或决定解题方法。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = (limit - 24) // 2
    return text[:half] + "\n…[部分预览]…\n" + text[-half:]


def result_reference(row: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """旧检查点的编号指向 assistant；只在能确定配对结果时修正发送引用。"""
    index = row.get("message_index")
    if type(index) is not int or not 0 <= index < len(messages):
        return {**row, "message_index": None}
    if messages[index].get("role") == "tool":
        return row
    calls = messages[index].get("tool_calls") or []
    if len(calls) == 1 and index + 1 < len(messages):
        result = messages[index + 1]
        if result.get("role") == "tool" and result.get("tool_call_id") == calls[0].get("id"):
            return {**row, "message_index": index + 1}
    invocation = row.get("invocation_id")
    if invocation:
        for following in range(index + 1, len(messages)):
            message = messages[following]
            if message.get("role") != "tool":
                break
            if str(message.get("content") or "").startswith(f"执行记录 {invocation}\n"):
                return {**row, "message_index": following}
    return {**row, "message_index": None}


def review_row(row: dict[str, Any], *, compact: bool = False) -> dict[str, Any]:
    """发送视图只含紧凑摘要与原文引用，完整观察仍留在状态和执行记录中。"""
    arguments = str(row.get("arguments_preview") or "")
    preview = str(row.get("preview") or "")
    streams = row.get("stream_previews")
    if streams:
        preview = json.dumps({name: _excerpt(text, 160) for name, text in streams.items() if text},
                             ensure_ascii=False)
    if compact and row.get("structured"):
        try:
            data = json.loads(preview)
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("items"), list) and len(data["items"]) > 3:
            items = data.pop("items")
            data.update(items_preview=items[:2] + items[-1:], displayed_items=len(items), partial=True)
            preview = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if not streams and not row.get("structured") and len(preview) > 400:
        preview = preview[:190] + "\n…[部分预览，完整结果按引用回读]…\n" + preview[-180:]
    return {"tool": row.get("name"), "ok": row.get("ok"), "empty": row.get("empty"),
            "arguments_preview": _excerpt(arguments, 300), "result_preview": preview,
            "partial": True, "same_result_count": row.get("same_result_count"),
            "message_index": row.get("message_index"), "invocation_id": row.get("invocation_id"),
            **({"output_file": row["output_file"]} if row.get("output_file") else {})}


def observe(state: dict[str, Any], name: str, arguments: dict[str, Any], outcome: Any) -> dict[str, Any] | None:
    messages = state.get("messages") or []
    message = messages[-1] if messages and messages[-1].get("role") == "tool" else None
    if message is not None and name not in {
            "history_read", "evidence_memory", "note", "blackboard_read", "blackboard_write",
            "exploration_write", "graph_read", "asset_link", "steering_feedback"}:
        message["_light_observation"] = {"tool": name, "ok": outcome.ok,
            "invocation_id": outcome.metadata.get("invocation_id", "")}
    if (message is not None and name in {'history_read', 'evidence_memory', 'record_answer'}
            and (not outcome.ok or name == 'evidence_memory' and outcome.metadata.get('memory_changed') is False)):
        # 这些回执不是新的执行证据，但相同请求/相同失败也要折叠，避免错误提交变成上下文示范。
        # 只标记已经发生的往返；最新回执保留，原始历史不删，也不阻止重新调用。
        message['_context_repeat_key'] = hashlib.sha256(json.dumps(
            [name, arguments, outcome.ok, outcome.text], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if outcome.stop or name in {"history_read", "evidence_memory", "steering_feedback", "ask_user", "record_answer", "tool_lookup",
                               "note", "blackboard_write", "exploration_write", "asset_link"}:
        return None
    if outcome.ok and isinstance(arguments, dict) and arguments.get("action") not in (None, "analysis"):
        # 无标准输出的写入也可能改变现场，不能仅凭输出相同推断没有推进。
        state.pop("last_evidence_request", None)
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
        preview = _excerpt(text, 900)
    request = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False, sort_keys=True)
    request_key = hashlib.sha256(request.encode()).hexdigest()
    previous_request = state.get("last_evidence_request") or {}
    request_count = previous_request.get("count", 0) + 1 if previous_request.get("key") == request_key else 1
    state["last_evidence_request"] = {"key": request_key, "count": request_count}
    key = hashlib.sha256((name + ":" + digest + ":" + str(outcome.ok)).encode()).hexdigest()
    seen = state.setdefault("evidence_seen", {})
    entry = seen.setdefault(key, {"count": 0, "first_request": _excerpt(request, 500)})
    entry["count"] += 1
    rows = state.setdefault("evidence_rows", [])
    # 仅相同请求与可验证相同结果可优先归档；有损展示预览不能证明原始结果相同。
    if message is not None and not (isinstance(observation, dict)
                                   and observation.get("comparison_scope") == "display_preview"):
        result_key = digest if isinstance(observation, dict) and observation.get("comparison_scope") == "raw_streams" else outcome.text
        message["_context_repeat_key"] = hashlib.sha256(
            json.dumps([request, outcome.ok, result_key], ensure_ascii=False).encode()).hexdigest()
    row = {"name": name, "observation_key": key, "arguments_preview": _excerpt(request, 500), "ok": outcome.ok, "empty": empty,
           "turn": state.get("turn", 0),
           "partial": bool(observation.get("partial")) if isinstance(observation, dict) else False,
           "preview": preview, "structured": structured, "same_result_count": entry["count"],
           "first_request": entry["first_request"],
           "message_index": len(messages) - 1 if message is not None else None,
           "tool_call_id": message.get("tool_call_id") if message else None,
           "invocation_id": outcome.metadata.get("invocation_id", ""),
           "output_file": outcome.metadata.get("output_file", ""),
           **({"stream_previews": observation["stream_previews"]}
              if isinstance(observation, dict) and observation.get("stream_previews") else {})}
    entry.setdefault("first_observation", row)
    if message is not None and message.get("_light_observation"):
        message["_light_observation"].update(same_result_count=entry["count"],
            first_message_index=result_reference(entry["first_observation"], messages).get("message_index"))
        message["_light_observation"].update({key: outcome.metadata[key] for key in
            ("output_file", "previous_output_file", "output_rotated", "output_complete",
             "output_sha256", "previous_output_sha256") if key in outcome.metadata})
        message["_light_observation"]["partial"] = row['partial']
    rows.append(row)
    del rows[:-12]
    if entry["count"] < 2 and request_count < 2:
        return None
    if entry["count"] < 2:
        # 日志计数/时间等变化会改变输出；只陈述参数重复，不推断结果相同或阻断合法轮询。
        return {"tool": name, "same_request_count": request_count, "same_result_count": entry["count"],
                "empty": empty, "failed": not outcome.ok,
                "reason": "连续使用相同工具参数，本次返回不同；核对变化和重试依据"}
    return {"tool": name, "same_result_count": entry["count"], "empty": empty,
            "failed": not outcome.ok,
            "reason": "重复空查询" if empty else "重复失败结果" if not outcome.ok else "返回已观察过的相同结果"}
