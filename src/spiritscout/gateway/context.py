"""阶段间上下文投影：保留出处、用户答复和决策约束，减少原文重复灌入。"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

from spiritscout.core.models import BlackboardLayer


def estimate_input_tokens(messages: list[dict[str, Any]],
                          tools: list[dict[str, Any]] | None = None) -> int:
    """离线保守估算文本、思考和工具声明；不是供应商计费 token 数。

    ASCII 按三字符一 token，非 ASCII 按 UTF-8 字节数的一半估算，
    另留协议开销。无需下载分词器；窗口压力判断使用估值，账单只用服务端用量。
    """
    text = json.dumps({"messages": messages, "tools": tools or []},
                      ensure_ascii=False, separators=(",", ":"), default=str)
    ascii_chars = sum(character.isascii() for character in text)
    non_ascii_bytes = len(text.encode("utf-8")) - ascii_chars
    return math.ceil(ascii_chars / 3 + non_ascii_bytes / 2) + len(messages) * 8 + 32


def input_window_budget(context_window: int, reserved_output: int = 0) -> int | None:
    """按声明容量留出输出和整理空间；未知容量不伪装成某模型的真实窗口。"""
    if context_window <= 0:
        return None
    reserve = min(max(reserved_output, 4096), context_window // 2)
    headroom = min(65536, context_window // 10)
    return max(1, min(int(context_window * 0.8), context_window - reserve - headroom))

# 这些字段可以超过软预算，不能为了缩短输入而丢掉授权、任务或用户回答。
_PROTECTED = {"constraints", "task_intent", "用户答复", "scope", "rules", "mission",
              "target", "available_tactics", "shared_context", "business_context",
              "reference_skills", "world_state", "project_rules", "user_context",
              "authorization", "testing_rules", "user_replies",
              "competition_context", "platform_facts", "灵枢当前指令",
              "active_tasks", "steering_status", "competition_status"}


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
    """汇总资产、线索和漏洞的数量与样本，供子 Agent 和旁路会话使用。

    各类记录有读取与抽样上限；样本不包含全部黑板数据。"""
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
    project_memory = blackboard.read(BlackboardLayer.TOPOLOGY, "project-memory")
    if project_memory:
        out["project_memory"] = project_memory
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
    """只节选工具/助手旧观察；用户声明完整保留，维持工具调用配对。"""
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
        if (message.get("_context_contract") or message.get("role") not in {"assistant", "tool"}
                or not isinstance(content, str) or len(content) < 800):
            continue
        # 只压缩观察正文；函数名、调用 ID 和参数仍完整保留。
        message["content"] = content[:400] + "\n[早期观察已节选；需要细节时查对应执行记录]\n" + content[-250:]


def compact_history_tokens(messages: list[dict[str, Any]], max_tokens: int, *,
                           tools: list[dict[str, Any]] | None = None) -> bool:
    """真正有窗口压力才整理旧观察，工具声明/参数/思考也计入输入。

    保留末尾完整工具往返、用户声明及契约。无法缩小到阈值时诚实交给调用端，
    不删除授权或拆散工具调用；返回值只报告是否实际改写过历史。
    """
    if estimate_input_tokens(messages, tools) <= max_tokens:
        return False
    # 高水位触发后整理到低水位，避免每新增一条结果就再次破坏缓存前缀。
    target_tokens = max(1, int(max_tokens * 0.6))
    last_assistant = next((i for i in range(len(messages) - 1, -1, -1)
                           if messages[i].get("role") == "assistant"), len(messages))
    changed = False
    for index, message in enumerate(messages):
        if index < 2 or index >= last_assistant or message.get("_context_contract"):
            continue
        content = message.get("content")
        if (message.get("role") not in {"assistant", "tool"}
                or not isinstance(content, str) or len(content) <= 800):
            continue
        message["content"] = (content[:400]
                              + "\n[早期观察已节选；完整内容见执行记录]\n" + content[-250:])
        changed = True
        if estimate_input_tokens(messages, tools) <= target_tokens:
            break
    return changed


def decision_messages(system: str, payload: dict[str, Any], max_chars: int = 8000) -> list[dict[str, Any]]:
    """契约与重复事实在前，单次观察在后；不扩充无意义前缀来虚增命中率。"""
    dynamic = dict(payload)
    constraints = dynamic.pop("constraints", "")
    # 保留完整 JSON 以兼容决策解析；键顺序按稳定程度排列，不受调用方构造顺序影响。
    projected = json.loads(compact_json(dynamic, max_chars))
    stable_keys = ("world_state", "available_tactics", "task_intent", "mission",
                   "rules", "target", "execution_recovery", "reference_skills",
                   "business_context", "shared_context")
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


def append_instruction_events(state: dict[str, Any], records: list[dict[str, Any]], *,
                              consumer_key: str) -> None:
    """指令及回执按变化追加，已发出的请求前缀不因下一轮临时注入而改变。"""
    previous = state.get("instruction_state") or {}
    current = {}
    for record in records:
        current[record["id"]] = {
            k: record[k] for k in ("id", "agent", "task_id", "hint", "rationale") if k in record}
        feedback = (record.get("feedback") or {}).get(consumer_key)
        if feedback:
            current[record["id"]]["feedback"] = dict(feedback)
    changed = [v for k, v in current.items() if previous.get(k) != v]
    removed = sorted(previous.keys() - current.keys())
    if changed or removed:
        state["messages"].append({"role": "user", "content":
            "灵枢任务指令事件：同一 ID 以最新状态为准，失效 ID 不再执行；"
            "completed 表示已回报完成，不再重复执行；未变化的其他指令继续有效。"
            "优先调整实际操作，用 steering_feedback "
            "回报计划、完成证据或受阻原因，实际操作遵守用户授权与比赛规则。\n"
            + json.dumps({"灵枢当前指令": changed, "失效指令": removed},
                         ensure_ascii=False, separators=(",", ":"), sort_keys=True)})
    state["instruction_state"] = current


class DecisionMemory:
    """同阶段追加式决策：完整现场只发送一次，随后发送可还原的数据补丁。"""

    note = (
        "\n同阶段现场以 snapshot 初始化，后续 patch 为 JSON Patch（add/replace/remove、JSON Pointer 路径）。"
        "按顺序更新现场；未改变字段沿用，删除字段不再有效，最新值覆盖旧值。"
        "历史 assistant 是旧决策，不是新增事实；仍需根据当前完整现场独立判断并输出原约定的完整 JSON。"
    )

    def __init__(self, soft_chars: int = 256000) -> None:
        self.soft_chars = soft_chars
        self.messages: list[dict[str, Any]] = []
        self.snapshot: dict[str, Any] = {}
        self.system = ""
        self.pending: tuple[list[dict[str, Any]], dict[str, Any], str] | None = None
        self.segment_changed = False
        self.segment_info: dict[str, Any] = {}
        self.journal: Any = None
        self.checkpoint_key = ""

    @staticmethod
    def _same_value(old: Any, new: Any) -> bool:
        """JSON 类型也属于事实；Python 的 True == 1 不能吞掉现场变化。"""
        if type(old) is not type(new):
            return False
        if isinstance(old, dict):
            return old.keys() == new.keys() and all(
                DecisionMemory._same_value(value, new[key]) for key, value in old.items())
        if isinstance(old, list):
            return len(old) == len(new) and all(
                DecisionMemory._same_value(a, b) for a, b in zip(old, new))
        return old == new

    @staticmethod
    def patch(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
        if DecisionMemory._same_value(old, new):
            return []
        if isinstance(old, list) and isinstance(new, list):
            # 数组追加或某一项状态变化不应重发整份历史。
            changes = []
            for index in range(min(len(old), len(new))):
                changes.extend(DecisionMemory.patch(old[index], new[index], path + f"/{index}"))
            changes.extend({"op": "remove", "path": path + f"/{index}"}
                           for index in range(len(old) - 1, len(new) - 1, -1))
            changes.extend({"op": "add", "path": path + "/-", "value": value}
                           for value in new[len(old):])
            return changes
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

    @staticmethod
    def compact_patch(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
        """无损选择增量表达：只缩短新消息，不修改历史、原文或字段含义。

        少量变更沿用原补丁；密集变化可用子树整值替换，仍是原契约支持的
        JSON Patch。只有编码字节至少减少两成且 token 估值不增加才采用。
        """
        regular = DecisionMemory.patch(old, new, path)
        if len(regular) < 8:
            return regular
        compact: list[dict[str, Any]] = []
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                child = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key in old and key in new:
                    compact.extend(DecisionMemory.compact_patch(old[key], new[key], child))
                elif key not in new:
                    compact.append({"op": "remove", "path": child})
                else:
                    compact.append({"op": "add", "path": child, "value": new[key]})
        elif isinstance(old, list) and isinstance(new, list):
            for index in range(min(len(old), len(new))):
                compact.extend(DecisionMemory.compact_patch(old[index], new[index], path + f"/{index}"))
            compact.extend({"op": "remove", "path": path + f"/{index}"}
                           for index in range(len(old) - 1, len(new) - 1, -1))
            compact.extend({"op": "add", "path": path + "/-", "value": value}
                           for value in new[len(old):])
        else:
            return regular

        def encode(operations: list[dict[str, Any]]) -> str:
            return json.dumps({"patch": operations}, ensure_ascii=False,
                              separators=(",", ":"), sort_keys=True)

        baseline = encode(regular)
        baseline_bytes = len(baseline.encode())
        baseline_tokens = estimate_input_tokens([{"role": "user", "content": baseline}])
        best, best_bytes = regular, baseline_bytes
        for candidate in (compact, [{"op": "replace", "path": path, "value": new}]):
            content = encode(candidate)
            size = len(content.encode())
            # 大证据整块替换通常更长，先按字节淘汰，不再做逐字符 token 估算。
            if size * 5 > baseline_bytes * 4 or size >= best_bytes:
                continue
            tokens = estimate_input_tokens([{"role": "user", "content": content}])
            if tokens <= baseline_tokens:
                best, best_bytes = candidate, size
        return best

    def prepare(self, messages: list[dict[str, Any]], *,
                max_input_tokens: int | None = None,
                compact_patches: bool = False,
                stable_snapshot_prefix: bool = False) -> list[dict[str, Any]]:
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
                "patch": (self.compact_patch(self.snapshot, snapshot) if compact_patches
                          else self.patch(self.snapshot, snapshot))})}]
        else:
            prepared = []
        # 达到上下文软窗口时，用真实最新现场重新建段；不限制回合、不沿用旧授权。
        pressure = (estimate_input_tokens(prepared) > max_input_tokens
                    if max_input_tokens is not None else len(serialize(prepared)) > max(
                        self.soft_chars, len(serialize(snapshot)) * 3))
        self.segment_changed = not prepared or pressure
        if self.segment_changed:
            self.segment_info = {"reason": "initial" if not self.messages else
                                 "contract_changed" if system != self.system else "context_window",
                                 "snapshot_chars": len(serialize(snapshot)),
                                 "previous_chars": len(serialize(self.messages)),
                                 "estimated_input_tokens": estimate_input_tokens(prepared),
                                 "max_input_tokens": max_input_tokens}
            if stable_snapshot_prefix:
                # 只布局新建段；已提交的历史（含旧版本检查点）绝不重新排序。
                stable = ("world_state", "available_tactics", "task_intent", "mission",
                          "rules", "target", "execution_recovery", "reference_skills",
                          "business_context", "shared_context", "project_rules")
                canonical = json.loads(serialize(snapshot))
                ordered = {key: canonical.pop(key) for key in stable if key in canonical}
                ordered.update(canonical)
                initial = json.dumps({"snapshot": ordered}, ensure_ascii=False, separators=(",", ":"))
            else:
                initial = serialize({"snapshot": snapshot})
            prepared = [{"role": "system", "content": system + self.note},
                        {"role": "user", "content": initial}]
        self.pending = (prepared, copy.deepcopy(snapshot), system)
        return copy.deepcopy(prepared)

    def commit(self, content: str, *, reasoning: str = "") -> None:
        if self.pending is None:
            return
        messages, self.snapshot, self.system = self.pending
        self.messages = copy.deepcopy([*messages, {"role": "assistant", "content": content,
                                     **({"reasoning_content": reasoning} if reasoning else {})}])
        self.pending = None
        if self.journal is not None:
            self.journal.checkpoint("decision_memory", self.checkpoint_key, "committed", {
                "messages": self.messages, "snapshot": self.snapshot, "system": self.system})


def decision_memory(owner: Any, key: str) -> DecisionMemory:
    """记忆只属于当前 Agent；任务/阶段分开，失败不提交，换契约重新建段。"""
    memories = getattr(owner, "_decision_memories", None)
    if memories is None:
        memories = {}
        owner._decision_memories = memories
    if key not in memories:
        journal = getattr(getattr(owner, "ctx", None), "journal", None)
        memory = DecisionMemory()
        if callable(getattr(journal, "latest_checkpoint", None)):
            # 会话数据库隔离会话；类型隔离 Agent，key 已含任务/路线/阶段。
            checkpoint_key = f"{type(owner).__name__}:{key}"
            saved = journal.latest_checkpoint("decision_memory", checkpoint_key)
            state = saved.get("state") if isinstance(saved, dict) else None
            if (isinstance(state, dict) and isinstance(state.get("messages"), list)
                    and isinstance(state.get("snapshot"), dict) and isinstance(state.get("system"), str)):
                memory.messages = copy.deepcopy(state["messages"])
                memory.snapshot = copy.deepcopy(state["snapshot"])
                memory.system = state["system"]
            memory.journal, memory.checkpoint_key = journal, checkpoint_key
            # 只淘汰已提交且可从磁盘恢复的记忆；无持久化时不能悄悄丢上下文。
            if len(memories) >= 128:
                victim = next((k for k, m in memories.items()
                               if m.pending is None and m.journal is not None), None)
                if victim is not None:
                    memories.pop(victim)
        memories[key] = memory
    memory = memories.pop(key)
    memories[key] = memory
    return memory
