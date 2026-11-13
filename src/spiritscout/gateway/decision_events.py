"""从世界状态提取会改变专家决策的事件；忽略时间与展示噪声。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def decision_event_view(world: dict[str, Any]) -> dict[str, Any]:
    summary = world.get("intelligence_summary") if isinstance(
        world.get("intelligence_summary"), dict) else {}
    opportunities = world.get("testing_opportunities") if isinstance(
        world.get("testing_opportunities"), dict) else {}
    hypotheses = world.get("vulnerability_hypotheses") if isinstance(
        world.get("vulnerability_hypotheses"), dict) else {}
    prioritized = opportunities.get("prioritized") if isinstance(
        opportunities.get("prioritized"), list) else []
    return {
        "asset_maturity": summary.get("asset_maturity"),
        "business_complexity": summary.get("business_complexity"),
        "js_surface": summary.get("js_surface"),
        "api_surface": summary.get("api_surface"),
        "auth_surface": summary.get("auth_surface"),
        "action_signals": summary.get("action_signals"),
        "hypothesis_counts": hypotheses.get("counts"),
        "review_pending": [item.get("hypothesis_ref") for item in
                           hypotheses.get("review_pending", []) if isinstance(item, dict)],
        "opportunities": [{
            "ref": item.get("opportunity_ref"),
            "revision": item.get("opportunity_revision"),
            "state": item.get("direction_state"),
            "score": item.get("priority_score"),
            "pivot": item.get("pivot_recommendation"),
        } for item in prioritized[:12] if isinstance(item, dict)],
        "portfolio": opportunities.get("portfolio_review"),
        "expert_scorecard": world.get("expert_scorecard"),
    }


def decision_event_fingerprint(world: dict[str, Any]) -> str:
    payload = json.dumps(decision_event_view(world), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()
