"""实时督导的决策依据校验；验证来源与新鲜度，不替模型评判路线价值。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from spiritscout.core.models import BlackboardLayer


def execution_digest(journal: Any, task_id: str) -> list[dict[str, Any]]:
    """本任务最近实际执行的紧凑证据；成功执行不等同于漏洞或假设验证成功。"""
    if journal is None or not callable(getattr(journal, "list_recent", None)):
        return []
    records = [row for row in journal.list_recent(500) if row.get("task_id") == task_id][:8]
    out = []
    for row in reversed(records):
        result = journal.result(row["invocation_id"])
        item = {k: row.get(k) for k in ("invocation_id", "tool_name", "params_digest", "execution_status")}
        if result is not None:
            content = (result.stdout or "") + "\n" + (result.stderr or "") + "\n" + (result.error or "")
            item.update(exit_code=result.returncode, execution_ok=result.ok,
                        result_digest=hashlib.sha256(content.encode()).hexdigest(),
                        stdout_preview=(result.stdout or "")[:500], stderr_preview=(result.stderr or "")[:500],
                        error=result.error, workdir=result.parsed.get("workdir"),
                        output_file=result.parsed.get("output_file"), partial=True)
            parameters = journal.parameters(row["invocation_id"]) or {}
            item["parameters_preview"] = json.dumps(parameters, ensure_ascii=False)[:500]
        out.append(item)
    return out


def record_ref(ref: dict[str, Any]) -> str:
    return f'{ref["layer"]}:{ref["record_id"]}@{ref["version"]}'


def platform_ref(progress: dict[str, Any]) -> str:
    value = {key: val for key, val in progress.items() if key != 'updated_at'}
    return 'platform:' + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def check_basis(bb: Any, item: dict[str, Any], task_ids: list[str],
                *, current_platform: str = '') -> bool:
    refs = item.get('basis_refs')
    if not isinstance(refs, list) or not refs or len(refs) > 6 or bb is None:
        return False
    snapshot, evidence = False, False
    for ref in refs:
        if not isinstance(ref, str):
            return False
        if ref.startswith('platform:'):
            if ref != current_platform:
                return False
            evidence = True
            continue
        layer, _, identifier = ref.partition(':')
        key, _, version = identifier.rpartition('@')
        try:
            if not key or not version.isdigit():
                return False
            row = bb.read_versioned(BlackboardLayer(layer), key)
        except (ValueError, TypeError, sqlite3.Error):
            return False
        if row is None or row[1] != int(version):
            return False
        data = row[0]
        if layer == 'task_state':
            task = data.get('task') or {}
            cid = str(((task.get('payload') or {}).get('challenge') or {}).get('id') or '')
            if key not in task_ids or cid != str(item.get('id') or '') or task.get('status') not in {'running', 'dispatched', 'pending'}:
                return False
            snapshot = True
        else:
            owner = (data.get('provenance') or {}).get('producer', {}).get('task_id') or data.get('task_id')
            if owner not in task_ids or data.get('evidence_state') not in {'observed', 'supported', 'independently_verified'}:
                return False
            evidence = True
    # 暂缓派工允许引用现场摘要；取消／释放须有同题事实或当前平台状态，不凭摘要单独执行。
    critical = item.get('cancel_running') is True or item.get('release_instance') is True
    return snapshot and (not critical or evidence)
