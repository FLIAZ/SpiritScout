"""上传、会话消息和文件交付路由；文件及审计处理保持原有语义。"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from spiritscout.config.settings import SystemConfig
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.web.attachment_uploads import materialize_uploads
from spiritscout.web.sessions import TERMINAL_STATES, Session, SessionManager

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _clean_upload_name(name: str) -> str:
    """附件名安全化：只留 basename，拒绝路径分隔符/控制字符/超长。"""
    clean = Path(name.replace("\\", "/")).name.strip()
    if (not clean or clean in (".", "..") or len(clean) > 200
            or any(ord(c) < 32 for c in clean)):
        raise HTTPException(422, "附件名不合法")
    return clean


def _gc_staging(root: Path, *, max_age_s: float = 86400.0) -> None:
    """暂存区惰性回收：超过 24h 未被会话创建带走的上传整目录清除。"""
    now = time.time()
    for d in root.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > max_age_s:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def resolve_staged_uploads(cfg: SystemConfig, payload: dict[str, Any]) -> list[Path]:
    """校验并收集任务下达携带的暂存附件（路径全部收窄在暂存区内）。"""
    ids = [str(u).strip() for u in (payload.get("upload_ids") or [])
           if str(u).strip()]
    if not ids:
        return []
    if len(ids) > 20:
        raise HTTPException(422, "一次最多携带 20 个上传附件")
    staging = (Path(cfg.workspace.root) / "uploads-staging").resolve()
    out: list[Path] = []
    for uid in ids:
        if "/" in uid or "\\" in uid or uid in (".", ".."):
            raise HTTPException(422, "上传标识不合法")
        updir = staging / uid
        if not updir.is_dir():
            raise HTTPException(422, "上传不存在或已过期，请重新选择文件")
        out.extend(sorted(p for p in updir.iterdir() if not p.is_symlink() and (p.is_file() or p.is_dir())))
    return out


def mount_artifact_routes(app: FastAPI, _require: Callable[[str], Session]) -> None:
    """挂载产物下载；会话查找沿用主应用的统一入口。"""
    @app.get("/api/sessions/{session_id}/artifacts/download")
    def session_artifact(session_id: str, path: str = Query(..., max_length=1000)) -> StreamingResponse:
        from spiritscout.web.deliverables import open_artifact

        try:
            file = open_artifact(_require(session_id).workspace, path)
        except (OSError, ValueError) as exc:
            # NUL 等非法路径字符抛 ValueError——按「不存在」404 处理而非 500
            raise HTTPException(404, "产出文件不存在或不在本会话产出目录中") from exc

        def chunks() -> Iterator[bytes]:
            with file:
                while chunk := file.read(65536):
                    yield chunk

        return StreamingResponse(chunks(), media_type="application/octet-stream", headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(Path(path).name)}"})


def mount_collaboration_routes(app: FastAPI, cfg: SystemConfig, manager: SessionManager,
                               _require: Callable[[str], Session]) -> None:
    """挂载附件暂存、会话消息、附件上传与文件分享。"""

    @app.post("/api/uploads")
    async def stage_upload(request: Request, name: str = Query(...)) -> dict[str, Any]:
        """任务下达前暂存附件（初始会话的附件上传）。

        请求体为文件原始字节，?name= 携带文件名。返回 upload_id，创建会话
        时随 upload_ids 一并材料化进会话工作区；未被带走的暂存 24h 后惰性回收。
        """
        clean = _clean_upload_name(name)
        body = await request.body()
        if not body:
            raise HTTPException(422, "附件内容为空")
        if len(body) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"附件过大（上限 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）")
        staging = Path(cfg.workspace.root) / "uploads-staging"
        staging.mkdir(parents=True, exist_ok=True)
        _gc_staging(staging)
        updir = staging / uuid.uuid4().hex[:12]
        updir.mkdir()
        dest = updir / clean
        if dest.exists():                      # 单目录内同名（理论不可达）不覆盖
            stem, suffix, n = dest.stem, dest.suffix, 2
            while dest.exists():
                dest = updir / f"{stem}-{n}{suffix}"
                n += 1
        dest.write_bytes(body)
        return {"upload_id": updir.name, "name": dest.name, "size": len(body)}

    @app.post("/api/uploads/folders")
    async def stage_folder(name: str = Query(...)) -> dict[str, Any]:
        clean = _clean_upload_name(name)
        staging = Path(cfg.workspace.root) / "uploads-staging"
        staging.mkdir(parents=True, exist_ok=True)
        _gc_staging(staging)
        updir = staging / uuid.uuid4().hex[:12]
        (updir / clean).mkdir(parents=True)
        return {"upload_id": updir.name}

    @app.put("/api/uploads/folders/{upload_id}")
    async def stage_folder_entry(upload_id: str, request: Request, path: str = Query(...),
                                 directory: bool = False) -> dict[str, Any]:
        if len(upload_id) != 12 or any(c not in "0123456789abcdef" for c in upload_id):
            raise HTTPException(422, "上传标识不合法")
        updir = Path(cfg.workspace.root) / "uploads-staging" / upload_id
        roots = list(updir.iterdir()) if updir.is_dir() else []
        if len(roots) != 1 or not roots[0].is_dir() or roots[0].is_symlink():
            raise HTTPException(422, "文件夹上传不存在或已过期")
        parts = path.split("/")
        if (not path or "\\" in path or any(p in ("", ".", "..") or len(p) > 200
                or any(ord(c) < 32 for c in p) for p in parts)):
            raise HTTPException(422, "文件夹相对路径不合法")
        root = roots[0].resolve()
        dest = root.joinpath(*parts)
        if not dest.resolve().is_relative_to(root):
            raise HTTPException(422, "文件夹相对路径不合法")
        body = await request.body()
        used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        if len(body) + used > _MAX_UPLOAD_BYTES:
            raise HTTPException(413, "单个文件夹总大小不能超过 50MB")
        if dest.exists():
            raise HTTPException(409, "文件夹条目已存在，请重新上传")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if directory:
            dest.mkdir()
        else:
            dest.write_bytes(body)
        return {"path": path, "size": len(body)}


    @app.post("/api/sessions/{session_id}/messages")
    async def post_message(session_id: str,
                           payload: dict[str, Any]) -> dict[str, Any]:
        """会话任意时刻给灵枢发消息：入会话收件箱 + 落对话史 + 审计留痕。

        灵枢在最近的决策点取走消费：派发期督导（~2s 节拍，化为对子 Agent
        的方向建议）或轮末复盘；消费动作有审计事件，意见不重不漏。
        会话已终态时消息即这一轮对话的输入（对话史末尾接着聊）——灵枢带着
        同一工作区、审计与 Flag 库和全部上下文继续，提问即回答，
        布置新方向即派工，与网页版大模型对话同一形态。
        """
        session = _require(session_id)
        try:
            manager.ensure_storage_idle(session_id)
        except SpiritScoutError as exc:
            raise HTTPException(409, str(exc)) from exc
        content = str(payload.get("content") or "").strip()
        if len(content) > 4000:
            raise HTTPException(422, "意见过长（≤4000 字）")
        message_id = payload.get("message_id") or f"message-{uuid.uuid4().hex}"
        if (not isinstance(message_id, str) or len(message_id) > 100
                or any(not char.isascii() or not (char.isalnum() or char in "-_") for char in message_id)):
            raise HTTPException(422, "消息标识不合法")
        upload_ids = payload.get("upload_ids") or []
        if (not isinstance(upload_ids, list) or not all(isinstance(uid, str) for uid in upload_ids)
                or len(set(upload_ids)) != len(upload_ids)):
            raise HTTPException(422, "上传标识必须是不重复的列表")
        existing = session.user_statement(message_id)
        if existing is not None:
            if existing.get("text") != content or existing.get("upload_ids", []) != upload_ids:
                raise HTTPException(409, "消息标识已用于另一条消息")
        else:
            staged = resolve_staged_uploads(cfg, payload)
            if not content and not staged:
                raise HTTPException(422, "消息及附件均为空")
            dest_dir = session.workspace / "attachments"
            records = materialize_uploads(staged, dest_dir) if staged else []
            session.remember_user_message(content, message_id=message_id, attachments=records,
                                          upload_ids=upload_ids, enqueue=True)
            session.opinions.append({"text": content, "ts": time.time()})
            del session.opinions[:-8]
            manager._persist(session)
            audit = AuditLogger(session.audit_path)
            if content:
                audit.log("user.message", "user", {"content": content, "message_id": message_id})
            for record in records:
                audit.log("user.attachment", "user", {**record, "message_id": message_id})
            for directory in {source.parent for source in staged}:
                shutil.rmtree(directory, ignore_errors=True)
        resumed = False
        pending = message_id not in session.handled_user_messages
        if pending and session.status in TERMINAL_STATES:
            try:
                await manager.resume(session_id)
                resumed = True
            except SpiritScoutError as exc:
                raise HTTPException(409, str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    503, f"续传未完成：{type(exc).__name__}: {exc}") from exc
        return {"queued": pending, "resumed": resumed, "message_id": message_id}

    @app.post("/api/sessions/{session_id}/attachments")
    async def upload_attachment(session_id: str, request: Request,
                                name: str = Query(...)) -> dict[str, Any]:
        """会话区上传附件（请求体为文件原始字节，?name= 携带文件名）。

        落到会话工作区 attachments/——file_analyze 的允许目录、沙箱工作区
        的子目录，两类工具通道都能直接读取；审计留痕后由灵枢在下一决策点
        知会各 Agent（督导建议里带文件路径）。终态会话也可上传：
        附件随下一次续传（发消息触发）进入决策环。
        """
        session = _require(session_id)
        clean = _clean_upload_name(name)
        body = await request.body()
        if not body:
            raise HTTPException(422, "附件内容为空")
        if len(body) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"附件过大（上限 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）")
        dest_dir = session.workspace / "attachments"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / clean
        if dest.exists():                      # 同名不覆盖：追加序号
            stem, suffix, n = dest.stem, dest.suffix, 2
            while dest.exists():
                dest = dest_dir / f"{stem}-{n}{suffix}"
                n += 1
        dest.write_bytes(body)
        message_id = f"message-{uuid.uuid4().hex}"
        record = {"name": dest.name, "size": len(body), "path": str(dest.resolve()),
                  "rel": f"attachments/{dest.name}"}
        session.remember_user_message("", message_id=message_id, attachments=[record], enqueue=True)
        manager._persist(session)
        AuditLogger(session.audit_path).log("user.attachment", "user", {**record, "message_id": message_id})
        return {**record, "message_id": message_id}

    @app.post("/api/sessions/{session_id}/files/share")
    async def share_session_file(session_id: str,
                                 payload: dict[str, Any]) -> dict[str, Any]:
        """把会话产出目录里的文件发进会话界面（文件卡片）。

        前端「发送到会话」触发：路径经 artifact_path 校验（产出/报告/附件
        目录内、拒绝穿越与符号链接），随后写一条 file.shared 审计事件，
        会话界面渲染为可预览/可下载的文件卡片。发件动作不改任务状态。
        """
        from spiritscout.web.deliverables import artifact_path

        session = _require(session_id)
        path = str(payload.get("path") or "").strip()[:1000]
        rel = artifact_path(session.workspace, path)
        if rel is None:
            raise HTTPException(404, "产出文件不存在或不在本会话产出目录中")
        note = str(payload.get("note") or "")[:200]
        stat = rel.stat()
        rel_posix = rel.relative_to(session.workspace).as_posix()
        AuditLogger(session.audit_path).log("file.shared", "master", {
            "name": rel.name, "path": rel_posix, "size": stat.st_size,
            "note": note, "by": "user"})
        return {"shared": True, "name": rel.name, "path": rel_posix,
                "size": stat.st_size}


def mount_report_routes(app: FastAPI, _require: Callable[[str], Session]) -> None:
    """挂载报告读取与报告、审计日志导出。"""
    @app.get("/api/sessions/{session_id}/report")
    async def session_report(session_id: str) -> dict[str, Any]:
        session = _require(session_id)
        report = session.report_path()
        if report is None:
            from spiritscout.ctf.report_jobs import status
            job = status(session.workspace)
            if job:
                return {"status": job.get("status"), "markdown": "", "error": job.get("error", "")}
            raise HTTPException(404, "报告尚未生成（任务完成后可用）")
        return {"path": str(report),
                "markdown": report.read_text(encoding="utf-8")}

    @app.get("/api/sessions/{session_id}/report/download")
    async def session_report_download(session_id: str) -> FileResponse:
        """报告文件下载（Web 功能补全）。"""
        report = _require(session_id).report_path()
        if report is None:
            raise HTTPException(404, "报告尚未生成（任务完成后可用）")
        return FileResponse(report, media_type="text/markdown",
                            filename=report.name)

    @app.get("/api/sessions/{session_id}/audit/download")
    async def session_audit_download(session_id: str) -> StreamingResponse:
        """审计日志 JSONL 导出（Web 功能补全）。"""
        audit = _require(session_id).audit_path
        if not audit.exists():
            raise HTTPException(404, "审计日志不存在（会话未产生事件）")
        from spiritscout.core.audit import sanitize_record

        def records() -> Iterator[str]:
            # 同步生成器由响应在线程池消费，不一次性物化整个历史文件。
            with audit.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        record = sanitize_record(json.loads(line))
                    except (ValueError, TypeError, AttributeError):
                        record = {"type": "audit.invalid", "detail": {"error": "日志行无效"}}
                    yield json.dumps(record, ensure_ascii=False) + "\n"

        return StreamingResponse(records(), media_type="application/x-ndjson",
                                 headers={"Content-Disposition":
                                          f'attachment; filename="{session_id}-audit.jsonl"'})
