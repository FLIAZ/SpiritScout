"""运行时状态的增量消息。

系统提示保持稳定；授权与环境放入 world_state，模型侧规则仅携带黑板引用。
授权与规则变化按顶层字段整体发送；保留原始片段的 CTF 会话可增量同步近期事实目录。"""

from __future__ import annotations

import json
import platform as _platform
from datetime import datetime
from pathlib import Path
from typing import Any

from spiritscout.core.exceptions import VersionConflict
from spiritscout.core.models import BlackboardLayer


def authorization(guard: Any) -> dict[str, Any]:
    """模型上下文中的用户声明：初始范围、批准答复与排除规则，不做硬校验。"""
    scope = getattr(guard, "scope", None) if guard is not None else None
    if scope is None:
        return {}
    approved = getattr(guard, "approved_targets", None)
    return {
        "allowed_domains": list(getattr(scope, "allowed_domains", []) or []),
        "allowed_cidrs": list(getattr(scope, "allowed_cidrs", []) or []),
        "allowed_urls": list(getattr(scope, "allowed_urls", []) or []),
        "approved_by_user": sorted(approved()) if callable(approved) else [],
        "denied": list(getattr(scope, "denied", []) or []),
        "testing_rules": str(getattr(scope, "testing_rules", "") or ""),
        "notes": str(getattr(scope, "notes", "") or ""),
        "boundary_enforcement": "model_context",
        "local_analysis": bool(getattr(scope, "local_analysis", False)),
    }


def environment(*, workspace: str = "", artifacts: str = "") -> dict[str, Any]:
    """沙箱运行态事实：只陈述位置与平台，不枚举可用的命令或库。"""
    facts: dict[str, Any] = {
        "platform": _platform.system().lower() or "unknown",
        "date": datetime.now().date().isoformat(),
    }
    if workspace:
        facts["workspace"] = workspace
    if artifacts:
        facts["artifacts"] = artifacts
    return facts


def record_user_statement(blackboard: Any, statement: dict[str, Any]) -> None:
    """用户消息/询问答复原文留存；模型自行理解边界含义，后端不分类。"""
    if blackboard is None:
        return
    while True:
        row = blackboard.read_versioned(BlackboardLayer.TARGET_AUTH, "user-context")
        current, version = row if row is not None else ({}, 0)
        statements = list(current.get("statements") or [])
        statement_id = statement.get("statement_id")
        if statement_id and any(item.get("statement_id") == statement_id for item in statements):
            return
        statements.append(statement)
        try:
            blackboard.write(BlackboardLayer.TARGET_AUTH, "user-context",
                             {**current, "statements": statements}, updated_by="user-context",
                             expected_version=version)
        except VersionConflict:
            # 多 Agent 的问题可并发作答，重读后追加，不能用旧列表覆盖其他用户原文。
            continue
        return


def record_user_statement_at_path(path: str | Path, statement: dict[str, Any]) -> None:
    """未开工或运行态已关闭时仍立即留存用户原话，不依赖主 Agent 消费。"""
    from spiritscout.blackboard import Blackboard

    blackboard = Blackboard(path)
    try:
        record_user_statement(blackboard, statement)
    finally:
        blackboard.close()


def sync_user_context(blackboard: Any, *,
                      dialogue_history: list[dict[str, str]] | None = None,
                      mission: str | None = None, standing_rules: str | None = None) -> None:
    """恢复可用的旧对话与带 ID 的新消息，原子更新任务，不覆盖并发答复。"""
    while True:
        row = blackboard.read_versioned(BlackboardLayer.TARGET_AUTH, "user-context")
        current, version = row if row is not None else ({}, 0)
        statements = list(current.get("statements") or [])
        ids = {item["statement_id"] for item in statements if item.get("statement_id")}
        for message in dialogue_history or []:
            if message.get("role") != "user":
                continue
            statement_id = message.get("statement_id")
            # 无 ID 的旧历史只在事实块缺失时回填；相同文本的不同消息不能去重。
            if statement_id in ids or (not statement_id and current):
                continue
            statement = {"kind": "message", "text": str(message.get("content") or "")}
            if statement_id:
                statement["statement_id"] = statement_id
                ids.add(statement_id)
            statements.append(statement)
        updated = {**current}
        if statements:
            updated["statements"] = statements
        if mission is not None:
            updated["mission"] = mission
        if standing_rules is not None:
            updated["standing_rules"] = standing_rules
        if updated == current:
            return
        try:
            blackboard.write(BlackboardLayer.TARGET_AUTH, "user-context", updated,
                             updated_by="user-context", expected_version=version)
        except VersionConflict:
            continue
        return


