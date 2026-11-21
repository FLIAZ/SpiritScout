"""会话经验提炼、持久快照及前后台协调；统一持有调度状态。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spiritscout.config import SystemConfig
from spiritscout.config.settings import ModelEntry
from spiritscout.core.audit import AuditLogger
from spiritscout.core.background import _background_slot
from spiritscout.core.ids import new_id
from spiritscout.core.models import BlackboardLayer
from spiritscout.gateway import AgentBoundGateway, ModelGateway, ModelRegistry
from spiritscout.knowledge.experience import ExperienceStore, experience_db_path

if TYPE_CHECKING:
    from spiritscout.runtime import _Infra


def _distill_digest(infra: _Infra, assembly: Any,
                    run_summary: dict[str, Any] | None,
                    mission: str, outcome: str, error: str = "") -> dict[str, Any]:
    """收尾提炼的战报：把成/败/坑的可复用信号压成小块 JSON（不含步骤序列）。"""
    summary = run_summary or {}
    digest: dict[str, Any] = {
        "mode": infra.authorization.mode.value,
        "platform": infra.authorization.src_platform or "",
        "target": str(summary.get("target")
                      or infra.authorization.target_description or "")[:200],
        "mission": str(mission or "")[:200],
        "outcome": outcome,
        "rounds": summary.get("rounds_executed", 0),
        "stop_reason": str(summary.get("stop_reason") or "")[:160],
    }
    if error:
        digest["error"] = error[:300]
    flag_manager = getattr(assembly, "flag_manager", None)
    if flag_manager is not None:
        try:
            digest["flags"] = flag_manager.stats()
        except Exception:  # noqa: BLE001 —— 统计失败不阻断提炼
            pass
    dag = summary.get("dag_nodes") or []
    if dag:
        digest["plan"] = [{"title": str(node.get("title", ""))[:80],
                           "status": str(node.get("status", ""))}
                          for node in dag[:10]]
    try:
        bb = infra.blackboard
        findings: dict[str, Any] = {"total": bb.count(BlackboardLayer.VULN),
                                    "by_status": bb.status_counts(BlackboardLayer.VULN)}
        accepted = []
        for rec in bb.query(BlackboardLayer.VULN, limit=200):
            data = rec["data"]
            if str(data.get("status")) == "accepted" and data.get("title"):
                accepted.append({"title": str(data["title"])[:100],
                                 "severity": str(data.get("severity", "")),
                                 "type": str(data.get("vuln_type", ""))})
        if accepted:
            findings["accepted"] = accepted[:8]
        digest["findings"] = findings
        trials: list[dict[str, str]] = []
        for rec in bb.query(BlackboardLayer.TASK_STATE, limit=100):
            data = rec["data"]
            if "dag_snapshot" in data:
                continue
            task = data.get("task", {})
            for route in data.get("routes", []):
                if str(route.get("status")) in {"pruned", "failed"}:
                    note = (route.get("notes") or [""])[-1]
                    trials.append({"agent": str(task.get("agent_type", "")),
                                   "route": str(route.get("name", ""))[:60],
                                   "status": str(route.get("status")),
                                   "note": str(note)[:140]})
        if trials:
            digest["trials"] = trials[:8]
        digest["tasks_recorded"] = bb.count(BlackboardLayer.TASK_STATE)
        # 只提供事实摘要与版本化引用，不把凭据、Flag、工具参数或原始响应搬入经验。
        sources = []
        for layer in (BlackboardLayer.VULN, BlackboardLayer.TOPOLOGY, BlackboardLayer.RECON_CLUE):
            for rec in bb.query(layer, limit=80):
                data = rec['data']
                if layer == BlackboardLayer.TOPOLOGY and not (data.get('kind') == 'finding' and data.get('type') == 'fact'):
                    continue
                if layer == BlackboardLayer.RECON_CLUE and data.get('clue_type') == 'user_reply':
                    continue
                text = data.get('title') or data.get('text')
                if not isinstance(text, str) or not text.strip():
                    continue
                sources.append({'ref': f'{layer.value}:{rec["record_id"]}@{rec["version"]}',
                                'summary': text[:240], 'truncated': len(text) > 240,
                                'evidence_state': data.get('evidence_state', 'unverified')})
                if len(sources) >= 12:
                    break
            if len(sources) >= 12:
                break
        if sources:
            digest['evidence_catalog'] = sources
    except Exception:  # noqa: BLE001 —— 黑板读取异常时保留最小战报
        pass
    return digest


def _worth_distilling(digest: dict[str, Any]) -> bool:
    """正常收尾总提炼（提示词自行决定是否空数组）；中止/失败需确实跑过东西。"""
    if digest.get("outcome") == "completed":
        return True
    flags = digest.get("flags") or {}
    findings = digest.get("findings") or {}
    return bool(digest.get("rounds") or digest.get("tasks_recorded")
                or flags.get("extracted") or findings.get("total"))


async def _distill_experience(gateway: Any, experience: ExperienceStore,
                              digest: dict[str, Any], *,
                              timeout_seconds: int,
                              audit: Any = None,
                              source_session: str = "") -> dict[str, Any]:
    """提炼一次会话经验并返回摘要，由调用方记录审计。

    使用 chat_decision 处理思考型模型正文为空时的重试。"""
    import json as _json

    from spiritscout.gateway.backends import extract_json
    from spiritscout.gateway.context import compact_json
    from spiritscout.gateway.gateway import chat_decision

    # 保持原来的系统提示及 8000 字符观察预算；投影后再校验引用，不能引用被裁掉的来源。
    payload = {**digest, 'constraints': (
        '每条经验提供 source_refs 数组（最多3条，无依据时为空），仅引用 evidence_catalog 中支持该陈述的 ref；'
        '没有适用依据时不补造引用。摘要不是完整原文，引用也不代表经验已独立验证。')}
    user_content = compact_json(payload, 8000)
    visible = _json.loads(user_content)
    original_catalog = {item['ref']: item for item in digest.get('evidence_catalog', [])
                        if isinstance(item, dict) and isinstance(item.get('ref'), str)}
    catalog = {item['ref']: original_catalog[item['ref']] for item in visible.get('evidence_catalog', [])
               if isinstance(item, dict) and isinstance(item.get('ref'), str) and item['ref'] in original_catalog}

    resp = await asyncio.wait_for(chat_decision(
        gateway,
        [{"role": "system", "content": (
            "你是灵枢。一场安全测试刚结束（正常收尾、被中断或失败均可能）。"
            "从以下战报中提炼 3~5 条可复用的实战洞察——不是解题步骤，"
            "而是「这类场景下我学到了什么」：目标/平台特性、手法适用条件、"
            "容易踩的坑、判读信号、中断或失败的教训。"
            "纪律：①洞察必须脱离本题仍然成立——单题发现要抽象成同类场景"
            "的信号与事实，抽象不出来的（具体路径、参数、口令、端口、命令、"
            "flag 原文）一律不写；②不写操作序列与处方（「先…再…」「应优先…」"
            "「直接…即可」），只写陈述句：什么条件下、能看到什么信号、"
            "为什么值得注意。反例（禁止写成这样）：「本题入口位于 /xxx 路径，"
            "应优先探测登录/日志等端点」——单题坐标加步骤处方。"
            "没有值得提炼的就返回空数组。"
            "每条一句话独立可复用；context 写适用场景（目标类型/业务/"
            "技术栈），tags 写 2~4 个检索标签（如 web、登录、越权、ctf、java）。"
            '返回 JSON：{"insights": [{"insight": str, "context": str, '
            '"tags": [str]}]}')},
         {"role": "user", "content": user_content}],
        task_type="meta_planning", response_format="json",
        # 起步额度给足：思考型模型首调常把额度耗在思考上（正文 0 字符
        # 作废重试白烧一次调用）——实测 glm-5.3 提炼可吃满 2048 额度，
        # 8192 起步一次成功，比失败后重试省一整轮
        max_tokens=8192, reasoning_effort="low",
        timeout_seconds=timeout_seconds,
        audit=audit, actor="master"),
        timeout=timeout_seconds + 40)
    try:
        data = extract_json(resp.content)
    except (ValueError, TypeError):
        return {"result": "unparsed", "insights": 0,
                "error": "模型输出不是可解析的经验 JSON"}
    items = data.get("insights") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {"result": "unparsed", "insights": 0,
                "error": "经验 JSON 缺少 insights 数组"}
    if not items:
        return {"result": "empty", "insights": 0}
    added = duplicates = parsed = 0
    for item in items[:5]:
        if isinstance(item, dict) and isinstance(item.get("insight"), str) and item["insight"].strip():
            parsed += 1
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            requested = item.get('source_refs') if isinstance(item.get('source_refs'), list) else []
            sources = [{**catalog[ref], 'source_session': source_session} for ref in dict.fromkeys(
                ref for ref in requested if isinstance(ref, str)) if ref in catalog][:3]
            if experience.add(str(item["insight"]),
                              context=str(item.get("context", "")),
                              tags=[str(tag) for tag in tags],
                              source_session=source_session,
                              source_model="master", sources=sources):
                added += 1
            else:
                duplicates += 1
    if not parsed:
        return {"result": "unparsed", "insights": 0,
                "error": "经验数组没有有效洞察"}
    return {"result": "ok" if added else "duplicate", "insights": added,
            "duplicates": duplicates, "parsed": parsed}


def _prepare_distill(infra: _Infra, assembly: Any, enabled: bool, experience: Any,
                     run_summary: dict[str, Any] | None, *,
                     mission: str, outcome: str,
                     error: str = "") -> dict[str, Any] | None:
    """提炼前判定：战报同步取好（读黑板/统计），不值得提炼则留痕返回 None。

    同步完成是硬要求：退场路径必须在 infra 回收前拿到快照，后台任务才能
    脱离黑板/知识库等已回收资源独立跑完。
    """
    if not enabled or experience is None or not getattr(getattr(infra.cfg, "experience", None), "auto_distill", True):
        return None
    digest = _distill_digest(infra, assembly, run_summary, mission, outcome, error)
    if not _worth_distilling(digest):
        infra.audit.log("experience.distill", "master", {
            "result": "skipped", "outcome": outcome,
            "reason": "没有值得沉淀的活动"})
        return None
    return digest


async def _distill_run(infra: _Infra, experience: Any, digest: dict[str, Any], *,
                       outcome: str, trigger: str = "auto") -> dict[str, Any]:
    """一次提炼执行 + 审计留痕（完成态原地等待与退场后台任务共用）。"""
    started = time.monotonic()
    # 来源记真实会话 ID（Web 会话工作区名即 sess-xxx，经验页据此回链）
    workspace_root = getattr(
        getattr(getattr(infra, "cfg", None), "workspace", None), "root", "")
    try:
        result = await _distill_experience(
            AgentBoundGateway(infra.gateway, infra.agent_models.get("master", ""), prefer_session=True),
            experience, digest,
            timeout_seconds=infra.cfg.experience.distill_timeout_seconds,
            audit=infra.audit,
            source_session=Path(str(workspace_root)).name if workspace_root else "")
    except Exception as exc:  # noqa: BLE001 —— 不吞 CancelledError（停止语义优先）
        result = {"result": "error", "insights": 0,
                  "error": f"{type(exc).__name__}: {exc}"[:200]}
    infra.audit.log("experience.distill", "master", {
        **result, "outcome": outcome, "trigger": trigger,
        "duration_ms": round((time.monotonic() - started) * 1000, 1)})
    return result


async def _distill_best_effort(infra: _Infra, assembly: Any, experience: Any,
                               enabled: bool,
                               run_summary: dict[str, Any] | None,
                               *, mission: str, outcome: str,
                               error: str = "") -> None:
    """需前台完成的终态提炼；普通 CTF 与智能体模式使用持久后台队列。"""
    digest = _prepare_distill(infra, assembly, enabled, experience, run_summary,
                              mission=mission, outcome=outcome, error=error)
    if digest is None:
        return
    await _distill_run(infra, experience, digest, outcome=outcome)


_BACKGROUND_DISTILLS: set[asyncio.Task[Any]] = set()


def _distill_queue_dir(workspace: Path) -> Path:
    return workspace / "experience_distill"


def _write_distill_snapshot(path: Path, payload: dict[str, Any]) -> None:
    """原子写任务快照；后台任务与重启恢复共读同一格式。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def _update_distill_snapshot(path: Path | None, **updates: Any) -> None:
    if path is None:
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(updates)
        _write_distill_snapshot(path, payload)
    except (OSError, ValueError, TypeError):
        pass


