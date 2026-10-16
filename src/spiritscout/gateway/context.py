"""阶段间上下文投影：保留出处、用户答复和决策约束，减少原文重复灌入。"""

from __future__ import annotations

import copy
import json
from typing import Any

from spiritscout.core.models import BlackboardLayer

# 这些字段可以超过软预算，不能为了缩短输入而丢掉授权、任务或用户回答。
_PROTECTED = {"constraints", "task_intent", "用户答复", "scope", "rules", "mission",
              "target", "available_tactics", "shared_context", "business_context"}


def compact_json(payload: dict[str, Any], max_chars: int = 8000) -> str:
    """按完整字段裁剪观察数据，始终输出合法 JSON；不是执行预算或回合限制。"""
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    result = copy.deepcopy(payload)
    original = len(encode(result))
    if original <= max_chars:
        return encode(result)
    reduced: dict[str, Any] = {}

    def shrink(value: Any) -> tuple[Any, bool]:
        if isinstance(value, str) and len(value) > 1:
            return value[:max(1, len(value) // 2)], True
        if isinstance(value, list) and len(value) > 1:
            return value[:max(1, len(value) // 2)], True
        if isinstance(value, dict):
            for key in sorted(value, key=lambda k: len(encode(value[k])), reverse=True):
                if key in _PROTECTED:
                    continue
                smaller, changed = shrink(value[key])
                if changed:
                    return {**value, key: smaller}, True
        if isinstance(value, list) and value:
            smaller, changed = shrink(value[0])
            if changed:
                return [smaller], True
        return value, False

    # 不删除字典键；嵌套在 situation 内的业务摘要与约束也不可被裁掉。
    while len(encode(result)) > max_chars:
        changed = False
        for key in sorted(result, key=lambda k: len(encode(result[k])), reverse=True):
            if key in _PROTECTED or key == "_context_projection":
                continue
            value = result[key]
            smaller, changed = shrink(value)
            if not changed:
                continue
            reduced.setdefault(key, {"original_chars": len(encode(value)),
                                     **({"original_items": len(value)} if isinstance(value, list) else {})})
            result[key] = smaller
            result["_context_projection"] = {"original_chars": original, "reduced": reduced,
                                             "note": "观察数据为节选；未展示不代表不存在"}
            break
        if not changed:
            break
    return encode(result)


def business_digest(context: dict[str, Any], *, version: int | None = None) -> dict[str, Any]:
    """引用只随实际结论传递；证据原文保留黑板，模型看到来源与必要定位。"""
    if not context:
        return {}
    keys = ("coverage", "ready_for_testing", "target", "observed_pages", "observed_endpoints",
            "recorded_transactions", "har_transactions")
    out = {key: context[key] for key in keys if key in context}
    out["record_id"] = "business-context"
    if version is not None:
        out["version"] = version
    totals = {}
    for key in ("roles", "objects", "workflows", "trust_boundaries", "facts", "hypotheses", "gaps"):
        values = context.get(key) or []
        out[key] = values[:12]
        totals[key] = len(values)
    refs = {str(ref) for key in ("roles", "objects", "workflows", "trust_boundaries", "facts", "hypotheses")
            for item in out[key] if isinstance(item, dict) for ref in item.get("evidence_refs", [])}
    out["evidence_index"] = [
        {key: evidence[key] for key in ("id", "record_id", "kind", "url", "method", "status_code")
         if key in evidence}
        for evidence in context.get("evidence", [])
        if isinstance(evidence, dict) and str(evidence.get("id")) in refs
    ]
    out["totals"] = {**totals, "evidence": len(context.get("evidence", []))}
    out["projection_note"] = "分类为节选；facts 与 hypotheses 分离；evidence_index 仅为引用定位，不是完整请求。"
    return out


def shared_context(blackboard: Any) -> dict[str, Any]:
    """规划、执行与复盘共享同一版本的事实投影，用户回答不再只剩线索类型。"""
    if blackboard is None:
        return {}
    context = blackboard.read_versioned(BlackboardLayer.TOPOLOGY, "business-context")
    out = business_digest(context[0], version=context[1]) if context else {}
    replies = blackboard.query(BlackboardLayer.RECON_CLUE, limit=8,
                               data_filter=("clue_type", "user_reply"))
    if replies:
        out["user_replies"] = [{"record_id": rec["record_id"], "version": rec["version"],
                                "question": rec["data"].get("detail", ""),
                                "answer": rec["data"].get("value", "")}
                               for rec in replies]
    return out