def snapshot(*, guard: Any = None, env: dict[str, Any] | None = None,
             blackboard: Any = None, references: bool = False) -> dict[str, Any]:
    """当前世界状态：空字段不占位，片段只承载运行时事实。"""
    out: dict[str, Any] = {}
    auth = authorization(guard)
    if auth:
        out["authorization"] = auth
    if env:
        out["environment"] = env
    if blackboard is not None:
        context = blackboard.read(BlackboardLayer.TARGET_AUTH, "user-context")
        if context:
            out["user_context"] = context
        project = blackboard.read(BlackboardLayer.TOPOLOGY, "project-memory") or {}
        if project.get("description"):
            out["project_rules"] = project["description"]
        from spiritscout.blackboard.exchange import fact_index

        facts = fact_index(blackboard) if references else []
        if facts:
            out["shared_facts"] = facts
        if references and callable(getattr(blackboard, 'query', None)):
            from spiritscout.blackboard.exploration import directory

            # 仅广播小目录；详细图与失败分支由模型按需查询。
            recent = directory(blackboard, 'exploration', limit=8, recent=True)
            if recent['items']:
                out['exploration_graph'] = recent
    if references and blackboard is not None:
        from spiritscout.blackboard.exchange import project_rules
        return project_rules(blackboard, out)
    return out


def render(values: dict[str, Any], *, update: bool = False) -> str:
    attribute = ' update="true"' if update else ""
    body = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return f"<world_state{attribute}>\n{body}\n</world_state>"


def update(previous: dict[str, Any] | None, current: dict[str, Any], *,
           incremental_facts: bool = False, incremental_exploration: bool = False) -> str | None:
    """返回与上次不同的顶层字段（整值重发）；无变化返回 None。

    不发送 JSON Patch：旧片段可能被上下文压缩折叠，后续片段必须能独立
    读懂。整值重发让最新片段始终包含该字段的完整现状（与 Codex 的
    environment_context 按区块重绘同语义）。incremental_facts 仅供完整保留
    初始片段和后续更新的 CTF 对话启用，按记录身份同步近期目录，不变更授权更新契约。
    探索目录增量另行协商；仅在补丁明显更小时发送，旧会话继续接收完整字段。
    """
    if not current:
        return None
    if not previous:
        return render(current)
    changed: dict[str, Any] = {}
    for key, value in current.items():
        if previous.get(key) != value:
            changed[key] = value
    for key in previous.keys() - current.keys():
        changed[key] = None
    if 'exploration_graph' in changed and isinstance(current.get('exploration_graph'), dict):
        before, after = previous.get('exploration_graph') or {}, current['exploration_graph']
        def graph_index(value: Any) -> dict[tuple[str, str], dict[str, Any]] | None:
            if not isinstance(value, dict) or not isinstance(value.get('items', []), list):
                return None
            result = {}
            for item in value.get('items', []):
                ref = item.get('blackboard_ref') if isinstance(item, dict) else None
                if (not isinstance(ref, dict) or not isinstance(ref.get('layer'), str)
                        or not isinstance(ref.get('record_id'), str) or not ref['layer'] or not ref['record_id']):
                    return None
                identity = ref['layer'], ref['record_id']
                if identity in result:
                    return None
                result[identity] = item
            return result

        old, new = graph_index(before), graph_index(after)
        if old is not None and new is not None:
            old_meta = {key: value for key, value in before.items() if key != 'items'}
            new_meta = {key: value for key, value in after.items() if key != 'items'}
            upsert = [row for key, row in new.items() if old.get(key) != row]
            retired = [{'layer': key[0], 'record_id': key[1]} for key in old if key not in new]
            if not upsert and not retired and old_meta == new_meta:
                changed.pop('exploration_graph')  # 仅展示次序变化不产生模型消息。
            elif incremental_exploration:
                delta = {'upsert': upsert, 'retired': retired}
                if old_meta != new_meta:
                    delta['meta'] = new_meta
                compact = render({'exploration_graph_delta': delta}, update=True)
                full = render({'exploration_graph': after}, update=True)
                from spiritscout.gateway.context import estimate_input_tokens

                # 传输保留全部字段；补丁膨胀时回退整块，避免全图变化反而更贵。
                if (len(compact.encode()) * 5 <= len(full.encode()) * 4
                        and estimate_input_tokens([{'role': 'user', 'content': compact}])
                        < estimate_input_tokens([{'role': 'user', 'content': full}])):
                    changed.pop('exploration_graph')
                    changed['exploration_graph_delta'] = delta
    if incremental_facts and "shared_facts" in changed:
        # 仅用于原始世界片段完整保留的追加式会话；授权/规则仍按原契约整字段更新。
        before, after = previous.get("shared_facts", []), current.get("shared_facts", [])
        def indexed(rows: Any) -> dict[tuple[str, str], dict[str, Any]] | None:
            if not isinstance(rows, list):
                return None
            result = {}
            for row in rows:
                ref = row.get("blackboard_ref") if isinstance(row, dict) else None
                if (not isinstance(ref, dict) or not isinstance(ref.get("layer"), str)
                        or not isinstance(ref.get("record_id"), str) or not ref["layer"] or not ref["record_id"]):
                    return None
                identity = (ref["layer"], ref["record_id"])
                if identity in result:
                    return None
                result[identity] = row
            return result
        old_index, new_index = indexed(before), indexed(after)
        if old_index is not None and new_index is not None:
            changed.pop("shared_facts")
            upsert = [row for key, row in new_index.items() if old_index.get(key) != row]
            retired = [{"layer": key[0], "record_id": key[1]} for key in old_index if key not in new_index]
            if upsert or retired:
                changed["shared_facts_delta"] = {"upsert": upsert, "retired": retired}
    if not changed:
        return None
    return render(changed, update=True)
