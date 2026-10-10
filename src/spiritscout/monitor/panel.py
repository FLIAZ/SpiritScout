"""状态面板渲染与审计导出（4.7）。

面板为纯文本（CLI 场景；Web 面板为 + 适配决策 A5），
数据全部来自黑板 SQLite 与审计 JSONL，只读不写。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spiritscout.blackboard.store import Blackboard
from spiritscout.core.models import BlackboardLayer, VulnStatus

_LAYER_LABELS = {
    BlackboardLayer.TARGET_AUTH: "目标与授权",
    BlackboardLayer.ASSET: "资产",
    BlackboardLayer.RECON_CLUE: "侦察线索",
    BlackboardLayer.VULN: "漏洞",
    BlackboardLayer.CREDENTIAL: "凭据",
    BlackboardLayer.TOPOLOGY: "拓扑",
    BlackboardLayer.TASK_STATE: "任务状态",
    BlackboardLayer.EXPERIENCE: "经验反馈",
}


def render_status(blackboard: Blackboard, *, flags_path: str | Path | None = None) -> str:
    """渲染实时状态面板（子 Agent 集群/路线进度/漏洞核验/赛况）。"""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("灵探 SpiritScout 运行状态面板")
    lines.append("=" * 60)

    # ---- 黑板层统计
    lines.append("\n【黑板八层】")
    for layer, label in _LAYER_LABELS.items():
        lines.append(f"  {label:<6} 记录数: {blackboard.count(layer)}")

    # ---- 任务与路线
    tasks = blackboard.query(BlackboardLayer.TASK_STATE, limit=1000)
    lines.append(f"\n【任务与路线】任务数: {len(tasks)}")
    for rec in tasks[:20]:
        data = rec["data"]
        task = data.get("task", {})
        routes = data.get("routes", [])
        route_str = " ".join(
            f"{r['name']}:{r['status']}({r['steps_executed']}步)"
            for r in routes)
        lines.append(f"  [{task.get('status', '?')}] {task.get('task_type', '?')}"
                     f" → {route_str}")

    # ---- 漏洞核验统计
    vulns = blackboard.query(BlackboardLayer.VULN, limit=10000)
    by_status: dict[str, int] = {}
    for rec in vulns:
        status = rec["data"].get("status", "?")
        by_status[status] = by_status.get(status, 0) + 1
    lines.append("\n【漏洞核验】")
    if vulns:
        for status in VulnStatus:
            if status.value in by_status:
                lines.append(f"  {status.value:<24} {by_status[status.value]}")
    else:
        lines.append("  （暂无漏洞记录）")

    # ---- Flag 统计（CTF/渗透赛）
    if flags_path and Path(flags_path).exists():
        try:
            flags = json.loads(Path(flags_path).read_text(encoding="utf-8"))
            accepted = sum(1 for f in flags if f.get("accepted") is True)
            lines.append(f"\n【Flag】候选 {len(flags)} ｜ 已提交 "
                         f"{sum(1 for f in flags if f.get('submitted'))} ｜ 正确 {accepted}")
        except json.JSONDecodeError:
            lines.append("\n【Flag】存档损坏，无法读取")
    lines.append("")
    return "\n".join(lines)


def export_audit(
    audit_path: str | Path,
    out_path: str | Path,
    *,
    event_type_prefix: str | None = None,
    since_iso: str | None = None,
) -> dict[str, Any]:
    """导出审计日志（可按事件类型前缀/起始时间过滤），返回导出统计。"""
    src = Path(audit_path)
    if not src.exists():
        return {"exported": 0, "skipped": 0, "out": str(out_path)}
    exported = skipped = 0
    out_lines: list[str] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if event_type_prefix and not str(entry.get("type", "")).startswith(event_type_prefix):
            skipped += 1
            continue
        if since_iso and str(entry.get("ts", "")) < since_iso:
            skipped += 1
            continue
        out_lines.append(line)
        exported += 1
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
    return {"exported": exported, "skipped": skipped, "out": str(out)}
