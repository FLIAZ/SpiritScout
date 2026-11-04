"""会话经验反馈和人工整理路由；手动提炼复用运行期提示词与持久队列。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool

from spiritscout.blackboard import Blackboard
from spiritscout.core.audit import AuditLogger
from spiritscout.knowledge import merge as experience_merge
from spiritscout.knowledge.experience import (
    ExperienceStore,
    _with_quality,
    experience_db_path,
)

_ACTIVE = {'created', 'queued', 'running', 'paused', 'stopping'}
_TERMINAL = {'completed', 'stopped', 'failed', 'interrupted'}
_TASKS: dict[str, asyncio.Task[Any]] = {}


def experience_library(cfg: Any) -> Path:
    return experience_db_path(cfg.experience.root, cfg.workspace.root)


def read_session_insights(db_path: str | Path, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    path = Path(db_path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='insights'").fetchone():
            return []
        quality = conn.execute("SELECT 1 FROM sqlite_master WHERE name='insight_quality'").fetchone()
        if quality:
            rows = conn.execute(
                'SELECT i.* FROM insights i LEFT JOIN insight_quality q USING(insight_id) '
                'WHERE i.source_session=? OR EXISTS (SELECT 1 FROM json_each(q.sources) '
                "WHERE json_extract(value,'$.source_session')=?) ORDER BY i.ts DESC LIMIT ?",
                (session_id, session_id, max(1, min(100, limit)))).fetchall()
        else:
            rows = conn.execute('SELECT * FROM insights WHERE source_session=? ORDER BY ts DESC LIMIT ?',
                                (session_id, max(1, min(100, limit)))).fetchall()
        return _with_quality(conn, rows)


def read_distill_events(audit_path: str | Path, *, limit: int = 8) -> list[dict[str, Any]]:
    try:
        with Path(audit_path).open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 131072))
            lines = stream.read().decode('utf-8', 'ignore').splitlines()
    except OSError:
        return []
    events = []
    for line in reversed(lines):
        if '"experience.distill"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get('type') != 'experience.distill':
            continue
        detail = record.get('detail')
        if isinstance(detail, dict):
            events.append({'ts': record.get('ts', ''), 'trigger': 'auto', **detail})
        if len(events) >= limit:
            break
    return events


def session_digest(session: Any) -> dict[str, Any]:
    """复用证据投影，停止不是完成；任务文字本身不算执行活动。"""
    from spiritscout.runtime import _distill_digest

    request = session.request if isinstance(session.request, dict) else {}
    mission = next((str(request[key]) for key in ('mission', 'text', 'task') if request.get(key)), '')
    authorization = SimpleNamespace(mode=SimpleNamespace(value=session.mode), src_platform='',
                                    target_description=session.target)
    assembly = SimpleNamespace()
    try:
        flags = json.loads(Path(session.flags_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        flags = []
    if isinstance(flags, list):
        stats = {'extracted': sum(isinstance(item, dict) for item in flags)}
        assembly.flag_manager = SimpleNamespace(stats=lambda: stats)
    bb = None
    try:
        if Path(session.bb_path).exists():
            bb = Blackboard(session.bb_path, readonly=True)
        summary = session.summary if isinstance(session.summary, dict) else {}
        run = summary.get('run', summary)
        return _distill_digest(SimpleNamespace(authorization=authorization, blackboard=bb), assembly,
                               run if isinstance(run, dict) else {},
                               mission, session.status, str(session.error or ''))
    finally:
        if bb is not None:
            bb.close()


def distill_blocker(session: Any, digest: dict[str, Any] | None = None) -> str:
    task = getattr(session, 'task', None)
    if session.status in _ACTIVE or (task is not None and not task.done()):
        return '会话还在执行，请等收尾或停止后再提炼'
    if session.status not in _TERMINAL:
        return '当前会话状态不支持提炼'
    data = digest if digest is not None else session_digest(session)
    if not (data.get('rounds') or data.get('tasks_recorded') or data.get('trials')
            or data.get('evidence_catalog') or (data.get('flags') or {}).get('extracted')
            or (data.get('findings') or {}).get('total')):
        return '这次会话没有可提炼的执行活动（任务文字不算活动）'
    return ''


def _busy(session: Any) -> bool:
    from spiritscout.runtime import pending_distill_snapshots

    task = _TASKS.get(str(session.workspace))
    return bool((task and not task.done()) or pending_distill_snapshots(session.workspace))


def session_page(cfg: Any, session: Any) -> dict[str, Any]:
    items = read_session_insights(experience_library(cfg), session.session_id)
    busy = _busy(session)
    blocker = distill_blocker(session)
    return {'session_id': session.session_id, 'items': items, 'total': len(items),
            'history': read_distill_events(session.audit_path),
            'state': {'status': session.status, 'distilling': busy,
                      'can_distill': not blocker and not busy,
                      'reason': blocker or ('正在提炼或等待后台恢复' if busy else '')}}


async def _manual_worker(cfg: Any, manager: Any, session: Any, digest: dict[str, Any], snapshot: Path) -> None:
    from spiritscout.config import load_model_config
    from spiritscout.runtime import _background_distill, _update_distill_snapshot
    from spiritscout.web import agentcfg, vault
    from spiritscout.web.sessions import _conversation_gateway

    audit = AuditLogger(session.audit_path)
    store = None
    handed_off = False
    try:
        models = vault.apply_keys(load_model_config(cfg.models_config_path))
        models = [model for model in models if model.api_key or model.provider == 'ollama']
        if not models:
            raise ValueError('没有可用的模型渠道，请配置供应商密钥或本地 Ollama')
        scoped = manager._session_cfg(session, None)
        gateway = _conversation_gateway(scoped, models, audit)
        if hasattr(gateway, 'set_session_model'):
            gateway.set_session_model(lambda: getattr(session, 'model', ''))
        store = ExperienceStore(experience_library(cfg), audit=audit)
        infra = SimpleNamespace(cfg=scoped, workspace=session.workspace, gateway=gateway, audit=audit,
                                agent_models=agentcfg.read_assignments(scoped.agent_models_path),
                                distill_guard=lambda: ('会话已移除' if manager.get(session.session_id) is not session
                                                      else distill_blocker(session, digest)))
        handed_off = True
        await _background_distill(infra, store, digest, outcome=session.status, snapshot=snapshot)
    except Exception as exc:  # noqa: BLE001 —— 后台配置错误也必须留下真实结果
        result = {'result': 'error', 'insights': 0, 'trigger': 'manual',
                  'error': f'{type(exc).__name__}: {exc}'[:200]}
        audit.log('experience.distill', 'master', result)
        _update_distill_snapshot(snapshot, status='failed', attempts=1, result=result)
    finally:
        if store is not None and not handed_off:
            store.close()


async def request_distill(cfg: Any, manager: Any, session: Any) -> dict[str, Any]:
    from spiritscout.runtime import _persist_distill_snapshot

    if _busy(session):
        raise HTTPException(409, '正在提炼或等待后台恢复，请勿重复请求')
    # 本函数在持久化和登记 task 之间不 await，重复 HTTP 请求不会同时入队。
    digest = session_digest(session)
    reason = distill_blocker(session, digest)
    if reason:
        raise HTTPException(409, reason)
    audit = AuditLogger(session.audit_path)
    snapshot = _persist_distill_snapshot(SimpleNamespace(workspace=session.workspace, audit=audit),
                                         digest, outcome=session.status, trigger='manual')
    if snapshot is None:
        raise HTTPException(503, '提炼任务无法持久化，未调用模型')
    task = asyncio.create_task(_manual_worker(cfg, manager, session, digest, snapshot))
    key = str(session.workspace)
    _TASKS[key] = task
    task.add_done_callback(lambda finished: _TASKS.pop(key, None) if _TASKS.get(key) is finished else None)
    return {'result': 'queued', 'session_id': session.session_id, 'job_id': snapshot.stem,
            'insights': 0, 'trigger': 'manual'}


async def close_manual_distills(manager: Any) -> None:
    root = Path(manager.root).resolve()
    tasks = [task for path, task in list(_TASKS.items())
             if Path(path).resolve().is_relative_to(root) and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class PreviewRequest(BaseModel):
    insight_ids: list[str] = Field(min_length=2, max_length=6)


class MergeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    confirmed: StrictBool = False


class RestoreRequest(BaseModel):
    merged_id: str = Field(min_length=1, max_length=200)


def mount_experience_routes(app: Any, cfg: Any, manager: Any) -> None:
    """只负责新路由；已有经验库 GET/DELETE 保持由 app.py 托管。"""
    def session_by_id(session_id: str) -> Any:
        session = manager.get(session_id)
        if session is None:
            raise HTTPException(404, '会话不存在')
        return session

    def operation(function: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return function(experience_library(cfg), *args, **kwargs)
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except sqlite3.Error as exc:
            raise HTTPException(503, '经验库暂不可用，请稍后重试') from exc

    @app.get('/api/sessions/{session_id}/experience')
    def get_session_experience(session_id: str) -> dict[str, Any]:
        try:
            return session_page(cfg, session_by_id(session_id))
        except sqlite3.Error as exc:
            raise HTTPException(503, '经验库暂不可用') from exc

    @app.post('/api/sessions/{session_id}/experience/distill', status_code=202)
    async def distill_session(session_id: str) -> JSONResponse:
        return JSONResponse(await request_distill(cfg, manager, session_by_id(session_id)), status_code=202)

    @app.get('/api/experience/merge/candidates')
    def get_candidates(threshold: float = experience_merge.DEFAULT_THRESHOLD) -> dict[str, Any]:
        return operation(experience_merge.candidates, threshold=threshold)

    @app.post('/api/experience/merge/preview')
    def preview_merge(body: PreviewRequest) -> dict[str, Any]:
        return operation(experience_merge.preview, body.insight_ids)

    @app.post('/api/experience/merge')
    def confirm_merge(body: MergeRequest) -> dict[str, Any]:
        result = operation(experience_merge.merge, body.token, confirmed=body.confirmed)
        AuditLogger(cfg.audit_log_path).log('experience.merged', 'user', result)
        return result

    @app.post('/api/experience/merge/restore')
    def restore_merge(body: RestoreRequest) -> dict[str, Any]:
        result = operation(experience_merge.restore, body.merged_id)
        AuditLogger(cfg.audit_log_path).log('experience.restored', 'user', result)
        return result
