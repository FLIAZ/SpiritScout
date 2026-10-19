"""黑板写权限回归：端口测绘/主机审计的 RECON_CLUE 线索写入。

权限表缺 RECON_CLUE 时，PortScan / HostVuln 路线的线索留档在生产上
直接抛 PolicyViolation「无黑板层 recon_clue 写入权限」——工具调用即挂。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spiritscout.blackboard.layers import can_write
from spiritscout.blackboard.store import Blackboard
from spiritscout.core.models import AgentType, BlackboardLayer


class ReconClueLayerAccessTests(unittest.TestCase):
    def test_port_scan_and_host_vuln_may_write_recon_clue(self) -> None:
        self.assertTrue(can_write(AgentType.PORT_SCAN, BlackboardLayer.RECON_CLUE))
        self.assertTrue(can_write(AgentType.HOST_VULN, BlackboardLayer.RECON_CLUE))

    def test_clue_write_passes_store_permission_gate(self) -> None:
        for agent in (AgentType.PORT_SCAN, AgentType.HOST_VULN):
            with self.subTest(agent=agent.value), \
                    tempfile.TemporaryDirectory() as tmp:
                bb = Blackboard(Path(tmp) / "bb.db")
                version = bb.write(
                    BlackboardLayer.RECON_CLUE, f"clue-{agent.value}",
                    {"clue_type": "tech_stack", "value": "nginx", "confidence": 0.7},
                    agent_type=agent, dedup_key=f"clue|tech_stack|{agent.value}")
                self.assertEqual(version, 1)


if __name__ == "__main__":
    unittest.main()
