"""专家行动记分卡：只测量当前事实并给校准提示，不作为自动停机门。"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _earliest(rows: list[dict[str, Any]], field: str, *, accepted_only: bool = False) -> str | None:
    values = []
    for row in rows:
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        if accepted_only and str(data.get("status") or "") != "accepted":
            continue
        value = data.get(field)
        if value:
            try:
                values.append((datetime.fromisoformat(str(value)), str(value)))
            except (TypeError, ValueError):
                continue
    return min(values, default=(None, None), key=lambda item: item[0])[1]


def build_scorecard(*, asset_count: int, asset_rows: list[dict[str, Any]],
                    vuln_rows: list[dict[str, Any]], opportunities: dict[str, Any],
                    hypotheses: dict[str, Any], vuln_counts: dict[str, int]) -> dict[str, Any]:
    action = opportunities.get("action_yield") if isinstance(
        opportunities.get("action_yield"), dict) else {}
    portfolio = opportunities.get("portfolio_review") if isinstance(
        opportunities.get("portfolio_review"), dict) else {}
    formal_assets = max(0, int(asset_count))
    attempted_targets = int(portfolio.get("active_targets") or 0)
    effective = action.get("effective_action_rate")
    repeat_rate = hypotheses.get("repeat_attempt_rate")
    accepted = int(vuln_counts.get("accepted") or 0)
    in_pipeline = sum(int(value or 0) for key, value in vuln_counts.items()
                      if key not in {"accepted", "rejected_screen", "rejected_unrepro",
                                     "rejected_fp", "rejected_compliance", "rejected_low"})
    coverage = round(attempted_targets / formal_assets, 3) if formal_assets else None
    signals = int(action.get("detection_signals") or 0)
    tool_calls = int(action.get("tool_calls") or 0)
    productive = int(action.get("productive_tasks") or 0)
    calibration = []
    if formal_assets and attempted_targets < formal_assets:
        calibration.append("仍有已确认资产未形成测试机会或任务，应优先解释未覆盖原因")
    if effective is not None and effective < 0.35:
        calibration.append("有效动作率偏低，应减少同质实验并转向能改变决策的最小验证")
    if repeat_rate is not None and repeat_rate > 0.35:
        calibration.append("假设重复实验比例偏高，下一轮应要求新身份、变量或证据版本")
    if signals:
        calibration.append("已出现防护或限流信号，应降低频率并避免把拦截当作漏洞反证")
    if in_pipeline and not accepted:
        calibration.append("存在候选但尚无独立确认，优先完成鉴真而不是继续扩大枚举")
    return {
        "asset_coverage_rate": coverage,
        "formal_assets": formal_assets,
        "attempted_targets": attempted_targets,
        "effective_action_rate": effective,
        "productive_per_100_tool_calls": (
            round(productive * 100 / tool_calls, 3) if tool_calls else None),
        "repeat_attempt_rate": repeat_rate,
        "detection_signals": signals,
        "accepted_vulnerabilities": accepted,
        "vulnerabilities_in_pipeline": in_pipeline,
        "milestones": {
            "first_verified_asset_at": _earliest(asset_rows, "first_seen"),
            "first_hypothesis_at": hypotheses.get("first_created_at"),
            "first_supported_hypothesis_at": hypotheses.get("first_supported_at"),
            "first_confirmed_vulnerability_at": _earliest(
                vuln_rows, "found_at", accepted_only=True),
        },
        "calibration": calibration[:8],
        "contract": "记分卡是验收与复盘事实，不代表漏洞成立、授权扩大或自动停止条件",
    }
