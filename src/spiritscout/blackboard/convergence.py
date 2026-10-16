"""结果汇聚引擎（设计书 4.2.2）：统一采集 → 去重 → 冲突校验 → 聚合视图。

- 全局去重：不同子 Agent 发现的重复资产/漏洞按 dedup_key 合并，保留最完整验证结果；
- 冲突校验：同 dedup_key 不同结论 → 标记冲突、触发二次核验，以验证更充分者为准（6.1）；
- 数据聚合：按资产/漏洞类型/危害等级多维聚合，形成全局统一视图，供主 Agent 决策。
"""

from __future__ import annotations

from typing import Any

from spiritscout.blackboard.store import Blackboard
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    AgentType,
    Asset,
    BlackboardLayer,
    Severity,
    Vulnerability,
    VulnStatus,
)

_TERMINAL_REJECT = {
    VulnStatus.REJECTED_SCREEN,
    VulnStatus.REJECTED_UNREPRO,
    VulnStatus.REJECTED_FP,
    VulnStatus.REJECTED_COMPLIANCE,
    VulnStatus.REJECTED_LOW,
}


class ConvergenceEngine:
    """汇聚引擎：黑板写入前的去重/冲突解决网关 + 轮次级全局视图聚合。"""

    def __init__(self, blackboard: Blackboard):
        self._bb = blackboard
        self.conflicts: list[dict[str, Any]] = []

    # ------------------------------------------------------------ 资产汇聚

    def merge_asset(self, asset: Asset, agent_type: AgentType) -> tuple[str, bool]:
        """写入资产（自动去重合并）。返回 (record_id, 是否新建）。"""
        key = asset.dedup_key()
        existing = self._bb.find_by_dedup_key(BlackboardLayer.ASSET, key)
        if existing is None:
            self._bb.write(
                BlackboardLayer.ASSET,
                asset.asset_id,
                asset.model_dump(mode="json"),
                agent_type=agent_type,
                updated_by=asset.source_agent,
                dedup_key=key,
            )
            return asset.asset_id, True
        # 合并：保留更完整字段（fingerprint/attrs 并集，新的实测覆盖同字段旧值，历史由黑板版本与共享观测保留）
        merged = existing["data"]
        changed = False
        for field_name in ("fingerprint", "attrs"):
            combined = {**merged.get(field_name, {}), **asset.model_dump().get(field_name, {})}
            if combined != merged.get(field_name):
                merged[field_name] = combined
                changed = True
        for field_name in ("service", "port"):
            if not merged.get(field_name) and getattr(asset, field_name):
                merged[field_name] = getattr(asset, field_name)
                changed = True
        if changed:
            self._bb.write(
                BlackboardLayer.ASSET,
                existing["record_id"],
                merged,
                agent_type=agent_type,
                updated_by=asset.source_agent,
                dedup_key=key,
                expected_version=existing["version"],
            )
        return existing["record_id"], False

    # ------------------------------------------------------------ 漏洞汇聚

    def merge_vulnerability(
        self, vuln: Vulnerability, agent_type: AgentType
    ) -> tuple[str, bool, bool]:
        """写入漏洞（去重 + 冲突校验）。返回 (record_id, 是否新建, 是否冲突）。"""
        key = vuln.dedup_key()
        existing = self._bb.find_by_dedup_key(BlackboardLayer.VULN, key)
        if existing is None:
            self._bb.write(
                BlackboardLayer.VULN,
                vuln.vuln_id,
                vuln.model_dump(mode="json"),
                agent_type=agent_type,
                updated_by=vuln.found_by,
                dedup_key=key,
                priority=self._vuln_priority(vuln),
            )
            return vuln.vuln_id, True, False

        prev = Vulnerability.model_validate(existing["data"])
        conflict = self._detect_vuln_conflict(prev, vuln)
        if conflict:
            self.conflicts.append(
                {
                    "conflict_id": new_id("conflict"),
                    "dedup_key": key,
                    "existing": {"record_id": existing["record_id"], "status": prev.status.value,
                                 "severity": prev.severity.value, "by": prev.found_by},
                    "incoming": {"vuln_id": vuln.vuln_id, "status": vuln.status.value,
                                 "severity": vuln.severity.value, "by": vuln.found_by},
                    "resolution": "keep_better_verified",
                }
            )
        # 冲突解决：以验证更充分、证据更完整者为准（6.1）
        winner = self._pick_better_vuln(prev, vuln)
        merged_evidence = self._merge_evidence(prev, vuln)
        if prev.verification and len(merged_evidence) > len(prev.evidence):
            # 新实证交回独立复测，不把“上次误报/上次收录”当成永远有效。
            winner = winner.model_copy(deep=True)
            winner.status = VulnStatus.CANDIDATE
            winner.poc_verified = winner.exploitable = False
            winner.verification = {"verdict": "pending", "previous_verdict": prev.verification.get("verdict")}
        winner.evidence = merged_evidence
        self._bb.write(
            BlackboardLayer.VULN,
            existing["record_id"],
            winner.model_dump(mode="json"),
            agent_type=agent_type,
            updated_by=vuln.found_by,
            dedup_key=key,
            expected_version=existing["version"],
            priority=self._vuln_priority(winner),
        )
        return existing["record_id"], False, conflict

    # ------------------------------------------------------------ 全局视图

    def aggregate_round_view(self, round_no: int) -> dict[str, Any]:
        """轮次结束后的全局汇聚视图，提交主 Agent 决策（4.2.2）。"""
        assets = self._bb.query(BlackboardLayer.ASSET, limit=10000)
        vulns = self._bb.query(BlackboardLayer.VULN, limit=10000)
        creds = self._bb.query(BlackboardLayer.CREDENTIAL, limit=10000)

        vuln_models = [Vulnerability.model_validate(r["data"]) for r in vulns]
        accepted = [v for v in vuln_models if v.status == VulnStatus.ACCEPTED]
        in_pipeline = [v for v in vuln_models if v.status not in _TERMINAL_REJECT and v.status != VulnStatus.ACCEPTED]

        by_severity: dict[str, int] = {}
        for v in accepted:
            by_severity[v.severity.value] = by_severity.get(v.severity.value, 0) + 1
        by_type: dict[str, int] = {}
        for v in vuln_models:
            by_type[v.vuln_type] = by_type.get(v.vuln_type, 0) + 1

        return {
            "round": round_no,
            "asset_count": len(assets),
            "vuln_total": len(vuln_models),
            "vuln_accepted": len(accepted),
            "vuln_in_pipeline": len(in_pipeline),
            "accepted_by_severity": by_severity,
            "vuln_by_type": by_type,
            "credential_count": len(creds),
            "open_conflicts": len(self.conflicts),
            "accepted_vuln_ids": [v.vuln_id for v in accepted],
        }

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _vuln_priority(v: Vulnerability) -> int:
        """高危事件优先（6.9 焦点与优先级）。"""
        if v.severity in (Severity.CRITICAL, Severity.HIGH) and v.status == VulnStatus.ACCEPTED:
            return 1
        if v.severity in (Severity.CRITICAL, Severity.HIGH):
            return 2
        return 5

    @staticmethod
    def _detect_vuln_conflict(a: Vulnerability, b: Vulnerability) -> bool:
        """同去重键但严重度/结论显著不一致 → 冲突（4.2.2 冲突校验）。"""
        if a.status != b.status and (
            a.status in _TERMINAL_REJECT or b.status in _TERMINAL_REJECT
        ):
            return True
        return abs(a.severity.rank - b.severity.rank) >= 2

    @staticmethod
    def _pick_better_vuln(a: Vulnerability, b: Vulnerability) -> Vulnerability:
        """以验证更充分（状态机进度、复现数、PoC 验证、置信度）者为准。"""
        if a.verification or b.verification:
            # 独立复测的拒收/待补证不能被探测侧候选重新升级为收录。
            if a.verification and not b.verification:
                return a
            if b.verification and not a.verification:
                return b
        def score(v: Vulnerability) -> tuple:
            return (
                v.status.value,
                v.poc_verified,
                len(v.reproductions),
                v.confidence,
                len(v.evidence),
            )

        return b if score(b) >= score(a) else a

    @staticmethod
    def _merge_evidence(a: Vulnerability, b: Vulnerability) -> list[dict[str, Any]]:
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for ev in [*a.evidence, *b.evidence]:
            marker = str(sorted(ev.items()))
            if marker not in seen:
                seen.add(marker)
                merged.append(ev)
        return merged
