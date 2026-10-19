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
