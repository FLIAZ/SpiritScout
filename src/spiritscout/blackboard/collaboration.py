"""按任务身份保存协作消息和接力资料；正文按引用读取。"""
from __future__ import annotations

import hashlib
from typing import Any

from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, BlackboardLayer

SCHEMAS = [
    {"type": "function", "function": {
        "name": "team_message",
        "description": "向指定任务发送进展或接力资料。提供已排除项、未决问题和证据引用；投递不表示目标完成或对方已读。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string", "description": "收件任务 ID"},
            "text": {"type": "string", "maxLength": 12000},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        }, "required": ["task_id", "text"]}}},
    {"type": "function", "function": {
        "name": "team_read",
        "description": "查看当前分工和消息目录；提供 message_id 读取完整接力内容并记录已读。先核对已有任务再分工，避免重复。",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}}}}},
]
NAMES = {schema["function"]["name"] for schema in SCHEMAS}


def overview(bb: Any, task_id: str = "", offset: int = 0) -> dict[str, Any]:
    tasks = [row for row in bb.query(BlackboardLayer.TASK_STATE, limit=10000)
             if isinstance(row["data"].get("task"), dict)]
    messages = bb.query(BlackboardLayer.TOPOLOGY, limit=10000,
                        data_filter=("kind", "team_message"))
    findings = bb.query(BlackboardLayer.TOPOLOGY, limit=10000,
                        data_filter=("kind", "finding"))
    waits = bb.query(BlackboardLayer.TOPOLOGY, limit=10000,
                     data_filter=("kind", "team_wait"))
    messages = [row for row in messages if not task_id or row["data"]["to_task"] == task_id]
    return {
        "tasks": [{"task_id": row["record_id"],
                   "agent_id": row["updated_by"],
                   "status": row["data"].get("task", {}).get("status"),
                   "goal": row["data"].get("task", {}).get("payload", {}).get("node_title", ""),
                   "target": row["data"].get("task", {}).get("target", ""),
                   "routes": [{"route_id": route.get("route_id"), "status": route.get("status"),
                               "latest_note": (route.get("notes") or [""])[-1][:180]}
                              for route in row["data"].get("routes", [])[:6]],
                   "blackboard_ref": {"layer": "task_state", "record_id": row["record_id"],
                                      "version": row["version"]}}
                  for row in tasks[offset:offset + 30]],
        "messages": [{"message_id": row["record_id"], "version": row["version"],
                      "from_task": row["data"].get("from_task", ""),
                      "from_route": row["data"].get("from_route", ""),
                      "to_task": row["data"]["to_task"],
                      "read_by": row["data"].get("read_by", []),
                      "summary": row["data"]["text"][:180]}
                     for row in messages[offset:offset + 30]],
        "tasks_total": len(tasks), "messages_total": len(messages),
        "next_offset": offset + 30 if max(len(tasks), len(messages)) > offset + 30 else None,
        "handoffs": [{"record_id": row["record_id"], "version": row["version"],
                       "task_id": row["data"].get("task_id", ""), "agent_id": row["updated_by"],
                       "summary": row["data"].get("text", "")[:180]}
                      for row in findings if row["data"].get("type") in {"note", "conclusion"}][:8],
        "waiting": [row["data"] for row in waits if row["data"].get("status") == "waiting"],
        "notice": "消息为协作者陈述；证据原文按引用读取。任务重叠由模型协商分工；接力消息不表示完成。",
    }


def execute(bb: Any, name: str, args: dict[str, Any], *, actor: str,
            agent_type: AgentType | None, source: dict[str, Any]) -> dict[str, Any]:
    from spiritscout.tools.invocation import current_execution_scope

    scope = current_execution_scope.get()
    task = source.get("task_id") or (scope.task_id if scope else "")
    route = source.get("route_id") or (scope.route_id if scope else "")
    if name == "team_message":
        target, text = args.get("task_id"), args.get("text")
        refs = args.get("evidence_refs", [])
        target_record = bb.read(BlackboardLayer.TASK_STATE, target) if isinstance(target, str) else None
        if not target_record or not isinstance(target_record.get("task"), dict):
            raise ValueError("收件任务不存在；先 team_read 查看任务目录")
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise ValueError("消息正文需要 1–12000 字符")
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            raise ValueError("evidence_refs 需要字符串列表")
        call_id = scope.tool_call_id if scope else ""
        identifier = ("team-" + hashlib.sha256(f"{actor}|{task}|{route}|{call_id}".encode()).hexdigest()
                      if call_id else new_id("team"))
        existing = bb.read(BlackboardLayer.TOPOLOGY, identifier)
        if existing is not None:
            if (existing.get("to_task"), existing.get("text"), existing.get("evidence_refs")) != (target, text, refs):
                raise ValueError("同一调用标识已经用于另一条协作消息")
            return {"ok": True, "message_id": identifier, "delivery": "stored",
                    "read": bool(existing.get("read_by"))}
        bb.write(BlackboardLayer.TOPOLOGY, identifier,
                 {"kind": "team_message", "from_agent": actor, "from_task": task,
                  "from_route": route, "to_task": target, "text": text,
                  "evidence_refs": refs, "read_by": []}, agent_type=agent_type, updated_by=actor,
                 expected_version=0)
        return {"ok": True, "message_id": identifier, "delivery": "stored", "read": False}
    identifier = args.get("message_id")
    if not identifier:
        offset = args.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("offset 需要非负整数")
        return {"ok": True, **overview(bb, task, offset)}
    row = bb.read_versioned(BlackboardLayer.TOPOLOGY, identifier)
    if not row or row[0].get("kind") != "team_message":
        raise ValueError("协作消息不存在")
    data, version = row
    reader = f"{actor}|{task}|{route}"
    if reader not in data.get("read_by", []):
        data = {**data, "read_by": [*data.get("read_by", []), reader]}
        bb.write(BlackboardLayer.TOPOLOGY, identifier, data, expected_version=version,
                 updated_by=actor)
    return {"ok": True, "message_id": identifier, **data}
