"""单题报告持久后台队列；主会话不等待写作，重启可恢复未完成报告。"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from spiritscout.ctf.writeup import writeup_body
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope

_TASKS: dict[str, asyncio.Task] = {}


def status(workspace: Path) -> dict[str, Any]:
    try:
        data = json.loads((workspace / 'report_job.json').read_text(encoding='utf-8'))
        return {key: data.get(key) for key in ('status', 'error', 'report_path')}
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        os.chmod(temp, 0o600)
        json.dump(data, stream, ensure_ascii=False)
    temp.replace(path)


def enqueue(workspace: Path, report_path: Path, digest: dict, gateway: Any, audit: Any,
            *, enabled: bool = True) -> asyncio.Task | None:
    snapshot = workspace / 'report_job.json'
    # 每次解题完成产生一份报告；同一后台任务的重复调度不启动第二个写手。
    key = str(snapshot.resolve())
    try:
        previous = json.loads(snapshot.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    if (previous.get('digest') == digest and previous.get('report_path') == str(report_path)
            and previous.get('enabled') == enabled):
        if previous.get('status') == 'done' and report_path.exists():
            return None
        if previous.get('status') in {'queued', 'running'}:
            return _start(snapshot, previous, gateway, audit)
    if key in _TASKS and not _TASKS[key].done():
        _TASKS.pop(key).cancel()
    job = {'job_id': uuid.uuid4().hex, 'status': 'queued', 'report_path': str(report_path), 'digest': digest, 'enabled': enabled}
    _save(snapshot, job)
    return _start(snapshot, job, gateway, audit)


def resume(workspace: Path, gateway: Any, audit: Any) -> asyncio.Task | None:
    snapshot = workspace / 'report_job.json'
    try:
        job = json.loads(snapshot.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if job.get('status') not in {'queued', 'running'}:
        return None
    return _start(snapshot, job, gateway, audit)


def _start(snapshot: Path, job: dict, gateway: Any, audit: Any) -> asyncio.Task:
    key = str(snapshot.resolve())
    if key in _TASKS and not _TASKS[key].done():
        return _TASKS[key]
    task = asyncio.create_task(_run(snapshot, job, gateway, audit))
    _TASKS[key] = task
    task.add_done_callback(lambda done: _TASKS.pop(key, None) if _TASKS.get(key) is done else None)
    return task


def _update(snapshot: Path, job: dict) -> None:
    try:
        if json.loads(snapshot.read_text()).get('job_id') != job.get('job_id'):
            return
    except (OSError, ValueError):
        return
    _save(snapshot, job)


async def _run(snapshot: Path, job: dict, gateway: Any, audit: Any) -> None:
    token = current_execution_scope.set(ExecutionScope())
    try:
        # 不与解题争抢前台；报告与经验提炼之间没有串行依赖。
        from spiritscout import runtime
        while runtime._FOREGROUND_DEPTH > 0:
            await asyncio.sleep(.2)
        job['status'] = 'running'
        _update(snapshot, job)
        digest = job['digest']
        body = await writeup_body(gateway, digest=digest, audit=audit) if job.get('enabled') else None
        if not body:
            answers = '\n'.join('- ' + str(row.get('value', '')) for row in digest.get('flags', []))
            stages = '\n\n'.join(str(row.get('text', '')) for row in digest.get('chronology', []))
            body = '# 解题报告\n\n以下由已有执行记录整理，未补造缺失过程。\n\n## 答案\n\n' + answers + '\n\n## 过程与依据\n\n' + stages
        if json.loads(snapshot.read_text()).get('job_id') != job.get('job_id'):
            return
        path = Path(job['report_path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(body, encoding='utf-8')
        temporary.replace(path)
        job.update(status='done', error='')
        _update(snapshot, job)
        audit.log('report.ready', 'report', {'path': str(path)})
    except asyncio.CancelledError:
        job['status'] = 'queued'
        _update(snapshot, job)
        raise
    except Exception as exc:  # noqa: BLE001 —— 后台失败不得改变已完成会话
        job.update(status='failed', error=str(exc)[:500])
        _update(snapshot, job)
        audit.log('report.failed', 'report', {'error': job['error']})
    finally:
        current_execution_scope.reset(token)
