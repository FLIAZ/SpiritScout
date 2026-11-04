"""轻量会话请求投影：归档完整工具往返，保留原始历史和当前契约。"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from spiritscout.gateway.context import estimate_input_tokens
from spiritscout.gateway.world_state import render


class ContextWindowError(ValueError):
    """保留的当前契约本身超窗；不发送必然耗尽输出空间的请求。"""


@dataclass
class ContextView:
    messages: list[dict[str, Any]]
    estimated_tokens: int
    archived_turns: int


def _tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    return estimate_input_tokens([{k: v for k, v in m.items() if not k.startswith("_")}
                                  for m in messages], tools)


def _instructions(records: list[dict[str, Any]], *, compact: bool = False) -> str:
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        # 同一指令的多个编号保留，不能用内容去重丢掉反馈目标。
        key = json.dumps({k: record.get(k) for k in ("agent", "task_id", "hint", "rationale")},
                         sort_keys=True, ensure_ascii=False)
        item = grouped.setdefault(key, {k: copy.deepcopy(record[k]) for k in
            ("agent", "task_id", "hint", "rationale") if k in record})
        item.setdefault("records", []).append({k: copy.deepcopy(record[k]) for k in
            ("id", "feedback") if k in record})
    values = list(grouped.values())
    if compact:
        values = [{**{k: value[k] for k in ("agent", "task_id", "records") if k in value},
                   "hint_preview": str(value.get("hint") or "")[:160], "partial": True}
                  for value in values[:-1]] + values[-1:]
    return ("当前灵枢指令与回执（partial 的前期指令只有摘要，行动前用 history_read 的 instruction_id 读原文）：\n"
            + json.dumps(values, ensure_ascii=False, separators=(",", ":")))


def project_context(messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                    budget: int, *, world: dict[str, Any] | None = None,
                    instructions: list[dict[str, Any]] | None = None) -> ContextView:
    """只返回分离的发送视图；不改写原始历史、不拆工具配对、不节选用户声明。"""
    if _tokens(messages, tools) <= budget:
        return ContextView(copy.deepcopy(messages), _tokens(messages, tools), 0)
    indexed: list[tuple[int, dict[str, Any]]] = []
    last_recovery = next((i for i in range(len(messages) - 1, -1, -1)
                          if messages[i].get("_light_kind") == "recovery"), -1)
    for index, message in enumerate(messages):
        kind = message.get("_light_kind")
        content = str(message.get("content") or "")
        # 旧检查点缺内部标签；这里只识别框架序列化格式，不分类用户意图。
        # 用户原话与最新授权仍由完整 world.user_context/authorization 保留。
        if world is not None and isinstance(world.get("user_context"), dict) and message.get("role") == "user" and kind is None:
            if (content.startswith("可用技能目录（仅元数据，选择由你决定）：")
                    and {"skill_search", "skill_open"} <= {
                        (tool.get("function") or {}).get("name") for tool in tools}):
                continue
            if content.startswith("灵枢任务指令事件："):
                kind = "steering"
            elif content.startswith(("<world_state>\n", '<world_state update="true">\n')):
                kind = "world"
            elif content.startswith(("上一回合没有携带工具调用。", "上一回合只输出了文字，恢复调用")):
                kind = "recovery"
        if kind in {"world", "steering"}:
            continue
        if kind == "recovery" and index != last_recovery:
            continue
        value = copy.deepcopy(message)
        if "_light_base" in value:
            value["content"] = value["_light_base"]
        elif index == 1 and world is not None:
            start = content.rfind("<world_state>\n")
            end = content.find("\n</world_state>", start) if start >= 0 else -1
            if end >= 0:
                try:
                    parsed = json.loads(content[start + len("<world_state>\n"):end])
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict) and ("environment" in parsed or "authorization" in parsed):
                    value["content"] = content[:start] + content[end + len("\n</world_state>"):]
        indexed.append((index, value))
    fixed: list[dict[str, Any]] = []
    if world:
        fixed.append({"role": "user", "content": render(world)})
    if instructions:
        fixed.append({"role": "user", "content": _instructions(instructions)})
    # 当前状态置于固定题面后，不能插进 assistant/tool 往返中。
    insertion = min(2, len(indexed))
    groups: list[tuple[int, int]] = []
    index = 2
    while index < len(indexed):
        message = indexed[index][1]
        if message.get("role") != "assistant":
            index += 1
            continue
        end = index + 1
        calls = message.get("tool_calls") or []
        pending = {call.get("id") for call in calls}
        while end < len(indexed) and indexed[end][1].get("role") == "tool":
            pending.discard(indexed[end][1].get("tool_call_id"))
            end += 1
        if (not pending and not any(m.get("_context_contract") for _, m in indexed[index:end])):
            groups.append((index, end))
        index = end
    removed: set[int] = set()
    archived: list[dict[str, Any]] = []
    history_rows = 8

    def view() -> list[dict[str, Any]]:
        history = []
        if archived:
            tools_archive = [row for row in archived if row.get("tools")]
            selected = tools_archive
            if len(selected) > history_rows:
                early = history_rows // 2
                selected = selected[:early] + selected[-(history_rows - early):]
            history = [{"role": "user", "content": (
                f"已归档 {len(archived)} 个较早完整回复/工具往返；这不是新的执行或验证。"
                "以下是已执行动作的部分索引，包含早期与近期记录；不要把已知结果的相同动作当新证据。"
                "原始参数与结果可用 history_read 按 message_index 分页读取，不能把摘要当完整证据。\n"
                + json.dumps(selected, ensure_ascii=False, separators=(",", ":")))}]
        prefix = [m for i, (_, m) in enumerate(indexed[:insertion]) if i not in removed]
        suffix = [m for i, (_, m) in enumerate(indexed[insertion:], insertion) if i not in removed]
        return prefix + fixed + history + suffix

    result = view()
    # 优先保留近期往返；预算不够时也完整归档，不靠截断参数或结束任务释放窗口。
    for start, end in groups:
        if _tokens(result, tools) <= budget:
            break
        original_index, assistant = indexed[start]
        outcomes = [m for _, m in indexed[start + 1:end]]
        calls = assistant.get("tool_calls") or []
        arguments = [str((call.get("function") or {}).get("arguments") or "") for call in calls]
        previews = []
        for outcome in outcomes:
            text = str(outcome.get("content") or "")
            # 旧沙箱记录的外壳含很长路径；摘要展示实际输出片段而非只重复路径。
            for marker in ("\nstdout:\n", "\nstderr:\n"):
                if marker in text:
                    text = text.split(marker, 1)[1]
                    break
            previews.append(text[:80])
        archived.append({"message_index": original_index,
                         "tools": [(c.get("function") or {}).get("name") for c in calls],
                         "arguments_preview": [value if len(value) <= 160 else value[:50] + " … " + value[-110:]
                                               for value in arguments],
                         "result_preview": previews})
        removed.update(range(start, end))
        result = view()
    estimate = _tokens(result, tools)
    if estimate > budget and instructions:
        fixed[-1]["content"] = _instructions(instructions, compact=True)
        result = view()
        estimate = _tokens(result, tools)
    for count in (6, 4, 2):
        if estimate <= budget:
            break
        history_rows = count
        result = view()
        estimate = _tokens(result, tools)
    if estimate > budget:
        raise ContextWindowError(
            f"当前保留的用户契约、工具参数或近期往返仍需约 {estimate} tokens，输入预算 {budget}；"
            "未向模型发送超窗请求，不能把此问题判为赛题无解。")
    return ContextView(result, estimate, len(archived))
