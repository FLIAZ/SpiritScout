"""上下文、配置、历史和只读秘书的跨模块回归。"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.secretary import Secretary
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import ModelEntry, SystemConfig, _expand_env
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.exceptions import ConfigurationError, SpiritScoutError
from spiritscout.core.models import AgentType, BlackboardLayer
from spiritscout.gateway.backends import LiteLLMBackend, cache_usage
from spiritscout.gateway.context import compact_history, decision_messages
from spiritscout.gateway.gateway import BudgetTracker, ModelGateway
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.web import agentcfg, modelcfg
from spiritscout.web.deliverables import read_record_history, read_vulnerability_page


class ConfigurationRepairTests(unittest.TestCase):
    def test_invalid_concurrency_unknown_keys_and_missing_env_fail_explicitly(self) -> None:
        for data in ({"web": {"max_concurrent_sessions": 0}},
                     {"gateway": {"request_timeout_second": 1}},
                     {"mcp_servers": [{"name": "test", "timeout": -1}]}):
            with self.assertRaises(ValidationError):
                SystemConfig.model_validate(data)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigurationError):
            _expand_env({"url": "${MISSING_CONFIGURATION_VARIABLE}"})

    def test_rename_migrates_assignments_and_backups_delete_rejects_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model_path, agent_path = Path(directory) / "models.yaml", Path(directory) / "agents.yaml"
            modelcfg.write_config(model_path, [], [{"model": "old", "provider": "mock"},
                {"model": "backup", "provider": "mock", "is_fallback_for": ["old"]}])
            agentcfg.set_assignment(agent_path, "master", "old", {"old"})
            agentcfg.set_naming_chain(agent_path, ["old", "keeper"], {"old", "keeper"})
            modelcfg.upsert_entry(model_path, {"model": "new", "provider": "mock"},
                                  original_model="old", assignments_path=agent_path)
            self.assertEqual(agentcfg.read_assignments(agent_path), {"master": "new"})
            self.assertEqual(agentcfg.read_naming_chain(agent_path), ["new", "keeper"])
            self.assertEqual(modelcfg.read_entries(model_path)[1]["is_fallback_for"], ["new"])
            with self.assertRaises(SpiritScoutError):
                modelcfg.delete_entry(model_path, "new", assignments_path=agent_path)
            self.assertIn("secretary", agentcfg._VALID_KEYS)

    def test_delete_rejects_model_in_naming_chain_until_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model_path, agent_path = Path(directory) / "models.yaml", Path(directory) / "agents.yaml"
            modelcfg.write_config(model_path, [], [{"model": "mini", "provider": "mock"}])
            agentcfg.set_naming_chain(agent_path, ["mini"], {"mini"})
            with self.assertRaises(SpiritScoutError) as ctx:
                modelcfg.delete_entry(model_path, "mini", assignments_path=agent_path)
            self.assertIn("命名链", str(ctx.exception))
            # 迁走（清空链）后删除放行
            agentcfg.set_naming_chain(agent_path, [], {"mini"})
            self.assertEqual(modelcfg.delete_entry(
                model_path, "mini", assignments_path=agent_path), [])

    def test_naming_chain_and_assignments_share_one_file_safely(self) -> None:
        """同一分配表文件的两段（assignments/naming）一起读一起写，互不覆盖。"""
        with tempfile.TemporaryDirectory() as directory:
            agent_path = Path(directory) / "agents.yaml"
            agentcfg.set_assignment(agent_path, "master", "big", {"big"})
            agentcfg.set_naming_chain(agent_path, ["small", "big"], {"big", "small"})
            # 写指派保留命名链；写命名链保留指派
            agentcfg.set_assignment(agent_path, "recon", "small", {"big", "small"})
            self.assertEqual(agentcfg.read_naming_chain(agent_path), ["small", "big"])
            agentcfg.set_naming_chain(agent_path, ["small"], {"big", "small"})
            self.assertEqual(agentcfg.read_assignments(agent_path),
                             {"master": "big", "recon": "small"})
            # 清空链不落 naming 键；文件里手工加的非法形状显式报错
            agentcfg.set_naming_chain(agent_path, [], {"big", "small"})
            self.assertEqual(agentcfg.read_naming_chain(agent_path), [])
            agent_path.write_text("naming: 不是数组\n", encoding="utf-8")
            with self.assertRaises(SpiritScoutError):
                agentcfg.read_naming_chain(agent_path)

    def test_failed_model_write_rolls_back_assignment_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            models, agents = Path(directory) / "models.yaml", Path(directory) / "agents.yaml"
            modelcfg.write_config(models, [], [{"model": "old", "provider": "mock"}])
            agentcfg.write_assignments(agents, {"master": "old"})
            with patch.object(modelcfg, "write_config", side_effect=OSError("disk full")), self.assertRaises(OSError):
                modelcfg.upsert_entry(models, {"model": "new", "provider": "mock"},
                                      original_model="old", assignments_path=agents)
            self.assertEqual(agentcfg.read_assignments(agents), {"master": "old"})


class HistoryRepairTests(unittest.TestCase):
    def test_readonly_does_not_migrate_and_history_preserves_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bb = Blackboard(root / "bb.db")
            for index in range(55):
                bb.write(BlackboardLayer.VULN, str(index), {"status": "accepted"})
            for text in ("old", "new"):
                bb.write(BlackboardLayer.TOPOLOGY, "business-context", {"facts": [text]})
            bb.close()
            reader = Blackboard(root / "bb.db", readonly=True)
            self.assertEqual(reader.status_counts(BlackboardLayer.VULN), {"accepted": 55})
            with self.assertRaises(sqlite3.OperationalError):
                reader.write(BlackboardLayer.TOPOLOGY, "forbidden", {})
            reader.close()
            first = read_vulnerability_page(root)
            second = read_vulnerability_page(root, offset=50)
            self.assertEqual(first["total"], 55)
            self.assertEqual(len(first["items"]), 50)
            self.assertEqual(len(second["items"]), 5)
            history = read_record_history(root, "topology", "business-context", limit=1)
            self.assertTrue(history["has_more"])
            self.assertEqual(history["items"][0]["data"]["facts"], ["new"])
            older = read_record_history(root, "topology", "business-context", before=2)
            self.assertEqual(older["items"][0]["data"]["facts"], ["old"])

    def test_context_prefix_is_stable_and_compaction_preserves_call_pairing(self) -> None:
        first = decision_messages("role", {"constraints": "fixed", "observations": "first"})
        second = decision_messages("role", {"constraints": "fixed", "observations": "second"})
        self.assertEqual(first[0], second[0])
        messages = [first[0], {"role": "user", "content": "mission"},
                    {"role": "assistant", "content": "x" * 4000, "tool_calls": [{"id": "a"}]},
                    {"role": "tool", "tool_call_id": "a", "content": "e" * 4000},
                    {"role": "user", "content": "user reply"}, {"role": "assistant", "content": "latest"}]
        compact_history(messages, 2000)
        self.assertEqual(messages[0], first[0])
        self.assertEqual(messages[2]["tool_calls"], [{"id": "a"}])
        self.assertEqual(messages[3]["tool_call_id"], "a")
        self.assertEqual(messages[4]["content"], "user reply")


class CacheRepairTests(unittest.IsolatedAsyncioTestCase):
    def test_usage_distinguishes_unknown_zero_and_real_cache(self) -> None:
        self.assertIsNone(cache_usage(None)["cached_input_tokens"])
        self.assertEqual(cache_usage({"prompt_tokens_details": {"cached_tokens": 0}})["cached_input_tokens"], 0)
        usage = SimpleNamespace(input_tokens_details=SimpleNamespace(cached_tokens=80))
        self.assertEqual(cache_usage(usage)["cached_input_tokens"], 80)
        tracker = BudgetTracker(0, 0)
        tracker.record("model", 100, 0, input_tokens=100, cached_input_tokens=80)
        tracker.record("model", 900, 0, input_tokens=900, cached_input_tokens=90)
        tracker.record("model", 1000, 0)
        restored = BudgetTracker(0, 0)
        restored.restore(tracker.persist_snapshot())
        self.assertEqual(restored.cache_snapshot()["hit_rate"], 0.17)
        self.assertEqual(restored.cache_snapshot()["unmeasured_calls"], 1)

    async def test_cache_hint_is_opt_in_and_input_is_not_mutated(self) -> None:
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
                                   usage=SimpleNamespace(prompt_tokens=100, completion_tokens=2,
                                       prompt_tokens_details=SimpleNamespace(cached_tokens=80)))
        backend = LiteLLMBackend()
        messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "dynamic"}]
        complete = AsyncMock(return_value=response)
        with patch("litellm.acompletion", complete):
            result = await backend.complete(ModelEntry(model="claude", provider="anthropic_compatible",
                prompt_caching="anthropic_ephemeral"), messages,
                temperature=0.2, max_tokens=None, timeout=30, response_format=None)
            self.assertEqual(result["cached_input_tokens"], 80)
            self.assertEqual(complete.call_args.kwargs["messages"][0]["content"][0]["cache_control"], {"type": "ephemeral"})
            await backend.complete(ModelEntry(model="generic"), messages,
                temperature=0.2, max_tokens=None, timeout=30, response_format=None)
            self.assertEqual(complete.call_args.kwargs["messages"][0]["content"], "stable")
        self.assertEqual(messages[0]["content"], "stable")

    async def test_cache_metrics_and_discount_reach_audit_and_ledger(self) -> None:
        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([ModelEntry(model="test", provider="mock",
            cost_input_per_mtok=10, cost_cached_input_per_mtok=1)]), audit=audit)
        fake = SimpleNamespace(complete=AsyncMock(return_value={"content": "ok", "prompt_tokens": 100,
            "completion_tokens": 0, "cached_input_tokens": 80, "cache_write_tokens": None}))
        with patch("spiritscout.gateway.gateway.backend_for", return_value=fake):
            result = await gateway.chat([{"role": "user", "content": "hello"}])
        self.assertAlmostEqual(result.cost_usd, 0.00028)
        self.assertEqual(audit.records[-1]["detail"]["cached_input_tokens"], 80)
        self.assertIsNone(audit.records[-1]["detail"]["cache_write_tokens"])
        self.assertEqual(gateway.usage_report()["cache"]["hit_rate"], 0.8)


class SecretaryRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_secretary_filters_invented_refs_and_has_no_tools(self) -> None:
        fake = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content=json.dumps({"suggestions": [
            {"summary": "real", "evidence_refs": ["topology:a@1"]},
            {"summary": "invented", "evidence_refs": ["missing"]}]}))))
        result = await Secretary(fake, NullAuditLogger()).advise({"records": [{"ref": "topology:a@1"}]}, {})
        self.assertEqual([item["summary"] for item in result["suggestions"]], ["real"])
        self.assertNotIn("tools", fake.chat.call_args.kwargs)
        self.assertEqual(AgentType.SECRETARY.value, "secretary")

    async def test_master_decides_and_failure_preserves_original_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bb = Blackboard(Path(directory) / "bb.db")
            self.addCleanup(bb.close)
            bb.write(BlackboardLayer.TOPOLOGY, "fact", {"text": "observed"})
            secretary = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content=json.dumps({"suggestions": [
                {"summary": "verify", "evidence_refs": ["topology:fact@1"]}]}))))
            async def decide(messages, **kwargs):
                payload = next(json.loads(m["content"]) for m in messages
                               if m["role"] == "user" and "snapshot" in json.loads(m["content"]))
                advice = payload["snapshot"]["advice"]
                return SimpleNamespace(finish_reason="stop", content=json.dumps({"continue_iteration": True, "dag_nodes": [],
                    "advice_decisions": [{"id": advice["suggestions"][0]["id"], "verdict": "rejected", "reason": "已有覆盖"}]}))
            master = object.__new__(MasterAgent)
            master.agent_id = "master-test"
            master.ctx = SimpleNamespace(blackboard=bb, audit=NullAuditLogger(), gateway=SimpleNamespace(chat=decide),
                extra={"secretary_enabled": True, "secretary_gateway": secretary})
            master._system_prompt = lambda: "master"
            draft = {"request_advice": True, "continue_iteration": True, "dag_nodes": []}
            final = await master._consult_secretary(draft, {})
            self.assertIn("advice_decisions", final, master.ctx.audit.records)
            self.assertEqual(final["advice_decisions"][0]["verdict"], "rejected")
            self.assertEqual(master.ctx.audit.records[-1]["type"], "secretary.decided")
            secretary.chat.side_effect = RuntimeError("unavailable")
            self.assertIs(await master._consult_secretary(draft, {}), draft)
            self.assertEqual(master.ctx.audit.records[-1]["type"], "secretary.failed")
            secretary.chat.reset_mock()
            await master._consult_secretary({"continue_iteration": False}, {})
            secretary.chat.assert_not_awaited()
