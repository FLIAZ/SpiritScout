"""会话后台 Shell 的只读输出和显式停止入口。"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from spiritscout.tools.background import active_jobs, task_rows


def mount_background_tasks(app: FastAPI, manager):
    def session(sid):
        value = manager.get(sid)
        if value is None:
            raise HTTPException(404, "会话不存在")
        return value

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{session_id}/background-tasks")
    async def jobs(session_id: str):
        value = session(session_id)
        try:
            return response({"items": task_rows(value.workspace)})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(503, "后台任务索引暂时不可读") from exc

    @app.post("/api/sessions/{session_id}/background-tasks/{job_id}/stop")
    async def stop(session_id: str, job_id: str):
        value = session(session_id)
        control = active_jobs(value.workspace)
        job = control.jobs.get(job_id) if control else None
        if job is None:
            raise HTTPException(404, "本会话没有该运行中的后台任务")
        result = await control.stop(job_id, workspace_scope=job.scope)
        return response(json.loads(result.stdout))
