"""隔离审计复现：仅使用临时目录、无害子进程和本机 HTTP 服务。

运行：.venv/bin/python audits/2026-10-15/reproduce.py
需已安装项目依赖与可用的 bubblewrap；不调用真实模型或比赛提交接口。
输出为当前行为证据，不是通过即代表问题已修复的回归测试。
"""

import asyncio
import gzip
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from spiritscout.agents.master.agent import MasterAgent
from spiritscout.agents.master.dag import TaskDAG
from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent, SubAgentContext
from spiritscout.blackboard import Blackboard, ConvergenceEngine
from spiritscout.bus.inprocess import InProcessBus
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    RunMode,
    Scope,
    Task,
    TaskStatus,
)
from spiritscout.ctf.flags import FlagManager, SubmitResult
from spiritscout.policy.engine import PolicyEngine
from spiritscout.policy.scope import ScopeGuard
from spiritscout.tools.builtin import http_probe
from spiritscout.tools.executor import LocalExecutor
from spiritscout.tools.journal import ExecutionJournal, read_recovery_view
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.run_lock import ExecutionRunLock
from spiritscout.tools.sandbox import sandbox_exec
from spiritscout.tools.spec import ToolKind, ToolSpec


class FailingAgent(SubAgent):
    health_prune_enabled = False

    def route_templates(self, task):
        return []

    async def plan_routes(self, task):
        return [RoutePlan("only", "audit", [{"action": "no-op"}])]

    async def execute_step(self, task, route, plan, step):
        return StepResult(ok=False, note="test adapter unavailable")


