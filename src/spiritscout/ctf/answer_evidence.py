"""正常单题的证据准入：核对来源与执行状态，答案含义仍由模型判断。"""
from __future__ import annotations

import json
from typing import Any

from spiritscout.blackboard.provenance import resolve_source
from spiritscout.core.models import Task

REVIEW_SCHEMA = {"type": "object", "properties": {
    **{key: {"type": "string", "description": text} for key, text in (
        ("requirements", "用户目标与原始题面的验收条件"),
        ("derivation", "证据如何推导出该答案，为什么不是中间结果"),
        ("validation", "实际验证方式、观察与局限；成功退出不等于答案正确"))},
    "validation_evidence_refs": {"type": "array", "items": {"type": "string"},
        "description": "支持上述实际验证的执行 ID 或有执行来源的黑板引用；不能捏造"}},
    "required": ["requirements", "derivation", "validation", "validation_evidence_refs"]}


def review_error(review: Any) -> str:
    if not isinstance(review, dict) or any(not isinstance(review.get(k), str) or not review[k].strip()
            for k in ("requirements", "derivation", "validation")):
        return "需要 review.requirements、derivation、validation，说明验收条件、推导与实际验证，不能只靠格式或工具退出成功自证。"
    refs = review.get('validation_evidence_refs')
    if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
        return "review.validation_evidence_refs 需引用支持实际验证的成功执行或有执行来源的黑板事实。"
    return ''


def check_evidence(bb: Any, journal: Any, task: Task, refs: list[str], *, environment_id: str = '') -> dict[str, Any]:
    """只接受当前会话、当前题可追溯到成功执行的引用，拒绝过期/循环/无来源记录。"""
    issues, executions = [], set()
    if bb is None or journal is None:
        return {'ok': False, 'issues': [{'reason': '执行证据台账不可用'}], 'executions': []}
    cid = str((task.payload.get('challenge') or {}).get('id') or '')
    active, checked = set(), set()
    with bb.transaction() as conn:
        def visit(ref: str) -> None:
            if ref in active:
                issues.append({'ref': ref, 'reason': '证据引用存在循环'})
                return
            if ref in checked:
                return
            active.add(ref)
            if '@' in ref and not ref.rsplit('@', 1)[1].isdigit():
                issues.append({'ref': ref, 'reason': '黑板版本必须是整数'})
                active.remove(ref)
                checked.add(ref)
                return
            source = resolve_source(conn, ref)
            if not source.get('resolved'):
                issues.append({'ref': ref, 'reason': '无法解析；文件请引用读取或产生它的执行 ID'})
            elif source['kind'] == 'execution':
                owner = source.get('task_id')
                same_challenge = owner == task.task_id
                if not same_challenge and cid and owner:
                    row = conn.execute("SELECT data FROM bb_records WHERE layer='task_state' AND record_id=?", (owner,)).fetchone()
                    other = (json.loads(row[0]).get('task') or {}) if row else {}
                    same_challenge = str(((other.get('payload') or {}).get('challenge') or {}).get('id') or '') == cid
                reason = ''
                if not same_challenge:
                    reason = '执行不属于当前题目，不能借用其他题的结果'
                elif environment_id and source.get('environment_id') and source['environment_id'] != environment_id:
                    reason = '执行环境已变化，需要核对当前环境的证据'
                elif source.get('execution_status') != 'succeeded':
                    reason = '执行失败、中断或结果缺失，不能作为完成依据'
                if reason:
                    issues.append({'ref': ref, 'reason': reason})
                else:
                    executions.add(source['invocation_id'])
            else:
                row = conn.execute('SELECT version,data FROM bb_records WHERE layer=? AND record_id=?',
                    (source['layer'], source['record_id'])).fetchone()
                if row is None or row[0] != source['version']:
                    issues.append({'ref': ref, 'reason': '来源版本已变化，需要重新读取'})
                else:
                    record = json.loads(row[1])
                    # 黑板写入时由运行时生成的来源快照能保留依赖版本；不能只跟随裸引用的最新版。
                    sources = (record.get('provenance') or {}).get('sources') or []
                    children = [f"{s['layer']}:{s['record_id']}@{s['version']}" if s.get('kind') == 'blackboard'
                                and s.get('resolved') else s.get('invocation_id') or s.get('ref')
                                for s in sources] if sources else record.get('evidence_refs') or []
                    if record.get('assessment') in {'expired', 'conflicting'}:
                        issues.append({'ref': ref, 'reason': '来源已过期或存在冲突'})
                    elif not isinstance(children, list) or not children:
                        issues.append({'ref': ref, 'reason': '记录没有可核对的执行来源'})
                    elif len(active) > 32:
                        issues.append({'ref': ref, 'reason': '引用链过长，请直接引用原始执行'})
                    else:
                        for child in children:
                            if isinstance(child, str) and child:
                                visit(child)
                            else:
                                issues.append({'ref': ref, 'reason': '引用格式无效'})
            active.remove(ref)
            checked.add(ref)

        for ref in dict.fromkeys(refs):
            visit(ref)
    # Journal 使用同一黑板锁，不能在 transaction 内递归获取它。
    for identifier in sorted(executions):
        result = journal.result(identifier)
        if result is None or not result.ok or result.returncode != 0 or result.timed_out:
            issues.append({'ref': identifier, 'reason': '实际执行结果缺失、失败或超时'})
    return {'ok': not issues and bool(executions), 'issues': issues, 'executions': sorted(executions)}
