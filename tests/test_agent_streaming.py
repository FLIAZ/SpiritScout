"""角色流式输出的审计节流、并发归属与既有回调兼容。"""
from __future__ import annotations

import asyncio
import unittest

from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import NullAuditLogger
from spiritscout.gateway import ModelGateway, ModelRegistry
from spiritscout.gateway.backends import MockBackend
from spiritscout.gateway.streaming import AuditStream
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope


class AgentStreamingTests(unittest.IsolatedAsyncioTestCase):
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