async def main():
    output = {}
    with tempfile.TemporaryDirectory(prefix="ss-audit-") as temp:
        root = Path(temp)
        bb = Blackboard(root / "session" / "bb.db")
        bus = InProcessBus(rate_limit_per_second=0)
        await bus.start()
        guard = ScopeGuard(Scope(local_analysis=True))
        policy = PolicyEngine(RunMode.CTF, guard)
        registry = ToolRegistry(LocalExecutor(), policy=policy)
        ctx = SubAgentContext(
            bus, bb, ConvergenceEngine(bb), SimpleNamespace(), registry, policy
        )
        agent = FailingAgent(ctx, enable_llm_planning=False)
        task = Task(
            task_id="audit-task",
            task_type="recon",
            target="local",
            agent_type=AgentType.RECON,
        )
        summary = await agent.run(task)
        rec = bb.read(BlackboardLayer.TASK_STATE, task.task_id)
        output["failed_step_task"] = {
            "task_status": task.status.value,
            "routes_done": summary["routes_done"],
            "route_progress": rec["routes"][0]["progress"],
        }

        dag = TaskDAG()
        parent = dag.add_node("PARENT", "failed parent")
        dag.add_node("CHILD", "blocked child", depends_on=[parent.node_id])
        dag.mark(parent.node_id, TaskStatus.FAILED)
        master = object.__new__(MasterAgent)
        master.agent_id = "audit-master"
        master.ctx = SimpleNamespace(
            audit=NullAuditLogger(),
            enable_llm_planning=True,
            extra={"user_inbox": []},
            plan_override=None,
            pause_gate=None,
        )
        master._final_verdicts = Mock(return_value=[])
        master._llm_review = AsyncMock(
            return_value={"continue_iteration": False, "reason": "用户目标已结束"}
        )
        state = {
            "round_no": 0,
            "dag": dag.to_dict(),
            "round_views": [{"flags": {}, "vuln_in_pipeline": 0}],
        }
        review = await master._review(state)
        output["model_stop"] = {
            "ready_nodes": len(dag.ready_nodes()),
            "continue_iteration": review["continue_iteration"],
            "stop_reason": review["stop_reason"],
        }

        marker = root / "child-marker"
        child_code = f"import time; from pathlib import Path; time.sleep(.25); Path({str(marker)!r}).write_text('audit')"
        parent_code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child_code!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
        result = await LocalExecutor().run_argv(
            ToolSpec("audit-cli", ToolKind.CLI, timeout_seconds=2),
            [sys.executable, "-c", parent_code],
        )
        before = marker.exists()
        await asyncio.sleep(0.5)
        output["background_process"] = {
            "tool_ok": result.ok,
            "marker_when_returned": before,
            "marker_after_return": marker.exists(),
        }

        calls = []

        async def submit(value, challenge):
            calls.append((value, challenge))
            await asyncio.sleep(0.02)
            return SubmitResult(True, "accepted")

        fm = FlagManager(submit_fn=submit, rate_limit_per_minute=600000)
        candidate = fm.register(
            "flag{audit}", challenge_id="audit-challenge", found_by="audit"
        )
        await asyncio.gather(fm.submit(candidate), fm.submit(candidate))
        output["flag_concurrent"] = {
            "submitted_requests": len(calls),
            "accepted": candidate.accepted,
        }

        journal = ExecutionJournal(bb, root / "session", "run-one")
        lock = ExecutionRunLock(root / "session")
        lock.acquire()
        try:
            journal.activate_run(lock)
            journal.begin("audit-inv", "audit-tool", "digest", "safe")
            journal.transition("audit-inv", "failed")
            code = (
                "import sqlite3; c=sqlite3.connect("
                + repr(str(root / "session" / "bb.db"))
                + "); c.execute(\"UPDATE tool_invocations SET execution_status='succeeded' WHERE invocation_id='audit-inv'\"); c.commit(); print('canary updated')"
            )
            result = await sandbox_exec(
                code=code, language="python", workspace_root=str(root / "session")
            )
            output["sandbox_control_write"] = {
                "tool_ok": result.ok,
                "returncode": result.returncode,
                "stored_status": journal.list_recent()[0]["execution_status"],
                "error": result.error,
            }

            other = root / "other-session"
            second_lock = ExecutionRunLock(other)
            second_lock.acquire()
            try:
                blocked = False
                try:
                    second = ExecutionJournal(bb, other, "run-two")
                    second.activate_run(second_lock)
                except ConfigurationError:
                    blocked = True
                output["shared_database"] = {
                    "second_owner_blocked": blocked,
                    "first_lock_still_owned": lock.owned,
                    "runs": {
                        r["run_id"]: r["status"]
                        for r in read_recovery_view(root / "session" / "bb.db")["runs"]
                    },
                }
            finally:
                second_lock.close()
        finally:
            lock.close()

        payload = gzip.compress(b"A" * (8 * 1024 * 1024))

        async def serve(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: "
                + str(len(payload)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + payload
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sizes = []
        original = httpx.Response.aiter_bytes

        async def measured(response, *args, **kwargs):
            async for chunk in original(response, *args, **kwargs):
                sizes.append(len(chunk))
                yield chunk

        try:
            with patch.object(httpx.Response, "aiter_bytes", measured):
                result = await http_probe(
                    f"http://127.0.0.1:{port}", guard=guard, max_output_chars=1000
                )
            output["http_cap"] = {
                "tool_ok": result.ok,
                "configured_byte_window": 4000,
                "largest_httpx_auto_decoded_chunk": max(sizes, default=0),
                "body_truncated": result.parsed.get("body_truncated", False),
                "wire_body_bytes": len(payload),
            }
        finally:
            server.close()
            await server.wait_closed()

        audit_path = root / "audit.jsonl"
        AuditLogger(audit_path).log(
            "agent.tool_call",
            "audit",
            {
                "calls": [
                    {
                        "arguments": {
                            "code": 'curl -H "Authorization: Bearer DUMMY_AUDIT_SECRET" http://example.invalid'
                        }
                    }
                ]
            },
        )
        output["audit_redaction"] = {
            "dummy_secret_in_plaintext": "DUMMY_AUDIT_SECRET" in audit_path.read_text()
        }
        await bb.aclose()
        await bus.stop()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
