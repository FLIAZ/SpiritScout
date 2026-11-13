"""汇聚合并的「模型裁决」契约：后端不排状态优先级、不替模型下结论。

回归：同键冲突曾按状态名的字母序挑赢家——'candidate' 字母序大于
'accepted'、'screened' 大于 'reproduced'，已收录/已验证的结论可能被
更弱的一条顶掉。现在只认「事实载体」：一侧有独立核验（鉴真）背书就
保留有背书的一侧；其余保留既有记录、证据取并集，有新实证经重开路径
交回模型核验重判。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.convergence import ConvergenceEngine
from spiritscout.core.models import (
    AgentType,
    Asset,
    BlackboardLayer,
    Severity,
    Vulnerability,
    VulnStatus,
)


def _vuln(vid: str, *, status: VulnStatus, evidence: list[dict],
          severity: Severity = Severity.HIGH, **kw) -> Vulnerability:
    return Vulnerability(vuln_id=vid, title="t", vuln_type="sqli",
                         target="https://x", endpoint="/a", parameter="id",
                         severity=severity, status=status, evidence=evidence, **kw)


class ConvergenceMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bb = Blackboard(Path(self._tmp.name) / "bb.db")
        self.addCleanup(self.bb.close)
        self.engine = ConvergenceEngine(self.bb)

    def _stored(self) -> Vulnerability:
        rows = self.bb.query(BlackboardLayer.VULN, limit=10)
        self.assertEqual(len(rows), 1)
        return Vulnerability.model_validate(rows[0]["data"])

    def test_endpoint_unknown_observation_upgrades_and_preserves_all_sources(self) -> None:
        url = "https://x.test/api/user"
        unknown_id, is_new = self.engine.merge_asset(Asset(
            asset_id="asset-unknown", asset_type="endpoint", value=url,
            attrs={"method": "UNKNOWN", "params": ["id"], "source": "js_static",
                   "sources": ["js_static"], "source_script_url": "https://x.test/app.js"},
            source_agent="js_reverse"), AgentType.JS_REVERSE)
        self.assertTrue(is_new)
        get_id, is_new = self.engine.merge_asset(Asset(
            asset_id="asset-get", asset_type="endpoint", value=url,
            attrs={"method": "GET", "params": ["page"], "source": "model_observed"},
            source_agent="business_context"), AgentType.BUSINESS_CONTEXT)
        self.assertFalse(is_new)
        self.assertEqual(get_id, unknown_id)
        row = self.bb.read(BlackboardLayer.ASSET, get_id)
        self.assertEqual(row["attrs"]["method"], "GET")
        self.assertEqual(row["attrs"]["params"], ["id", "page"])
        self.assertEqual(row["attrs"]["sources"], ["js_static", "model_observed"])
        self.assertEqual(row["attrs"]["source_script_urls"], ["https://x.test/app.js"])
        self.assertEqual(Asset.model_validate(row).dedup_key(), f"endpoint|GET|{url}")

    def test_same_endpoint_with_different_methods_stays_distinct(self) -> None:
        url = "https://x.test/api/user"
        ids = []
        for method in ("GET", "POST"):
            record_id, is_new = self.engine.merge_asset(Asset(
                asset_id=f"asset-{method.lower()}", asset_type="endpoint", value=url,
                attrs={"method": method}, source_agent="js_reverse"), AgentType.JS_REVERSE)
            self.assertTrue(is_new)
            ids.append(record_id)
        self.assertEqual(len(set(ids)), 2)
        methods = {row["data"]["attrs"]["method"]
                   for row in self.bb.query(BlackboardLayer.ASSET, limit=10)}
        self.assertEqual(methods, {"GET", "POST"})

    def test_unknown_observation_does_not_downgrade_single_known_method(self) -> None:
        url = "https://x.test/api/known"
        record_id, _ = self.engine.merge_asset(Asset(
            asset_id="known-get", asset_type="endpoint", value=url,
            attrs={"method": "GET"}, source_agent="business_context"),
            AgentType.BUSINESS_CONTEXT)
        merged_id, is_new = self.engine.merge_asset(Asset(
            asset_id="unknown", asset_type="endpoint", value=url,
            attrs={"method": "UNKNOWN", "source": "js_static"}, source_agent="js_reverse"),
            AgentType.JS_REVERSE)
        self.assertFalse(is_new)
        self.assertEqual(merged_id, record_id)
        self.assertEqual(self.bb.read(BlackboardLayer.ASSET, record_id)["attrs"]["method"], "GET")

    def test_legacy_endpoint_dedup_key_matches_same_method_before_unknown(self) -> None:
        url = "https://x.test/api/legacy"
        for record_id, method in (("legacy-get", "GET"), ("legacy-unknown", "UNKNOWN")):
            asset = Asset(asset_id=record_id, asset_type="endpoint", value=url,
                          attrs={"method": method}, source_agent="legacy")
            self.bb.write(BlackboardLayer.ASSET, record_id, asset.model_dump(mode="json"),
                          dedup_key=f"endpoint|{url}" if method == "GET" else "")
        record_id, is_new = self.engine.merge_asset(Asset(
            asset_id="incoming-get", asset_type="endpoint", value=url,
            attrs={"method": "GET", "source": "js_static"}, source_agent="js_reverse"),
            AgentType.JS_REVERSE)
        self.assertFalse(is_new)
        self.assertEqual(record_id, "legacy-get")
        self.assertEqual(self.bb.count(BlackboardLayer.ASSET), 2)

    def test_stronger_conclusion_is_not_downgraded_by_name_order(self) -> None:
        self.engine.merge_vulnerability(
            _vuln("v1", status=VulnStatus.ACCEPTED, evidence=[{"m": "a"}]),
            AgentType.WEB_VULN)
        # 修复前：'candidate' 字母序大于 'accepted' → 收录结论被新候选顶掉
        self.engine.merge_vulnerability(
            _vuln("v2", status=VulnStatus.CANDIDATE, evidence=[{"m": "a"}]),
            AgentType.WEB_VULN)
        self.assertEqual(self._stored().status, VulnStatus.ACCEPTED)

    def test_verified_verdict_survives_duplicate_report(self) -> None:
        rejected = _vuln("v1", status=VulnStatus.REJECTED_FP,
                         evidence=[{"m": "a"}],
                         verification={"verdict": "false_positive", "rules_hash": "h"})
        self.engine.merge_vulnerability(rejected, AgentType.WEB_VULN)
        self.engine.merge_vulnerability(
            _vuln("v2", status=VulnStatus.CANDIDATE, evidence=[{"m": "a"}]),
            AgentType.WEB_VULN)
        self.assertEqual(self._stored().status, VulnStatus.REJECTED_FP)

    def test_duplicate_vulnerability_preserves_all_hypothesis_refs(self) -> None:
        first = _vuln("v1", status=VulnStatus.CANDIDATE,
                      evidence=[{"m": "a"}]).model_copy(update={
            "hypothesis_ref": "topology:hypothesis-one@1",
            "hypothesis_refs": ["topology:hypothesis-one@1"],
        })
        second = _vuln("v2", status=VulnStatus.CANDIDATE,
                       evidence=[{"m": "b"}]).model_copy(update={
            "hypothesis_ref": "topology:hypothesis-two@2",
            "hypothesis_refs": ["topology:hypothesis-one@3", "topology:hypothesis-two@2"],
        })

        self.engine.merge_vulnerability(first, AgentType.WEB_VULN)
        self.engine.merge_vulnerability(second, AgentType.WEB_VULN)

        self.assertEqual(self._stored().hypothesis_refs, [
            "topology:hypothesis-one@3", "topology:hypothesis-two@2",
        ])

    def test_conflict_with_new_evidence_requeues_for_model_reverify(self) -> None:
        rejected = _vuln("v1", status=VulnStatus.REJECTED_FP,
                         evidence=[{"m": "a"}],
                         verification={"verdict": "false_positive", "rules_hash": "h"})
        self.engine.merge_vulnerability(rejected, AgentType.WEB_VULN)
        merged = _vuln("v2", status=VulnStatus.CANDIDATE,
                       evidence=[{"m": "a"}, {"m": "b"}])
        _, is_new, conflict = self.engine.merge_vulnerability(merged, AgentType.WEB_VULN)
        self.assertFalse(is_new)
        self.assertTrue(conflict)
        self.assertEqual(self.engine.conflicts[0]["resolution"], "requeue_reverify")
        stored = self._stored()
        self.assertEqual(stored.status, VulnStatus.CANDIDATE)
        self.assertEqual(stored.verification.get("verdict"), "pending")
        self.assertEqual(stored.verification.get("previous_verdict"), "false_positive")
        self.assertEqual(len(stored.evidence), 2)

    def test_conflict_without_new_evidence_keeps_existing_carrier(self) -> None:
        rejected = _vuln("v1", status=VulnStatus.REJECTED_FP,
                         evidence=[{"m": "a"}],
                         verification={"verdict": "false_positive", "rules_hash": "h"})
        self.engine.merge_vulnerability(rejected, AgentType.WEB_VULN)
        self.engine.merge_vulnerability(
            _vuln("v2", status=VulnStatus.CANDIDATE, evidence=[{"m": "a"}]),
            AgentType.WEB_VULN)
        self.assertEqual(self.engine.conflicts[0]["resolution"], "keep_existing")
        self.assertEqual(self._stored().status, VulnStatus.REJECTED_FP)

    def test_same_conclusion_merges_evidence_union(self) -> None:
        self.engine.merge_vulnerability(
            _vuln("v1", status=VulnStatus.CANDIDATE, evidence=[{"m": "a"}]),
            AgentType.WEB_VULN)
        self.engine.merge_vulnerability(
            _vuln("v2", status=VulnStatus.CANDIDATE,
                  evidence=[{"m": "a"}, {"m": "b"}]),
            AgentType.WEB_VULN)
        stored = self._stored()
        self.assertEqual(stored.status, VulnStatus.CANDIDATE)
        self.assertEqual(len(stored.evidence), 2)
        self.assertEqual(self.engine.conflicts, [])


if __name__ == "__main__":
    unittest.main()
