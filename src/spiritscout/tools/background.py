"""Linux 沙箱后台作业与事件等待；等待期间不调用模型，不轮询目标。"""

from __future__ import annotations

import asyncio
import codecs
import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from spiritscout.tools.invocation import current_execution_scope
from spiritscout.tools.sandbox import background_output, sandbox_exec
from spiritscout.tools.spec import (
    ActionCategory,
    RiskLevel,
    ToolKind,
    ToolResult,
    ToolSpec,
)

NAMES = frozenset(
    {
        "shell_start",
        "shell_status",
        "shell_wait",
        "shell_watch",
        "shell_events",
        "shell_stop",
        "task_wait",
    }
)
WINDOW = 65536
_ACTIVE: dict[str, Any] = {}


def active_jobs(workspace: str | Path):
    return _ACTIVE.get(str(Path(workspace).resolve()))


def task_rows(workspace: str | Path):
    manager = active_jobs(workspace)
    if manager:
        return manager.rows()
    folder = Path(workspace) / "background-tasks"
    path = folder / "jobs.json"
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        return []
    with path.open("rb") as source:
        raw = source.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("后台任务索引过大")
    rows = json.loads(raw)
    for row in rows:
        if row["status"] == "running":
            row["status"] = "interrupted"
    return rows


def take_notifications(workspace: str | Path, owner: str, scope: str = ""):
    manager = active_jobs(workspace) if workspace else None
    return manager.drain(owner, scope) if manager else []


def seconds_until(seconds: float | None = None, until: str = "") -> float:
    if bool(until) == (seconds is not None):
        raise ValueError("seconds 和 until 需要且只能填写一个")
    if until:
        when = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("until 需要包含时区，例如 +08:00")
        value = max(0, when.timestamp() - time.time())
    else:
        value = float(seconds)
    if not math.isfinite(value) or value < 0:
        raise ValueError("等待秒数须为有限的非负数")
    return value


@dataclass
class Job:
    job_id: str
    scope: str
    task: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    output: dict[str, str] = field(default_factory=lambda: {"stdout": "", "stderr": ""})
    matched: dict[tuple[str, str], bool] = field(default_factory=dict)
    result: ToolResult | None = None
    status: str = "running"
    error: str = ""
    pid: int | None = None
    label: str = "后台 Shell"
    owner: str = ""
    created_at: float = field(default_factory=time.time)
    watchers: list[dict[str, Any]] = field(default_factory=list)
    on_output: Any = None

    def emit(self, stream: str, text: str) -> None:
        previous = self.output[stream]
        combined = previous + text
        for kind, value in self.matched:
            if kind in (stream, "both") and value in combined:
                self.matched[(kind, value)] = True
        self.output[stream] = combined[-WINDOW:]
        self.changed.set()
        if self.on_output:
            self.on_output()


