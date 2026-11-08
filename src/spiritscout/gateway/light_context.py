"""轻量会话请求投影：归档完整工具往返，保留原始历史和当前契约。"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from spiritscout.ctf.light_memory import call_details, catalog_rows, tool_records
from spiritscout.gateway.world_state import render


class ContextWindowError(ValueError):
    """保留的当前契约本身超窗；不发送必然耗尽输出空间的请求。"""


@dataclass
class ContextView:
    messages: list[dict[str, Any]]
    estimated_tokens: int
    archived_turns: int
    original_tokens: int = 0

    @property
    def skill_refs(self) -> list[str]:
        return sorted({m["_skill_ref"] for m in self.messages if m.get("_skill_ref")})


def _contract_fields(world: dict[str, Any]):
    """只处理已知的用户文本字段；重复声明仍保留编号、顺序和各自附加信息。"""
    context = world.get("user_context") or {}
    if isinstance(context, dict):
        for key in ("mission", "standing_rules"):
            yield context, key, f"world_state.user_context.{key}"
        for index, statement in enumerate(context.get("statements") or []):
            if isinstance(statement, dict):
                yield statement, "text", f"world_state.user_context.statements[{index}].text"
    auth = world.get("authorization") or {}
    if isinstance(auth, dict):
        yield auth, "testing_rules", "world_state.authorization.testing_rules"
    yield world, "project_rules", "world_state.project_rules"


def _deduplicate_world(world: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    result = copy.deepcopy(world)
    sources: dict[str, str] = {}
    for parent, key, path in _contract_fields(result):
        text = parent.get(key)
        if not isinstance(text, str) or len(text) < 96:
            continue
        if text in sources:
            parent[key] = {"exact_text_ref": sources[text]}
        else:
            sources[text] = path
    return result, sources


def _reference_group(messages: list[dict[str, Any]]) -> bool:
    calls = messages[0].get("tool_calls") or []
    names = set()
    for call in calls:
        function = call.get("function") or {}
        name = function.get("name")
        if name == "tool_invoke":
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            name = arguments.get("name") if isinstance(arguments, dict) else None
        names.add(name)
    return all(not m.get("_context_contract") or (
        m.get("role") == "tool" and (m.get("_skill_ref") or names == {"mcp_search"})) for m in messages)


class _ProjectionTokens:
    """与离线估算器同口径；同一投影内每条长消息只扫描一次，避免逐轮归档反复扫描全文。"""

    def __init__(self, tools: list[dict[str, Any]]):
        self.base = self._units({"messages": [], "tools": tools or []})
        # 保留对象引用，防止临时索引消息释放后 id 被复用。
        self.cache: dict[int, tuple[dict[str, Any], int]] = {}

    @staticmethod
    def _units(value: Any) -> int:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        ascii_chars = sum(character.isascii() for character in text)
        return ascii_chars * 2 + (len(text.encode("utf-8")) - ascii_chars) * 3

    def __call__(self, messages: list[dict[str, Any]]) -> int:
        units = self.base + max(0, len(messages) - 1) * 2
        for message in messages:
            key = id(message)
            if key not in self.cache:
                wire = {k: v for k, v in message.items() if not k.startswith("_")}
                self.cache[key] = (message, self._units(wire))
            units += self.cache[key][1]
        return (units + 5) // 6 + len(messages) * 8 + 32

    def invalidate(self, message: dict[str, Any]) -> None:
        self.cache.pop(id(message), None)


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
                    instructions: list[dict[str, Any]] | None = None,
                    target_budget: int | None = None, evict_references: bool = False,
                    deduplicate_contracts: bool = False, plain_progress: bool = False,
                    deduplicate_observations: bool = False,
                    fold_repeated_turns: bool = False,
                    fold_repeated_requests: bool = False,
                    evidence_first: bool = False, archive_before: int = 0) -> ContextView:
    """只返回分离的发送视图；不改写原始历史、不拆工具配对、不节选用户声明。"""
    count_tokens = _ProjectionTokens(tools)
    display_contract = None
    if plain_progress:
        from spiritscout.gateway.progress import EXPLORATION_PROGRESS_GUIDANCE

        display_contract = EXPLORATION_PROGRESS_GUIDANCE
    has_display_contract = display_contract is not None and any(
        m.get("role") == "user" and m.get("_context_contract") and m.get("content") == display_contract for m in messages)
    target = max(1, min(budget, target_budget if target_budget is not None else budget))
    initial_tokens = count_tokens(messages)
    if (initial_tokens <= target and not has_display_contract and not world and not instructions
            and not evidence_first and not archive_before and not fold_repeated_turns and not fold_repeated_requests):
        return ContextView(copy.deepcopy(messages), initial_tokens, 0, initial_tokens)
    world_view, contract_sources = (_deduplicate_world(world) if world and deduplicate_contracts else (world, {}))
    indexed: list[tuple[int, dict[str, Any]]] = []
    user_feed: list[dict[str, Any]] = []
    last_recovery = next((i for i in range(len(messages) - 1, -1, -1)
                          if messages[i].get("_light_kind") == "recovery"), -1)
    last_tool_result = next((i for i in range(len(messages) - 1, -1, -1)
                             if messages[i].get("role") == "tool"), -1)
    for index, message in enumerate(messages):
        kind = message.get("_light_kind")
        content = str(message.get("content") or "")
        if kind == "user_feed":
            user_feed.append(copy.deepcopy(message))
            continue
        if has_display_contract and message.get("role") == "user" and message.get("_context_contract") and content == display_contract:
            continue
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
        if kind == "recovery" and (index != last_recovery or index < last_tool_result):
            continue
        value = copy.deepcopy(message)
        if contract_sources and isinstance(value.get("_light_task_frame"), dict):
            frame = copy.deepcopy(value["_light_task_frame"])
            fields = [(frame, "mission"), (frame, "rules")]
            if isinstance(frame.get("challenge"), dict):
                fields.append((frame["challenge"], "description"))
            for parent, key in fields:
                text = parent.get(key)
                if isinstance(text, str) and text in contract_sources:
                    parent[key] = {"exact_text_ref": contract_sources[text]}
            value["content"] = value.get("_light_task_heading", "当前任务：\n") + json.dumps(frame, ensure_ascii=False)
        elif "_light_base" in value:
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
    if world_view:
        fixed.append({"role": "user", "content": render(world_view)})
    if instructions:
        fixed.append({"role": "user", "content": _instructions(instructions)})
    # 当前状态置于固定题面后，不能插进 assistant/tool 往返中。
    insertion = min(2, len(indexed))
    groups: list[tuple[int, int]] = []
    complete_groups: list[tuple[int, int]] = []
    memory_groups: list[tuple[int, int]] = []
    has_current_memory = any(m.get('_light_kind') == 'memory' and m.get('_context_contract') for m in messages)
    index = 2
    while index < len(indexed):
        message = indexed[index][1]
        if message.get("_light_kind") == "observation":
            groups.append((index, index + 1))
            index += 1
            continue
        if message.get("role") != "assistant":
            index += 1
            continue
        end = index + 1
        calls = message.get("tool_calls") or []
        pending = {call.get("id") for call in calls}
        while end < len(indexed) and indexed[end][1].get("role") == "tool":
            pending.discard(indexed[end][1].get("tool_call_id"))
            end += 1
        group = [m for _, m in indexed[index:end]]
        if not pending and calls:
            complete_groups.append((index, end))
            if evidence_first and has_current_memory and all(call_details(call)[0] == 'evidence_memory' for call in calls):
                memory_groups.append((index, end))
        if not pending and (not any(m.get("_context_contract") for m in group)
                            or (evict_references and _reference_group(group))):
            groups.append((index, end))
        index = end
    removed: set[int] = set()
    archived: list[dict[str, Any]] = []
    history_rows = 8
    records = tool_records(messages) if evidence_first else []
    repeated: dict[int, dict[str, Any]] = {}
    seen_results: dict[str, list[int]] = {}
    for row in records:
        if key := row['message'].get('_context_repeat_key'):
            indices = seen_results.setdefault(key, [])
            indices.append(row['message_index'])
            if len(indices) > 1:
                repeated[row['message_index']] = {'same_request_and_result_count': len(indices),
                                                  'first_message_index': indices[0]}
    catalog_count = 0
    catalog_enabled = False
    catalog_cache: dict[str, Any] = {}

    def evidence_catalog() -> list[dict[str, Any]]:
        if not records:
            return []
        if catalog_cache.get('removed') != removed:
            removed_indices = {indexed[i][0] for i in removed}
            visible = [{'message_index': row['message_index'], 'tool_call_id': row['message'].get('tool_call_id'),
                        **repeated.get(row['message_index'], {})}
                       for row in records if row['message_index'] not in removed_indices]
            rows = catalog_rows([row for row in records if row['message_index'] in removed_indices])
            # 命名结构是工具自身给出的定位事实；先保留这些短标签，剩余目录始终可检索。
            rows.sort(key=lambda row: not bool(row.get('function')))
            catalog_cache.update(removed=removed.copy(), visible=visible, rows=rows)
        visible, rows = catalog_cache['visible'], catalog_cache['rows']
        data = {'visible_results': visible, 'archived': rows[:catalog_count],
                'omitted_groups': max(0, len(rows) - catalog_count)}
        request_archives = [{key: item[key] for key in
            ('message_index', 'result_message_indices', 'same_request_at', 'results_may_differ')}
            for item in archived if 'same_request_at' in item]
        if request_archives:
            data['repeated_request_archives'] = request_archives[-8:]
            data['request_archive_notice'] = '只因参数连续重复归档，结果可能不同；最近两次全文保留，旧结果按编号读取。'
        return [{'role': 'user', 'content': (
            '历史证据目录（预览不完整，非新证据）。history_read(view=index, find=片段) 搜索消息；'
            '按 message_index 用 content 读消息、output 读落盘输出，next_call 续页；不同编号可能是不同页。\n'
            + json.dumps(data, ensure_ascii=False, separators=(',', ':')))}]

    def view() -> list[dict[str, Any]]:
        history = []
        if evidence_first:
            history = evidence_catalog() if catalog_enabled else []
        elif archived:
            ordered = sorted(archived, key=lambda row: row["message_index"])
            tools_archive = [row for row in ordered if row.get("tools") or row.get("kind") == "observation"]
            selected = tools_archive
            if history_rows == 0:
                selected = [{"first_message_index": ordered[0]["message_index"],
                             "last_message_index": ordered[-1]["message_index"]}]
            elif len(selected) > history_rows:
                early = history_rows // 2
                selected = selected[:early] + selected[-(history_rows - early):]
            history = [{"role": "user", "content": (
                f"已归档 {len(archived)} 个较早完整回复/工具往返；这不是新的执行或验证。"
                + "以下是已执行动作的部分索引，包含早期与近期记录；不要把已知结果的相同动作当新证据。"
                "原始参数与结果可用 history_read 按 message_index 分页读取，不能把摘要当完整证据。\n"
                + json.dumps(selected, ensure_ascii=False, separators=(",", ":")))}]
        prefix = [m for i, (_, m) in enumerate(indexed[:insertion]) if i not in removed]
        suffix = [m for i, (_, m) in enumerate(indexed[insertion:], insertion) if i not in removed]
        return prefix + fixed + history + suffix + user_feed

    def archive(start: int, end: int, *, repeated_at: int | None = None,
                request_repeated_at: int | None = None) -> None:
        original_index, assistant = indexed[start]
        outcomes = [m for _, m in indexed[start + 1:end]]
        observation = assistant.get("_light_kind") == "observation"
        if observation:
            outcomes = [assistant]
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
                         "result_message_indices": [i for i, _ in indexed[start + 1:end]],
                         **({"same_request_and_result_at": repeated_at} if repeated_at is not None else {}),
                         **({"same_request_at": request_repeated_at, "results_may_differ": True}
                            if request_repeated_at is not None else {}),
                         **({"skill_refs": [m["_skill_ref"] for m in outcomes if m.get("_skill_ref")]}
                            if any(m.get("_skill_ref") for m in outcomes) else {}),
                         **({"kind": "observation"} if observation else {}),
                         "tools": [(c.get("function") or {}).get("name") for c in calls],
                         "arguments_preview": [value if len(value) <= 160 else value[:50] + " … " + value[-110:]
                                               for value in arguments],
                         "result_preview": previews})
        removed.update(range(start, end))

    for start, end in memory_groups:
        # 最新工具回执仍须可见：只保留笔记快照会吞掉保存成功/冲突/未变化状态，诱发重复更新。
        if complete_groups and (start, end) == complete_groups[-1]:
            continue
        # 当前笔记由调用者独立提供；旧笔记整轮归档，不能挤掉原始证据或重新激活旧判断。
        archive(start, end)
    result = view()
    # 手动整理只覆盖请求时的旧历史；后续新增往返不受这次请求影响。
    for start, end in groups:
        if start in removed or indexed[end - 1][0] >= archive_before:
            continue
        group = [m for _, m in indexed[start:end]]
        if any(m.get('_context_contract') for m in group):
            continue
        calls = group[0].get('tool_calls') or []
        ids = [call.get('id') for call in calls]
        results = [m.get('tool_call_id') for m in group[1:] if m.get('role') == 'tool']
        if (None in ids or len(set(ids)) != len(ids) or len(results) != len(ids) or set(results) != set(ids)):
            continue
        previous_size = count_tokens(result)
        archive(start, end)
        candidate = view()
        if count_tokens(candidate) < previous_size:
            result = candidate
        else:
            archived.pop()
            removed.difference_update(range(start, end))
    if fold_repeated_turns or (deduplicate_observations and count_tokens(result) > target):
        latest_results: dict[tuple[str, ...], int] = {}
        duplicates = []
        archivable = set(groups)
        for start, end in reversed(complete_groups):
            group = [message for _, message in indexed[start:end]]
            keys = tuple(message.get("_context_repeat_key", "") for message in group[1:])
            # 不拆并发工具批次，不去掉用户契约或必须可见的参考资料。
            if (not keys or not all(keys) or not group[0].get("tool_calls")
                    or len(keys) != len(group[0]["tool_calls"])):
                continue
            if keys in latest_results:
                if (start, end) in archivable and not any(message.get("_context_contract") for message in group):
                    duplicates.append((start, end, latest_results[keys]))
            else:
                latest_results[keys] = indexed[start][0]
        for start, end, repeated_at in reversed(duplicates):
            if not fold_repeated_turns and count_tokens(result) <= target:
                break
            if start in removed:
                continue
            archive(start, end, repeated_at=repeated_at)
            result = view()
    if fold_repeated_requests:
        # 连续相同参数的往返仅保留最近两次全文作对照，旧结果仍有独立原文引用。
        # 不将时间戳等变化抹成“相同结果”，不跨用户消息/不同工具动作合并，也不阻止重试。
        runs: list[list[tuple[int, int]]] = []
        previous_key = None
        previous_end = None
        for start, end in complete_groups:
            calls = indexed[start][1].get('tool_calls') or []
            key = json.dumps([call_details(call) for call in calls], ensure_ascii=False, sort_keys=True)
            if (previous_key == key and previous_end == start
                    and indexed[start][0] == indexed[start - 1][0] + 1):
                runs[-1].append((start, end))
            else:
                runs.append([(start, end)])
            previous_key, previous_end = key, end
        archivable = set(groups)
        for run in runs:
            for start, end in run[:-2]:
                if start in removed or (start, end) not in archivable:
                    continue
                if any(message.get('_context_contract') for _, message in indexed[start:end]):
                    continue
                archive(start, end, request_repeated_at=indexed[run[-1][0]][0])
                result = view()
    latest_reply = next((start for start, _ in reversed(groups) if indexed[start][1].get("role") == "assistant"), None)
    recent_evidence = {start for start, _ in complete_groups if start not in removed}
    recent_evidence = set(sorted(recent_evidence)[-2:]) if evidence_first else set()
    recent_evidence.add(latest_reply)
    # 相同请求/结果先去重，再按时间归档；为跨文件联合分析保留更多不同的近期证据。
    for start, end in groups:
        if start in removed:
            continue
        current = count_tokens(result)
        if current <= target or (start in recent_evidence and current <= budget):
            break
        archive(start, end)
        result = view()
    estimate = count_tokens(result)
    if estimate > budget and instructions:
        fixed[-1]["content"] = _instructions(instructions, compact=True)
        count_tokens.invalidate(fixed[-1])
        result = view()
        estimate = count_tokens(result)
    for count in (6, 4, 2, 1, 0):
        if estimate <= budget:
            break
        history_rows = count
        result = view()
        estimate = count_tokens(result)
    if estimate > budget:
        raise ContextWindowError(
            f"当前保留的用户契约、工具参数或近期往返仍需约 {estimate} tokens，输入预算 {budget}；"
            "未向模型发送超窗请求，不能把此问题判为赛题无解。")
    if evidence_first and records:
        # 原文和当前笔记先留位，再用剩余空间补实质目录，不为软阈值把目录永久清成编号范围。
        baseline = estimate
        catalog_enabled = True
        for count in range(len(records) + 1):
            catalog_count = count
            candidate = view()
            size = count_tokens(candidate)
            if size > budget or size - baseline > min(1600, budget // 5):
                break
            result, estimate = candidate, size
            if len(catalog_cache.get('rows', [])) <= count:
                break
    return ContextView(result, estimate, len(archived), initial_tokens)
