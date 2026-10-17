"""阶段间上下文投影：保留出处、用户答复和决策约束，减少原文重复灌入。"""

from __future__ import annotations

import copy
import json
from typing import Any

from spiritscout.core.models import BlackboardLayer

# 这些字段可以超过软预算，不能为了缩短输入而丢掉授权、任务或用户回答。
_PROTECTED = {"constraints", "task_intent", "用户答复", "scope", "rules", "mission",
              "target", "available_tactics", "shared_context", "business_context", "reference_skills"}


def compact_json(payload: dict[str, Any], max_chars: int = 8000) -> str:
    """按完整字段裁剪观察数据，始终输出合法 JSON；不是执行预算或回合限制。"""
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str, sort_keys=True)

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


def blackboard_findings(blackboard: Any) -> dict[str, Any]:
    """本会话实时成果投影：资产/线索/漏洞的计数与抽样（子 Agent 与旁路共用）。

    子 Agent 此前只拿到业务上下文与用户答复，看不到其他分支已探到的资产、
    线索与漏洞，容易重复劳动或在「黑板里没有可用信息」上误判（2026-10-16
    复盘：post_exploit 拿着用户给的账号却报「暂无凭据证据」）。这里只回传
    事实与选样，硬上限防 token；未展示不代表不存在。
    """
    assets = blackboard.query(BlackboardLayer.ASSET, limit=500)
    by_type: dict[str, int] = {}
    samples: dict[str, list[str]] = {"endpoint": [], "service": [], "url": []}
    for rec in assets:
        data = rec["data"]
        atype = str(data.get("asset_type") or "?")
        by_type[atype] = by_type.get(atype, 0) + 1
        value = str(data.get("value") or "")[:160]
        if atype in samples and value and len(samples[atype]) < 10:
            samples[atype].append(value)
    clues = blackboard.query(BlackboardLayer.RECON_CLUE, limit=300)
    clue_types: dict[str, int] = {}
    clue_samples: list[dict[str, str]] = []
    for rec in clues:
        data = rec["data"]
        ctype = str(data.get("clue_type") or "?")
        clue_types[ctype] = clue_types.get(ctype, 0) + 1
        if ctype != "user_reply" and len(clue_samples) < 8:
            clue_samples.append({"type": ctype,
                                 "value": str(data.get("value") or "")[:120]})
    vulns = blackboard.query(BlackboardLayer.VULN, limit=300)
    vuln_status: dict[str, int] = {}
    vuln_samples: list[dict[str, str]] = []
    for rec in vulns:
        data = rec["data"]
        status = str(data.get("status") or "candidate")
        vuln_status[status] = vuln_status.get(status, 0) + 1
        if len(vuln_samples) < 6:
            vuln_samples.append({
                "title": str(data.get("title") or "")[:100],
                "status": status,
                "severity": str(data.get("severity") or "")})
    creds = blackboard.query(BlackboardLayer.CREDENTIAL, limit=200)
    return {
        "assets": {"total": len(assets), "by_type": by_type, "sample": samples},
        "clues": {"total": len(clues), "by_type": clue_types, "sample": clue_samples},
        "vulns": {"total": len(vulns), "by_status": vuln_status,
                  "sample": vuln_samples},
        "credentials": {
            "total": len(creds),
            "sample": [str(r["data"].get("where") or "")[:120] for r in creds[:6]]},
        "note": "本会话黑板实时成果（节选；凭据值已打码，明文见相应原始线索）。",
    }


def shared_context(blackboard: Any) -> dict[str, Any]:
    """规划、执行与复盘共享同一版本的事实投影，用户回答不再只剩线索类型。"""
    if blackboard is None:
        return {}
    context = blackboard.read_versioned(BlackboardLayer.TOPOLOGY, "business-context")
    out = business_digest(context[0], version=context[1]) if context else {}
    replies = blackboard.query(BlackboardLayer.RECON_CLUE, limit=8,
                               data_filter=("clue_type", "user_reply"))
    historical = blackboard.read(BlackboardLayer.TOPOLOGY, "historical-assets")
    if historical:
        out["historical_assets"] = historical
    if replies:
        out["user_replies"] = [{"record_id": rec["record_id"], "version": rec["version"],
                                "question": rec["data"].get("detail", ""),
                                "answer": rec["data"].get("value", "")}
                               for rec in replies]
    findings = blackboard_findings(blackboard)
    if (findings["assets"]["total"] or findings["clues"]["total"]
            or findings["vulns"]["total"] or findings["credentials"]["total"]):
        out["findings"] = findings
    return out


def compact_history(messages: list[dict[str, Any]], max_chars: int = 48000) -> None:
    """按完整消息折叠旧观察；保留系统、任务、最新反馈及工具调用配对。"""
    def size() -> int:
        return len(json.dumps(messages, ensure_ascii=False, default=str))

    if size() <= max_chars:
        return
    recent_users = [i for i, m in enumerate(messages) if m.get("role") == "user"][-2:]
    for i, message in enumerate(messages):
        if size() <= max_chars:
            break
        if i < 2 or i >= len(messages) - 2 or i in recent_users:
            continue
        content = message.get("content")
        if message.get("role") == "system" or not isinstance(content, str) or len(content) < 800:
            continue
        # 只压缩观察正文；函数名、调用 ID 和参数仍完整保留。
        message["content"] = content[:400] + "\n[早期观察已节选；需要细节时查对应执行记录]\n" + content[-250:]


