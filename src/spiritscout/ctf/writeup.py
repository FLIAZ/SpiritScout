"""由主 Agent 汇总题目记录并生成解题报告。"""

from __future__ import annotations

import json
import re
from typing import Any

from spiritscout.core.audit import AuditLogger, NullAuditLogger

# 喂给写手的材料上限（字符）：路线笔记按路线截尾，超出部分说明取舍
_DIGIT_BUDGET = 36000
_NOTES_PER_ROUTE = 8
_NOTE_CHARS = 500
# 干净收尾的 finish_reason：chat completions 是 stop、anthropic 原生是
# end_turn/stop_sequence、Responses 协议是 completed。流被中转站中途掐断时
# LiteLLM 不抛错、只交回半截正文，finish_reason 停在空值——不在集合内即视为截断
_CLEAN_FINISH = frozenset({"stop", "end_turn", "stop_sequence", "completed"})

_WRITER_SYSTEM = (
    "你是灵枢，智能体集群的总负责人。整场作业已经结束，现在由你亲自为每道"
    "赛题撰写解题报告（WriteUp），面向队员复盘与赛后留档。\n"
    "写作要求：\n"
    "- 开头一段全场总览（做了哪些题、战果如何、整体思路）；\n"
    "- 每题一节：题面要点 → 关键过程（按「看到了什么 → 怎么推的 → 试了什么 →"
    " 哪一步破了题」的顺序讲成连贯故事，不逐条罗列笔记）→ Flag 与验证状态 →"
    " 值得沉淀的经验或未竟方向；\n"
    "- 只使用给定材料中的事实：材料是各路线的原始执行笔记与发现台账，"
    "你来提炼、去噪、成文；材料没有的步骤不得编造；\n"
    "- 中文，Markdown，直接输出完整文档正文（不要代码块包裹，不要 JSON，"
    "不要寒暄）。"
)


def build_digest(
    *,
    challenges: list[dict[str, Any]],
    flags: list[Any],
    run_summary: dict[str, Any],
    blackboard: Any,
) -> dict[str, Any]:
    """全场材料汇总：任务路线完整笔记 + 发现台账 + Flag 状态 + 决策脉络。"""
    from spiritscout.core.models import BlackboardLayer

    tasks: list[dict[str, Any]] = []
    for rec in blackboard.query(BlackboardLayer.TASK_STATE, limit=100):
        data = rec["data"]
        if "dag_snapshot" in data:
            continue
        t = data.get("task", {})
        if not t:
            continue
        tasks.append({
            "agent": t.get("agent_type"),
            "node": t.get("payload", {}).get("node_title", ""),
            "status": t.get("status"),
            "routes": [
                {"name": r.get("name"), "status": r.get("status"),
                 "notes": [str(n)[:_NOTE_CHARS]
                           for n in (r.get("notes") or [])[-_NOTES_PER_ROUTE:]]}
                for r in data.get("routes", [])
            ],
        })
    findings = []
    for rec in blackboard.query(BlackboardLayer.TOPOLOGY, limit=400):
        d = rec["data"]
        if isinstance(d, dict) and d.get("kind") == "finding":
            findings.append({"type": d.get("type"), "text": str(d.get("text", ""))[:200],
                             "by": d.get("by"), "evidence_refs": d.get("evidence_refs", [])})
    return {
        "challenges": [
            {"id": str(c.get("id") or ""), "title": str(c.get("title") or ""),
             "url": str(c.get("url") or ""),
             "direction": str(c.get("direction") or ""),
             "description": str(c.get("description") or "")[:400]}
            for c in challenges
        ],
        "flags": [
            {"value": getattr(f, "value", ""), "challenge_id": getattr(f, "challenge_id", ""),
             "found_by": getattr(f, "found_by", ""), "submitted": getattr(f, "submitted", False),
             "accepted": getattr(f, "accepted", None)}
            for f in flags
        ],
        "rounds": run_summary.get("rounds_executed", 0),
        "dag": [
            {"title": n.get("title"), "status": n.get("status")}
            for n in run_summary.get("dag_nodes", [])
        ][:30],
        "routes_notes": tasks,
        "findings": findings[-60:],
    }


async def writeup_body(
    gateway: Any, *, digest: dict[str, Any], audit: AuditLogger | None = None,
) -> str | None:
    """灵枢撰写 WriteUp 正文（Markdown）；不可用/空产出返回 None。

    半截正文宁可重写也不当作成品：截断的流重试一次，两次都不完整时保留
    较长者并在审计里如实记 ``truncated``（而不是谎报 ok）。
    """
    audit = audit or NullAuditLogger()
    messages = [
        {"role": "system", "content": _WRITER_SYSTEM + (
            "\n这是无提交接口的单题解答，按 local_answer_completion 说明答案完整性与证据确认状态，"
            "不要写等待人工提交或虚构平台判定。" if digest.get("local_answer_completion") else "")},
        {"role": "user", "content":
            json.dumps(digest, ensure_ascii=False, separators=(",", ":"))[:_DIGIT_BUDGET]},
    ]
    best = ""
    clean = False
    for attempt in (1, 2):
        try:
            resp = await gateway.chat(
                messages,
                task_type="meta_planning", max_tokens=8192, timeout_seconds=420,
                # 战报综合是散文写作不是规划：限定低思考强度（不限时单次可磨
                # 100 秒+，占收尾等待的大头）
                reasoning_effort="low",
            )
        except Exception as exc:  # noqa: BLE001 —— 写手失败回退模板，报告不缺失
            audit.log("report.writeup", "master",
                      {"result": "failed", "error": str(exc)[:200]})
            return None
        body = (resp.content or "").strip()
        # 剥掉误包裹的 Markdown 代码围栏（模型偶发 ```markdown … ```）
        if body.startswith("```"):
            body = re.sub(r"^```[a-z]*\n|\n```$", "", body).strip()
        finish = str(getattr(resp, "finish_reason", "") or "").strip().lower()
        if body and finish in _CLEAN_FINISH:
            best, clean = body, True
            break
        audit.log("report.writeup", "master",
                  {"result": "truncated" if body else "empty", "chars": len(body),
                   "finish_reason": finish, "attempt": attempt})
        if len(body) > len(best):
            best = body
    if not best:
        return None
    audit.log("report.writeup", "master",
              {"result": "ok" if clean else "truncated", "chars": len(best)})
    return best
