"""共享事实与规则按需读取；上下文只携带可追溯引用。"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from spiritscout.blackboard import exploration
from spiritscout.core.exceptions import VersionConflict
from spiritscout.core.models import AgentType, BlackboardLayer

GUIDANCE = ("用户规则和共享事实保存在黑板。blackboard_ref 是版本引用；inline_content 若存在，是该版本完整规则原文，可直接遵守，无需重复读取。"
            "仅有引用时，行动前用 blackboard_read 读取适用规则；版本变化后使用新原文或重新读取，按 offset 分段读完，"
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

MEMORY_GUIDANCE = (
    '跨路线复用用 blackboard_write 的 memory_id 稳定标识事实、假设或排除路径，'
    'base_version 初次为0，修订先读当前版本；claim_kind 不代表独立验证。'
    'scope/conditions 限定适用实例、身份与测试条件，revalidate_when 写明重新验证的依据；'
    '复用前核对适用条件，反证或条件变化时修订，不把局部失败推广到整个任务。'
    'origin_assessment 是模型来源判断：generated/mixed/unknown 不自动成为目标线索，'
    '工具成功仅证明执行成功。先追溯原始响应/文件，笔记不作独立证据。'
    '重要变化才更新，无需每轮记录或重复测试；旧结论可用 blackboard_read 的 find/claim_kind 检索。'
)
MEMORY_SCHEMAS = copy.deepcopy(SCHEMAS[:2])
_write = MEMORY_SCHEMAS[1]['function']['parameters']['properties']
_write.update({
    'memory_id': {'type': 'string', 'maxLength': 80, 'description': '会话内稳定且跨 Agent 共享的标识'},
    'base_version': {'type': 'integer', 'minimum': 0, 'description': '初次为0；修订先读 blackboard_ref 当前版本'},
    'claim_kind': {'type': 'string', 'enum': ['fact', 'hypothesis', 'rejected']},
    **{key: {'type': 'string', 'maxLength': 600} for key in ('scope', 'conditions', 'revalidate_when', 'origin_basis')},
    'origin_assessment': {'type': 'string', 'enum': ['target_response', 'user_material', 'generated', 'mixed', 'unknown']},
})
_read = MEMORY_SCHEMAS[0]['function']['parameters']['properties']
_read.update({'find': {'type': 'string', 'maxLength': 200, 'description': '在记录原文中精确检索，非正则'},
              'claim_kind': {'type': 'string', 'enum': ['fact', 'hypothesis', 'rejected']}})
MEMORY_SCHEMAS[1]['function']['description'] += ' 跨 Agent 的长期结论用 memory_id 与版本修订，按 scope/conditions 复用；排除路径写 revalidate_when，来源自评不等于认证。'


def memory_contract(*, readonly: bool = False) -> dict[str, Any]:
    return {'guidance': MEMORY_GUIDANCE, 'tools': [s for s in MEMORY_SCHEMAS
            if not readonly or s['function']['name'] == 'blackboard_read']}


def ensure_memory_contract(state: dict[str, Any], *, readonly: bool = False) -> None:
    """扩展可选参数的说明只追加一次，不替换原有 system 或工具声明。"""
    if state.get('shared_memory_contract_v1'):
        return
    content = '共享记忆扩展契约（框架说明）：' + json.dumps(memory_contract(readonly=readonly),
                                                       ensure_ascii=False, separators=(',', ':'))
    messages = state.setdefault('messages', [])
    if not any(m.get('content') == content for m in messages):
        messages.append({'role': 'user', 'content': content, '_context_contract': True})
    state['shared_memory_contract_v1'] = True


def reference(bb: Any, layer: BlackboardLayer, key: str) -> dict[str, Any]:
    row = bb.read_versioned(layer, key)
    if not row:
        return {}
    text = json.dumps(row[0], ensure_ascii=False)
    result = {"blackboard_ref": {"layer": layer.value, "record_id": key, "version": row[1]},
              "chars": len(text)}
    # 短规则随版本引用完整提供，避免为读取几百字原话多走一次模型决策。
    # 大规则仍按原协议分页，不把截断内容标成完整原文。
    if layer == BlackboardLayer.TARGET_AUTH and len(text) <= 2400:
        result["inline_content"] = row[0]
    return result


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
                ref = item['blackboard_ref']
                current = (reference(bb, BlackboardLayer.TARGET_AUTH, ref['record_id'])
                           if isinstance(ref, dict) and ref.get('layer') == 'target_auth'
                           and ref.get('record_id') else {})
                # 只内联同版本记录，不把旧引用悄悄升级为新版本。
                out[key] = current if current.get('blackboard_ref') == ref else item
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
    memories = query(BlackboardLayer.TOPOLOGY, limit=12, data_filter=('memory_record', 'shared'))
    if isinstance(memories, list):
        rows = list({r['record_id']: r for r in [*rows, *memories]}.values())
    return [{"blackboard_ref": {"layer": "topology", "record_id": row["record_id"], "version": row["version"]},
             "summary": str(row["data"].get("text") or "")[:120],
             "task_id": row["data"].get("task_id", ""), "updated_by": row["updated_by"],
             "evidence_state": row["data"].get("evidence_state", "unverified"),
             **({key: row['data'].get(key, '') for key in ('claim_kind', 'origin_assessment')}
                | {'scope_summary': str(row['data'].get('scope') or '')[:120]}
                if row['data'].get('memory_record') == 'shared' else {})}
            for row in rows]


def _write_memory(bb: Any, args: dict[str, Any], *, actor: str,
                  agent_type: AgentType | None, source: dict[str, Any] | None) -> dict[str, Any]:
    identifier, base = args.get('memory_id'), args.get('base_version')
    claim = args.get('claim_kind')
    origin = args.get('origin_assessment', 'unknown')
    if (not isinstance(identifier, str) or not 1 <= len(identifier.strip()) <= 80
            or type(base) is not int or base < 0 or claim not in ('fact', 'hypothesis', 'rejected')
            or origin not in ('target_response', 'user_material', 'generated', 'mixed', 'unknown')):
        raise ValueError('共享记忆需要稳定 memory_id、base_version、claim_kind 和有效 origin_assessment')
    qualifiers = {key: args.get(key, '') for key in ('scope', 'conditions', 'revalidate_when', 'origin_basis')}
    if any(not isinstance(v, str) or len(v) > 600 for v in qualifiers.values()):
        raise ValueError('适用条件和来源依据须为不超过600字的字符串')
    if not qualifiers['scope'].strip() or not qualifiers['conditions'].strip():
        raise ValueError('共享结论需要 scope 和 conditions；未知条件须明确说明，不可默认全局有效')
    if claim == 'rejected' and not qualifiers['revalidate_when'].strip():
        raise ValueError('排除路径需要 revalidate_when，说明重新检验的依据')
    if origin in ('target_response', 'user_material') and not qualifiers['origin_basis'].strip():
        raise ValueError('目标响应/用户资料判断需要 origin_basis，工具成功不能证明来源')
    key = 'memory-' + hashlib.sha256(identifier.encode()).hexdigest()[:24]
    row = bb.read_versioned(BlackboardLayer.TOPOLOGY, key)
    version = row[1] if row else 0
    if version != base:
        return {'ok': False, 'error': '共享记忆修订冲突，请读取当前版本后更新',
                **reference(bb, BlackboardLayer.TOPOLOGY, key)}
    data = {'kind': 'finding', 'type': 'memory', 'memory_record': 'shared',
            'memory_id': identifier, 'claim_kind': claim, 'text': args['text'],
            'evidence_refs': args['evidence_refs'], 'origin_assessment': origin, **qualifiers}
    if args.get('assessment') is not None:
        if args['assessment'] not in ('conflicting', 'expired') or not str(args.get('assessment_reason') or '').strip():
            raise ValueError('仅可标记 conflicting／expired，并提供 assessment_reason')
        data.update(assessment=args['assessment'], assessment_reason=str(args['assessment_reason']))
    existing = row[0] if row else None
    unchanged = existing is not None and all(existing.get(k) == data.get(k)
                                            for k in set(data) | {'assessment', 'assessment_reason'})
    if not unchanged:
        bb.write(BlackboardLayer.TOPOLOGY, key, {**(source or {}), **data}, updated_by=actor,
                 agent_type=agent_type or AgentType.MASTER, expected_version=base)
    return {'ok': True, 'already_present': unchanged, **reference(bb, BlackboardLayer.TOPOLOGY, key),
            'notice': '共享结论用于复用与定位；origin_assessment 是模型判断，evidence_state 不认证目标来源。'}


def execute(bb: Any, name: str, args: dict[str, Any], *, actor: str,
            agent_type: AgentType | None = None, source: dict[str, Any] | None = None,
            memory_enabled: bool = False) -> dict[str, Any]:
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
                if memory_enabled:
                    find, claim = args.get('find', ''), args.get('claim_kind')
                    if not isinstance(find, str) or len(find) > 200 or claim not in (None, 'fact', 'hypothesis', 'rejected'):
                        raise ValueError('需要有效 find（至多200字符）或 claim_kind')
                    rows = [r for r in rows if (not claim or r['data'].get('claim_kind') == claim)
                            and (not find or find in json.dumps(r['data'], ensure_ascii=False))]
                return {"ok": True, "items": [{"record_id": r["record_id"], "version": r["version"],
                        "kind": r["data"].get("kind", ""), "updated_by": r["updated_by"],
                        **({key: r['data'].get(key, '') for key in ('memory_id', 'claim_kind')}
                           if memory_enabled and r['data'].get('memory_record') == 'shared' else {}),
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
        if memory_enabled and args.keys() & {'memory_id', 'base_version', 'claim_kind', 'scope', 'conditions',
                                             'revalidate_when', 'origin_assessment', 'origin_basis'}:
            return _write_memory(bb, args, actor=actor, agent_type=agent_type, source=source)
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