def decision_messages(system: str, payload: dict[str, Any], max_chars: int = 8000) -> list[dict[str, Any]]:
    """契约与重复事实在前，单次观察在后；不扩充无意义前缀来虚增命中率。"""
    dynamic = dict(payload)
    constraints = dynamic.pop("constraints", "")
    # 保留完整 JSON 以兼容决策解析；键顺序按稳定程度排列，不受调用方构造顺序影响。
    projected = json.loads(compact_json(dynamic, max_chars))
    stable_keys = ("available_tactics", "task_intent", "mission", "rules", "target",
                   "execution_recovery", "reference_skills", "business_context", "shared_context")
    ordered = {key: projected.pop(key) for key in stable_keys if key in projected}
    situation = projected.pop("situation", None)
    if isinstance(situation, dict):
        # 业务事实通常不变，计数和活动节点每轮变化；不能把 round/count 放在最前。
        ordered["situation"] = {key: situation[key] for key in sorted(
            situation, key=lambda key: (key not in {"business_context", "historical_assets"}, key))}
    elif situation is not None:
        ordered["situation"] = situation
    ordered.update(projected)
    # 单独的事实块为显式缓存提供边界；变化观察始终追加在末尾。
    stable = {key: ordered.pop(key) for key in stable_keys if key in ordered}
    messages = [{"role": "system", "content": system + ("\n" + constraints if constraints else "")}]
    if stable:
        messages.append({"role": "user", "content": json.dumps(stable, ensure_ascii=False,
                         separators=(",", ":"), default=str)})
    messages.append({"role": "user", "content": json.dumps(ordered, ensure_ascii=False,
                     separators=(",", ":"), default=str)})
    return messages


class DecisionMemory:
    """同阶段追加式决策：完整现场只发送一次，随后发送可还原的数据补丁。"""

    note = (
        "\n同阶段现场以 snapshot 初始化，后续 patch 为 JSON Patch（add/replace/remove、JSON Pointer 路径）。"
        "按顺序更新现场；未改变字段沿用，删除字段不再有效，最新值覆盖旧值。"
        "历史 assistant 是旧决策，不是新增事实；仍需根据当前完整现场独立判断并输出原约定的完整 JSON。"
    )

    def __init__(self, soft_chars: int = 48000) -> None:
        self.soft_chars = soft_chars
        self.messages: list[dict[str, Any]] = []
        self.snapshot: dict[str, Any] = {}
        self.system = ""
        self.pending: tuple[list[dict[str, Any]], dict[str, Any], str] | None = None

    @staticmethod
    def patch(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
        if old == new:
            return []
        if not isinstance(old, dict) or not isinstance(new, dict):
            return [{"op": "replace", "path": path, "value": new}]
        changes = []
        for key in sorted(old.keys() | new.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in new:
                changes.append({"op": "remove", "path": child})
            elif key not in old:
                changes.append({"op": "add", "path": child, "value": new[key]})
            else:
                changes.extend(DecisionMemory.patch(old[key], new[key], child))
        return changes

    def prepare(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        snapshot: dict[str, Any] = {}
        for message in messages:
            if message.get("role") == "user":
                payload = json.loads(message["content"])
                if not isinstance(payload, dict):
                    raise ValueError("决策现场必须为 JSON 对象")
                snapshot.update(payload)
        def serialize(value: Any) -> str:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if self.messages and system == self.system:
            prepared = [*self.messages, {"role": "user", "content": serialize({
                "patch": self.patch(self.snapshot, snapshot)})}]
        else:
            prepared = []
        # 达到上下文软窗口时，用真实最新现场重新建段；不限制回合、不沿用旧授权。
        if not prepared or len(serialize(prepared)) > max(self.soft_chars, len(serialize(snapshot)) * 3):
            prepared = [{"role": "system", "content": system + self.note},
                        {"role": "user", "content": serialize({"snapshot": snapshot})}]
        self.pending = (prepared, copy.deepcopy(snapshot), system)
        return copy.deepcopy(prepared)

    def commit(self, content: str) -> None:
        if self.pending is None:
            return
        messages, self.snapshot, self.system = self.pending
        self.messages = [*messages, {"role": "assistant", "content": content}]
        self.pending = None


def decision_memory(owner: Any, key: str) -> DecisionMemory:
    """记忆只属于当前 Agent；任务/阶段分开，失败不提交，换契约重新建段。"""
    memories = getattr(owner, "_decision_memories", None)
    if memories is None:
        memories = {}
        owner._decision_memories = memories
    if key not in memories:
        if len(memories) >= 128:
            memories.pop(next(iter(memories)))
        memories[key] = DecisionMemory()
    return memories[key]
