"""单题 WriteUp 的读者材料与追溯附录；不改写真实命令、源码和输出。"""
from __future__ import annotations

import copy
from typing import Any

from spiritscout.ctf.report_evidence import technical_appendix


def reader_digest(digest: dict[str, Any]) -> dict[str, Any]:
    """编排元数据不作为正文材料；原始证据留存，使用短编号关联附录。"""
    data = copy.deepcopy(digest)
    if 'local_answer_completion' not in data:
        return data
    completions = data.get('local_answer_completion') or {}
    challenges = []
    for challenge in data.get('challenges', []):
        completion = completions.get(str(challenge.get('id') or ''), {})
        challenges.append({
            **{k: v for k, v in challenge.items() if k != 'id'},
            '答案记录状态': ('已在本地记录完成；需根据下方实际证据说明验证程度，无平台判定'
                           if completion.get('completed') is True else '尚未在本地确认完成，无平台判定'),
        })
    technical = data.get('technical_evidence') or {}
    for index, row in enumerate(technical.get('executions', []), 1):
        row['source_ref'] = f'E{index}'
        for key in ('invocation_id', 'task_id', 'route_id'):
            row.pop(key, None)
    for index, row in enumerate(technical.get('source_files', []), 1):
        row['source_ref'] = f'S{index}'
    return {
        'challenges': challenges,
        'answers': [{'value': f.get('value', ''), '平台判定': (
            '正确' if f.get('accepted') is True else '错误' if f.get('accepted') is False else '无判定')}
            for f in data.get('flags', [])],
        'working_notes': {
            '说明': '执行中的草稿与观察，仅作线索；不能当作已验证事实，也不要照抄系统状态或编排过程。',
            '观察': [n for task in data.get('routes_notes', []) for route in task.get('routes', [])
                     for n in route.get('notes', [])],
            '发现': [f.get('text', '') for f in data.get('findings', [])],
            '过程摘录': [r.get('text', '') for r in data.get('chronology', [])],
        },
        'technical_evidence': technical,
    }


def source_index(digest: dict[str, Any]) -> str:
    """由程序补齐追溯编号，写手无需在叙述中抄写内部 ID。"""
    evidence = digest.get('technical_evidence') or {}
    entries = []
    for index, row in enumerate(evidence.get('executions', []), 1):
        parts = [f"- **E{index}**：调用 `{row.get('invocation_id', '')}`"]
        for key, label in (('workdir', '原执行目录'), ('output_file', '原始日志')):
            if row.get(key):
                parts.append(f"{label} `{row[key]}`")
        entries.append('；'.join(parts))
    for index, row in enumerate(evidence.get('source_files', []), 1):
        entries.append(f"- **S{index}**：`{row.get('path', '')}`，从第 {row.get('start_line', 1)} 行开始"
                       + ('（节选）' if row.get('excerpt') else '（采集到文件末尾）'))
    return ('\n\n## 证据索引（供追溯）\n\n以下是原会话的定位信息，不是复现环境要求。\n\n'
            + '\n'.join(entries) + '\n') if entries else ''


def fallback_writeup(digest: dict[str, Any]) -> str:
    """未生成叙述时只提供可核对的材料，不把 Agent 草稿包装成完整解题过程。"""
    answers = '\n'.join('- `' + str(row.get('value', '')) + '`' for row in digest.get('flags', []))
    return ('# 解题记录（待整理）\n\n解题正文未生成。以下保留答案记录与实际技术证据，'
            '不据此推断答案已验证或平台已判对。\n\n## 答案记录\n\n'
            + (answers or '未记录答案。') + technical_appendix(digest))
