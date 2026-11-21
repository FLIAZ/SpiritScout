"""子任务报告的无损文件化；不调用模型，不改变结果与完成判据。"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

from spiritscout.core.models import BlackboardLayer, RouteState, Task


def save_task_report(workspace: Path, task: Task, routes: list[RouteState],
                     summary: dict[str, Any], *, actor: str, historical: bool = False) -> dict[str, Any]:
    title = str(task.payload.get('node_title') or task.task_type)
    parts = [f'# {title}', '## 任务信息',
             f'- 任务：`{task.task_id}`\n- 执行者：`{actor}`\n'
             f'- 目标：`{task.target}`\n- 状态：`{task.status.value}`']
    if historical:
        parts.extend(['## 历史补齐说明', '此报告根据当时保存的任务、路线摘要与交接结果补齐；未保存的完整输出无法恢复。'])
    if summary.get('incomplete_reason'):
        parts.extend(['## 未完成原因', str(summary['incomplete_reason'])])
    for route in routes:
        parts.extend([f'## {route.name}', f'状态：`{route.status.value}`'])
        # 每条笔记独立保留 Markdown 边界，不能截断或用分号拼接代码块/表格。
        parts.extend(str(note) for note in (route.report_notes or route.notes))
    handoff = summary.get('handoff') if isinstance(summary.get('handoff'), dict) else {}
    parts.extend(['## 交接摘要'])
    for heading, key in (
            ('已确认事实', 'established_facts'),
            ('已解决未知', 'resolved_unknowns'),
            ('剩余未知', 'remaining_unknowns'),
            ('阻塞点', 'blockers'),
            ('下一步最小实验', 'next_minimal_experiment'),
            ('停止条件', 'stop_condition')):
        value = handoff.get(key, summary.get(key, ''))
        if isinstance(value, list):
            value = '\n'.join(f'- {item}' for item in value) or '- 无'
        else:
            value = str(value or '').strip() or '无'
        parts.extend([f'### {heading}', value])
    evidence = handoff.get('evidence_refs') or []
    parts.extend(['### 证据引用', '\n'.join(f'- `{ref}`' for ref in evidence) or '- 无'])
    content = '\n\n'.join(parts) + '\n'
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    identifier = hashlib.sha256(task.task_id.encode('utf-8')).hexdigest()[:12]
    root = workspace / 'reports' / 'tasks'
    if root.is_symlink() or root.parent.is_symlink():
        raise OSError('任务报告目录不能是符号链接')
    root.mkdir(parents=True, exist_ok=True)
    name = f'task-{identifier}-{digest[:16]}.md'
    dest = root / name
    if dest.is_symlink():
        raise OSError('任务报告文件不能是符号链接')
    if not dest.exists() or dest.read_text(encoding='utf-8') != content:
        fd, temporary = tempfile.mkstemp(prefix='.report-', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                output.write(content)
            os.replace(temporary, dest)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {'path': dest.relative_to(workspace).as_posix(), 'name': name,
            'size': len(content.encode('utf-8')), 'sha256': digest,
            'task_id': task.task_id, 'title': title, 'actor': actor,
            'status': task.status.value}


def index_task_report(blackboard: Any, report: dict[str, Any], task: Task,
                      summary: dict[str, Any], *, actor: str) -> None:
    """图内只存引用与来源；完整正文只存在文件，避免重复进入模型上下文。"""
    key = 'task-report:' + report['sha256']
    data = {'kind': 'task_report', 'type': 'task_report', 'node_type': 'report',
            'title': report['title'], 'task_id': task.task_id,
            'parent_dag_node': task.parent_dag_node, 'report': report,
            'status': task.status.value,
            'evidence_refs': (summary.get('handoff') or {}).get('evidence_refs', []),
            'provenance': {'producer': {'task_id': task.task_id, 'agent_id': actor}}}
    if blackboard.read(BlackboardLayer.TASK_REPORT, key) != data:
        blackboard.write(BlackboardLayer.TASK_REPORT, key, data,
                         agent_type=task.agent_type, updated_by=actor, dedup_key=key)


def recover_task_reports(workspace: Path) -> int:
    """显式补齐旧会话报告；不重新执行任务、不把旧结论升级为已验证事实。"""
    from spiritscout.blackboard import Blackboard

    if not (workspace / 'bb.db').is_file():
        return 0
    bb = Blackboard(workspace / 'bb.db')
    count = 0
    try:
        existing_tasks = {row['data']['task_id'] for row in bb.query(BlackboardLayer.TASK_REPORT, limit=10000)}
        for row in bb.query(BlackboardLayer.TASK_STATE, limit=10000):
            data = row['data']
            raw = data.get('task')
            if not isinstance(raw, dict) or raw.get('status') not in {
                    'done', 'failed', 'pruned', 'cancelled'}:
                continue
            task = Task.model_validate(raw)
            summary = data.get('summary') or task.result_summary or {}
            actor = str(summary.get('agent_id') or row['updated_by'])
            if task.task_id in existing_tasks:
                continue
            report = save_task_report(workspace, task,
                [RouteState.model_validate(route) for route in data.get('routes', [])],
                summary, actor=actor, historical=True)
            index_task_report(bb, report, task, summary, actor=actor)
            count += 1
    finally:
        bb.close()
    return count


def list_task_reports(workspace: Path) -> list[dict[str, Any]]:
    from spiritscout.blackboard import Blackboard

    if not (workspace / 'bb.db').is_file():
        return []
    bb = Blackboard(workspace / 'bb.db', readonly=True)
    try:
        return [{**row['data']['report'], 'updated_at': row['updated_at']}
                for row in bb.query(BlackboardLayer.TASK_REPORT, limit=10000,
                                    data_filter=('type', 'task_report'))]
    finally:
        bb.close()
