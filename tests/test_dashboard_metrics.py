"""仪表盘北京时间、未知计价、成果口径与增量审计回归。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spiritscout.blackboard import Blackboard
from spiritscout.core.models import BlackboardLayer
from spiritscout.web.dashboard import (
    BJT,
    DashboardReader,
    start_of_period,
    usage_metrics,
)
from spiritscout.web.sessions import Session


def ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


class DashboardMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = ts("2026-10-26T01:00:00+08:00")
        self.reader = DashboardReader()

    def session(self, sid: str = "sess-one", status: str = "running") -> Session:
        folder = self.root / sid
        folder.mkdir()
        return Session(session_id=sid, mode="ctf", target="fixture", title=sid,
                       workspace=folder, status=status, phase="executing", started_at=self.now - 3600)

    def usage(self, session: Session, calls: list[dict]) -> None:
        (session.workspace / "usage.json").write_text(json.dumps({
            "used_tokens": sum(c.get("tokens", 0) for c in calls),
            "call_series": calls, "calls": len(calls)}))

    def test_beijing_midnight_and_daily_buckets_ignore_host_timezone(self) -> None:
        session = self.session()
        self.usage(session, [
            {"ts": ts("2026-10-25T15:59:59+00:00"), "tokens": 10},
            {"ts": ts("2026-10-25T16:00:00+00:00"), "tokens": 20},
        ])
        data = self.reader.build([session], now=self.now)
        self.assertEqual(data["timezone"], "Asia/Shanghai")
        self.assertEqual(data["summary"]["tokens"], 20)
        self.assertEqual(data["daily"], {"2026-10-25": 10, "2026-10-26": 20})
        self.assertEqual(sum(b["tokens"] for b in data["trend"]), 20)
        self.assertEqual(data["trend"][0]["label"], "10-26 00:00")
        self.assertEqual(data["trend"][1]["ts"] - data["trend"][0]["ts"], 3600,
                         "今天按北京时间每小时聚合一根柱子")
        self.assertEqual(datetime.fromtimestamp(data["range_start"], BJT).hour, 0)
        self.assertEqual(datetime.fromtimestamp(start_of_period("7d", self.now), BJT).day, 20)
        self.assertEqual(start_of_period("hour", self.now), self.now - 3600)

    def test_weighted_input_cache_rate_and_unknown_usage_not_zero(self) -> None:
        rows = [
            {"input_tokens": 100, "cached_input_tokens": 99, "output_tokens": 1000},
            {"input_tokens": 900, "cached_input_tokens": 810},
            {"input_tokens": 200, "cached_input_tokens": None},
        ]
        metrics = usage_metrics(rows)
        self.assertAlmostEqual(metrics["cache"]["hit_rate"], .909)
        self.assertEqual(metrics["cache"]["unmeasured_calls"], 1)
        self.assertEqual(metrics["cache"]["uncached_input_tokens"], 91)
        self.assertIsNone(usage_metrics([rows[-1]])["cache"]["hit_rate"])
        self.assertIsNone(metrics["cost"]["usd"])
        self.assertIsNone(metrics["cost"]["cny_min"])
        self.assertEqual(metrics["cost"]["unpriced_calls"], 3)
        free = usage_metrics([{"estimated_cost_cny_min": 0, "estimated_cost_cny_max": 0}])
        self.assertEqual(free["cost"]["cny_min"], 0)
        self.assertEqual(free["cost"]["priced_calls"], 1)
        partial = usage_metrics([{"estimated_cost_cny_min": 1, "cost_usd": .2}])
        self.assertEqual(partial["cost"]["usd"], .2, "人民币区间不完整时仍展示已有美元计价")
        self.assertIsNone(partial["cost"]["cny_min"])

    def test_all_history_trend_keeps_results_without_timed_usage(self) -> None:
        session = self.session()
        when = self.now - 3 * 86400
        session.audit_path.write_text(json.dumps({
            "ts": when, "type": "flag.submit", "detail": {
                "flag_id": "old", "accepted": True, "challenge_id": "c1"}}) + "\n")
        data = self.reader.build([session], period="all", now=self.now)
        self.assertEqual(sum(len(b["results"]) for b in data["trend"]), 1)
        self.assertEqual(data["sessions"][0]["last_result"], when)

    def test_malformed_snapshot_is_reported_without_hiding_healthy_sessions(self) -> None:
        one, two = self.session(), self.session("sess-two")
        (one.workspace / "flags.json").write_text("[3]")
        data = self.reader.build([one, two], now=self.now)
        self.assertEqual(len(data["sessions"]), 2)
        self.assertIn("暂不可读", data["sessions"][0]["data_note"])
        self.assertEqual(data["sessions"][1]["data_note"], "")

    def test_cost_sources_not_double_counted_and_periods_are_real_call_time(self) -> None:
        session = self.session()
        self.usage(session, [
            {"ts": self.now - 60, "tokens": 100, "model": "flash", "actor": "web-one",
             "input_tokens": 90, "cached_input_tokens": 81, "output_tokens": 10,
             "estimated_cost_cny_min": 1, "estimated_cost_cny_max": 2, "cost_usd": .2},
            {"ts": self.now - 3700, "tokens": 200, "model": "other", "cost_usd": .3},
        ])
        data = self.reader.build([session], period="hour", now=self.now)
        self.assertEqual(data["recent_hour"]["cost"]["cny_max"], 2)
        self.assertIsNone(data["recent_hour"]["cost"]["usd"], "人民币与美元是同一调用的不同估算，不能重复相加")
        self.assertEqual(data["previous_hour"]["cost"]["usd"], .3)
        self.assertEqual(data["cache_groups"]["model"][0]["name"], "flash")
        self.assertEqual(data["cache_groups"]["actor"][0]["name"], "web-one")
        self.assertEqual(data["cache_groups"]["session_id"][0]["name"], session.session_id)

    def test_global_findings_task_capacity_and_verified_outcomes(self) -> None:
        one, two = self.session(), self.session("sess-two", "completed")
        one.plan = SimpleNamespace(platform_profile={"limits": {"max_active": 3}})
        bb = Blackboard(one.bb_path)
        bb.write(BlackboardLayer.TASK_STATE, "dag", {"dag_snapshot": [
            {"node_id": "n1", "status": "running", "task_ids": ["t1"]},
            {"node_id": "n2", "status": "pending", "task_ids": []},
        ]}, updated_by="test")
        bb.write(BlackboardLayer.TASK_STATE, "t1", {"task": {"task_id": "t1", "status": "running",
                 "payload": {"challenge": {"id": "c1"}}}}, updated_by="test")
        bb.write(BlackboardLayer.TOPOLOGY, "fa", {"kind": "finding", "type": "note", "text": "第一会话的事实", "ts": self.now - 10}, updated_by="test")
        bb.close()
        bb = Blackboard(two.bb_path)
        bb.write(BlackboardLayer.TOPOLOGY, "fb", {"kind": "finding", "type": "note", "text": "第二会话的事实", "ts": self.now - 5}, updated_by="test")
        bb.close()
        (one.workspace / "flags.json").write_text(json.dumps([
            {"submitted": True, "accepted": True}, {"submitted": True, "accepted": False}, {"submitted": False}]))
        (one.workspace / "ctf_progress.json").write_text(json.dumps({"solved_ids": ["c1"], "unsolved_ids": ["c2", "c3"]}))
        data = self.reader.build([one, two], now=self.now)
        row = data["sessions"][0]
        self.assertEqual(row["outcomes"]["flags_extracted"], 3)
        self.assertEqual(row["outcomes"]["flags_accepted"], 1)
        self.assertEqual(row["outcomes"]["solved"], 1)
        self.assertIsNone(row["outcomes"]["score"], "没有平台分数不能用提取数充当得分")
        self.assertEqual(row["capacity"]["free"], 2)
        self.assertEqual(row["tasks"]["pending"], 1)
        self.assertEqual(data["alerts"][0]["kind"], "idle_slot")
        self.assertEqual({f["session_id"] for f in data["findings"]}, {one.session_id, two.session_id})
        self.assertEqual(data["findings"][0]["session_title"], "sess-two")

    def test_incremental_audit_partial_lines_and_duplicate_flags(self) -> None:
        session = self.session()
        def event(kind: str, detail: dict, when: float) -> str:
            return json.dumps({"type": kind, "actor": "master-one", "ts": datetime.fromtimestamp(when, BJT).isoformat(), "detail": detail}) + "\n"
        session.audit_path.write_text(event("model.started", {"call_id": "m1", "model": "fixture", "task_type": "meta_planning"}, self.now - 180))
        first = self.reader.build([session], now=self.now)
        self.assertEqual(first["alerts"][0]["kind"], "model_wait")
        self.assertEqual(first["sessions"][0]["stage"], "planning")
        with patch.object(Path, "open", side_effect=AssertionError("缓存未变化不应重新打开审计")):
            self.reader._audit(session.audit_path)
        line = event("model.call", {"call_id": "m1"}, self.now - 10)
        with session.audit_path.open("a") as stream:
            stream.write(line[:len(line)//2])
        self.assertEqual(self.reader.build([session], now=self.now)["sessions"][0]["pending_models"], 1)
        with session.audit_path.open("a") as stream:
            stream.write(line[len(line)//2:])
            stream.write(event("flag.submit", {"flag_id": "f1", "accepted": True, "challenge_id": "c1"}, self.now - 5))
            stream.write(event("flag.submit", {"flag_id": "f1", "accepted": True, "challenge_id": "c1"}, self.now - 1))
        data = self.reader.build([session], now=self.now)
        self.assertEqual(data["sessions"][0]["pending_models"], 0)
        self.assertEqual(data["sessions"][0]["last_result"], self.now - 5)
        self.assertEqual(len(data["outcomes"]), 1)
        self.assertEqual(sum(len(b["results"]) for b in data["trend"]), 1)

    def test_unknown_legacy_time_is_not_assigned_to_session_start(self) -> None:
        session = self.session()
        session.summary = {"run": {"model_usage": {"used_tokens": 123, "used_usd": .2}}}
        data = self.reader.build([session], now=self.now)
        self.assertEqual(data["unallocated_tokens"], 123)
        self.assertEqual(data["daily"], {})
        self.assertEqual(data["summary"]["tokens"], 0)
        self.assertEqual(self.reader.build([session], period="all", now=self.now)["summary"]["tokens"], 123)

    def test_one_corrupt_session_does_not_hide_other_sessions(self) -> None:
        one, two = self.session(), self.session("sess-two")
        one.bb_path.write_bytes(b"invalid sqlite")
        data = self.reader.build([one, two], now=self.now)
        self.assertEqual(len(data["sessions"]), 2)
        self.assertIn("部分记录暂不可读", data["sessions"][0]["data_note"])
        self.assertEqual(data["sessions"][1]["data_note"], "")
