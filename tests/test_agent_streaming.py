"""角色流式输出的审计节流、并发归属与既有回调兼容。"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spiritscout.agents.sub.base import SubAgent
from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import AgentType, RouteState, Task, TaskStatus
from spiritscout.ctf.agents import _DeltaEmitter
from spiritscout.gateway import ModelGateway, ModelRegistry
from spiritscout.gateway.backends import MockBackend
from spiritscout.gateway.streaming import AuditStream
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope


class AgentStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_task_event_contains_text_summary_for_partial_history(self) -> None:
        audit = NullAuditLogger()
        agent = SimpleNamespace(
            agent_id="recon-a", agent_type=AgentType.RECON,
            ctx=SimpleNamespace(audit=audit, blackboard=Mock(), bus=SimpleNamespace(publish=AsyncMock())),
        )
        task = Task(task_id="t1", task_type="recon", target="test", agent_type=AgentType.RECON,
                    status=TaskStatus.DONE)
        routes = [RouteState(route_id="r1", task_id="t1", name="入口核实", status=TaskStatus.DONE,
                             notes=["中途进度", "确认登录入口，尚未认证。"])]

        await SubAgent._report(agent, task, routes, {"routes_total": 1, "routes_done": 1})

        event = audit.records[-1]
        self.assertEqual(event["type"], "task.done")
        self.assertEqual(event["detail"]["task_id"], "t1")
        self.assertEqual(event["detail"]["conclusions"],
                         [{"route": "入口核实", "text": "确认登录入口，尚未认证。"}])

    async def test_default_stream_is_scoped_and_flushed_even_for_short_output(self) -> None:
        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                               audit=audit, mock_backend=MockBackend(script=["短输出"]))
        token = current_execution_scope.set(ExecutionScope(agent_id="recon-a", task_id="task-a", route_id="route-a"))
        try:
            await gateway.chat([{"role": "user", "content": "fixture"}])
        finally:
            current_execution_scope.reset(token)
        events = [r for r in audit.records if r["type"] == "agent.delta"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "recon-a")
        self.assertEqual(events[0]["detail"]["route"], "route-a")
        self.assertEqual(events[0]["detail"]["text"], "短输出")
        started = next(r for r in audit.records if r["type"] == "model.started")
        self.assertEqual(events[0]["detail"]["call_id"], started["detail"]["call_id"])

    async def test_custom_stream_callback_is_not_double_broadcast(self) -> None:
        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                               audit=audit, mock_backend=MockBackend(script=["CTF 原有回调"]))
        chunks = []
        token = current_execution_scope.set(ExecutionScope(agent_id="ctf-web-a"))
        try:
            await gateway.chat([], on_delta=lambda kind, text: chunks.append((kind, text)))
        finally:
            current_execution_scope.reset(token)
        self.assertTrue(chunks)
        self.assertFalse(any(r["type"] == "agent.delta" for r in audit.records))

    async def test_parallel_contexts_do_not_mix_actors(self) -> None:
        audit = NullAuditLogger()
        gateway = ModelGateway(ModelRegistry([ModelEntry(model="fixture", provider="mock")]),
                               audit=audit, mock_backend=MockBackend(script=["one", "two"]))
        async def run(actor: str) -> None:
            token = current_execution_scope.set(ExecutionScope(agent_id=actor))
            try:
                await gateway.chat([])
            finally:
                current_execution_scope.reset(token)
        await asyncio.gather(run("agent-one"), run("agent-two"))
        self.assertEqual({r["actor"] for r in audit.records if r["type"] == "agent.delta"},
                         {"agent-one", "agent-two"})

    def test_chunks_keep_complete_text_and_ignore_tool_argument_fragments(self) -> None:
        audit = NullAuditLogger()
        sink = AuditStream(audit, "agent", {"call_id": "call"})
        sink("tool_call", "private argument")
        sink("reasoning", "分析" * 3000)
        sink("text", "结果")
        sink.flush()
        deltas = [r["detail"] for r in audit.records]
        self.assertEqual("".join(r["text"] for r in deltas if r["kind"] == "reasoning"), "分析" * 3000)
        self.assertEqual(deltas[-1]["text"], "结果")
        self.assertTrue(all(len(r["text"]) <= 4000 for r in deltas))

    def test_local_display_stream_preserves_large_chunks_and_retry_reset(self) -> None:
        audit = NullAuditLogger()
        scope = ExecutionScope(agent_id="local-display", task_id="display-task", route_id="display-route")
        token = current_execution_scope.set(scope)
        try:
            sink = _DeltaEmitter(audit, "local-display", route="display", challenge="fixture",
                                 turn=2, interval=3600)
            original = "配置说明" * 2500
            sink("text", original)
            sink("text", "尚未刷出的旧尾部")
            sink("reset", "")
            self.assertFalse(sink.has_public_text)
            sink("text", "重新生成的说明")
            sink.flush()
        finally:
            current_execution_scope.reset(token)
        deltas = [record["detail"] for record in audit.records]
        reset = next(i for i, detail in enumerate(deltas) if detail["kind"] == "reset")
        self.assertEqual("".join(detail["text"] for detail in deltas[:reset]), original)
        self.assertEqual(deltas[reset]["text"], "")
        self.assertEqual("".join(detail["text"] for detail in deltas[reset + 1:]), "重新生成的说明")
        self.assertTrue(all(len(detail["text"]) <= 4000 for detail in deltas))
        self.assertTrue(all(detail["task_id"] == scope.task_id and detail["route_id"] == scope.route_id
                            and detail["turn"] == 2 for detail in deltas))
