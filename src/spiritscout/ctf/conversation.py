"""赛题上下文投影与原生工具对话的无损回放。"""
from __future__ import annotations

import copy
import json
from typing import Any

from spiritscout.ctf.contracts import _ToolOutcome


def _challenge_text(ch: dict[str, Any]) -> str:
    """赛题载荷的全部字符串（含嵌套，逐段截断）——题面线索不限于固定字段名。"""
    parts: list[str] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 2 or len(parts) > 40:
            return
        if isinstance(node, str):
            if node.strip():
                parts.append(node[:4000])
        elif isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:10]:
                walk(value, depth + 1)

    walk(ch, 0)
    return "\n".join(parts)


def _challenge_view(ch: dict[str, Any], *, total_cap: int = 6000) -> dict[str, Any]:
    """赛题载荷的 LLM 上下文视图：全字段透传（截断），附件单列不走此口。

    总长超预算时逐档收紧单字段上限——视图整体可入上下文，调用侧不触发截断。
    """
    view: dict[str, Any] = {}
    for per_field in (1500, 700, 300, 120):
        view = {}
        for key, value in list(ch.items())[:20]:
            if key in {"attachments", "user_type_hints"}:
                continue
            if isinstance(value, str):
                view[key] = value[:per_field]
            elif isinstance(value, (int, float, bool)) or value is None:
                view[key] = value
            elif isinstance(value, list):
                view[key] = [str(x)[: min(per_field, 200)] for x in value[:10]]
            elif isinstance(value, dict):
                view[key] = {str(kk)[:40]: str(vv)[: min(per_field, 200)]
                             for kk, vv in list(value.items())[:10]}
        if len(json.dumps(view, ensure_ascii=False)) <= total_cap:
            break
    if ch.get("user_type_hints"):
        view["user_type_hints"] = copy.deepcopy(ch["user_type_hints"])
    return view


def _model_messages(messages: list[dict[str, Any]], *, initialized: bool) \
        -> list[dict[str, Any]]:
    """原样回放模型正文、思考与工具调用；仅移除框架内部元数据。

    空正文的工具调用也是有效原生响应，不能合成正文污染缓存前缀。
    嵌套工具参数必须脱离持久状态，防止适配器修改历史。
    """
    return copy.deepcopy([{k: v for k, v in message.items() if not k.startswith("_")}
                          for message in messages])


def _complete_tool_history(messages: Any) -> bool:
    """恢复时只接受完整的原生工具往返；不猜测中断工具的执行结果。"""
    if not isinstance(messages, list):
        return False
    pending: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            return False
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                return False
            pending.remove(call_id)
        else:
            if pending:
                return False
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                    return False
                if not call["id"] or call["id"] in pending:
                    return False
                pending.add(call["id"])
    return not pending


def _head_tail(text: str, cap: int) -> str:
    """超长输出的上下文摘要：头尾保留、中段略去（完整内容已落盘可查）。"""
    if len(text) <= cap:
        return text
    dropped = len(text) - cap
    return (text[:cap * 3 // 4]
            + f"\n…[中段略去 {dropped} 字符——完整输出已存工作区文件，"
              "需要时用沙箱读取]…\n"
            + text[-(cap // 4):])



def read_history(state: dict[str, Any], args: dict[str, Any], *, lightweight: bool) -> _ToolOutcome:
    """按原始消息或指令身份分页读取与查找，保留完整性和引用元数据。"""
    index, identifier = args.get("message_index"), args.get("instruction_id")
    offset, length = args.get("offset", 0), args.get("length", 2000)
    if (type(offset) is not int or offset < 0
            or type(length) is not int or not 1 <= length <= 4000):
        return _ToolOutcome(ok=False, text="需要非负 offset 与 1–4000 的 length")
    if identifier is not None:
        original = (state.get("instruction_state") or {}).get(identifier) if isinstance(identifier, str) else None
        if original is None:
            return _ToolOutcome(ok=False, text="instruction_id 不在本题当前指令中")
    else:
        if type(index) is not int or not 0 <= index < len(state["messages"]):
            return _ToolOutcome(ok=False, text="需要有效 message_index 或本题当前 instruction_id")
        original = state["messages"][index]
    raw = json.dumps({k: v for k, v in original.items() if not k.startswith("_")},
                     ensure_ascii=False, separators=(",", ":"))
    extra = {}
    if not lightweight:
        from spiritscout.ctf.evidence_memory import source_digest

        view = args.get('view', 'message')
        if view not in ('message', 'content'):
            return _ToolOutcome(ok=False, text='view 需要 message 或 content')
        if view == 'content':
            if not isinstance(original.get('content'), str):
                return _ToolOutcome(ok=False, text='该消息不是文本 content，请用 view=message 读取')
            raw = original['content']
        invocation_id = (original.get('_normal_observation') or {}).get('invocation_id')
        extra = {'view': view, 'source_sha256': source_digest(original), 'offset_unit': 'characters',
                 'evidence_refs': [invocation_id] if invocation_id else []}
        query = args.get('find')
        if query is not None:
            if not isinstance(query, str) or not 1 <= len(query) <= 200:
                return _ToolOutcome(ok=False, text='find 需要 1–200 字符的精确原文')
            position = raw.find(query, offset)
            positions, cursor = [], offset
            while len(positions) < 16:
                hit = raw.find(query, cursor)
                if hit < 0:
                    break
                positions.append(hit)
                cursor = hit + len(query)
            count = raw.count(query, offset)
            extra.update(found=position >= 0, match_offset=position if position >= 0 else None,
                total_matches=raw.count(query), matches_from_offset=count, match_offsets=positions,
                offsets_truncated=count > len(positions), search_complete=True,
                search_semantics='精确、区分大小写、非重叠；total_matches 覆盖该 view 全文，位置从请求 offset 起列举')
            if position < 0:
                return _ToolOutcome(ok=True, text=json.dumps({'message_index': index, **extra,
                    'offset': offset, 'text': '', 'total_chars': len(raw), 'next_offset': len(raw),
                    'has_more': False}, ensure_ascii=False))
            # 保证短页也包含命中位置；分页仍以返回的 view 和 next_offset 延续。
            offset = max(offset, position - min(200, length // 4))
    return _ToolOutcome(ok=True, text=json.dumps({"message_index": index, "instruction_id": identifier,
        "offset": offset, "text": raw[offset:offset + length], "total_chars": len(raw),
        "next_offset": min(offset + length, len(raw)), "has_more": offset + length < len(raw), **extra},
        ensure_ascii=False))
