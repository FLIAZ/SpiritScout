"""可观测性不牺牲凭据保密：计量、摘要和模型调用生命周期。"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import NullAuditLogger, tool_preview
from spiritscout.gateway.gateway import ModelGateway
from spiritscout.gateway.registry import ModelRegistry
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope


class ObservationPrivacyTests(unittest.TestCase):
    def test_numeric_usage_visible_but_authentication_tokens_hidden(self) -> None:
        audit = NullAuditLogger()
        record = audit.log("model.call", "test", {
            "tokens": 123, "input_tokens": 100, "output_tokens": 23,
            "access_token": "AUTH_SECRET", "api_key": "KEY_SECRET"})
        self.assertEqual(record["detail"]["tokens"], 123)
        self.assertEqual(record["detail"]["input_tokens"], 100)
        self.assertNotIn("AUTH_SECRET", json.dumps(record))
        self.assertNotIn("KEY_SECRET", json.dumps(record))
        self.assertNotIn("SECRET", json.dumps(audit.log("test", "test", {"tokens": "SECRET"})))

    def test_safe_tool_target_preview_keeps_endpoint_without_credentials_or_payload(self) -> None:
        args = {"url": "https://user:PASS_SECRET@example.test/api/list?token=TOKEN_SECRET&page=42#hidden",
                "method": "GET", "headers": {"Authorization": "HEADER_SECRET"},
                "code": "ARBITRARY_SCRIPT_SECRET"}
        preview = tool_preview(args)
        self.assertIn("example.test/api/list", preview)
        self.assertNotIn("42", preview)
        audit = NullAuditLogger()
        record = audit.log("agent.tool_call", "test", {"calls": [
            {"name": "http_probe", "arguments": args}]})
        call = record["detail"]["calls"][0]
        self.assertTrue(call["arguments"]["_redacted"])
        self.assertIn("example.test/api/list", call["preview"])
        for secret in ("PASS_SECRET", "TOKEN_SECRET", "HEADER_SECRET", "ARBITRARY_SCRIPT_SECRET"):
            self.assertNotIn(secret, json.dumps(record))


class ModelLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_started_and_completed_calls_keep_actor_usage_and_correlation(self) -> None:
        entry = ModelEntry(model="test-model")
        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([entry]), audit=audit)
        backend = SimpleNamespace(complete=AsyncMock(return_value={
            "prompt_tokens": 11, "completion_tokens": 7, "content": "done"}))
        token = current_execution_scope.set(ExecutionScope(task_id="task-one", agent_id="agent-one"))
        try:
            with patch("spiritscout.gateway.gateway.backend_for", return_value=backend):
                await gateway._invoke(entry, [], temperature=.2, max_tokens=None,
                                      response_format=None, task_type="execution")
        finally:
            current_execution_scope.reset(token)
        started, completed = [r for r in audit.records if r["type"] in {"model.started", "model.call"}]
        self.assertEqual(started["type"], "model.started")
        self.assertEqual(completed["type"], "model.call")
        self.assertEqual(started["actor"], "agent-one")
        self.assertEqual(completed["detail"]["call_id"], started["detail"]["call_id"])
        self.assertEqual(completed["detail"]["tokens"], 18)
        self.assertEqual(completed["detail"]["task_id"], "task-one")

    async def test_failed_or_cancelled_model_is_not_left_as_running(self) -> None:
        for error, kind in ((RuntimeError("unavailable"), "model.failed"),
                            (asyncio.CancelledError(), "model.cancelled")):
            entry = ModelEntry(model="test-model")
            audit = NullAuditLogger()
            gateway = ModelGateway(ModelRegistry([entry]), audit=audit)
            backend = SimpleNamespace(complete=AsyncMock(side_effect=error))
            with (patch("spiritscout.gateway.gateway.backend_for", return_value=backend),
                  self.assertRaises(type(error))):
                await gateway._invoke(entry, [], temperature=.2, max_tokens=None, response_format=None)
            self.assertEqual([e["type"] for e in audit.records], ["model.started", kind])
            self.assertEqual(gateway.budget.used_tokens, 0)