class ShellJobs:
    def __init__(
        self,
        workspace: str,
        *,
        audit: Any,
        guard_root: str = "",
        pause_gate: Any = None,
    ):
        self.workspace, self.audit, self.guard_root, self.pause_gate = (
            workspace,
            audit,
            guard_root,
            pause_gate,
        )
        self.jobs: dict[str, Job] = {}
        self.closed = False
        self.notifications: list[dict[str, Any]] = []
        _ACTIVE[str(Path(workspace).resolve())] = self

    def rows(self):
        return [
            {
                **self.snapshot(job).parsed,
                "stdout": job.output["stdout"][-2000:],
                "stderr": job.output["stderr"][-2000:],
                "error": job.error,
                "watches": [
                    {key: value for key, value in watch.items() if key != "owner"}
                    for watch in job.watchers
                ],
            }
            for job in sorted(
                self.jobs.values(), key=lambda value: (value.status == "running", value.created_at), reverse=True
            )[:50]
        ]

    def persist(self):
        try:
            folder = Path(self.workspace) / "background-tasks"
            if folder.is_symlink():
                return
            folder.mkdir(mode=0o700, exist_ok=True)
            path = folder / (uuid.uuid4().hex + ".tmp")
            try:
                with os.fdopen(
                    os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
                ) as output:
                    json.dump(self.rows(), output, ensure_ascii=False)
                path.replace(folder / "jobs.json")
            finally:
                path.unlink(missing_ok=True)
        except OSError:
            pass  # 证据副本失败不改变后台进程，也不重放命令。

    def notify(self, job, kind, owner, **data):
        item = {
            "event_id": uuid.uuid4().hex,
            "kind": kind,
            "job_id": job.job_id,
            "label": job.label,
            "status": job.status,
            "scope": job.scope,
            "owner": owner,
            **data,
        }
        self.notifications.append(item)
        self.notifications = self.notifications[-200:]
        self.audit.log(
            "shell.notification",
            owner or "runtime",
            {key: value for key, value in item.items() if key != "owner"},
        )

    def check_watches(self, job):
        changed = False
        for watch in job.watchers:
            if watch["notified"]:
                continue
            matched = job.matched[(watch["stream"], watch["contains"])]
            if matched or job.status != "running":
                watch["notified"] = True
                watch["matched"] = matched
                self.notify(
                    job,
                    "condition",
                    watch["owner"],
                    watch_id=watch["watch_id"],
                    matched=matched,
                    contains=watch["contains"],
                )
                changed = True
        if changed:
            self.persist()

    def drain(self, owner, scope=""):
        ready = [
            item
            for item in self.notifications
            if item["owner"] == owner and item["scope"] == scope
        ][:10]
        keys = {item["event_id"] for item in ready}
        self.notifications = [
            item for item in self.notifications if item["event_id"] not in keys
        ]
        return [
            {key: value for key, value in item.items() if key not in {"owner", "scope"}}
            for item in ready
        ]

    def get(self, job_id: str, workspace_scope: str = "") -> Job:
        job = self.jobs.get(job_id)
        if job is None or job.scope != str(workspace_scope or ""):
            raise ValueError("当前会话 / 题目没有该后台作业")
        return job

    def snapshot(
        self, job: Job, *, matched: bool | None = None, timed_out: bool = False
    ) -> ToolResult:
        parsed = {
            "job_id": job.job_id,
            "status": job.status,
            "returncode": job.result.returncode if job.result else None,
            "matched": matched,
            "wait_timed_out": timed_out,
            "output_window_chars": WINDOW,
            "label": job.label,
            "owner": job.owner,
            "scope": job.scope,
            "created_at": job.created_at,
            "output_file": job.result.parsed.get("output_file", "")
            if job.result
            else "",
        }
        return ToolResult(
            tool="shell_status",
            ok=not job.error,
            stdout=json.dumps(
                {**parsed, **{name: text[-2000:] for name, text in job.output.items()}},
                ensure_ascii=False,
            ),
            parsed=parsed,
            error=job.error,
        )

    async def start(
        self,
        code: str,
        label: str = "后台 Shell",
        *,
        workspace_scope: str = "",
        **_: Any,
    ) -> ToolResult:
        if self.closed or not str(code).strip():
            return ToolResult(
                "shell_start", False, error="后台作业服务已关闭或命令为空"
            )
        job = Job(uuid.uuid4().hex, str(workspace_scope or ""))
        job.label = str(label or "后台 Shell")[:100]
        scope = current_execution_scope.get()
        job.owner = scope.agent_id if scope else ""
        job.on_output = lambda: self.check_watches(job)
        self.jobs[job.job_id] = job

        async def run():
            decoders = {
                name: codecs.getincrementaldecoder("utf-8")(errors="replace")
                for name in ("stdout", "stderr")
            }

            def emit(stream, data):
                if stream == "started":
                    job.pid = data
                    return
                job.emit(stream, decoders[stream].decode(data))

            token = background_output.set(emit)
            try:
                job.result = await sandbox_exec(
                    code,
                    workspace_root=self.workspace,
                    workspace_scope=job.scope,
                    guard_root=self.guard_root,
                    log_name="background-" + job.job_id,
                    max_output_chars=4000,
                )
                job.status = "completed" if job.result.ok else "failed"
                job.error = job.result.error
            except asyncio.CancelledError:
                job.status = "stopped"
                raise
            except Exception as exc:  # noqa: BLE001 — 后台异常必须唤醒等待者
                job.status, job.error = "failed", type(exc).__name__
            finally:
                for name, decoder in decoders.items():
                    job.emit(name, decoder.decode(b"", final=True))
                background_output.reset(token)
                job.changed.set()
                self.audit.log(
                    "shell.finished",
                    "runtime",
                    {
                        "job_id": job.job_id,
                        "status": job.status,
                        "returncode": job.result.returncode if job.result else None,
                    },
                )
                self.check_watches(job)
                self.notify(
                    job,
                    "completed",
                    job.owner,
                    returncode=job.result.returncode if job.result else None,
                )
                self.persist()

        job.task = asyncio.create_task(run(), name="shell-" + job.job_id)
        self.audit.log(
            "shell.started",
            "runtime",
            {"job_id": job.job_id, "workspace_scope": job.scope},
        )
        result = self.snapshot(job)
        self.persist()
        result.tool = "shell_start"
        return result

    async def status(
        self, job_id: str, *, workspace_scope: str = "", **_: Any
    ) -> ToolResult:
        return self.snapshot(self.get(job_id, workspace_scope))

    async def watch(
        self,
        job_id: str,
        contains: str,
        stream: str = "both",
        *,
        workspace_scope: str = "",
        **_: Any,
    ):
        job = self.get(job_id, workspace_scope)
        if (
            stream not in {"stdout", "stderr", "both"}
            or not isinstance(contains, str)
            or not contains
            or len(contains) > 4096
        ):
            raise ValueError("需要非空的字面量条件和 stdout/stderr/both 输出流")
        scope = current_execution_scope.get()
        key = (stream, contains)
        job.matched.setdefault(
            key,
            any(
                contains in text
                for name, text in job.output.items()
                if stream in {name, "both"}
            ),
        )
        watch = {
            "watch_id": uuid.uuid4().hex,
            "contains": contains,
            "stream": stream,
            "notified": False,
            "matched": False,
            "owner": scope.agent_id if scope else "",
        }
        job.watchers.append(watch)
        self.check_watches(job)
        self.persist()
        return ToolResult(
            "shell_watch",
            True,
            stdout=json.dumps(
                {key: value for key, value in watch.items() if key != "owner"},
                ensure_ascii=False,
            ),
            parsed={"job_id": job_id, "watch_id": watch["watch_id"], "blocking": False},
        )

    async def events(self, *, workspace_scope: str = "", **_: Any):
        scope = current_execution_scope.get()
        return ToolResult(
            "shell_events",
            True,
            stdout=json.dumps(
                self.drain(scope.agent_id if scope else "", workspace_scope),
                ensure_ascii=False,
            ),
        )

    async def wait(
        self,
        job_id: str,
        contains: str = "",
        stream: str = "both",
        timeout: float | None = None,
        *,
        workspace_scope: str = "",
        **_: Any,
    ) -> ToolResult:
        job = self.get(job_id, workspace_scope)
        if (
            stream not in {"stdout", "stderr", "both"}
            or not isinstance(contains, str)
            or len(contains) > 4096
        ):
            raise ValueError(
                "stream 为 stdout/stderr/both，contains 为最多 4096 字符的字面量"
            )
        if timeout is not None:
            timeout = seconds_until(timeout)
        key = (stream, contains)
        job.matched.setdefault(
            key,
            bool(
                contains
                and any(
                    contains in text
                    for name, text in job.output.items()
                    if stream in (name, "both")
                )
            ),
        )
        self.audit.log(
            "agent.waiting",
            "runtime",
            {
                "kind": "shell",
                "job_id": job_id,
                "condition": contains,
                "stream": stream,
            },
        )

        async def pending():
            while job.status == "running" and not (contains and job.matched[key]):
                await job.changed.wait()
                job.changed.clear()

        timed_out = False
        if job.status == "running" and not (contains and job.matched[key]):
            try:
                await asyncio.wait_for(pending(), timeout=timeout)
            except TimeoutError:
                timed_out = True
        if self.pause_gate is not None:
            await self.pause_gate.wait()
        matched = job.matched[key] if contains else job.status != "running"
        self.audit.log(
            "agent.wait_ready",
            "runtime",
            {
                "kind": "shell",
                "job_id": job_id,
                "matched": matched,
                "timed_out": timed_out,
            },
        )
        result = self.snapshot(job, matched=matched, timed_out=timed_out)
        result.tool = "shell_wait"
        return result

    async def stop(
        self, job_id: str, *, workspace_scope: str = "", **_: Any
    ) -> ToolResult:
        job = self.get(job_id, workspace_scope)
        if job.task and not job.task.done():
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            job.status = "stopped"
            job.changed.set()
        result = self.snapshot(job)
        result.tool = "shell_stop"
        return result

    async def delay(
        self, seconds: float | None = None, until: str = "", **_: Any
    ) -> ToolResult:
        delay = seconds_until(seconds, until)
        self.audit.log(
            "agent.waiting",
            "runtime",
            {"kind": "timer", "wake_at": time.time() + delay},
        )
        await asyncio.sleep(delay)
        if self.pause_gate is not None:
            await self.pause_gate.wait()
        self.audit.log("agent.wait_ready", "runtime", {"kind": "timer"})
        return ToolResult("task_wait", True, stdout="已到达唤醒时间，可继续任务。")

    async def aclose(self):
        self.closed = True
        tasks = [
            job.task for job in self.jobs.values() if job.task and not job.task.done()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self.jobs.values():
            if job.status == "running":
                job.status = "stopped"
                job.changed.set()
        self.persist()
        if active_jobs(self.workspace) is self:
            _ACTIVE.pop(str(Path(self.workspace).resolve()), None)


PARAMS = {
    "shell_start": {"code", "action", "label"},
    "shell_status": {"job_id"},
    "shell_wait": {"job_id", "contains", "stream", "timeout"},
    "shell_stop": {"job_id"},
    "task_wait": {"seconds", "until"},
    "shell_watch": {"job_id", "contains", "stream"},
    "shell_events": set(),
}
SPECS = {
    name: ToolSpec(
        name,
        ToolKind.BUILTIN,
        risk=RiskLevel.READONLY,
        allowed_params=params,
        action_required=name == "shell_start",
        automatic_replay=False,
        strict_params=True,
        description="沙箱后台执行或本地事件等待；等待期间不调用模型",
    )
    for name, params in PARAMS.items()
}


def schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


SCHEMAS = [
    schema(
        "shell_watch",
        "注册后台输出条件并立即返回，不阻塞当前 Agent 继续其他工作。命中或进程退出后，下一回合自动收到通知；确实依赖结果且无其他工作时才 shell_wait。",
        {
            "job_id": {"type": "string"},
            "contains": {"type": "string"},
            "stream": {"type": "string", "enum": ["stdout", "stderr", "both"]},
        },
        ["job_id", "contains"],
    ),
    schema(
        "shell_events",
        "一次性读取自己的后台任务通知。已自动注入的通知不会重复返回，不要轮询。",
        {},
        [],
    ),
    schema(
        "shell_start",
        "在 Linux 沙箱启动持续运行的后台 Shell，立即返回 job_id；不要再用 nohup。结束会话时回收进程。按本次行为声明 action，危险操作仍走审批。",
        {
            "code": {"type": "string"},
            "label": {"type": "string", "description": "便于区分并行后台任务的名称"},
            "action": {
                "type": "string",
                "enum": [item.value for item in ActionCategory],
            },
        },
        ["code", "action"],
    ),
    schema(
        "shell_status",
        "只读查看后台作业及最近输出；等待条件应使用 shell_wait，避免反复轮询消耗模型。",
        {"job_id": {"type": "string"}},
        ["job_id"],
    ),
    schema(
        "shell_wait",
        "挂起当前 Agent，等待后台 Shell 输出指定字面量（跨分块匹配），contains 留空则等待退出；退出未匹配会明确返回 matched=false。等待超时不终止后台进程。",
        {
            "job_id": {"type": "string"},
            "contains": {"type": "string"},
            "stream": {"type": "string", "enum": ["stdout", "stderr", "both"]},
            "timeout": {"type": "number", "minimum": 0},
        },
        ["job_id"],
    ),
    schema(
        "shell_stop",
        "停止本会话/题目的后台作业，回收沙箱进程组。",
        {"job_id": {"type": "string"}},
        ["job_id"],
    ),
    schema(
        "task_wait",
        "挂起当前 Agent，定时唤醒后继续；无需反复调用模型检查时间。seconds 或含时区的 ISO until 二选一。",
        {"seconds": {"type": "number", "minimum": 0}, "until": {"type": "string"}},
        [],
    ),
]
