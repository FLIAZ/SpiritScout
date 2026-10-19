"""参数查看从加密副本读取，保留工具结构、凭据脱敏和只读边界。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.audit import AuditLogger, sanitize_record
from spiritscout.resources.store import redact_credentials
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session
from spiritscout.web.tool_parameters import read_tool_parameters


class ToolParameterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bb = Blackboard(self.root / "bb.db")
        self.addCleanup(self.bb.close)
        self.journal = ExecutionJournal(self.bb, self.root, "run-params")

    @staticmethod
    def digest(args: dict) -> str:
        return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def test_historical_journal_params_are_readable_without_modification(self) -> None:
        args = {"target": "e247", "element": "轮播链接"}
        digest = self.digest(args)
        self.journal.begin("inv-params", "browser_click", digest, "never_automatic", params=args)
        before = self.journal.list_recent()
        self.assertEqual(read_tool_parameters(self.root, "browser_click", digest)["parameters"], args)
        self.assertEqual(self.journal.list_recent(), before)
        self.assertFalse(read_tool_parameters(self.root, "different", digest)["available"])

    def test_model_entry_parameters_survive_executor_transformations(self) -> None:
        args = {"command": "printf archive-param-marker", "action": "analysis"}
        audit = AuditLogger(self.root / "audit.jsonl")
        record = audit.log("agent.tool_call", "test", {"calls": [{"name": "shell", "arguments": args}]})
        call = record["detail"]["calls"][0]
        # 参数原文直达审计（会话页实时监控），SSE 出口剥掉加密副本
        self.assertEqual(call["arguments"]["command"], "printf archive-param-marker")
        self.assertNotIn("arguments_cipher", json.dumps(sanitize_record(record)))
        self.assertIn("archive-param-marker", (self.root / "audit.jsonl").read_text())

    def test_resource_credentials_redacted_but_plain_fields_visible(self) -> None:
        result = redact_credentials({"code": "print('visible')", "body": {"page": 2, "password": "hidden-pass"},
            "headers": {"X-Api-Key": "hidden-key", "Accept": "application/json"},
            "prompt_tokens": 22})
        text = json.dumps(result)
        self.assertEqual(result["code"], "print('visible')")
        self.assertEqual(result["body"]["page"], 2)
        self.assertEqual(result["prompt_tokens"], 22)
        for marker in ("hidden-pass", "hidden-key"):
            self.assertNotIn(marker, text)

    def test_missing_or_wrong_key_is_not_replaced(self) -> None:
        args = {"url": "https://example.test"}
        self.journal.begin("inv-key", "nav", self.digest(args), "safe", params=args)
        key = self.root / "execution.key"
        key.write_bytes(b"wrong-key")
        self.assertFalse(read_tool_parameters(self.root, "nav", self.digest(args))["available"])
        self.assertEqual(key.read_bytes(), b"wrong-key")

    def test_session_scoped_api_does_not_cache_and_rejects_bad_digest(self) -> None:
        args = {"url": "https://example.test"}
        self.journal.begin("inv-api", "nav", self.digest(args), "safe", params=args)
        cfg = SystemConfig(workspace=WorkspaceConfig(root=str(self.root)))
        app = create_app(cfg, [])
        app.state.manager._sessions["sess-params"] = Session(session_id="sess-params", mode="src",
            target="example.test", workspace=self.root, status="completed")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            path = "/api/sessions/sess-params/tool-parameters"
            response = client.get(path, params={"tool": "nav", "digest": self.digest(args)})
            self.assertEqual(response.json()["parameters"], args)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get(path, params={"tool": "nav", "digest": "bad"}).status_code, 422)
