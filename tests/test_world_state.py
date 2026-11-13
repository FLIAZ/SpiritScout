"""世界状态片段：系统提示保持稳定，授权扩围只追加自包含的变化片段。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.profiles import agent_system_prompt
from spiritscout.blackboard import Blackboard
from spiritscout.blackboard.hypotheses import summary as hypothesis_summary
from spiritscout.blackboard.hypotheses import transition, upsert
from spiritscout.blackboard.leads import upsert_lead
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    RunMode,
    Scope,
    TaskStatus,
)
from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.gateway import world_state
from spiritscout.gateway.context import decision_messages
from spiritscout.policy.scope import ScopeGuard


class WorldStateSnapshotTests(unittest.TestCase):
    def test_expert_scorecard_reports_coverage_and_calibration_without_stop_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "asset-a", {
                "asset_id": "asset-a", "asset_type": "endpoint",
                "value": "https://a.test/api/orders", "attrs": {},
            })
            bb.write(BlackboardLayer.TASK_STATE, "task-a", {
                "task": {"task_id": "task-a", "status": "done",
                         "target": "https://a.test/api/orders", "agent_type": "web_vuln",
                         "payload": {}},
                "summary": {"status": "done", "findings": [],
                            "execution_feedback": {"tool_calls": 4, "duration_ms": 1200,
                                                   "detection_signals": 1},
                            "handoff": {"remaining_unknowns": ["对象边界仍未知"]}},
            })

            scorecard = world_state.snapshot(blackboard=bb)["expert_scorecard"]

            self.assertEqual(scorecard["formal_assets"], 1)
            self.assertEqual(scorecard["attempted_targets"], 1)
            self.assertEqual(scorecard["asset_coverage_rate"], 1.0)
            self.assertEqual(scorecard["detection_signals"], 1)
            self.assertTrue(scorecard["calibration"])
            self.assertIn("不代表漏洞成立", scorecard["contract"])

    def test_expert_scorecard_uses_full_asset_count_beyond_display_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            for index in range(205):
                bb.write(BlackboardLayer.ASSET, f"asset-{index}", {
                    "asset_id": f"asset-{index}", "asset_type": "domain",
                    "value": f"a{index}.test", "attrs": {},
                })

            scorecard = world_state.snapshot(blackboard=bb)["expert_scorecard"]

            self.assertEqual(scorecard["formal_assets"], 205)

    def test_dag_preserves_investigation_contract_for_task_handoff(self) -> None:
        dag = TaskDAG()
        result = dag.upsert_from_plan([{
            "tactic": "TA0043", "title": "核实接口真实性", "depends_on": [],
            "expected_information": "路径是否源于真实业务流量",
            "decision_impact": "决定是否派发深入业务测试",
            "why_now": "当前候选来自猜测路径",
            "next_minimal_experiment": "比较首页与候选响应摘要",
            "stop_condition": "内容一致且无接口标记时关闭",
            "unknowns_to_resolve": ["候选路径是否来自真实业务流量"],
            "evidence_refs": ["asset:candidate@1"],
            "hypothesis_ref": "topology:hypothesis-fixture@2",
        }], 1)
        contract = result.created[0].meta["investigation_contract"]
        self.assertEqual(contract["expected_information"], "路径是否源于真实业务流量")
        self.assertEqual(contract["remaining_unknowns"], ["候选路径是否来自真实业务流量"])
        self.assertEqual(contract["evidence_refs"], ["asset:candidate@1"])
        self.assertEqual(contract["hypothesis_ref"], "topology:hypothesis-fixture@2")

    def test_plan_resolution_normalizes_hypothesis_ref_and_rejects_unknown(self) -> None:
        master = object.__new__(MasterAgent)
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            ref = upsert(
                bb, claim="接口可能存在对象级权限问题", subject="https://a.test/api/user",
                test_type="idor", actor="js-reverse", agent_type=AgentType.JS_REVERSE,
            )["hypothesis_ref"]
            master.ctx = SimpleNamespace(blackboard=bb, extra={})
            resolved = master._resolve_plan_challenges([{
                "tactic": "WEB-VULN", "hypothesis_ref": ref,
            }])[0]
            self.assertEqual(resolved["hypothesis_ref"], ref)
            with self.assertRaisesRegex(SpiritScoutError, "hypothesis_ref"):
                master._resolve_plan_challenges([{
                    "tactic": "WEB-VULN", "hypothesis_ref": "topology:missing@1",
                }])

    def test_world_state_projects_hypothesis_lifecycle_and_quality_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            supported = upsert(
                bb, claim="组件可能受 CVE-2026-0001 影响", subject="https://a.test",
                test_type="cve", technical_basis={"cve_ids": ["CVE-2026-0001"]},
                actor="recon", agent_type=AgentType.RECON,
            )["hypothesis_ref"]
            blocked = upsert(
                bb, claim="接口可能缺少对象权限校验", subject="https://a.test/api/user",
                test_type="idor", next_minimal_experiment="比较两个账号读取结果",
                stop_condition="对象归属校验一致时关闭", actor="js-reverse",
                agent_type=AgentType.JS_REVERSE,
            )["hypothesis_ref"]
            transition(
                bb, supported, state="supported", reason="模板命中待复核",
                task_id="scan-1", evidence_refs=["scan-finding-1"],
                outcome="supporting_signal", actor="scanner", agent_type=AgentType.SCANNER,
            )
            transition(
                bb, blocked, state="blocked", reason="缺少第二账号",
                task_id="web-1", outcome="missing_prerequisite",
                actor="web", agent_type=AgentType.WEB_VULN,
            )
            metrics = hypothesis_summary(bb)
            projected = world_state.snapshot(blackboard=bb)["vulnerability_hypotheses"]

            self.assertEqual(metrics["by_state"], {"blocked": 1, "supported": 1})
            self.assertEqual(metrics["supported"], 1)
            self.assertEqual(len(metrics["review_pending"]), 1)
            self.assertEqual(metrics["independently_confirmed"], 0)
            self.assertEqual(metrics["blocked_ratio"], 0.5)
            self.assertEqual(metrics["repeat_attempt_rate"], 0.0)
            self.assertIsNotNone(metrics["first_supported_at"])
            self.assertIsNotNone(metrics["time_to_first_supported_seconds"])
            self.assertEqual(projected["by_state"], metrics["by_state"])
            self.assertEqual(projected["active"][0]["hypothesis_ref"].split("@")[0],
                             blocked.split("@")[0])

            latest_supported = next(
                row for row in bb.query(BlackboardLayer.TOPOLOGY)
                if row["data"].get("state") == "supported")
            supported_ref = f"topology:{latest_supported['record_id']}@{latest_supported['version']}"
            confirmed = transition(
                bb, supported_ref, state="supported", reason="独立复核确认危害",
                task_id="verify-confirmed", outcome="independently_confirmed",
                actor="verifier", agent_type=AgentType.VERIFIER,
            )["hypothesis_ref"]
            transition(
                bb, confirmed, state="supported", reason="晚到的扫描支持信号",
                task_id="scan-late", outcome="supporting_signal",
                actor="scanner", agent_type=AgentType.SCANNER,
            )
            settled = hypothesis_summary(bb)
            self.assertEqual(settled["review_pending"], [])
            self.assertEqual(settled["independently_confirmed"], 1)

            latest_supported = next(
                row for row in bb.query(BlackboardLayer.TOPOLOGY)
                if row["data"].get("state") == "supported")
            supported_ref = f"topology:{latest_supported['record_id']}@{latest_supported['version']}"
            transition(
                bb, supported_ref, state="refuted", reason="独立复核未能复现模板信号",
                task_id="verify-1", outcome="independent_refutation",
                actor="verifier", agent_type=AgentType.VERIFIER,
            )
            self.assertEqual(
                next(row for row in bb.query(BlackboardLayer.TOPOLOGY)
                     if row["record_id"] == latest_supported["record_id"])["data"]["state"],
                "refuted",
            )
            with self.assertRaisesRegex(ValueError, "不能从 refuted"):
                transition(
                    bb, supported_ref, state="blocked", reason="迟到的扫描取消",
                    actor="scanner", agent_type=AgentType.SCANNER,
                )

            master = object.__new__(MasterAgent)
            master.ctx = SimpleNamespace(blackboard=bb, extra={})
            with self.assertRaisesRegex(SpiritScoutError, "refuted"):
                master._resolve_plan_challenges([{
                    "tactic": "WEB-VULN",
                    "hypothesis_ref": f"topology:{latest_supported['record_id']}",
                }])

    def test_testing_opportunities_prioritize_review_and_penalize_empty_repetition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            hypothesis = upsert(
                bb, claim="接口可能缺少对象权限校验",
                subject="https://a.test/api/orders", test_type="idor",
                expected_signal="替换对象编号后返回其他账号订单",
                counter_signal="服务端始终按会话账号过滤对象",
                next_minimal_experiment="使用两个授权账号比较同一订单请求",
                stop_condition="跨账号请求稳定拒绝时关闭",
                actor="js-reverse", agent_type=AgentType.JS_REVERSE,
            )["hypothesis_ref"]
            transition(
                bb, hypothesis, state="supported", reason="观察到跨对象响应差异",
                task_id="web-signal", outcome="supporting_signal",
                actor="web-vuln", agent_type=AgentType.WEB_VULN,
            )
            bb.write(BlackboardLayer.TOPOLOGY, "lead-generated", {
                "kind": "lead_card", "lead_id": "lead-generated",
                "subject": "https://a.test/guessed-admin", "status": "needs_disambiguation",
                "origins": ["model_generated_test"], "origin_class": "generated_candidate",
                "evidence_state": "unverified", "unknowns": ["该路径是否是真实独立入口"],
                "next_minimal_experiment": "与不存在路径响应轮廓比较",
                "observation_count": 2,
            })
            generated_ref = "opportunity:fixture"
            # 先取真实稳定引用，再写两次没有新事实的完成任务，模拟换标题空转。
            initial = world_state.snapshot(blackboard=bb)["testing_opportunities"]
            generated = next(item for item in initial["prioritized"]
                             if item["subject"].endswith("guessed-admin"))
            generated_ref = generated["opportunity_ref"]
            generated_revision = generated["opportunity_revision"]
            for index in range(2):
                bb.write(BlackboardLayer.TASK_STATE, f"empty-{index}", {
                    "task": {"status": "done", "payload": {"investigation_contract": {
                        "opportunity_ref": generated_ref,
                        "opportunity_revision": generated_revision,
                    }}},
                    "summary": {"status": "done", "findings": [], "handoff": {
                        "established_facts": [], "resolved_unknowns": [],
                        "remaining_unknowns": ["该路径是否是真实独立入口"]}},
                })

            pool = world_state.snapshot(blackboard=bb)["testing_opportunities"]
            review = pool["prioritized"][0]
            repeated = next(item for item in pool["prioritized"]
                            if item["opportunity_ref"] == generated_ref)

            self.assertEqual(review["state"], "review_pending")
            self.assertEqual(review["recommended_work"], "independent_verification")
            self.assertGreater(review["priority_score"], repeated["priority_score"])
            self.assertTrue(repeated["stalled"])
            self.assertEqual(repeated["attempts"]["attempts"], 2)
            self.assertEqual(pool["stalled"][0]["opportunity_ref"], generated_ref)
            self.assertEqual(pool["action_yield"]["empty_completion_count"], 2)
            self.assertEqual(pool["action_yield"]["effective_action_rate"], 0.0)

    def test_plan_opportunity_reference_inherits_contract_and_orders_ready_nodes(self) -> None:
        master = object.__new__(MasterAgent)
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, "lead-api", {
                "kind": "lead_card", "lead_id": "lead-api",
                "subject": "https://a.test/api/orders", "status": "actionable",
                "origins": ["page_network"], "origin_class": "target_observation",
                "evidence_state": "verified", "unknowns": ["接口是否存在对象权限边界"],
                "next_minimal_experiment": "比较两个授权账号读取同一对象",
                "observation_count": 1,
            })
            master.ctx = SimpleNamespace(
                blackboard=bb, extra={}, tactic_map=None,
                policy=SimpleNamespace(mode=RunMode.SRC),
            )
            opportunity = world_state.snapshot(blackboard=bb)[
                "testing_opportunities"]["prioritized"][0]

            resolved = master._resolve_plan_challenges([{
                "tactic": "TA0001", "opportunity_ref": opportunity["opportunity_ref"],
                "why_now": "该机会是当前最高优先级的真实接口边界",
            }])[0]
            self.assertEqual(resolved["target"], "https://a.test/api/orders")
            self.assertEqual(resolved["opportunity_revision"],
                             opportunity["opportunity_revision"])
            self.assertEqual(resolved["opportunity_score"], opportunity["priority_score"])
            self.assertIn("对象权限", resolved["expected_information"])
            self.assertEqual(resolved["next_minimal_experiment"],
                             "比较两个授权账号读取同一对象")
            self.assertTrue(resolved["evidence_refs"])
            with self.assertRaisesRegex(SpiritScoutError, "opportunity_ref"):
                master._resolve_plan_challenges([{
                    "tactic": "TA0001", "opportunity_ref": "opportunity:missing",
                }])

            dag = TaskDAG()
            result = dag.upsert_from_plan([
                {"tactic": "TA0043", "title": "低优先级", "opportunity_ref": "opportunity:low",
                 "opportunity_score": 20},
                {**resolved, "title": "高优先级"},
            ], 1)
            self.assertEqual([node.title for node in dag.ready_nodes()], ["高优先级", "低优先级"])
            contract = next(node for node in result.created if node.title == "高优先级").meta[
                "investigation_contract"]
            self.assertEqual(contract["opportunity_ref"], opportunity["opportunity_ref"])
            self.assertEqual(contract["opportunity_revision"],
                             opportunity["opportunity_revision"])
            self.assertEqual(contract["opportunity_score"], opportunity["priority_score"])
            self.assertTrue(contract["capability_candidates"])

    def test_opportunity_capabilities_drive_fallback_without_fixed_site_routing(self) -> None:
        master = object.__new__(MasterAgent)
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "observed-endpoint", {
                "asset_id": "observed-endpoint", "asset_type": "endpoint",
                "value": "https://a.test/api/orders",
                "attrs": {"method": "GET", "params": ["order_id"],
                          "sources": ["page_network", "target_response"]},
            })
            master.ctx = SimpleNamespace(
                blackboard=bb, extra={}, tactic_map=None,
                policy=SimpleNamespace(mode=RunMode.SRC),
            )

            pool = world_state.snapshot(blackboard=bb)["testing_opportunities"]
            opportunity = pool["prioritized"][0]
            candidates = opportunity["capability_candidates"]
            self.assertEqual(candidates[0]["tactic"], "TA0001")
            self.assertTrue(candidates[0]["preferred"])
            self.assertIn("JS-REVERSE", [item["tactic"] for item in candidates])

            fallback = master._rule_plan({})["dag_nodes"][0]
            self.assertEqual(fallback["tactic"], "TA0001")
            self.assertEqual(fallback["opportunity_ref"], opportunity["opportunity_ref"])
            resolved = master._resolve_plan_challenges([fallback])[0]
            self.assertEqual(resolved["selected_capability"]["capability"],
                             "web_boundary_test")

    def test_scanner_and_verification_opportunities_are_not_forced_into_dag(self) -> None:
        master = object.__new__(MasterAgent)
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            hypothesis = upsert(
                bb, claim="组件版本可能受 CVE-2026-0001 影响",
                subject="https://a.test", test_type="cve",
                technical_basis={"cve_ids": ["CVE-2026-0001"],
                                 "template_ids": ["fixture-cve"]},
                actor="recon", agent_type=AgentType.RECON,
            )["hypothesis_ref"]
            master.ctx = SimpleNamespace(
                blackboard=bb, extra={}, tactic_map=None,
                policy=SimpleNamespace(mode=RunMode.SRC),
            )
            scanner_opportunity = world_state.snapshot(blackboard=bb)[
                "testing_opportunities"]["prioritized"][0]
            scanner_candidates = scanner_opportunity["capability_candidates"]
            self.assertEqual(scanner_candidates[0]["execution"], "scanner_control")
            self.assertEqual(scanner_candidates[0]["tactic"], "SCANNER-CONTROL")
            with self.assertRaisesRegex(SpiritScoutError, "扫描控制通道"):
                master._resolve_plan_challenges([{
                    "tactic": "TA0001",
                    "opportunity_ref": scanner_opportunity["opportunity_ref"],
                }])

            transition(
                bb, hypothesis, state="supported", reason="定向模板获得支持信号",
                task_id="scan-1", outcome="supporting_signal",
                actor="scanner", agent_type=AgentType.SCANNER,
            )
            review = world_state.snapshot(blackboard=bb)[
                "testing_opportunities"]["prioritized"][0]
            self.assertEqual(review["capability_candidates"][0]["execution"],
                             "verification_queue")
            with self.assertRaisesRegex(SpiritScoutError, "独立复核"):
                master._resolve_plan_challenges([{
                    "tactic": "TA0001", "opportunity_ref": review["opportunity_ref"],
                }])

    def test_new_lead_evidence_changes_revision_and_clears_stall_penalty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            upsert_lead(
                bb, subject="https://a.test/api/profile", method="GET",
                origins=["js_static"], source_refs=["asset:script@1"],
                evidence_state="unverified", status="needs_disambiguation",
                unknowns=["接口是否来自当前部署版本"],
                next_minimal_experiment="从页面网络请求中确认接口是否被实际调用",
                actor="js-reverse", agent_type=AgentType.JS_REVERSE,
            )
            initial_pool = world_state.snapshot(blackboard=bb)["testing_opportunities"]
            initial = initial_pool["prioritized"][0]
            for index in range(2):
                bb.write(BlackboardLayer.TASK_STATE, f"empty-revision-{index}", {
                    "task": {"status": "done", "payload": {"investigation_contract": {
                        "opportunity_ref": initial["opportunity_ref"],
                        "opportunity_revision": initial["opportunity_revision"],
                    }}},
                    "summary": {"status": "done", "findings": [], "handoff": {
                        "resolved_unknowns": [],
                        "remaining_unknowns": ["接口是否来自当前部署版本"],
                    }},
                })

            stalled = world_state.snapshot(blackboard=bb)["testing_opportunities"]
            self.assertTrue(stalled["prioritized"][0]["stalled"])

            upsert_lead(
                bb, subject="https://a.test/api/profile", method="GET",
                origins=["page_network"], source_refs=["asset:endpoint@1"],
                evidence_state="verified", status="actionable",
                unknowns=["接口是否存在未授权读取"], replace_unknowns=True,
                next_minimal_experiment="比较匿名请求与授权请求的响应边界",
                actor="recon", agent_type=AgentType.RECON,
            )
            refreshed_pool = world_state.snapshot(blackboard=bb)["testing_opportunities"]
            refreshed = refreshed_pool["prioritized"][0]

            self.assertEqual(refreshed["opportunity_ref"], initial["opportunity_ref"])
            self.assertNotEqual(refreshed["opportunity_revision"],
                                initial["opportunity_revision"])
            self.assertFalse(refreshed["stalled"])
            self.assertEqual(refreshed["attempts"], {})
            self.assertEqual(refreshed_pool["action_yield"]["empty_completion_count"], 2)

    def test_empty_capability_pivots_before_opportunity_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "observed-endpoint", {
                "asset_id": "observed-endpoint", "asset_type": "endpoint",
                "value": "https://a.test/api/orders",
                "attrs": {"method": "GET", "params": ["order_id"],
                          "sources": ["page_network", "target_response"]},
            })
            master = object.__new__(MasterAgent)
            master.ctx = SimpleNamespace(
                blackboard=bb, extra={}, tactic_map=None,
                policy=SimpleNamespace(mode=RunMode.SRC),
            )
            initial = world_state.snapshot(blackboard=bb)["testing_opportunities"][
                "prioritized"][0]

            def empty_attempt(index: int, capability: dict[str, str]) -> None:
                bb.write(BlackboardLayer.TASK_STATE, f"empty-{capability['capability']}-{index}", {
                    "task": {"status": "done", "target": initial["subject"],
                             "payload": {"tactic": capability["tactic"],
                                         "investigation_contract": {
                        "opportunity_ref": initial["opportunity_ref"],
                        "opportunity_revision": initial["opportunity_revision"],
                        "selected_capability": capability,
                    }}},
                    "summary": {"status": "done", "findings": [], "handoff": {
                        "resolved_unknowns": [], "remaining_unknowns": initial["unknowns"],
                    }},
                })

            web = next(item for item in initial["capability_candidates"]
                       if item["tactic"] == "TA0001")
            for index in range(2):
                empty_attempt(index, web)

            pivoted = world_state.snapshot(blackboard=bb)["testing_opportunities"][
                "prioritized"][0]
            self.assertEqual(pivoted["direction_state"], "diminishing")
            self.assertFalse(pivoted["exhausted"])
            self.assertEqual(next(item for item in pivoted["capability_yield"]
                                  if item["capability"] == web["capability"])[
                                      "consecutive_empty"], 2)
            fallback = master._rule_plan({})["dag_nodes"][0]
            self.assertEqual(fallback["tactic"], "JS-REVERSE")

            js = next(item for item in pivoted["capability_candidates"]
                      if item["tactic"] == "JS-REVERSE")
            for index in range(2):
                empty_attempt(index, js)
            exhausted = world_state.snapshot(blackboard=bb)["testing_opportunities"][
                "prioritized"][0]
            self.assertTrue(exhausted["exhausted"])
            self.assertEqual(exhausted["direction_state"], "exhausted")

    def test_portfolio_review_exposes_concentration_and_user_resolvable_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, "lead-login", {
                "kind": "lead_card", "lead_id": "lead-login",
                "subject": "https://a.test/login", "status": "actionable",
                "origins": ["page_network"], "origin_class": "target_observation",
                "evidence_state": "verified", "unknowns": ["登录后接口是否存在权限边界"],
            })
            initial = world_state.snapshot(blackboard=bb)["testing_opportunities"][
                "prioritized"][0]
            capability = initial["capability_candidates"][0]
            blocker = {
                "type": "authentication_required", "description": "公开入口止于登录墙",
                "prerequisite": "测试账号或登录后请求记录", "user_resolvable": True,
                "alternative_capabilities": ["script_and_api_extraction"],
                "evidence_refs": ["asset:login@1"],
            }
            for index in range(3):
                bb.write(BlackboardLayer.TASK_STATE, f"blocked-{index}", {
                    "task": {"status": "done", "target": initial["subject"],
                             "payload": {"tactic": capability["tactic"],
                                         "investigation_contract": {
                        "opportunity_ref": initial["opportunity_ref"],
                        "opportunity_revision": initial["opportunity_revision"],
                        "selected_capability": capability,
                    }}},
                    "summary": {"status": "done", "findings": [], "handoff": {
                        "blockers": [blocker], "resolved_unknowns": [],
                        "remaining_unknowns": initial["unknowns"],
                    }},
                })

            pool = world_state.snapshot(blackboard=bb)["testing_opportunities"]
            opportunity = pool["prioritized"][0]
            review = pool["portfolio_review"]
            self.assertEqual(opportunity["direction_state"], "blocked")
            self.assertIn("索取", opportunity["pivot_recommendation"])
            self.assertTrue(review["target_concentration"]["over_concentrated"])
            self.assertEqual(review["user_resolvable_blockers"][0]["type"],
                             "authentication_required")

    def test_productive_action_resets_capability_decay_on_same_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "observed-endpoint", {
                "asset_id": "observed-endpoint", "asset_type": "endpoint",
                "value": "https://a.test/api/profile",
                "attrs": {"method": "GET", "params": ["id"],
                          "sources": ["page_network", "target_response"]},
            })
            initial = world_state.snapshot(blackboard=bb)["testing_opportunities"][
                "prioritized"][0]
            capability = next(item for item in initial["capability_candidates"]
                              if item["tactic"] == "TA0001")

            def write(index: int, useful: bool) -> None:
                bb.write(BlackboardLayer.TASK_STATE, f"attempt-{index}", {
                    "task": {"status": "done", "target": initial["subject"],
                             "payload": {"tactic": capability["tactic"],
                                         "investigation_contract": {
                        "opportunity_ref": initial["opportunity_ref"],
                        "opportunity_revision": initial["opportunity_revision"],
                        "selected_capability": capability,
                    }}},
                    "summary": {"status": "done",
                                "findings": ([{"type": "boundary_fact"}] if useful else []),
                                "handoff": {"resolved_unknowns": [],
                                            "remaining_unknowns": initial["unknowns"]}},
                })

            write(0, False)
            write(1, False)
            self.assertEqual(world_state.snapshot(blackboard=bb)["testing_opportunities"][
                "prioritized"][0]["direction_state"], "diminishing")
            write(2, True)
            refreshed = world_state.snapshot(blackboard=bb)["testing_opportunities"][
                "prioritized"][0]
            web_yield = next(item for item in refreshed["capability_yield"]
                             if item["capability"] == capability["capability"])
            self.assertEqual(web_yield["consecutive_empty"], 0)
            self.assertEqual(refreshed["direction_state"], "productive")

    def test_resolved_blocker_is_not_kept_as_current_direction_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, "lead-login", {
                "kind": "lead_card", "lead_id": "lead-login",
                "subject": "https://a.test/login", "status": "actionable",
                "origins": ["page_network"], "origin_class": "target_observation",
                "evidence_state": "verified", "unknowns": ["登录后接口边界"],
            })
            initial = world_state.snapshot(blackboard=bb)["testing_opportunities"][
                "prioritized"][0]
            capability = initial["capability_candidates"][0]
            common_task = {"status": "done", "target": initial["subject"],
                           "payload": {"tactic": capability["tactic"],
                                       "investigation_contract": {
                "opportunity_ref": initial["opportunity_ref"],
                "opportunity_revision": initial["opportunity_revision"],
                "selected_capability": capability,
            }}}
            bb.write(BlackboardLayer.TASK_STATE, "blocked-first", {
                "task": common_task,
                "summary": {"status": "done", "findings": [], "handoff": {
                    "blockers": [{
                        "type": "authentication_required", "description": "缺少测试账号",
                        "prerequisite": "提供测试账号", "user_resolvable": True,
                        "alternative_capabilities": [], "evidence_refs": ["asset:login@1"],
                    }], "resolved_unknowns": [], "remaining_unknowns": initial["unknowns"],
                }},
            })
            bb.write(BlackboardLayer.TASK_STATE, "resolved-second", {
                "task": common_task,
                "summary": {"status": "done", "findings": [{"type": "authenticated_flow"}],
                            "handoff": {"blockers": [], "resolved_unknowns": ["登录后接口边界"],
                                        "remaining_unknowns": []}},
            })

            current = world_state.snapshot(blackboard=bb)["testing_opportunities"][
                "prioritized"][0]
            self.assertEqual(current["blockers"], [])
            self.assertEqual(current["direction_state"], "productive")

    def test_rule_plan_moves_to_other_asset_after_direction_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            for suffix, score in (("a", 3), ("b", 1)):
                bb.write(BlackboardLayer.TOPOLOGY, f"lead-{suffix}", {
                    "kind": "lead_card", "lead_id": f"lead-{suffix}",
                    "subject": f"https://{suffix}.test/api", "status": "actionable",
                    "origins": ["page_network"], "origin_class": "target_observation",
                    "evidence_state": "verified", "unknowns": [f"{suffix} 资产边界"],
                    "observation_count": score,
                })
            master = object.__new__(MasterAgent)
            master.ctx = SimpleNamespace(
                blackboard=bb, extra={}, tactic_map=None,
                policy=SimpleNamespace(mode=RunMode.SRC),
            )
            pool = world_state.snapshot(blackboard=bb)["testing_opportunities"]
            first = pool["prioritized"][0]
            for capability in first["capability_candidates"]:
                if capability.get("execution") != "dag":
                    continue
                for index in range(2):
                    bb.write(BlackboardLayer.TASK_STATE,
                             f"exhaust-{capability['capability']}-{index}", {
                        "task": {"status": "done", "target": first["subject"],
                                 "payload": {"tactic": capability["tactic"],
                                             "investigation_contract": {
                            "opportunity_ref": first["opportunity_ref"],
                            "opportunity_revision": first["opportunity_revision"],
                            "selected_capability": capability,
                        }}},
                        "summary": {"status": "done", "findings": [], "handoff": {
                            "resolved_unknowns": [], "remaining_unknowns": first["unknowns"],
                        }},
                    })

            refreshed = world_state.snapshot(blackboard=bb)["testing_opportunities"]
            exhausted = next(item for item in refreshed["prioritized"]
                             if item["opportunity_ref"] == first["opportunity_ref"])
            self.assertEqual(exhausted["direction_state"], "exhausted")
            fallback = master._rule_plan({})["dag_nodes"][0]
            self.assertNotEqual(fallback["opportunity_ref"], first["opportunity_ref"])
            selected = next(item for item in refreshed["prioritized"]
                            if item["opportunity_ref"] == fallback["opportunity_ref"])
            self.assertEqual(fallback["tactic"], selected["capability_candidates"][0]["tactic"])

    def test_stale_opportunity_cleanup_only_cancels_pending_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            upsert_lead(
                bb, subject="https://a.test/api/orders", method="GET",
                origins=["js_static"], source_refs=["asset:script@1"],
                evidence_state="unverified", status="needs_disambiguation",
                unknowns=["接口是否为真实业务入口"],
                next_minimal_experiment="确认页面是否实际调用该接口",
                actor="js-reverse", agent_type=AgentType.JS_REVERSE,
            )
            old = world_state.snapshot(blackboard=bb)[
                "testing_opportunities"]["prioritized"][0]
            dag = TaskDAG()
            created = dag.upsert_from_plan([
                {
                    "tactic": "TA0043", "title": "等待执行的旧修订",
                    "opportunity_ref": old["opportunity_ref"],
                    "opportunity_revision": old["opportunity_revision"],
                },
                {
                    "tactic": "JS-REVERSE", "title": "已经运行的旧修订",
                    "opportunity_ref": old["opportunity_ref"],
                    "opportunity_revision": old["opportunity_revision"],
                },
            ], 1).created
            pending, running = created
            dag.mark(running.node_id, TaskStatus.RUNNING)
            audit_events: list[tuple[str, str, dict[str, object]]] = []
            master = object.__new__(MasterAgent)
            master.agent_id = "master-test"
            master.ctx = SimpleNamespace(
                blackboard=bb, extra={}, policy=SimpleNamespace(mode=RunMode.SRC),
                audit=SimpleNamespace(log=lambda *args: audit_events.append(args)),
            )

            upsert_lead(
                bb, subject="https://a.test/api/orders", method="GET",
                origins=["page_network"], source_refs=["asset:endpoint@1"],
                evidence_state="verified", status="actionable",
                unknowns=["接口是否存在对象权限边界"], replace_unknowns=True,
                next_minimal_experiment="比较两个授权账号读取同一对象",
                actor="recon", agent_type=AgentType.RECON,
            )
            master._cancel_stale_opportunity_nodes(dag)

            self.assertEqual(pending.status, TaskStatus.CANCELLED)
            self.assertIn("证据已更新", pending.meta["cancel_reason"])
            self.assertEqual(running.status, TaskStatus.RUNNING)
            self.assertTrue(any(
                payload.get("action") == "stale_opportunities_cancelled"
                for _, _, payload in audit_events
            ))

            current = world_state.snapshot(blackboard=bb)[
                "testing_opportunities"]["prioritized"][0]
            stalled_node = dag.upsert_from_plan([{
                "tactic": "HOST-VULN", "title": "当前修订待执行",
                "opportunity_ref": current["opportunity_ref"],
                "opportunity_revision": current["opportunity_revision"],
            }], 2).created[0]
            for index in range(2):
                bb.write(BlackboardLayer.TASK_STATE, f"current-empty-{index}", {
                    "task": {"status": "done", "payload": {"investigation_contract": {
                        "opportunity_ref": current["opportunity_ref"],
                        "opportunity_revision": current["opportunity_revision"],
                    }}},
                    "summary": {"status": "done", "findings": [], "handoff": {
                        "resolved_unknowns": [],
                        "remaining_unknowns": ["接口是否存在对象权限边界"],
                    }},
                })
            master._cancel_stale_opportunity_nodes(dag)

            self.assertEqual(stalled_node.status, TaskStatus.CANCELLED)
            self.assertIn("连续空转", stalled_node.meta["cancel_reason"])

    def test_plan_resolution_separates_historical_session_refs(self) -> None:
        master = object.__new__(MasterAgent)
        with tempfile.TemporaryDirectory(prefix="sess-current-") as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            master.ctx = SimpleNamespace(
                blackboard=bb, extra={"competition_context": {"challenges": []}})
            resolved = master._resolve_plan_challenges([{
                "tactic": "TA0043", "evidence_refs": [
                    "sess-old:asset:legacy@1", "asset:current@1",
                ],
            }])[0]
        self.assertEqual(resolved["evidence_refs"], ["asset:current@1"])
        self.assertEqual(resolved["historical_evidence_refs"], [
            "sess-old:asset:legacy@1",
        ])

    def test_empty_blackboard_does_not_emit_placeholder_intelligence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            self.assertNotIn("intelligence_summary", world_state.snapshot(blackboard=bb))

    def test_asset_intelligence_exposes_scripts_and_js_discovered_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "script", {
                "asset_id": "script", "asset_type": "script", "value": "https://a.test/app.js",
                "attrs": {"route_count": 1, "secrets": [{"secret_ref": "secret-reference"}]},
            })
            bb.write(BlackboardLayer.ASSET, "endpoint", {
                "asset_id": "endpoint", "asset_type": "endpoint", "value": "https://a.test/api/user",
                "attrs": {"method": "GET", "params": ["id"], "source": "model_observed",
                          "sources": ["js_static", "model_observed"]},
            })
            state = world_state.snapshot(blackboard=bb)
            intelligence = state["asset_intelligence"]
            self.assertEqual(intelligence["scripts_total"], 1)
            self.assertEqual(intelligence["js_endpoints_total"], 1)
            self.assertEqual(intelligence["scripts"][0]["url"], "https://a.test/app.js")
            self.assertEqual(intelligence["scripts"][0]["secret_count"], 1)
            self.assertEqual(intelligence["endpoints"][0], {
                "blackboard_ref": {"layer": "asset", "record_id": "endpoint", "version": 1},
                "url": "https://a.test/api/user", "method": "GET", "params": ["id"],
            })

    def test_intelligence_summary_unifies_scanner_js_failures_and_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "page", {
                "asset_id": "page", "asset_type": "url", "value": "https://a.test/login?token=plain-secret",
                "fingerprint": {"server": "nginx", "technologies": [{"name": "Vue"}]},
            })
            bb.write(BlackboardLayer.ASSET, "script", {
                "asset_id": "script", "asset_type": "script", "value": "https://a.test/app.js",
                "attrs": {"route_count": 1, "secrets": [{"secret_ref": "secret-reference"}]},
            })
            bb.write(BlackboardLayer.ASSET, "endpoint", {
                "asset_id": "endpoint", "asset_type": "endpoint", "value": "https://a.test/api/user",
                "attrs": {"method": "POST", "params": ["id"], "sources": ["js_static"]},
            })
            bb.write(BlackboardLayer.RECON_CLUE, "failed-probe", {
                "clue_type": "scanner_observation", "value": "https://a.test/admin?auth=plain-secret",
                "detail": {"tool": "http_probe", "ok": False, "error": "403 plain-secret",
                           "result": {"body": "plain-secret"}},
                "raw_output": "plain-secret", "verification_status": "unverified",
            })
            bb.write(BlackboardLayer.VULN, "candidate", {
                "vuln_id": "candidate", "title": "对象越权候选", "vuln_type": "idor",
                "target": "https://a.test", "endpoint": "https://a.test/api/user?id=plain-secret",
                "status": "candidate", "severity": "medium", "poc_code": "plain-secret",
            })
            summary = world_state.snapshot(blackboard=bb)["intelligence_summary"]
            self.assertEqual(summary["assets_by_type"], {"endpoint": 1, "script": 1, "url": 1})
            self.assertEqual(summary["vulnerabilities"]["by_status"], {"candidate": 1})
            self.assertEqual(summary["negative_evidence"]["samples"][0]["failure_mode"], "access_denied")
            self.assertIn("nginx", [item["name"] for item in summary["technologies"]])
            self.assertIn("Vue", [item["name"] for item in summary["technologies"]])
            encoded = json.dumps(summary, ensure_ascii=False)
            self.assertNotIn("plain-secret", encoded)
            self.assertIn("token=%E2%80%A6", encoded)

    def test_asset_candidates_are_ranked_without_becoming_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            for record_id, score, stage in (("low", 10, "external_candidate"),
                                             ("high", 75, "live_verified")):
                bb.write(BlackboardLayer.RECON_CLUE, record_id, {
                    "clue_type": "asset_candidate", "candidate_id": record_id,
                    "value": f"{record_id}.example.test", "asset_type": "domain",
                    "stage": stage, "priority_score": score,
                    "priority_components": [{"name": "fixture", "points": score,
                                             "basis": "test evidence"}],
                    "providers": ["fofa"], "unknowns": ["待复核"],
                })

            summary = world_state.snapshot(blackboard=bb)["intelligence_summary"]

            self.assertEqual(summary["assets_total"], 0)
            ranked = summary["asset_candidates"]["prioritized"]
            self.assertEqual([item["priority_score"] for item in ranked], [75, 10])
            signals = summary["action_signals"]
            self.assertEqual(signals["asset_maturity"]["level"], "candidate_only")
            self.assertTrue(signals["dispatch_evidence"]["continue_asset_verification"])
            self.assertFalse(signals["dispatch_evidence"]["web_vuln_supported"])
            self.assertFalse(signals["dispatch_evidence"]["port_scan_supported"])

    def test_action_signals_prioritize_js_and_api_after_verified_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "page", {
                "asset_id": "page", "asset_type": "url", "value": "https://a.test/",
                "attrs": {"status_code": 200, "sources": ["target_response"]},
            })
            bb.write(BlackboardLayer.ASSET, "script", {
                "asset_id": "script", "asset_type": "script", "value": "https://a.test/app.js",
                "attrs": {"route_count": 14, "source_maps": ["app.js.map"]},
            })
            bb.write(BlackboardLayer.ASSET, "endpoint", {
                "asset_id": "endpoint", "asset_type": "endpoint",
                "value": "https://a.test/api/orders",
                "attrs": {"method": "GET", "params": ["user_id"],
                          "sources": ["js_static", "page_network", "target_response"]},
            })

            signals = world_state.snapshot(blackboard=bb)["intelligence_summary"]["action_signals"]

            self.assertEqual(signals["asset_maturity"]["level"], "live_verified")
            self.assertEqual(signals["js_surface"]["density"], "high")
            self.assertEqual(signals["api_surface"]["observed_endpoints"], 1)
            self.assertTrue(signals["dispatch_evidence"]["js_reverse_supported"])
            self.assertTrue(signals["dispatch_evidence"]["web_vuln_supported"])

    def test_action_signals_include_structured_blocker_from_any_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            blocker = {
                "type": "captcha_required", "description": "登录流程需要人工验证码",
                "prerequisite": "用户在当前会话协助输入验证码", "user_resolvable": True,
                "alternative_capabilities": ["script_and_api_extraction"],
                "evidence_refs": ["asset:login@1"],
            }
            bb.write(BlackboardLayer.TASK_STATE, "web-blocked", {
                "task": {"task_id": "web-blocked", "agent_type": "web_vuln",
                         "status": "done", "payload": {}},
                "summary": {"status": "done", "handoff": {"blockers": [blocker]}},
            })

            stall = world_state.snapshot(blackboard=bb)["intelligence_summary"][
                "action_signals"]["route_stall"]
            self.assertTrue(stall["login_page_stall"])
            self.assertTrue(stall["user_resolvable"])
            self.assertEqual(stall["blockers"], [blocker])

    def test_action_signals_detect_login_wall_stall_and_port_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, "business-context", {
                "kind": "business_context", "coverage": "entry_only",
                "recorded_transactions": 0, "har_transactions": 0,
                "roles": [], "objects": [], "workflows": [], "trust_boundaries": [],
                "surface_assessment": {"type": "login_wall", "business_complexity": "low",
                                       "rationale": "仅观察到登录入口", "evidence_refs": ["E1"]},
                "evidence": [{"id": "E1", "kind": "page", "record_id": "login-page"}],
            })
            bb.write(BlackboardLayer.TASK_STATE, "business-1", {
                "task": {"task_id": "business-1", "agent_type": "business_context",
                         "status": "done"},
                "summary": {"status": "done", "handoff": {
                    "remaining_unknowns": ["缺少登录后业务流"]}},
            })
            bb.write(BlackboardLayer.ASSET, "service", {
                "asset_id": "service", "asset_type": "service", "value": "a.test:8443",
                "port": 8443, "service": "https", "attrs": {"sources": ["target_response"]},
            })

            signals = world_state.snapshot(blackboard=bb)["intelligence_summary"]["action_signals"]

            self.assertTrue(signals["route_stall"]["login_page_stall"])
            self.assertEqual(signals["route_stall"]["recommended_pivot"], "js_api_or_asset_surface")
            self.assertTrue(signals["auth_surface"]["login_wall"])
            self.assertTrue(signals["dispatch_evidence"]["port_scan_supported"])

    def test_complex_business_evidence_supports_business_role_without_type_regex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, "business-context", {
                "kind": "business_context", "coverage": "observed_transaction_flow",
                "surface_assessment": {"type": "transactional", "business_complexity": "high",
                                       "rationale": "已观察多角色订单状态流", "evidence_refs": ["E1"]},
                "roles": [{"name": "buyer", "evidence_refs": ["E1"]},
                          {"name": "seller", "evidence_refs": ["E1"]}],
                "objects": [{"name": "order", "evidence_refs": ["E1"]},
                            {"name": "payment", "evidence_refs": ["E1"]}],
                "workflows": [{"name": "checkout", "evidence_refs": ["E1"]},
                              {"name": "refund", "evidence_refs": ["E1"]}],
                "trust_boundaries": [{"description": "buyer/seller", "evidence_refs": ["E1"]}],
                "evidence": [{"id": "E1", "kind": "observed_transaction",
                              "record_id": "endpoint-1"}],
            })

            signals = world_state.snapshot(blackboard=bb)["intelligence_summary"]["action_signals"]

            self.assertEqual(signals["business_complexity"]["level"], "high")
            self.assertTrue(signals["dispatch_evidence"]["business_context_supported"])

    def test_src_evidence_gate_defers_premature_attack_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.RECON_CLUE, "candidate", {
                "clue_type": "asset_candidate", "value": "api.example.test",
                "stage": "external_candidate", "priority_score": 80,
            })
            master = object.__new__(MasterAgent)
            master.agent_id = "master-test"
            master.ctx = SimpleNamespace(
                blackboard=bb, audit=SimpleNamespace(log=lambda *args, **kwargs: None),
                policy=SimpleNamespace(mode=RunMode.SRC), tactic_map=None,
                extra={}, agents={},
            )

            nodes = master._apply_src_evidence_gates([
                {"tactic": "TA0043"}, {"tactic": "BUSINESS-CONTEXT"},
                {"tactic": "JS-REVERSE"}, {"tactic": "TA0001"},
                {"tactic": "TA0007"}, {"tactic": "HOST-VULN"},
            ])
            fallback = master._rule_plan({})["dag_nodes"]

            self.assertEqual([node["tactic"] for node in nodes], ["TA0043"])
            self.assertEqual([node["tactic"] for node in fallback], ["TA0043"])

    def test_rule_fallback_prefers_js_for_low_complexity_portal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "page", {
                "asset_id": "page", "asset_type": "url", "value": "https://news.test/",
                "attrs": {"status_code": 200, "sources": ["target_response"]},
            })
            bb.write(BlackboardLayer.ASSET, "script", {
                "asset_id": "script", "asset_type": "script", "value": "https://news.test/app.js",
                "attrs": {"route_count": 4},
            })
            bb.write(BlackboardLayer.TOPOLOGY, "business-context", {
                "kind": "business_context", "coverage": "public_flow",
                "surface_assessment": {"type": "content_portal", "business_complexity": "low",
                                       "rationale": "公开内容展示", "evidence_refs": ["E1"]},
                "roles": [], "objects": [], "workflows": [], "trust_boundaries": [],
                "evidence": [{"id": "E1", "kind": "page", "record_id": "page"}],
            })
            master = object.__new__(MasterAgent)
            master.agent_id = "master-test"
            master.ctx = SimpleNamespace(
                blackboard=bb, audit=SimpleNamespace(log=lambda *args, **kwargs: None),
                policy=SimpleNamespace(mode=RunMode.SRC), tactic_map=None,
                extra={}, agents={},
            )

            tactics = [node["tactic"] for node in master._rule_plan({})["dag_nodes"]]

            self.assertIn("JS-REVERSE", tactics)
            self.assertNotIn("BUSINESS-CONTEXT", tactics)
            self.assertNotIn("TA0007", tactics)

    def test_service_evidence_unlocks_port_role_without_unlocking_web_vuln(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "service", {
                "asset_id": "service", "asset_type": "service", "value": "a.test:22",
                "port": 22, "service": "ssh", "attrs": {"sources": ["target_response"]},
            })
            master = object.__new__(MasterAgent)
            master.agent_id = "master-test"
            master.ctx = SimpleNamespace(
                blackboard=bb, audit=SimpleNamespace(log=lambda *args, **kwargs: None),
                policy=SimpleNamespace(mode=RunMode.SRC), tactic_map=None,
                extra={}, agents={},
            )

            nodes = master._apply_src_evidence_gates([
                {"tactic": "TA0001"}, {"tactic": "TA0007"}, {"tactic": "HOST-VULN"},
            ])

            self.assertEqual([node["tactic"] for node in nodes], ["TA0007", "HOST-VULN"])

    def test_investigation_frontier_separates_observed_and_generated_leads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "real-api", {
                "asset_id": "real-api", "asset_type": "endpoint",
                "value": "https://vbapi.example.test/v1/home/news",
                "attrs": {"method": "GET", "sources": ["page_network", "target_response"]},
            })
            bb.write(BlackboardLayer.RECON_CLUE, "guessed-docs", {
                "clue_type": "scanner_observation", "value": "https://example.test/swagger-ui/",
                "status": "observed", "source": "model_generated_test",
                "detail": {"observation": {"ok": True, "status_code": 200}},
            })

            frontier = world_state.snapshot(blackboard=bb)["investigation_frontier"]
            observed = frontier["actionable_leads"][0]
            generated = frontier["needs_disambiguation"][0]
            self.assertEqual(observed["subject"], "https://vbapi.example.test/v1/home/news")
            self.assertEqual(observed["origin_class"], "target_observation")
            self.assertEqual(generated["subject"], "https://example.test/swagger-ui/")
            self.assertEqual(generated["origin_class"], "generated_test")

    def test_frontier_reobservations_update_one_lead_instead_of_multiplying_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bb = Blackboard(Path(tmp) / "bb.db")
            self.addCleanup(bb.close)
            data = {"asset_id": "api", "asset_type": "endpoint",
                    "value": "https://a.test/api", "attrs": {"sources": ["page_network"]}}
            bb.write(BlackboardLayer.ASSET, "api", data, dedup_key="endpoint|GET|https://a.test/api")
            bb.write(BlackboardLayer.ASSET, "other-id", data,
                     dedup_key="endpoint|GET|https://a.test/api")

            frontier = world_state.snapshot(blackboard=bb)["investigation_frontier"]
            leads = [item for group in ("actionable_leads", "needs_disambiguation")
                     for item in frontier[group] if item["subject"] == "https://a.test/api"]
            self.assertEqual(len(leads), 1)
            self.assertEqual(leads[0]["repetition"], {"record_versions": 2, "reobserved": True})

    def test_incremental_facts_preserve_versions_retirement_and_full_authorization(self) -> None:
        def fact(key, version=1):
            return {"blackboard_ref": {"layer": "topology", "record_id": key, "version": version},
                    "summary": "证据摘要" * 30}
        before = {"authorization": {"allowed_urls": ["https://a.test"]},
                  "shared_facts": [fact(str(i)) for i in range(12)]}
        after = {"authorization": {"allowed_urls": ["https://a.test", "https://b.test"]},
                 "shared_facts": [fact(str(i), 2 if i == 5 else 1) for i in range(1, 13)]}
        fragment = world_state.update(before, after, incremental_facts=True)
        data = json.loads(fragment.split('\n')[1])
        self.assertEqual(data["authorization"], after["authorization"])
        self.assertNotIn("shared_facts", data)
        delta = data["shared_facts_delta"]
        self.assertEqual(delta["upsert"], [fact("5", 2), fact("12")])
        self.assertEqual(delta["retired"], [{"layer": "topology", "record_id": "0"}])
        restored = {row["blackboard_ref"]["record_id"]: row for row in before["shared_facts"]}
        for ref in delta["retired"]:
            restored.pop(ref["record_id"])
        for row in delta["upsert"]:
            restored[row["blackboard_ref"]["record_id"]] = row
        self.assertEqual(restored, {row["blackboard_ref"]["record_id"]: row for row in after["shared_facts"]})
        self.assertLess(len(fragment), len(world_state.update(before, after)) / 2)
        self.assertIsNone(world_state.update(after, {**after, "shared_facts": list(reversed(after["shared_facts"]))}, incremental_facts=True))
        self.assertIn('"shared_facts"', world_state.update(before, after))

    def test_incremental_facts_fall_back_for_legacy_rows_and_keep_clear_events(self) -> None:
        old = {"shared_facts": [{"text": "旧结构"}]}
        new = {"shared_facts": [{"text": "新结构"}]}
        self.assertIn('"shared_facts"', world_state.update(old, new, incremental_facts=True))
        old = {"shared_facts": [{"blackboard_ref": {"layer": "topology", "record_id": "one", "version": 1}}]}
        # 清空整个状态仍沿用旧空状态契约；字段移除发生在其余世界状态仍在时。
        cleared = world_state.update({**old, "environment": {"date": "x"}}, {"environment": {"date": "x"}}, incremental_facts=True)
        self.assertIn('"retired":[{"layer":"topology","record_id":"one"}]', cleared)

    def setUp(self) -> None:
        self.guard = ScopeGuard(Scope(
            allowed_domains=["a.test"], allowed_urls=["https://b.test/app"],
            denied=["deny.test"]))

    def test_authorization_carries_scope_approved_and_denied(self) -> None:
        self.guard.approve_target("https://c.test/login")
        snap = world_state.snapshot(guard=self.guard)
        auth = snap["authorization"]
        self.assertEqual(auth["allowed_domains"], ["a.test"])
        self.assertEqual(auth["allowed_urls"], ["https://b.test/app"])
        self.assertEqual(auth["denied"], ["deny.test"])
        self.assertEqual(auth["approved_by_user"], ["https://c.test/"])

    def test_long_boundary_declarations_are_not_truncated_or_compiled(self) -> None:
        rules = "仅测授权环境，第三方不要访问。\n" * 2500 + "最终例外需再次询问用户"
        notes = "用户补充原话\n" * 1000
        self.guard.scope.testing_rules = rules
        self.guard.scope.notes = notes
        auth = world_state.snapshot(guard=self.guard)["authorization"]
        self.assertEqual(auth["testing_rules"], rules)
        self.assertEqual(auth["notes"], notes)
        self.assertEqual(auth["denied"], ["deny.test"])
        self.assertEqual(auth["boundary_enforcement"], "model_context")

    def test_first_fragment_then_only_changed_fields(self) -> None:
        first = world_state.snapshot(guard=self.guard)
        self.assertEqual(world_state.update(None, first),
                         world_state.render(first))
        self.assertIsNone(world_state.update(first, first))

        self.guard.approve_target("https://c.test/")
        second = world_state.snapshot(guard=self.guard)
        fragment = world_state.update(first, second)
        assert fragment is not None
        self.assertIn('update="true"', fragment)
        self.assertIn("https://c.test/", fragment)
        self.assertIn('"authorization"', fragment)
        self.assertNotIn('"environment"', fragment)
        self.assertIsNone(world_state.update(second, second))

    def test_removed_field_is_marked_null(self) -> None:
        previous = {"authorization": {"denied": []}, "environment": {"date": "x"}}
        current = {"authorization": {"denied": []}}
        fragment = world_state.update(previous, current)
        assert fragment is not None
        self.assertIn('"environment":null', fragment.replace(" ", ""))

    def test_environment_is_runtime_fact_not_capability_list(self) -> None:
        env = world_state.environment(workspace="/ws", artifacts="/ws/artifacts")
        self.assertEqual(env["workspace"], "/ws")
        self.assertEqual(env["artifacts"], "/ws/artifacts")
        self.assertNotIn("tools", env)
        self.assertNotIn("commands", env)


class UserBoundaryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "bb.db"
        self.bb = Blackboard(self.path)
        self.addCleanup(self.bb.close)

    def test_user_originals_and_project_rules_survive_database_reopen(self) -> None:
        statements = [{"kind": "message", "text": "  先看测试环境\n" * 3000 + "尾部不要访问生产"},
                      {"kind": "answer", "question": "能扩围吗？", "answer": "不能，保持原范围。", "scope_added": []}]
        project_rules = "项目规则\n" * 4000 + "尾部仅限测试租户"
        self.bb.write(BlackboardLayer.TOPOLOGY, "project-memory", {"description": project_rules})
        for statement in statements:
            world_state.record_user_statement(self.bb, statement)
        restored = Blackboard(self.path)
        self.addCleanup(restored.close)
        snapshot = world_state.snapshot(blackboard=restored)
        self.assertEqual(snapshot["user_context"]["statements"], statements)
        self.assertEqual(snapshot["project_rules"], project_rules)
        self.assertEqual(set(snapshot["user_context"]), {"statements"})

    def test_concurrent_user_answers_do_not_overwrite_each_other(self) -> None:
        read = self.bb.read_versioned
        barrier = threading.Barrier(2)
        worker = threading.local()

        def simultaneous_read(*args, **kwargs):
            row = read(*args, **kwargs)
            if not getattr(worker, "read_once", False):
                worker.read_once = True
                barrier.wait(timeout=5)
            return row

        statements = [{"kind": "answer", "answer": answer} for answer in ("仅测 A", "不测 B")]
        with patch.object(self.bb, "read_versioned", side_effect=simultaneous_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda statement: world_state.record_user_statement(self.bb, statement), statements))
        saved = world_state.snapshot(blackboard=self.bb)["user_context"]["statements"]
        self.assertCountEqual(saved, statements)

    def test_concurrent_connections_keep_every_user_statement(self) -> None:
        barrier = threading.Barrier(4)
        statements = [{"kind": "message", "text": f"用户原话 {index}",
                       "statement_id": f"message-{index}"} for index in range(4)]

        def append(statement):
            blackboard = Blackboard(self.path)
            read = blackboard.read_versioned
            first = True

            def simultaneous_read(*args, **kwargs):
                nonlocal first
                row = read(*args, **kwargs)
                if first:
                    first = False
                    barrier.wait(timeout=5)
                return row

            try:
                with patch.object(blackboard, "read_versioned", side_effect=simultaneous_read):
                    world_state.record_user_statement(blackboard, statement)
            finally:
                blackboard.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, statements))
        saved = world_state.snapshot(blackboard=self.bb)["user_context"]["statements"]
        self.assertCountEqual(saved, statements)

    def test_statement_id_is_idempotent_but_identical_text_is_not(self) -> None:
        statement = {"kind": "message", "text": "只测测试环境", "statement_id": "message-1"}
        world_state.record_user_statement(self.bb, statement)
        world_state.record_user_statement(self.bb, statement)
        repeated = {**statement, "statement_id": "message-2"}
        world_state.record_user_statement(self.bb, repeated)
        saved = world_state.snapshot(blackboard=self.bb)["user_context"]["statements"]
        self.assertEqual(saved, [statement, repeated])

    def test_resume_restores_identified_messages_even_with_existing_context(self) -> None:
        original = {"kind": "answer", "answer": "不能扩围", "statement_id": "answer-1"}
        world_state.record_user_statement(self.bb, original)
        history = [{"role": "user", "content": "稍后只测 A", "statement_id": "message-1"},
                   {"role": "user", "content": "稍后只测 A", "statement_id": "message-2"}]
        for _ in range(2):
            world_state.sync_user_context(self.bb, dialogue_history=history,
                                          mission="继续", standing_rules="不要访问 B")
        context = world_state.snapshot(blackboard=self.bb)["user_context"]
        self.assertEqual(context["statements"], [original, *[
            {"kind": "message", "text": message["content"], "statement_id": message["statement_id"]}
            for message in history]])
        self.assertEqual(context["standing_rules"], "不要访问 B")

    def test_mission_update_cannot_overwrite_a_concurrent_user_answer(self) -> None:
        original = {"kind": "message", "text": "仅测 A", "statement_id": "message-1"}
        answer = {"kind": "answer", "answer": "不测 B", "statement_id": "answer-1"}
        world_state.record_user_statement(self.bb, original)
        write = self.bb.write
        injected = False

        def concurrent_write(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                world_state.record_user_statement(self.bb, answer)
            return write(*args, **kwargs)

        with patch.object(self.bb, "write", side_effect=concurrent_write):
            world_state.sync_user_context(self.bb, mission="继续测试", standing_rules="只读")
        context = world_state.snapshot(blackboard=self.bb)["user_context"]
        self.assertEqual(context["statements"], [original, answer])
        self.assertEqual(context["mission"], "继续测试")
        self.assertEqual(context["standing_rules"], "只读")

    def test_master_does_not_duplicate_a_persisted_inbox_message(self) -> None:
        statement = {"kind": "message", "text": "保持原范围", "statement_id": "message-1"}
        world_state.record_user_statement(self.bb, statement)
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(blackboard=self.bb, extra={"user_inbox": [statement]})
        self.assertEqual(master._drain_user_feed(), [statement])
        self.assertEqual(world_state.snapshot(blackboard=self.bb)["user_context"]["statements"],
                         [statement])

    def test_master_keeps_every_user_message_without_boundary_classification(self) -> None:
        message = "普通观察\n" * 3000 + "暂时别碰该租户"
        inbox = [{"text": message}, {"text": "谢谢"}, {"attachments": ["capture.har"]}]
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(blackboard=self.bb, extra={"user_inbox": inbox})
        received = master._drain_user_feed()
        self.assertEqual(len(received), 3)
        self.assertEqual(inbox, received)
        self.assertEqual(world_state.snapshot(blackboard=self.bb)["user_context"]["statements"],
                         [{"kind": "message", "text": message}, {"kind": "message", "text": "谢谢"}])


class StablePromptTests(unittest.TestCase):
    def test_master_prompt_requires_information_gain_contract(self) -> None:
        master = object.__new__(MasterAgent)
        master.ctx = SimpleNamespace(
            extra={}, agents={}, policy=SimpleNamespace(mode=None), blackboard=None)
        prompt = master._system_prompt()
        self.assertIn("expected_information", prompt)
        self.assertIn("next_minimal_experiment", prompt)
        self.assertIn("stop_condition", prompt)

    def test_role_prompt_does_not_interpolate_scope(self) -> None:
        guard = ScopeGuard(Scope(allowed_domains=["a.test"]))
        before = agent_system_prompt(AgentType.CTF_WEB)
        guard.approve_target("b.test")
        after = agent_system_prompt(AgentType.CTF_WEB)

        self.assertEqual(before, after)
        self.assertIn("<world_state>", before)
        self.assertNotIn("a.test", before)

    def test_world_state_leads_the_stable_user_block(self) -> None:
        messages = decision_messages("sys", {
            "world_state": {"authorization": {"allowed_urls": ["https://a.test"]}},
            "round": 3,
            "situation": {"round": 3},
        })
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn('"world_state"', messages[1]["content"])
        self.assertNotIn('"world_state"', messages[2]["content"])

    def test_world_state_is_protected_from_projection_truncation(self) -> None:
        world = {"authorization": {"allowed_urls": ["https://" + "a" * 160]}}
        messages = decision_messages("sys", {
            "world_state": world,
            "situation": {"noise": "x" * 8000},
        }, max_chars=200)
        self.assertIn("allowed_urls", messages[1]["content"])
        self.assertIn("a" * 160, messages[1]["content"])


class CTFWorldStateUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = ScopeGuard(Scope(allowed_domains=["a.test"]))
        self.agent = object.__new__(CTFBaseAgent)
        self.agent.agent_id = "ctf"
        self.agent.ctx = SimpleNamespace(
            policy=SimpleNamespace(guard=self.guard),
            builtin_ctx={"workspace_root": "/ws"})

    def test_scope_expansion_appends_one_self_contained_fragment(self) -> None:
        state = {"world_state": self.agent._world_snapshot()}
        self.assertEqual(self.agent._sync_world_state(state), ("", []))

        self.guard.approve_target("b.test")
        fragment, changed = self.agent._sync_world_state(state)
        self.assertIn('update="true"', fragment)
        self.assertIn("b.test", fragment)
        self.assertEqual(changed, ["authorization"])
        self.assertEqual(self.agent._sync_world_state(state), ("", []))

    def test_user_boundary_update_is_complete_for_the_next_ctf_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bb = Blackboard(Path(directory) / "bb.db")
            self.addCleanup(bb.close)
            self.agent.ctx.blackboard = bb
            first = {"kind": "message", "text": "仅测 A"}
            second = {"kind": "answer", "question": "能测 B 吗？", "answer": "不可以。" * 2000}
            world_state.record_user_statement(bb, first)
            state = {"world_state": self.agent._world_snapshot()}
            world_state.record_user_statement(bb, second)
            fragment, changed = self.agent._sync_world_state(state)
            self.assertEqual(changed, ["user_context"])
            body = json.loads(fragment.split("\n", 1)[1].rsplit("\n", 1)[0])
            ref = body["user_context"]["blackboard_ref"]
            original = bb.read(BlackboardLayer(ref["layer"]), ref["record_id"])
            self.assertEqual(original["statements"], [first, second])
            self.assertEqual(self.agent._sync_world_state(state), ("", []))

    def test_missing_snapshot_on_resume_replays_full_state(self) -> None:
        fragment, changed = self.agent._sync_world_state({})
        self.assertIn("<world_state>", fragment)
        self.assertIn("authorization", fragment)
        self.assertIn("environment", fragment)
        self.assertEqual(changed, ["authorization", "environment"])


if __name__ == "__main__":
    unittest.main()
