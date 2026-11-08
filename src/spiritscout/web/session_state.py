"""会话只读状态投影；不参与模型调用、执行调度或用量记账。"""
from __future__ import annotations

import json
from typing import Any

from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import tool_preview
from spiritscout.core.models import BlackboardLayer
from spiritscout.web.sessions import Session


def _read_challenge_progress(session: Session) -> dict[str, Any] | None:
    """只读平台进度快照，刷新页面和重开历史会话沿用同一份完成标记。"""
    if session.mode != "competition":
        return None
    try:
        progress = json.loads((session.workspace / "competition_progress.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        progress = (session.summary.get("run") or {}).get("platform")
    if not isinstance(progress, dict) or "solved_ids" not in progress:
        return None
    solved, unsolved = progress.get("solved_ids"), progress.get("unsolved_ids")
    if not isinstance(solved, list) or not isinstance(unsolved, list):
        return None
    solved_ids = sorted({str(cid) for cid in solved if isinstance(cid, (str, int))})
    unsolved_ids = sorted({str(cid) for cid in unsolved
                           if isinstance(cid, (str, int))} - set(solved_ids))
    return {"total_challenges": len(solved_ids) + len(unsolved_ids),
            "solved_ids": solved_ids, "unsolved_ids": unsolved_ids,
            "updated_at": progress.get("updated_at")}


def _read_state(session: Session) -> dict[str, Any]:
    """数据面快照：短连接只读打开会话黑板（WAL 下与写入侧不互堵）。

    查询配方复用 monitor.panel 的只读组合（层计数 / TASK_STATE / VULN /
    flags.json），输出 JSON 而非文本面板。
    """
    from spiritscout.tools.journal import read_recovery_view
    from spiritscout.web.exploration_stages import read_public_stages
    from spiritscout.web.process_graph import project_blackboard

    run_summary: dict[str, Any] = session.summary.get("run", {})
    # 实时用量：运行中由网关逐笔持久化（usage.db，兼容旧 usage.json），终态回退运行汇总
    live_usage = session.model_usage()
    state: dict[str, Any] = {
        "session": session.brief(include_summary=False),
        # 附件常驻凭据：滑窗滚出首屏后，界面仍能确认附件已随题交付
        "attachments": [
            {"name": p.name, "size": p.stat().st_size}
            for p in sorted((session.workspace / "attachments").iterdir())
            if p.is_file()] if (session.workspace / "attachments").is_dir() else [],
        "recovery": read_recovery_view(session.bb_path, include_inflight=session.status != "running"),
        "layers": {}, "tasks": [],
        "vulns": {"by_status": {}, "items": []},
        "flags": None,
        "challenge_progress": _read_challenge_progress(session),
        "pending_approvals": session.approvals.view(),
        "pending_questions": session.questions.view(),
        "ring": session.ring.snapshot(),
        "user_opinions": session.opinions[-8:],
        "round_views": run_summary.get("round_views", []),
        "decision_rounds": max(len(run_summary.get("round_views", [])),
                               run_summary.get("rounds_executed", 0) if type(run_summary.get("rounds_executed")) is int else 0),
        "model_usage": live_usage or run_summary.get("model_usage"),
        "verify": session.summary.get("verify"),
        "business_context": None,
        "topology": {"dag_nodes": [], "tasks": [], "public_stages": read_public_stages(session.audit_path),
                     "process": {"nodes": [], "edges": []}},
    }
    if not session.bb_path.exists():
        # 黑板未建（排队中）：终态摘要兜底（重开历史会话）
        state["topology"]["dag_nodes"] = run_summary.get("dag_nodes", [])
        return state
    bb = Blackboard(session.bb_path, bus=None, readonly=True)
    try:
        state["topology"]["process"] = project_blackboard(bb)
        # 主控轮次在派发前已落盘，不等整轮所有并行任务结束才有数值。
        dag_state = bb.read(BlackboardLayer.TASK_STATE, 'dag-snapshot') or {}
        if type(dag_state.get('round')) is int and dag_state['round'] >= 0:
            state['decision_rounds'] = max(state['decision_rounds'], dag_state['round'] + 1)
        for layer in BlackboardLayer:
            state["layers"][layer.value] = bb.count(layer)
        for rec in bb.query(BlackboardLayer.TASK_STATE, limit=100):
            data = rec["data"]
            if "dag_snapshot" in data:
                # 主 Agent 每轮落盘的 DAG 快照（拓扑数据源，非任务记录）
                state["topology"]["dag_nodes"] = data["dag_snapshot"]
                continue
            if data.get('kind') == 'scanner_control':
                state['scanner'] = data
                continue
            t = data.get("task", {})
            if not t and data.get('agent_type') == 'scanner' and data.get('task_id'):
                t = {**data, 'task_type': 'scan',
                     'payload': {'node_title': (data.get('delegation') or {}).get('goal', '扫描信息收集')}}
            if not t.get('task_id'):
                continue
            if type(t.get('round_no')) is int and t['round_no'] >= 0:
                state['decision_rounds'] = max(state['decision_rounds'], t['round_no'] + 1)
            state["tasks"].append({
                "task_id": t.get("task_id"), "task_type": t.get("task_type"),
                "title": (t.get("payload") or {}).get("node_title", ""),
                "agent_type": t.get("agent_type"), "status": t.get("status"),
                "round_no": t.get("round_no"),
                "challenge_id": str(((t.get("payload") or {}).get("challenge") or {}).get("id") or ""),
                "result_summary": str(t.get("result_summary") or "")[:200],
                "parent_dag_node": t.get("parent_dag_node"),
                "routes": [
                    {"route_id": r.get("route_id"),
                     "name": r.get("name"), "status": r.get("status"),
                     "health": r.get("health"), "progress": r.get("progress"),
                     "value_score": r.get("value_score"),
                     "risk_score": r.get("risk_score"),
                     "steps_executed": r.get("steps_executed"),
                     "note": (r.get("notes") or [""])[-1][:200]}
                    for r in data.get("routes", [])
                ],
            })
            state["topology"]["tasks"].append({
                "task_id": t.get("task_id"), "task_type": t.get("task_type"),
                "delegation": data.get('delegation'),
                "agent_type": t.get("agent_type"), "status": t.get("status"),
                "round_no": t.get("round_no"),
                "parent_dag_node": t.get("parent_dag_node"),
                "routes_done": sum(1 for r in data.get("routes", [])
                                   if r.get("status") == "done"),
                "routes_total": len(data.get("routes", [])),
                "steps": sum(int(r.get("steps_executed") or 0)
                             for r in data.get("routes", [])),
                "routes": [
                    {"route_id": r.get("route_id"),
                     "name": r.get("name"), "status": r.get("status"),
                     "progress": r.get("progress"),
                     "note": (r.get("notes") or [""])[-1][:200]}
                    for r in data.get("routes", [])
                ],
            })
        if not state["topology"]["dag_nodes"]:
            # 终态会话兜底：摘要中的最终 DAG（如快照记录被清理）
            state["topology"]["dag_nodes"] = run_summary.get("dag_nodes", [])
        state["business_context"] = bb.read(
            BlackboardLayer.TOPOLOGY, "business-context")
        state["observed_evidence"] = [
            {"url": tool_preview({"url": rec["data"].get("value", "")}),
             "record_id": rec["record_id"],
             "method": (rec["data"].get("attrs") or {}).get("method", "GET"),
             "status_code": (rec["data"].get("attrs") or {}).get("status_code"),
             "note": "已观测登记，待业务分析整理"}
            for rec in bb.query(BlackboardLayer.ASSET, limit=100)
            if rec["data"].get("asset_type") in {"endpoint", "app"}]
        by_status: dict[str, int] = {}
        for rec in bb.query(BlackboardLayer.VULN, limit=50):
            d = rec["data"]
            status = str(d.get("status", "candidate"))
            by_status[status] = by_status.get(status, 0) + 1
            state["vulns"]["items"].append({
                "vuln_id": d.get("vuln_id"), "title": d.get("title"),
                "vuln_type": d.get("vuln_type"), "severity": d.get("severity"),
                "status": status, "cvss_score": d.get("cvss_score"),
                "poc_verified": d.get("poc_verified"),
                "version": rec["version"], "vulnerability_ids": d.get("vulnerability_ids", []),
                "verification": d.get("verification", {}),
                "false_positive_reason": d.get("false_positive_reason"),
                "endpoint": d.get("endpoint"),
            })
        state["vulns"]["by_status"] = bb.status_counts(BlackboardLayer.VULN)
        state["vulns"]["total"] = bb.count(BlackboardLayer.VULN)
        state["vulns"]["has_more"] = state["vulns"]["total"] > len(state["vulns"]["items"])
        # 灵枢应答流：拓扑「你 → 灵枢」节点的回执展示
        replies = []
        topology_records = bb.query(BlackboardLayer.TOPOLOGY, limit=400)
        for rec in topology_records:
            d = rec["data"]
            if isinstance(d, dict) and d.get("kind") == "reply":
                replies.append(d)
        replies.sort(key=lambda d: float(d.get("ts") or 0))
        state["master_replies"] = replies[-8:]
        # 发现台账：拓扑「发现」组数据源（共享笔记/入口/Flag 落黑板，跨轮持久）
        findings: list[dict[str, Any]] = []
        for rec in topology_records:
            d = rec["data"]
            if isinstance(d, dict) and d.get("kind") == "finding":
                findings.append({**d, "finding_id": rec["record_id"]})
        findings.sort(key=lambda d: float(d.get("ts") or 0))
        state["findings"] = findings[-80:]
        if session.flags_path.exists():
            try:
                flags = json.loads(
                    session.flags_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                flags = []
            state["flags"] = {
                "total": len(flags),
                "submitted": sum(1 for f in flags if f.get("submitted")),
                "accepted": sum(1 for f in flags if f.get("accepted")),
                "items": [
                    {"value": f.get("value"), "challenge_id": f.get("challenge_id"),
                     "submitted": f.get("submitted"), "accepted": f.get("accepted")}
                    for f in flags[:50]
                ],
            }
    finally:
        bb.close()
    # 黑板保留历次续传记录；状态页以当前 DAG 为准，历史任务仍可在审计中查。
    current_task_ids = {
        str(task_id)
        for node in state["topology"]["dag_nodes"]
        for task_id in node.get("task_ids", [])
    }
    if current_task_ids:
        state["tasks"] = [
            task for task in state["tasks"]
            if str(task.get("task_id")) in current_task_ids
        ]
        state["topology"]["tasks"] = [
            task for task in state["topology"]["tasks"]
            if str(task.get("task_id")) in current_task_ids
        ]
    return state
