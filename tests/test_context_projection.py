"""阶段上下文的合法 JSON、证据引用和最新用户答复传递。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer
from spiritscout.ctf.agents import CTFBaseAgent
from spiritscout.gateway.context import (
    business_digest,
    compact_history,
    compact_json,
    shared_context,
)


class ContextProjectionTests(unittest.TestCase):
    def test_large_observations_never_cut_json_or_critical_instructions(self) -> None:
        payload = {"evidence": [{"id": f"E{i}", "text": "观测" * 100} for i in range(100)],
                   "constraints": "必须保留的输出格式与范围约束",
                   "task_intent": {"mission": "授权目标"}, "用户答复": {"answer": "保留用户回答"}}
        rendered = compact_json(payload, max_chars=2500)
        result = json.loads(rendered)
        self.assertLess(len(rendered), 2500)
        for key in ("constraints", "task_intent", "用户答复"):
            self.assertEqual(result[key], payload[key])
        self.assertEqual(result["_context_projection"]["reduced"]["evidence"]["original_items"], 100)
        self.assertEqual(len(payload["evidence"]), 100)

    def test_critical_instructions_use_soft_budget(self) -> None:
        payload = {"constraints": "不得丢弃" * 1000}
        self.assertEqual(json.loads(compact_json(payload, max_chars=100)), payload)

    def test_competition_tasks_and_platform_receipts_survive_soft_budget(self) -> None:
        """规划器必须拿到完整题目 ID 与已完成预检，不能裁成单字符。"""
        protected = {
            "competition_context": {"challenges": [
                {"id": f"e1-{i:02d}", "title": "独立题目", "url": "",
                 "description": "赛题事实" * 100, "platform": "benchmark"}
                for i in range(1, 64)]},
            "platform_facts": [{"action": "precheck", "ok": True,
                                "detail": '{"status":"ok"}'}],
            "world_state": {"user_context": "完整任务规则" * 1000},
        }
        result = json.loads(compact_json({**protected, "observations": "旧响应" * 1000}, 8000))
        for key, value in protected.items():
            self.assertEqual(result[key], value)

    def test_boundary_rules_and_user_originals_are_protected_at_any_length(self) -> None:
        rules = "授权规则\n" * 10000 + "末尾禁止生产数据"
        statements = [{"kind": "answer", "answer": "后续用户原话\n" * 4000}]
        protected = {"authorization": {"testing_rules": rules, "denied": ["legacy.test"]},
                     "user_context": {"statements": statements}, "testing_rules": rules,
                     "project_rules": rules, "user_replies": statements}
        result = json.loads(compact_json({**protected, "observations": "响应" * 10000}, max_chars=500))
        for key, value in protected.items():
            self.assertEqual(result[key], value)
        self.assertLess(len(result["observations"]), 20000)

    def test_history_compaction_never_shortens_user_boundary_updates(self) -> None:
        for compact in (lambda messages: compact_history(messages, max_chars=1000),
                        CTFBaseAgent._compact_messages):
            original = "<world_state>\n" + "完整边界原文" * 20000 + "\n</world_state>"
            messages = [{"role": "system", "content": "稳定契约"},
                        {"role": "user", "content": "原始任务"},
                        {"role": "user", "content": original},
                        {"role": "assistant", "content": "旧观察" * 10000},
                        {"role": "tool", "tool_call_id": "call", "content": "工具结果" * 10000},
                        {"role": "user", "content": "本轮问题"},
                        {"role": "assistant", "content": "继续"}]
            compact(messages)
            self.assertEqual(messages[2]["content"], original)
            self.assertEqual(messages[4]["tool_call_id"], "call")
            self.assertLess(len(messages[3]["content"]), 30000)

    def test_nested_fact_projection_survives_large_situation(self) -> None:
        payload = {"situation": {"observations": ["响应" * 300] * 30,
                                 "business_context": {"facts": ["带证据的事实"]}},
                   "constraints": "必须保留约束"}
        result = json.loads(compact_json(payload, max_chars=1500))
        self.assertEqual(result["situation"]["business_context"], payload["situation"]["business_context"])
        self.assertIn("observations", result["situation"])

    def test_projection_keeps_reference_index_and_hypothesis_distinction(self) -> None:
        source = {"facts": [{"statement": "事实", "evidence_refs": ["E2"]}],
                  "hypotheses": [{"statement": "假设", "evidence_refs": ["E1"]}],
                  "evidence": [{"id": f"E{i}", "url": f"https://archive.test/{i}", "note": "响应正文" * 1000}
                               for i in range(100)]}
        result = business_digest(source, version=3)
        self.assertEqual(result["version"], 3)
        self.assertEqual({item["id"] for item in result["evidence_index"]}, {"E1", "E2"})
        self.assertNotIn("note", result["evidence_index"][0])
        self.assertEqual(result["hypotheses"], source["hypotheses"])
        self.assertLess(len(json.dumps(result)), len(json.dumps(source)) // 10)

    def test_user_reply_reaches_later_stages_even_after_many_other_clues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bb = Blackboard(Path(temp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, "business-context", {"facts": [], "evidence": []})
            bb.write(BlackboardLayer.RECON_CLUE, "reply", {
                "clue_type": "user_reply", "detail": "哪个账号？", "value": "请使用测试账号"})
            for index in range(205):
                bb.write(BlackboardLayer.RECON_CLUE, f"clue-{index}", {"clue_type": "endpoint"})
            first = shared_context(bb)
            self.assertEqual(first["user_replies"][0]["answer"], "请使用测试账号")
            bb.write(BlackboardLayer.TOPOLOGY, "business-context", {"coverage": "updated", "evidence": []})
            self.assertEqual(shared_context(bb)["version"], first["version"] + 1)

    def test_session_findings_reach_sub_agents_as_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bb = Blackboard(Path(temp) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.ASSET, "a1", {
                "asset_type": "endpoint", "value": "https://api.test/login", "port": 443})
            bb.write(BlackboardLayer.RECON_CLUE, "c1", {
                "clue_type": "login_form", "value": "登录表单在 /login"})
            bb.write(BlackboardLayer.VULN, "v1", {
                "title": "反射型 XSS", "status": "candidate", "severity": "medium"})
            result = shared_context(bb)
            self.assertEqual(result["findings"]["assets"]["total"], 1)
            self.assertIn("https://api.test/login",
                          result["findings"]["assets"]["sample"]["endpoint"])
            self.assertEqual(result["findings"]["clues"]["by_type"], {"login_form": 1})
            self.assertEqual(result["findings"]["vulns"]["by_status"], {"candidate": 1})
            self.assertIn("登录表单在 /login", str(result["findings"]["clues"]["sample"]))

    def test_findings_absent_when_blackboard_has_no_results_yet(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bb = Blackboard(Path(temp) / "bb.db")
            self.addCleanup(bb.close)
            self.assertNotIn("findings", shared_context(bb))
