from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from test_normal_context import history
from test_single_executor import context

from spiritscout.agents.sub.base import RoutePlan
from spiritscout.blackboard import Blackboard
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.core.models import BlackboardLayer, RunMode, Task
from spiritscout.ctf.agents import CTFWebAgent
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.gateway.context import decision_memory
from spiritscout.gateway.light_context import project_context
from spiritscout.gateway.manual_context import (
    REQUEST_KEY,
    archive_boundary,
    excerpt_view,
    pending_request,
)
from spiritscout.gateway.normal_context import project
from spiritscout.tools.journal import ExecutionJournal
from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session


class ManualProjectionTests(unittest.TestCase):
    def test_normal_force_below_threshold_persists_view_and_keeps_original(self):
        original = history()
        state = {"messages": copy.deepcopy(original)}
        self.assertEqual(project(state, [], 1000000)["archived_turns"], 0)
        first = project(state, [], 1000000, force=True)
        self.assertGreater(first["new_archived_turns"], 0)
        self.assertLess(first["estimated_tokens"], first["previous_tokens"])
        self.assertEqual(state["messages"], original)
        self.assertEqual(first["messages"][-8:], original[-8:])
        restored = json.loads(json.dumps(state))
        second = project(restored, [], 1000000)
        self.assertEqual(first["messages"], second["messages"])
        self.assertEqual(second["new_archived_turns"], 0)

    def test_short_or_protected_normal_history_reports_no_change(self):
        for messages in (history(4), history(8)):
            for message in messages:
                if message["role"] == "tool":
                    message["_context_contract"] = True
            view = project({"messages": messages}, [], 1000000, force=True)
            self.assertEqual(view["new_archived_turns"], 0)
            self.assertEqual(view["estimated_tokens"], view["previous_tokens"])

    def test_light_manual_boundary_keeps_users_contracts_pairs_and_new_turns(self):
        messages = history(9)
        messages[3]["_context_contract"] = True
        messages.insert(6, {"role": "user", "content": "用户补充的授权与约束，必须完整保留"})
        original = copy.deepcopy(messages)
        boundary = archive_boundary(messages)
        first = project_context(messages, [], 1000000, archive_before=boundary)
        self.assertGreater(first.archived_turns, 0)
        self.assertLess(first.estimated_tokens, first.original_tokens)
        self.assertEqual(messages, original)
        for message in original:
            if message["role"] in {"system", "user"} or message.get("_context_contract"):
                self.assertIn(message, first.messages)
        self.assertEqual(first.messages[-8:], original[-8:])
        calls = {c["id"] for m in first.messages for c in m.get("tool_calls", [])}
        self.assertEqual(calls, {m["tool_call_id"] for m in first.messages if m["role"] == "tool"})
        messages.extend(history(1)[2:])
        second = project_context(messages, [], 1000000, archive_before=boundary)
        self.assertEqual(first.messages, second.messages[:-2])

    def test_light_manual_does_not_archive_incomplete_batch(self):
        messages = history(8)
        messages[2]["tool_calls"].append({"id": "pending", "function": {"name": "tool", "arguments": "{}"}})
        view = project_context(messages, [], 1000000, archive_before=archive_boundary(messages))
        self.assertIn(messages[2], view.messages)
        self.assertIn(messages[3], view.messages)

    def test_business_excerpt_is_a_view_with_a_fixed_boundary(self):
        messages = history(8)
        original = copy.deepcopy(messages)
        view = excerpt_view(messages, archive_boundary(messages))
        self.assertEqual(messages, original)
        self.assertLess(len(view[3]["content"]), len(original[3]["content"]))
        self.assertEqual(view[:2], original[:2])
        self.assertEqual(view[-8:], original[-8:])

    def test_decision_rebases_latest_snapshot_and_only_consumes_on_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            board = Blackboard(Path(directory) / "bb.db")
            self.addCleanup(board.close)
            journal = ExecutionJournal(board, Path(directory), "fixture")
            owner = SimpleNamespace(ctx=SimpleNamespace(blackboard=board, journal=journal))
            memory = decision_memory(owner, "plan")
            def messages(value):
                return [{"role": "system", "content": "system rules"}, {"role": "user", "content": json.dumps({
                    "scope": "user authorized scope", "facts": {"current": value}, "rules": "original rules"})}]
            for index in range(8):
                memory.prepare(messages(index))
                memory.commit("old decision " * 200)
            board.write(BlackboardLayer.TOPOLOGY, REQUEST_KEY, {"request_id": "first"})
            prepared = memory.prepare(messages(99))
            self.assertEqual(len(prepared), 2)
            self.assertEqual(memory.segment_info["reason"], "manual")
            snapshot = json.loads(prepared[1]["content"])["snapshot"]
            self.assertEqual(snapshot["facts"]["current"], 99)
            self.assertEqual(snapshot["scope"], "user authorized scope")
            self.assertEqual(snapshot["rules"], "original rules")
            self.assertEqual(memory.manual_compaction_id, "")
            self.assertEqual(memory.prepare(messages(99)), prepared, "失败重试不能回到旧段")
            board.write(BlackboardLayer.TOPOLOGY, REQUEST_KEY, {"request_id": "second"})
            memory.commit("current decision")
            restored = decision_memory(SimpleNamespace(ctx=owner.ctx), "plan")
            self.assertEqual(restored.manual_compaction_id, "first")
            self.assertEqual(pending_request(board, restored.manual_compaction_id)["request_id"], "second")
            restored.prepare(messages(100))
            restored.commit("next decision")
            restored.prepare(messages(101))
            self.assertFalse(restored.segment_changed)


class ManualSolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_solver_consumes_once_and_keeps_raw_checkpoint_in_both_modes(self):
        for light in (False, True):
            with self.subTest(light=light), tempfile.TemporaryDirectory() as directory:
                board = Blackboard(Path(directory) / "bb.db")
                self.addCleanup(board.close)
                ctx = context(RunMode.CTF, light=light)
                ctx.blackboard, ctx.ring, ctx.audit = board, None, Mock()
                ctx.journal = ExecutionJournal(board, Path(directory), "fixture")
                ctx.gateway.context_budget = lambda **kwargs: 1000000
                agent_type = SingleCTFAgent if light else CTFWebAgent
                agent = agent_type(ctx, flag_manager=FlagManager(local_only=True))
                agent._world_snapshot = lambda: {"user_context": {"mission": "USER_GOAL"}}
                task = Task(task_id="one", agent_type=agent.agent_type, task_type="ctf_solve", target="",
                            payload={"challenge": {"id": "one"}, "mission": "USER_GOAL"})
                route = SimpleNamespace(route_id="r", name="solve")
                plan = RoutePlan(name="solve", strategy="", steps=[{"action": "investigate"}])
                state = agent._inv_state(task, route)
                agent._seed_messages(task, route, plan, state)
                state["messages"].extend(history(9)[2:])
                state["system_initialized"] = True
                raw = copy.deepcopy(state["messages"])
                ctx.gateway.chat = AsyncMock(return_value=SimpleNamespace(content="", tool_calls=[{
                    "id": "read", "name": "history_read", "arguments": {"message_index": 3, "length": 500}}]))
                board.write(BlackboardLayer.TOPOLOGY, REQUEST_KEY, {"request_id": "manual-1"})
                result = await agent._solve_turn(task, route, plan)
                self.assertTrue(result.ok)
                self.assertEqual(ctx.gateway.chat.await_count, 1, "压缩不增加模型调用")
                self.assertEqual(state["manual_compaction_id"], "manual-1")
                self.assertEqual(state["messages"][:len(raw)], raw)
                events = [call.args for call in ctx.audit.log.call_args_list if call.args[0] == "context.manual_compacted"]
                self.assertEqual(len(events), 1)
                self.assertTrue(events[0][2]["changed"])
                saved = ctx.journal.latest_checkpoint("ctf_conversation", "one:r")["state"]
                self.assertEqual(saved["manual_compaction_id"], "manual-1")
                self.assertEqual(saved["messages"][:len(raw)], raw)
                await agent._solve_turn(task, route, plan)
                self.assertEqual(len([c for c in ctx.audit.log.call_args_list if c.args[0] == "context.manual_compacted"]), 1)


class CommandApiTests(unittest.TestCase):
    def test_request_is_durable_idempotent_isolated_and_does_not_resume_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(SystemConfig(workspace=WorkspaceConfig(root=directory)), [])
            for sid in ("one", "two"):
                workspace = app.state.manager.root / sid
                workspace.mkdir()
                app.state.manager._sessions[sid] = Session(session_id=sid, mode="ctf", target="fixture",
                    workspace=workspace, status="completed")
            with TestClient(app, base_url="http://127.0.0.1") as client:
                for request_id in ("first", "first", "second", "first"):
                    response = client.post("/api/sessions/one/compact", json={"request_id": request_id})
                    self.assertEqual(response.status_code, 200, response.text)
                data = client.get("/api/sessions/one/context").json()
                self.assertEqual(data["request"]["request_id"], "second")
                self.assertEqual(data["results"], [])
                self.assertEqual(data["session"]["status"], "completed")
                self.assertIsNone(client.get("/api/sessions/two/context").json()["request"])
                self.assertEqual(client.post("/api/sessions/missing/compact", json={"request_id":"id"}).status_code, 404)
                self.assertEqual(client.post("/api/sessions/one/compact", json={"request_id":"../bad"}).status_code, 422)
                session = app.state.manager.get("one")
                self.assertEqual(session.user_inbox, [])
                self.assertEqual(session.dialogue_history, [])
                events = [json.loads(line) for line in session.audit_path.read_text().splitlines()]
                self.assertEqual(len([e for e in events if e["type"] == "context.compaction_requested"]), 2)
                board = Blackboard(session.bb_path)
                try:
                    self.assertEqual(pending_request(board)["request_id"], "second")
                finally:
                    board.close()
