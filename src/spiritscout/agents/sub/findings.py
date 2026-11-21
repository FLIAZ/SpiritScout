"""调查候选只核对证据来源；漏洞语义与实际危害留给模型和独立复测。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spiritscout.core.ids import new_id
from spiritscout.core.models import Severity, Vulnerability
from spiritscout.tools.http_responses import _read


def _evidence(raw: Any, workspace: str, observations: list[dict]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    quote = raw.get("quote", raw.get("evidence_quote"))
    if not isinstance(quote, str) or not quote.strip():
        return None
    ref = raw.get("body_ref")
    if ref:
        try:
            # _read 校验会话路径和符号链接；元数据及正文均来自同一个响应快照。
            observed = _read(workspace_root=workspace, body_ref=ref, mode="search", query=quote[:500])
            metadata = json.loads((Path(workspace) / "http-responses" / ref / "metadata.json").read_text(encoding="utf-8"))
            part = raw.get("part", "body")
            if part == "headers":
                text = json.dumps({key: metadata.get(key) for key in ("headers", "status_code")}, ensure_ascii=False)
                matched = quote in text
            else:
                matched = part == "body" and any(quote in item["text"] for item in observed["matches"])
            if not matched:
                return None
            return {"body_ref": ref, "quote": quote[:500], "part": part,
                    "source_invocation_id": observed["source_invocation_id"],
                    "body_sha256": observed["body_sha256"],
                    "download_complete": observed["download_complete"],
                    "response_is_partial": bool(metadata.get("response_is_partial")),
                    "status_code": metadata.get("status_code"),
                    "url": metadata.get("final_url") or metadata.get("url", "")}
        except (OSError, ValueError, TypeError, KeyError):
            return None
    execution_id = raw.get("execution_id")
    for observation in observations:
        if (execution_id and execution_id in (observation.get("invocation_id"), observation.get("source_invocation_id"))
                and observation.get("ok") and quote in observation.get("_result", "")
                and (observation.get("name") == "http_probe"
                     or str(observation.get("name", "")).startswith("mcp__")
                     or observation.get("name") == "tcp_scan")):
            return {"execution_id": observation.get("source_invocation_id") or execution_id, "quote": quote[:500],
                    "tool": observation["name"],
                    "url": observation.get("arguments", {}).get("url", "")}
    return None


def publish_findings(ctx: Any, task: Any, decision: dict, *, actor: str,
                     agent_type: Any, observations: list[dict]) -> list[dict]:
    """任何漏洞类型都可成为候选；无真实引用的声明不能进入复核队列。"""
    workspace = str(ctx.builtin_ctx.get("workspace_root") or "")
    raw_findings = decision.get("findings")
    findings = []
    for raw in raw_findings if isinstance(raw_findings, list) else []:
        if not isinstance(raw, dict):
            continue
        vuln_type, title = raw.get("vuln_type", raw.get("type")), raw.get("title")
        if not isinstance(vuln_type, str) or not vuln_type.strip() or not isinstance(title, str) or not title.strip():
            continue
        evidence = raw.get("evidence")
        evidence = [_evidence(item, workspace, observations) for item in evidence] if isinstance(evidence, list) else []
        evidence = [item for item in evidence if item is not None]
        if not evidence:
            continue
        contract = task.payload.get("investigation_contract")
        contract = contract if isinstance(contract, dict) else {}
        hypothesis_ref = str(contract.get("hypothesis_ref") or "")
        severity = raw.get("severity", "info")
        severity = Severity(severity) if isinstance(severity, str) and severity in {item.value for item in Severity} else Severity.INFO
        endpoint = str(raw.get("endpoint") or task.target)
        refs = list(dict.fromkeys(item.get("execution_id") or item.get("source_invocation_id")
                                 for item in evidence if item.get("execution_id") or item.get("source_invocation_id")))
        vuln = Vulnerability(
            vuln_id=new_id("vuln"), title=title, vuln_type=vuln_type,
            target=task.target, endpoint=endpoint, parameter=str(raw.get("parameter") or ""),
            severity=severity, confidence=0.5, evidence=evidence,
            found_by=actor, hypothesis_ref=hypothesis_ref,
            hypothesis_refs=[hypothesis_ref] if hypothesis_ref else [],
        )
        ctx.convergence.merge_vulnerability(vuln, agent_type)
        findings.append({"type": vuln_type, "endpoint": endpoint, "evidence_refs": refs,
                         "evidence_ref": refs[0] if refs else "", "title": title})
        previous = decision.get("evidence_refs")
        previous = [ref for ref in previous if isinstance(ref, str)] if isinstance(previous, list) else []
        decision["evidence_refs"] = list(dict.fromkeys([*previous, *refs]))
    if decision.get("hypothesis_outcome") == "supporting_signal" and not findings:
        decision.update(status="partial", hypothesis_outcome="inconclusive",
                        reason="候选缺少可核对的工具输出或响应原文引用",
                        missing=["可追溯的候选证据"])
    return findings
