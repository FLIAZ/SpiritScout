"""共享事实与规则按需读取；上下文只携带可追溯引用。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from spiritscout.blackboard import exploration
from spiritscout.core.exceptions import VersionConflict
from spiritscout.core.models import AgentType, BlackboardLayer

GUIDANCE = ("用户规则和共享事实保存在黑板。blackboard_ref 是引用而非规则正文；"
            "行动前用 blackboard_read 读取适用规则；版本变化后重新读取，按 offset 分段读完，"
            "不可因未读而忽略约束。需要同伴事实时可用 blackboard_read(layer=topology) 查询目录后按引用读取。事实用 blackboard_write 共享，必须附已有证据引用；"
            "不要登记执行计划、步骤、写文件或调用工具等行为。来源由运行时关联执行台账；unverified 表示依据尚不可核实，supported 仅表示关联了成功执行，不代表语义正确。可用 assessment 标记冲突或过期并说明原因；只有独立复测能提升验证状态。工具返回 reuse／source_invocation_id 表示复用已有观察，不能当作新鲜请求或独立证据；时序对照、状态变化后需新响应时用 refresh:true。GET 也可能改变现场，按实际 action 声明。")
SCHEMAS = [
    {"type": "function", "function": {"name": "blackboard_read", "description": "按引用读取规则/事实；省略 record_id 列出元数据目录。",
     "parameters": {"type": "object", "properties": {
         "layer": {"type": "string", "enum": [x.value for x in BlackboardLayer]},
         "record_id": {"type": "string"}, "projection_digest": {"type": "string", "description": "分段读取时沿用上页摘要，避免拼接来源状态不同的内容"}, "version": {"type": "integer", "minimum": 1}, "offset": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 12000}}, "required": ["layer"]}}},
    {"type": "function", "function": {"name": "blackboard_write", "description": "发布有依据的产出事实，保留来源；不写执行步骤或用户规则。",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string"}, "evidence_refs": {"type": "array", "items": {"type": "string"}},
         "parent_finding_id": {"type": "string"},
         "asset_refs": {"type": "array", "items": {"type": "string"}},
         "exploration_refs": {"type": "array", "items": {"type": "string"}},
         "assessment": {"type": "string", "enum": ["conflicting", "expired"]},
         "assessment_reason": {"type": "string"}}, "required": ["text", "evidence_refs"]}}},
]
NAMES = {s["function"]["name"] for s in SCHEMAS}
SCHEMAS.extend(exploration.SCHEMAS)
NAMES.update(exploration.NAMES)
GUIDANCE += exploration.GUIDANCE


def reference(bb: Any, layer: BlackboardLayer, key: str) -> dict[str, Any]:
    row = bb.read_versioned(layer, key)
    if not row:
        return {}
    return {"blackboard_ref": {"layer": layer.value, "record_id": key, "version": row[1]},
            "chars": len(json.dumps(row[0], ensure_ascii=False))}


def store_rules(bb: Any, key: str, value: Any) -> dict[str, Any]:
    data = {"kind": "rule", "content": value}
    if bb.read(BlackboardLayer.TARGET_AUTH, key) != data:
        bb.write(BlackboardLayer.TARGET_AUTH, key, data, updated_by="user-rules")
    return reference(bb, BlackboardLayer.TARGET_AUTH, key)


def project_rules(bb: Any, value: Any) -> Any:
    """只外置明确的规则字段，原文无损落盘；不分析规则含义。"""
    if bb is None:
        return value
    if isinstance(value, list):
        return [project_rules(bb, item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        # 观察、候选和工具正文不能因含同名键而被提升成用户规则。
        if key in {"candidate", "evidence", "observations", "facts", "hypotheses", "search_observations",
                   "skill_observations", "skill_context", "draft", "advice", "records", "platform_facts", "platform_handoff"}:
            out[key] = item
        elif key in {"rules", "standing_rules", "testing_rules", "project_rules", "user_context", "user_replies", "user_rules"} and item:
            if key == "user_context" and bb.read(BlackboardLayer.TARGET_AUTH, "user-context") == item:
                out[key] = reference(bb, BlackboardLayer.TARGET_AUTH, "user-context")
            elif isinstance(item, dict) and "blackboard_ref" in item:
                out[key] = item
            else:
                digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
                out[key] = store_rules(bb, f"rules-{digest}", item)
        elif key == "project_memory" and isinstance(item, dict):
            out[key] = project_rules(bb, {**item, "project_rules": item.get("description", "")})
            out[key].pop("description", None)
        else:
            out[key] = project_rules(bb, item)
    return out


def fact_index(bb: Any) -> list[dict[str, Any]]:
    """下一决策点只通知同伴的近期事实引用，不广播整张图或原始证据。"""
    query = getattr(bb, "query", None)
    if not callable(query):
        return []
    rows = query(BlackboardLayer.TOPOLOGY, limit=12, data_filter=("kind", "finding"))
    if not isinstance(rows, list):
        return []
    return [{"blackboard_ref": {"layer": "topology", "record_id": row["record_id"], "version": row["version"]},
             "summary": str(row["data"].get("text") or "")[:120],
             "task_id": row["data"].get("task_id", ""), "updated_by": row["updated_by"],
             "evidence_state": row["data"].get("evidence_state", "unverified")}
            for row in rows]


def execute(bb: Any, name: str, args: dict[str, Any], *, actor: str,
            agent_type: AgentType | None = None, source: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if not isinstance(args, dict):
            raise ValueError("黑板工具参数必须为对象")
        if bb is None:
            raise ValueError("当前会话没有黑板")
        if name in exploration.NAMES:
            return exploration.execute(bb, name, args, actor=actor, agent_type=agent_type, source=source)
        if name == "blackboard_read":
            layer = BlackboardLayer(args.get("layer"))
            key = args.get("record_id")
            offset, limit = args.get("offset", 0), args.get("limit", 4000)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
                raise ValueError("offset≥0，limit 为 1–12000 的整数")
            if not key:
                rows = bb.query(layer, limit=10000)
                return {"ok": True, "items": [{"record_id": r["record_id"], "version": r["version"],
                        "kind": r["data"].get("kind", ""), "updated_by": r["updated_by"],
                        "summary": str(r["data"].get("text") or r["data"].get("title") or r["data"].get("value") or "")[:160]}
                        for r in rows[offset:offset + min(limit, 50)]], "total": len(rows),
                        "next_offset": offset + min(limit, 50), "has_more": offset + min(limit, 50) < len(rows)}
            row = bb.read_versioned(layer, key)
            if not row:
                raise ValueError("黑板记录不存在，请重新查询目录")
            ref = {"layer": layer.value, "record_id": key, "version": row[1]}
            if args.get("version") is not None and args["version"] != row[1]:
                return {"ok": False, "error": "记录版本已更新，请从 offset=0 重新读取", "blackboard_ref": ref}
            text = json.dumps(row[0], ensure_ascii=False)
            projection_digest = hashlib.sha256(text.encode()).hexdigest()
            if args.get("projection_digest") and args["projection_digest"] != projection_digest:
                return {"ok": False, "error": "来源或证据状态已变化，请从 offset=0 重新读取", "blackboard_ref": ref,
                        "projection_digest": projection_digest}
            return {"ok": True, "blackboard_ref": ref, "chars": len(text), "version": row[1], "projection_digest": projection_digest,
                    "content": text[offset:offset + limit], "next_offset": offset + limit,
                    "has_more": offset + limit < len(text)}
        if agent_type in {AgentType.REPORT, AgentType.VERIFIER}:
            raise ValueError("当前角色只读黑板")
        if name != "blackboard_write":
            raise ValueError("未知黑板工具")
        text, refs = args.get("text"), args.get("evidence_refs")
        if not isinstance(text, str) or not text.strip() or not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r.strip() for r in refs):
            raise ValueError("事实需要 text 和非空 evidence_refs，引用已有工具执行、文件或黑板记录")
        key = "fact-" + hashlib.sha256(json.dumps([actor, source, text, refs], sort_keys=True).encode()).hexdigest()[:24]
        data = {**(source or {}), "kind": "finding", "type": "fact", "text": text,
                "evidence_refs": refs, "agent": agent_type.value if agent_type else actor,
                "parent_finding_id": str(args.get("parent_finding_id") or "")}
        for field, constraint in (('asset_refs', {'asset': True}), ('exploration_refs', {'exploration': True})):
            if field in args:
                data[field] = exploration.refs(bb, args[field], **constraint)
        assessment = args.get("assessment")
        if assessment is not None:
            if assessment not in {"conflicting", "expired"} or not str(args.get("assessment_reason") or "").strip():
                raise ValueError("仅可标记 conflicting／expired，并提供 assessment_reason")
            data.update(assessment=assessment, assessment_reason=str(args["assessment_reason"]))
        existing = bb.read(BlackboardLayer.TOPOLOGY, key)
        # 同一事实可被多个探索分支复用；后续发布不能抹掉先前的图关联。
        for field in ('asset_refs', 'exploration_refs'):
            if field in data and existing:
                data[field] = list(dict.fromkeys(existing.get(field, []) + data[field]))
        compared = set(data) | {"assessment", "assessment_reason"}
        already_present = bool(existing is not None and all(existing.get(field) == data.get(field) for field in compared))
        if not already_present:
            bb.write(BlackboardLayer.TOPOLOGY, key, data, updated_by=actor, agent_type=agent_type or AgentType.MASTER)
        stored = bb.read(BlackboardLayer.TOPOLOGY, key) or {}
        return {"ok": True, **reference(bb, BlackboardLayer.TOPOLOGY, key),
                "evidence_state": stored.get("evidence_state", "unverified"), "already_present": already_present,
                "unresolved_refs": [item["ref"] for item in stored.get("provenance", {}).get("sources", [])
                                    if not item.get("resolved")]}
    except (ValueError, TypeError, VersionConflict) as exc:
        return {"ok": False, "error": str(exc)}
