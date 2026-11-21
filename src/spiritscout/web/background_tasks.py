"""后台命令档案的分页读取、无损下载、显式清理及停止入口。"""
from __future__ import annotations

import json
import sqlite3
import zlib
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from spiritscout.tools.background import active_jobs, task_rows
from spiritscout.tools.background_archive import BackgroundArchive


def mount_background_tasks(app: FastAPI, manager):
    def session(sid):
        value = manager.get(sid)
        if value is None:
            raise HTTPException(404, "会话不存在")
        return value

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @contextmanager
    def archive(value, *, writable=False):
        store = None
        try:
            control = active_jobs(value.workspace)
            limit = control.archive_limit if control else getattr(getattr(getattr(manager, "_cfg", None), "storage", None), "background_archive_max_mb", 512) * 1024 * 1024
            store = BackgroundArchive(value.workspace, writable=writable, limit=limit)
            yield store
        except (OSError, ValueError, TypeError, sqlite3.Error, zlib.error) as exc:
            raise HTTPException(503, "后台任务存档不可读或写入失败，请保留文件并检查磁盘") from exc
        finally:
            if store:
                store.close()

    def live_item(value, item):
        control = active_jobs(value.workspace)
        job = control.jobs.get(item["job_id"]) if control else None
        if job and item.get("archive_state") != "cleared":
            item = {**item, **control.archive_row(job), "stdout": job.output["stdout"][-2000:], "stderr": job.output["stderr"][-2000:]}
            if job.archive_error:
                item.update(archive_state="error", archive_error=job.archive_error)
        elif item["status"] == "running":
            item = {**item, "status": "interrupted", "archive_state": "interrupted" if item.get("archive_state") == "complete" else item.get("archive_state", "legacy")}
        return item

    def lookup(value, store, job_id):
        item = store.get(job_id)
        if item is None:
            control = active_jobs(value.workspace)
            job = control.jobs.get(job_id) if control else None
            item = {**control.archive_row(job), "archive_state": "error", "archive_error": job.archive_error} if job else next((row for row in task_rows(value.workspace) if row["job_id"] == job_id), None)
            if item is None:
                raise HTTPException(404, "本会话没有该后台任务")
            item.setdefault("archive_state", "legacy")
        return live_item(value, item)

    @app.get("/api/sessions/{session_id}/background-tasks")
    def jobs(session_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        value = session(session_id)
        with archive(value) as store:
            data = store.listing(offset, limit)
            if store.conn is None:
                rows = [live_item(value, {**row, "archive_state": "legacy"}) for row in task_rows(value.workspace)]
                control = active_jobs(value.workspace)
                if control:
                    rows = [live_item(value, {**control.archive_row(job), "archive_state": "error"}) for job in control.jobs.values()]
                    rows.sort(key=lambda row: row["created_at"], reverse=True)
                data.update(items=rows[offset:offset + limit], total=len(rows), counts={status: sum(row["status"] == status for row in rows) for status in {row["status"] for row in rows}})
            else:
                data["items"] = [live_item(value, row) for row in data["items"]]
                control = active_jobs(value.workspace)
                if offset == 0 and control:
                    for job in control.jobs.values():
                        if job.archive_error and store.get(job.job_id) is None:
                            data["items"].insert(0, live_item(value, {"job_id": job.job_id, "status": job.status, "archive_state": "error"}))
                            data["total"] += 1
                            data["counts"][job.status] = data["counts"].get(job.status, 0) + 1
                running = sum(job.status == "running" for job in control.jobs.values()) if control else 0
                orphaned = max(0, data["counts"].get("running", 0) - running)
                data["counts"]["running"] = running
                data["counts"]["interrupted"] = data["counts"].get("interrupted", 0) + orphaned
            # 命令正文只在选中任务时读取，轮询不重复传输大脚本。
            for item in data["items"]:
                item.pop("command", None)
            return response({**data, "has_more": offset + limit < data["total"]})

    @app.get("/api/sessions/{session_id}/background-tasks/{job_id}")
    def detail(session_id: str, job_id: str):
        value = session(session_id)
        with archive(value) as store:
            return response(lookup(value, store, job_id))

    @app.get("/api/sessions/{session_id}/background-tasks/{job_id}/output")
    def output(session_id: str, job_id: str, stream: str = Query("stdout", pattern="^(stdout|stderr)$"),
               before: int | None = Query(None, ge=0), limit: int = Query(32768, ge=1024, le=65536),
               q: str = Query("", max_length=1024), after: int = Query(0, ge=0)):
        value = session(session_id)
        with archive(value) as store:
            item = lookup(value, store, job_id)
            if store.get(job_id) is None or item["archive_state"] in {"legacy", "cleared"}:
                return response({"text": item.get(stream, ""), "start": 0, "end": 0, "total_bytes": 0, "has_earlier": False, "match": None, "archive_state": item["archive_state"]})
            return response({**store.output(job_id, stream, before=before, limit=limit, query=q, after=after), "archive_state": item["archive_state"]})

    @app.get("/api/sessions/{session_id}/background-tasks/{job_id}/download")
    def download(session_id: str, job_id: str, stream: str = Query("stdout", pattern="^(stdout|stderr|command)$")):
        value = session(session_id)
        with archive(value) as store:
            item = lookup(value, store, job_id)
            if item["archive_state"] in {"legacy", "cleared"} or (stream == "command" and "command" not in item):
                raise HTTPException(409, "原始存档不可用；旧版预览或已清理记录不能作为完整日志下载")
            end = item.get(f"{stream}_bytes", 0)
            if stream != "command" and store.get(job_id) is None:
                raise HTTPException(503, "输出存档不可用")
        def content():
            if stream == "command":
                yield item["command"].encode("utf-8")
            else:
                with archive(value) as source:
                    yield from source.chunks(job_id, stream, end=end)
        suffix = "partial" if item["archive_state"] != "complete" else "snapshot" if item["status"] == "running" else "log"
        # 文件名不使用用户命令/标签，避免控制字符或路径注入。
        safe_id = "".join(char for char in job_id if char.isalnum())[:64]
        return StreamingResponse(content(), media_type="application/octet-stream", headers={
            "Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="background-{safe_id}-{stream}.{suffix}.txt"',
            "X-Archive-State": item["archive_state"]})

    @app.delete("/api/sessions/{session_id}/background-tasks/{job_id}/archive")
    def clear(session_id: str, job_id: str):
        value = session(session_id)
        with archive(value, writable=True) as store:
            item = lookup(value, store, job_id)
            if item["status"] == "running":
                raise HTTPException(409, "运行中的任务不能清理存档")
            if store.get(job_id) is None:
                raise HTTPException(409, "该任务没有可清理的存档")
            store.save(item)
            store.clear(job_id)
            return response({"job_id": job_id, "archive_state": "cleared"})

    @app.post("/api/sessions/{session_id}/background-tasks/{job_id}/stop")
    async def stop(session_id: str, job_id: str):
        value = session(session_id)
        control = active_jobs(value.workspace)
        job = control.jobs.get(job_id) if control else None
        if job is None:
            raise HTTPException(404, "本会话没有该运行中的后台任务")
        result = await control.stop(job_id, workspace_scope=job.scope)
        return response(json.loads(result.stdout))
