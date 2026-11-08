"""流量目录与会话记录只读 API；沿用控制台认证、项目归类和会话生命周期。"""
from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from spiritscout.traffic import packets, recorder
from spiritscout.traffic.capture import (
    MAX_FILE_BYTES,
    CaptureManager,
    capabilities,
    display_numbers,
    follow_stream,
    source_path,
)
from spiritscout.traffic.proxy import raw_evidence

COVERAGE = "本页展示任务执行过程中记录的 HTTP/HTTPS、DNS、TCP 及会话代理交互；离线 PCAP/PCAPNG 在附件查看器中分析，不作为任务交互混入列表。"


class CaptureStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interface: str = Field(min_length=1, max_length=100)
    capture_filter: str = Field(min_length=1, max_length=1024)
    duration: int = Field(default=60, ge=5, le=600)


class ProxyStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upstream: str = Field(default="", max_length=2048)


def mount_traffic(app: FastAPI, store: Any, manager: Any) -> CaptureManager:
    monitor = CaptureManager(manager)
    manager._traffic_capture = monitor
    app.state.traffic_capture = monitor
    def require(session_id: str) -> Any:
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在")
        return session

    def respond(data: dict[str, Any]) -> JSONResponse:
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/{session_id}/artifacts/packets")
    async def attachment_packets(session_id: str, path: str = Query(..., max_length=1000)) -> JSONResponse:
        from pathlib import Path

        from spiritscout.traffic.capture_io import wait_io
        from spiritscout.web.deliverables import open_artifact

        session = require(session_id)
        def read() -> bytes:
            with open_artifact(session.workspace, path) as source:
                data = source.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("离线流量包预览上限为 20 MiB，原附件仍可下载")
            return data
        try:
            data = await wait_io(read)
            return respond(await monitor.import_attachment(session_id, data, Path(path).name))
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/traffic")
    def catalog() -> JSONResponse:
        projects = store.projects()["items"]
        groups = [{"project_id": "*", "name": "全部项目"},
                  *[{"project_id": p["project_id"], "name": p["name"]} for p in projects],
                  {"project_id": "", "name": "未分类"}]
        known = {p["project_id"] for p in projects}
        for group in groups:
            group.update(session_count=0, total=0, errors=0, request_bytes=0, response_bytes=0,
                         packet_count=0, packet_bytes=0)
        indexed = {g["project_id"]: g for g in groups}
        sessions = []
        for session in manager.list():
            project_id = session.project_id if session.project_id in known else ""
            try:
                stats = recorder.summary(session.workspace)
                stats.update(packets.packet_summary(session.workspace))
                available = True
            except (OSError, ValueError, sqlite3.Error):
                stats = {"total": 0, "errors": 0, "request_bytes": 0, "response_bytes": 0,
                         "last_at": None, "packet_count": 0, "packet_bytes": 0}
                available = False
            sessions.append({"session_id": session.session_id, "title": session.display_title(),
                             "project_id": project_id, "status": session.status,
                             "available": available, **stats})
            for group in (indexed["*"], indexed[project_id]):
                group["session_count"] += 1
                for key in ("total", "errors", "request_bytes", "response_bytes", "packet_count", "packet_bytes"):
                    group[key] += stats[key]
        return respond({"projects": groups, "sessions": sessions, "coverage": COVERAGE})

    @app.get("/api/sessions/{session_id}/traffic")
    def records(session_id: str, before: int | None = Query(None, ge=1),
                limit: int = Query(50, ge=1, le=100), query: str = Query("", max_length=200),
                errors_only: bool = False, method: str = Query("", pattern=r"^[A-Za-z]{0,16}$"),
                status: str = Query("", pattern=r"^(?:[1-5](?:[0-9]{2}|xx))?$"),
                body: str = Query("", max_length=200)) -> JSONResponse:
        session = require(session_id)
        try:
            data = recorder.page(session.workspace, before=before, limit=limit,
                                 query=query, errors_only=errors_only, method=method, status=status, body=body)
            data["filters"] = {"method": method.upper(), "status": status, "body": body}
            data.update(summary=recorder.summary(session.workspace), coverage=COVERAGE)
            data["summary"].update(packets.packet_summary(session.workspace))
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(503, "流量记录暂时不可读，请稍后刷新") from exc
        return respond(data)

    @app.get("/api/sessions/{session_id}/traffic/proxy")
    def proxy_status(session_id: str) -> JSONResponse:
        require(session_id)
        return respond(monitor.proxies.status(session_id))

    @app.post("/api/sessions/{session_id}/traffic/proxy")
    async def proxy_start(session_id: str, payload: ProxyStart) -> JSONResponse:
        require(session_id)
        try:
            return respond(await monitor.proxies.start(session_id, payload.upstream))
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/sessions/{session_id}/traffic/proxy")
    async def proxy_stop(session_id: str) -> JSONResponse:
        require(session_id)
        await monitor.proxies.stop(session_id)
        return respond(monitor.proxies.status(session_id))

    @app.get("/api/sessions/{session_id}/traffic/proxy/environment")
    async def proxy_environment(session_id: str) -> JSONResponse:
        require(session_id)
        proxy = monitor.proxies.sessions.get(session_id)
        if proxy is None:
            raise HTTPException(409, "请先启用本会话录制代理")
        try:
            return respond({"environment": await proxy.environment()})
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/traffic/proxy/ca")
    def proxy_ca(session_id: str) -> FileResponse:
        session = require(session_id)
        root = session.workspace / "traffic-ca"
        path = root / "proxy-ca.pem"
        if root.is_symlink() or path.is_symlink() or not path.is_file():
            raise HTTPException(404, "会话证书不存在")
        return FileResponse(path, media_type="application/x-pem-file", filename="session-proxy-ca.pem",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/sessions/{session_id}/traffic/{record_id}/original")
    def proxy_original(session_id: str, record_id: int,
                       direction: Literal["request", "response", "metadata"] = "response"):
        session = require(session_id)
        try:
            path, metadata = raw_evidence(session.workspace, record_id, direction)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(404, "代理原文不可用") from exc
        if path is None:
            return JSONResponse(metadata, headers={"Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="exchange-{record_id}.json"'})
        return FileResponse(path, media_type="application/octet-stream", filename=f"exchange-{record_id}-{direction}.bin",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/traffic/capabilities")
    async def capture_capabilities() -> JSONResponse:
        return respond(await capabilities())

    @app.get("/api/sessions/{session_id}/traffic/captures")
    def capture_catalog(session_id: str) -> JSONResponse:
        require(session_id)
        try:
            return respond({"items": monitor.catalog(session_id)})
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(503, "抓包索引暂时不可用") from exc

    @app.post("/api/sessions/{session_id}/traffic/capture", status_code=202)
    async def capture_start(session_id: str, payload: CaptureStart) -> JSONResponse:
        require(session_id)
        try:
            return JSONResponse(await monitor.start(session_id, payload.interface, payload.capture_filter, payload.duration),
                                status_code=202, headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/sessions/{session_id}/traffic/capture")
    async def capture_stop(session_id: str) -> JSONResponse:
        require(session_id)
        await monitor.stop(session_id)
        return respond({"status": "stopped"})

    @app.post("/api/sessions/{session_id}/traffic/capture/import", status_code=202)
    async def capture_import(session_id: str, request: Request, name: str = Query("capture.pcap", max_length=200)) -> JSONResponse:
        require(session_id)
        try:
            result = await monitor.import_file(session_id, request.stream(), name)
            return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/traffic/packets")
    async def packet_list(session_id: str, capture_id: str = Query(..., pattern="^[a-f0-9]{32}$"),
                          before: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=200),
                          order: Literal["asc", "desc"] = "desc",
                          display_filter: str = Query("", max_length=512)) -> JSONResponse:
        session = require(session_id)
        try:
            row = packets.get_capture(session.workspace, capture_id)
            if row is None:
                raise HTTPException(404, "抓包记录不存在")
            if display_filter and row["status"] in {"starting", "running"}:
                raise HTTPException(409, "请先停止抓包再应用 Wireshark 显示过滤器")
            numbers = await display_numbers(session.workspace, capture_id, display_filter) if display_filter else None
            return respond(packets.packet_page(session.workspace, capture_id, before=before, limit=limit, numbers=numbers, order=order))
        except (OSError, ValueError, sqlite3.Error, TimeoutError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/traffic/packets/{packet_id}")
    def packet_get(session_id: str, packet_id: int) -> JSONResponse:
        session = require(session_id)
        try:
            row = packets.packet_detail(session.workspace, packet_id)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(503, "数据包详情不可用") from exc
        if row is None:
            raise HTTPException(404, "数据包不存在")
        return respond(row)

    @app.get("/api/sessions/{session_id}/traffic/captures/{capture_id}/download")
    def capture_download(session_id: str, capture_id: str) -> FileResponse:
        session = require(session_id)
        try:
            path, row = source_path(session.workspace, capture_id)
            if row["status"] in {"starting", "running"}:
                raise HTTPException(409, "请先停止抓包，再导出完整文件")
            with path.open("rb") as source:
                suffix = ".pcapng" if source.read(4) == b"\x0a\x0d\x0d\x0a" else ".pcap"
            return FileResponse(path, media_type="application/octet-stream", filename=capture_id + suffix,
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(404, "抓包文件不可用") from exc

    @app.get("/api/sessions/{session_id}/traffic/captures/{capture_id}/follow")
    async def stream_follow(session_id: str, capture_id: str, stream: int = Query(..., ge=0, le=2147483647),
                            protocol: Literal["tcp", "udp"] = "tcp") -> JSONResponse:
        session = require(session_id)
        try:
            return respond(await follow_stream(session.workspace, capture_id, stream, protocol))
        except (OSError, ValueError, sqlite3.Error, TimeoutError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/traffic/{record_id}")
    def record_detail(session_id: str, record_id: int) -> JSONResponse:
        session = require(session_id)
        try:
            data = recorder.detail(session.workspace, record_id)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(503, "流量记录暂时不可读，请稍后刷新") from exc
        if data is None:
            raise HTTPException(404, "流量记录不存在")
        return respond(data)

    return monitor
