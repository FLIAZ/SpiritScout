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


class SupervisionGate:
    """按实际结果与显式阻塞触发督导；待执行指令期间保留尚未消费的变化。"""

    def __init__(self) -> None:
        self.seen: dict[str, dict[str, str]] = {}
        self.results: dict[tuple[str, Any], str] = {}
        self.platform = ""

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                          default=str).encode()).hexdigest()

    @classmethod
    def _result_signature(cls, row: dict[str, Any], result: Any) -> str:
        parsed = result.parsed if result is not None else {}
        if row.get("tool_name") == "http_probe" and (parsed.get("body_sha256") or parsed.get("http_failure")):
            # 保存路径、调用 ID 和响应 Date 不是新的目标证据；传输失败也不等于业务拒绝。
            content = {key: parsed.get(key) for key in (
                "status_code", "body_sha256", "final_url", "redirect_chain", "content_type",
                "response_source", "http_failure", "download_complete", "body_truncated", "redirects")}
            content["headers"] = {key: value for key, value in (parsed.get("headers") or {}).items()
                                  if key.lower() not in {"date", "x-request-id", "x-correlation-id", "traceparent"}}
        else:
            content = ((result.stdout, result.stderr, result.error) if result is not None else None)
        return cls._digest([row.get("execution_status"), content])

    def ready(self, digest: dict[str, Any], journal: Any, ring: Any) -> bool:
        tasks = {str(task["task_id"]): task for task in digest["tasks"] if task.get("task_id")}
        self.seen = {key: value for key, value in self.seen.items() if key in tasks}
        signals: dict[str, dict[str, str]] = {key: {} for key in tasks}
        records = (journal.list_recent(500) if journal is not None
                   and callable(getattr(journal, "list_recent", None)) else [])
        cached = {}
        # 同一动作重试只比较最新一次结果；不因滚动窗口淘汰旧调用而触发。
        for row in sorted(records, key=lambda row: row.get("updated_at") or row.get("created_at") or 0):
            tid = row.get("task_id")
            if tid not in tasks or row.get("execution_status") not in {"succeeded", "failed", "cancelled", "unknown", "denied"}:
                continue
            key = (row["invocation_id"], row.get("updated_at"))
            signature = self.results.get(key)
            if signature is None:
                signature = self._result_signature(row, journal.result(row["invocation_id"]))
            cached[key] = signature
            signals[tid][self._digest([row.get("tool_name"), row.get("params_digest"), row.get("identity_id")])] = signature
        self.results = cached
        for tid, task in tasks.items():
            # 轻量部署已提供实际执行摘要，沿用它的结果契约。
            for row in task.get("executions", []):
                if row.get("execution_status") not in {"succeeded", "failed", "cancelled", "unknown", "denied"}:
                    continue
                signals[tid][self._digest([row.get("tool_name"), row.get("params_digest"), row.get("identity_id")])] = self._digest(
                    [row.get("execution_status"), row.get("exit_code"), row.get("result_digest")])
            for route in task.get("_supervision", task.get("routes", [])):
                payload = {key: route.get(key) for key in ("findings", "blockers")}
                payload["blocked"] = route.get("status") in {"failed", "blocked"}
                key = "route:" + str(route.get("route_id") or route.get("route") or "")
                if any(payload.values()) or key in self.seen.get(tid, {}):
                    signals[tid][key] = self._digest(payload)
            for instruction in task.get("steering", []):
                for route, feedback in (instruction.get("feedback") or {}).items():
                    if feedback.get("status") == "blocked":
                        signals[tid]["blocked:" + str(instruction.get("id")) + route] = self._digest(feedback)
        ready = False
        available = False
        for tid, task in tasks.items():
            if ring is not None and ring.pending_steering(task.get("agent"), task_id=tid) is not None:
                continue
            available = True
            seen = self.seen.setdefault(tid, {})
            ready |= any(seen.get(key) != value for key, value in signals[tid].items())
            seen.update(signals[tid])
        platform = digest.get("platform_progress") or {}
        if available and platform:
            fingerprint = self._digest(platform)
            ready |= fingerprint != self.platform
            self.platform = fingerprint
        return ready
