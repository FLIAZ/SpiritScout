"""扫描证据交接：原文保存、按引用读取、由模型提交候选后进入既有复核队列。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from spiritscout.blackboard import ConvergenceEngine
from spiritscout.core.models import AgentType, BlackboardLayer, Severity, Vulnerability


def write_artifact(directory: int, name: str, text: str) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
        output.write(text)


def save_nuclei_evidence(directory: int, relative: str, line: int, row: dict) -> dict:
    """保存工具实际返回的内容；无请求包/PoC 时明确留空，不补写验证证据。"""
    stem = f"finding-{line}"
    parts = {"result": (".json", json.dumps(row, ensure_ascii=False, indent=2)),
             "request": ("-request.http", row.get("request")),
             "response": ("-response.http", row.get("response")),
             "poc": ("-poc.sh", row.get("curl-command"))}
    files = {}
    for key, (suffix, text) in parts.items():
        if isinstance(text, str) and text:
            name = stem + suffix
            write_artifact(directory, name, text)
            files[key] = f"{relative}/{name}"
    classification = (row.get("info") or {}).get("classification") or {}
    identifiers = []
    if isinstance(classification, dict):
        for key in ("cve-id", "cwe-id"):
            values = classification.get(key) or []
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                identifiers.extend(value[:200] for value in values if isinstance(value, str))
    return {"files": files, "vulnerability_ids": list(dict.fromkeys(identifiers)),
            "missing": [key for key in ("request", "response", "poc") if key not in files],
            "poc_source": "scanner_reproduction_command", "independently_verified": False}


def finding(blackboard, finding_id: str) -> dict:
    row = blackboard.read(BlackboardLayer.RECON_CLUE, finding_id)
    if not row or row.get("clue_type") != "scanner_finding":
        raise ValueError("finding_id 必须引用本会话真实扫描命中")
    return row


def read_finding(blackboard, workspace: Path, finding_id: str, section: str = "result",
                 offset: int = 0, limit: int = 8000) -> dict:
    from spiritscout.web.deliverables import open_artifact

    row = finding(blackboard, finding_id)
    path = (row.get("scan_evidence") or {}).get("files", {}).get(section)
    if not path:
        return {"finding_id": finding_id, "section": section, "available": False,
                "note": "原扫描器没有提供此项证据"}
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 16000))
    # 文本交付件由扫描器保存；分页按字节读取，完整原件仍可下载。
    with open_artifact(workspace, path) as source:
        source.seek(offset)
        data = source.read(limit + 1)
    return {"finding_id": finding_id, "section": section, "available": True,
            "path": path, "offset": offset, "text": data[:limit].decode(errors="replace"),
            "has_more": len(data) > limit, "next_offset": offset + min(limit, len(data))}


def submit_candidate(blackboard, workspace: Path, arguments: dict[str, Any]) -> dict:
    """仅接受真实命中引用；是否属于漏洞、危害类型由模型判断，确认权留给独立复核。"""
    finding_id = str(arguments.get("finding_id") or "")
    row = finding(blackboard, finding_id)
    title, kind = arguments.get("title"), arguments.get("vuln_type")
    reason = arguments.get("reason")
    if not all(isinstance(value, str) and value.strip() for value in (title, kind, reason)):
        raise ValueError("提交候选需要 title、vuln_type 和判断依据 reason")
    evidence = row.get("scan_evidence") or {}
    endpoint = arguments.get('endpoint') or row['value']
    if not isinstance(endpoint, str):
        raise ValueError('endpoint 必须是根据实际观察给出的入口字符串')
    # 由独立复核角色判断可利用性；不能通过参数伪造 accepted/poc_verified。
    candidate = Vulnerability(vuln_id=f"vuln-{finding_id}", title=title[:500],
        vuln_type=kind[:100], target=row["value"], endpoint=endpoint,
        severity=Severity(arguments.get("severity", row.get("severity", "info"))),
        confidence=0.5, found_by="scanner", vulnerability_ids=evidence.get("vulnerability_ids", []),
        scanner_evidence=[{"finding_id": finding_id, "scanner": row.get("scanner"),
            "template_id": row.get("template_id"), "module": row.get("module"), **evidence}],
        evidence=[{"kind": "scanner_candidate", "finding_id": finding_id, "reason": reason[:4000],
                   "evidence_refs": [f"recon_clue:{finding_id}"],
                   "invocation_id": row.get("invocation_id", ""), "files": evidence.get("files", {})}])
    # PoC 原文通过文件引用完整交付，不将大文件灌入每次规划上下文。
    existing = blackboard.find_by_dedup_key(BlackboardLayer.VULN, candidate.dedup_key())
    if existing and any(item.get('finding_id') == finding_id
                        for item in existing['data'].get('scanner_evidence', [])):
        return {'vuln_id': existing['record_id'], 'created': False,
                'status': existing['data']['status'], 'note': '同一命中已经交接，保留原复核状态'}
    key, created, _ = ConvergenceEngine(blackboard).merge_vulnerability(candidate, AgentType.SCANNER)
    return {"vuln_id": key, "created": created, "status": "pending_independent_verification",
            "note": "已提交候选；模板命中不等于漏洞已确认", "finding_id": finding_id}