def _persist_distill_snapshot(
        infra: _Infra, digest: dict[str, Any], *, outcome: str,
        trigger: str = "auto") -> Path | None:
    workspace = getattr(infra, "workspace", None)
    if not isinstance(workspace, Path):
        return None
    try:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = _distill_queue_dir(workspace) / f"{stamp}-{new_id('distill')}.json"
        _write_distill_snapshot(path, {
            "schema": 1, "status": "pending", "attempts": 0,
            "outcome": outcome, "digest": digest, "trigger": trigger,
            "session": workspace.name,
            "created_at": datetime.now(UTC).isoformat(),
        })
        return path
    except OSError as exc:
        infra.audit.log("experience.distill", "master", {
            "result": "queue_persist_failed", "outcome": outcome,
            "error": f"{type(exc).__name__}: {exc}"[:200]})
        return None


def pending_distill_snapshots(workspace: Path) -> list[Path]:
    """待恢复的提炼快照；已完成或重试耗尽的任务不再入队。"""
    queue = _distill_queue_dir(workspace)
    out: list[Path] = []
    for path in sorted(queue.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        status = str(payload.get("status") or "pending")
        try:
            attempts = int(payload.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if status != "done" and attempts < 3:
            out.append(path)
    return out


def _schedule_background_distill(infra: _Infra, assembly: Any, experience: Any,
                                 enabled: bool,
                                 run_summary: dict[str, Any] | None,
                                 *, mission: str, outcome: str,
                                 error: str = "") -> asyncio.Task[Any] | None:
    """赛后提炼转后台：用户不再等待，前台任务始终优先。

    战报在 infra 回收前同步取好；经验库托付后台任务收尾（close 跳过它），
    其余资源照常即刻回收。快照先持久化，进程中止后可由会话管理器恢复；
    后台槽位串行且前台活跃时等待，避免与当前或下一场比赛争抢模型额度。
    """
    digest = _prepare_distill(infra, assembly, enabled, experience, run_summary,
                              mission=mission, outcome=outcome, error=error)
    if digest is None:
        return None
    snapshot = _persist_distill_snapshot(infra, digest, outcome=outcome)
    infra.experience_deferred = True
    task = asyncio.create_task(_background_distill(infra, experience, digest,
                                                   outcome=outcome,
                                                   snapshot=snapshot))
    _BACKGROUND_DISTILLS.add(task)
    task.add_done_callback(_BACKGROUND_DISTILLS.discard)
    return task


async def _background_distill(infra: _Infra, experience: Any,
                              digest: dict[str, Any], *, outcome: str,
                              snapshot: Path | None = None) -> None:
    """后台提炼收尾：执行提炼并关闭托付的经验库（连接不泄漏）。

    任何失败只吞掉不冒泡——后台任务无人接管异常，会话终态早已落定。
    会话可能在后台排队等待期间被删除（工作区连同快照一并移除），此时
    审计与快照写入都会失败：留痕无门，静默收场即可。
    """
    try:
        async with _background_slot():
            prior = 0
            trigger = "auto"
            if snapshot is not None:
                try:
                    payload = json.loads(snapshot.read_text(encoding="utf-8"))
                    prior = int(payload.get("attempts") or 0)
                    trigger = str(payload.get("trigger") or "auto")
                except (OSError, ValueError, TypeError):
                    prior = 0
                _update_distill_snapshot(
                    snapshot, status="running", attempts=prior + 1,
                    started_at=datetime.now(UTC).isoformat())
            guard = getattr(infra, "distill_guard", None)
            reason = guard() if guard is not None else ""
            if reason:
                result = {"result": "skipped", "insights": 0, "reason": reason}
                infra.audit.log("experience.distill", "master", {
                    **result, "outcome": outcome, "trigger": trigger})
            else:
                result = await _distill_run(
                    infra, experience, digest, outcome=outcome, trigger=trigger)
            failed = result.get("result") == "error"
            _update_distill_snapshot(
                snapshot, status="failed" if failed else "done",
                completed_at=datetime.now(UTC).isoformat(), result=result)
    except Exception:  # noqa: BLE001 —— 不吞 CancelledError（停止语义优先）
        with contextlib.suppress(Exception):
            _update_distill_snapshot(snapshot, status="failed",
                                     completed_at=datetime.now(UTC).isoformat())
    finally:
        with contextlib.suppress(Exception):
            experience.close()


async def resume_pending_distills(
    *,
    workspace: Path,
    cfg: SystemConfig,
    models: list[ModelEntry],
    agent_models: dict[str, str] | None = None,
) -> int:
    """恢复一个会话目录中的待提炼快照；供 Web 会话管理器启动时调用。"""
    snapshots = pending_distill_snapshots(workspace)
    if not snapshots or not models:
        return 0
    audit = AuditLogger(cfg.audit_log_path)
    gateway = ModelGateway(ModelRegistry(models), config=cfg.gateway, audit=audit)
    from spiritscout.gateway.usage import bind_usage

    bind_usage(gateway, workspace / "usage.json")
    store = ExperienceStore(
        experience_db_path(cfg.experience.root, str(workspace)), audit=audit)
    completed = 0
    try:
        for snapshot in snapshots:
            try:
                payload = json.loads(snapshot.read_text(encoding="utf-8"))
                digest = payload.get("digest") or {}
                attempts = int(payload.get("attempts") or 0)
            except (OSError, ValueError, TypeError):
                continue
            async with _background_slot():
                _update_distill_snapshot(
                    snapshot, status="running", attempts=attempts + 1,
                    started_at=datetime.now(UTC).isoformat())
                try:
                    result = await _distill_experience(
                        AgentBoundGateway(
                            gateway, str((agent_models or {}).get("master", ""))),
                        store, digest,
                        timeout_seconds=cfg.experience.distill_timeout_seconds,
                        audit=audit,
                        source_session=str(payload.get("session") or workspace.name))
                    failed = result.get("result") == "error"
                except Exception as exc:  # noqa: BLE001 —— 单条失败保留快照重试
                    result = {"result": "error", "insights": 0,
                              "error": f"{type(exc).__name__}: {exc}"[:200]}
                    failed = True
                audit.log("experience.distill", "master", {
                    **result, "outcome": str(payload.get("outcome") or ""),
                    "trigger": str(payload.get("trigger") or "auto"), "recovered": True})
                _update_distill_snapshot(
                    snapshot, status="failed" if failed else "done",
                    completed_at=datetime.now(UTC).isoformat(), result=result)
                if not failed:
                    completed += 1
    finally:
        store.close()
    return completed
