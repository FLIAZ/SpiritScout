"""CTF 报告技术材料：读取本会话执行证据与源码，生成带来源的代码附录。"""
from __future__ import annotations

import ast
import json
import os
import re
import stat
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from spiritscout.tools.sandbox_paths import open_directory

_FILE_REFS = re.compile(r"[^\s\"'`<>;|(){}=]+\.(?:py|c|h|cpp|js|ts|php|java|rs|go|asm|sh|md)\b")

_SOURCE_EXTENSIONS = {'.py', '.c', '.h', '.cpp', '.js', '.ts', '.php', '.java', '.rs', '.go', '.asm', '.sh'}


def collect_evidence(journal: Any = None, workspace: Path | None = None,
                     task_ids: set[str] | None = None, *,
                     evidence_refs: set[str] | None = None,
                     challenge_text: str = '') -> dict[str, Any]:
    executions = []
    challenge_refs = set(_FILE_REFS.findall(challenge_text))
    important_refs: set[str] = set()
    references: set[str] = set()
    budget = 10000
    if journal is not None:
        rows = journal.list_recent(limit=100)
        # 答案引用的验证调用优先，避免较晚的诊断输出占满材料预算。
        rows.sort(key=lambda row: row["invocation_id"] not in (evidence_refs or set()))
        for row in rows:
            if task_ids and row.get('task_id') not in task_ids:
                continue
            result = journal.result(row['invocation_id'])
            if result is None:
                continue
            parameters = journal.parameters(row['invocation_id']) or {}
            refs = set(_FILE_REFS.findall(json.dumps(parameters, ensure_ascii=False)))
            references.update(refs)
            if row['invocation_id'] in (evidence_refs or set()):
                important_refs.update(refs)
            item = {key: row.get(key) for key in ('invocation_id', 'task_id', 'route_id', 'tool_name', 'execution_status')}
            encoded = json.dumps(parameters, ensure_ascii=False)
            item.update(parameters=parameters if len(encoded) <= 6000 else {},
                parameters_omitted=len(encoded) > 6000, ok=result.ok, returncode=result.returncode,
                stdout=result.stdout[:3000], stderr=result.stderr[:1000], error=result.error[:500],
                output_excerpt=True,  # 工具本身也可能已经截窗，统一作为实际输出节选引用。
                output_file=result.parsed.get('output_file', ''), workdir=result.parsed.get('workdir', ''))
            size = len(json.dumps(item, ensure_ascii=False))
            if size > budget:
                continue
            budget -= size
            executions.append(item)
    sources, notes = [], []
    file_budgets = {'source': 12000, 'note': 8000}
    if workspace is not None:
        workspace = Path(workspace).absolute()
        candidates = []
        directories = deque(workspace / base for base in ('attachments', 'artifacts', 'sandbox'))
        visited = 0
        # 按目录层级采集：庞大的依赖源码树不能让并列的题目目录一直轮不到。
        while directories and visited < 2000:
            directory = directories.popleft()
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            visited += 1
            for path in entries:
                if path.is_symlink():
                    continue
                if path.is_dir():
                    if path.name not in {'.git', '.venv', 'node_modules', '__pycache__'}:
                        directories.append(path)
                elif path.suffix.lower() in _SOURCE_EXTENSIONS | {'.md'}:
                    candidates.append(path.relative_to(workspace))
                visited += 1
                if visited >= 2000:
                    break

        shallowest: dict[str, int] = {}
        for relative in candidates:
            shallowest[relative.name] = min(shallowest.get(relative.name, len(relative.parts)), len(relative.parts))

        def priority(relative: Path) -> tuple:
            short = Path(*relative.parts[2:]).as_posix() if relative.parts[0] == 'sandbox' else relative.as_posix()
            aliases = (str(workspace / relative), relative.as_posix(), short, relative.name)
            referenced = any(ref in references for ref in aliases)
            important = any(ref in important_refs for ref in aliases)
            from_challenge = (any(ref in challenge_refs for ref in aliases[:-1])
                              or relative.name in challenge_refs and len(relative.parts) == shallowest[relative.name])
            try:
                modified = (workspace / relative).stat().st_mtime if relative.suffix.lower() == '.md' else 0
            except OSError:
                modified = 0
            # 原附件与明确引用的源码优先；分析文档与依赖源码不互相挤占全部预算。
            return (not from_challenge, not important, not referenced, relative.parts[0] != 'attachments',
                    relative.parts[0] != 'artifacts',
                    -modified,
                    len(relative.parts), relative.name != 'exp.py', relative.as_posix())

        for relative in sorted(candidates, key=priority):
            is_note = relative.suffix.lower() == '.md'
            budget_key = 'note' if is_note else 'source'
            budget = file_budgets[budget_key]
            if len(notes if is_note else sources) >= (4 if is_note else 12) or budget <= 0:
                continue
            try:
                with open_directory(workspace, relative.parent) as directory:
                    fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(fd, 'rb') as source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            continue
                        limit = 8000 if is_note else 9000
                        data = source.read(limit + 1)
                if b'\0' in data:
                    continue
                cost = min(len(data), limit)
                if cost > budget:
                    continue
                text = data[:limit].decode('utf-8', errors='replace')
            except (OSError, ValueError, UnicodeError):
                continue
            file_budgets[budget_key] -= cost
            item = {'path': relative.as_posix(), 'start_line': 1, 'content': text,
                    'excerpt': len(data) > limit}
            if is_note:
                notes.append(item)
                continue
            symbols = []
            if relative.suffix == '.py' and len(data) <= limit:
                try:
                    symbols = [{'name': node.name, 'line': node.lineno, 'end_line': node.end_lineno}
                               for node in ast.walk(ast.parse(text))
                               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))][:40]
                except (SyntaxError, ValueError):
                    pass
            sources.append({**item, 'symbols': symbols})
    evidence = {'executions': list(reversed(executions)), 'source_files': sources,
                'limitations': '材料为本会话有界节选；未展示不代表不存在，不能据此补造命令输出、源码行号或函数地址。'}
    if notes:
        evidence['analysis_notes'] = notes
    return evidence


def fenced(text: str, language: str = '') -> str:
    fence = '`' * max(3, max((len(m.group()) + 1 for m in re.finditer(r'`+', text)), default=3))
    return f'{fence}{language}\n{text.rstrip()}\n{fence}'


def technical_appendix(digest: dict[str, Any]) -> str:
    evidence = digest.get('technical_evidence') or {}
    blocks = []
    for row in evidence.get('executions', []):
        parameters = row.get('parameters') or {}
        command = parameters.get('command')
        blocks.append(f"### {row.get('tool_name', '工具')} · `{row.get('invocation_id', '')}`\n\n"
                      f"任务：`{row.get('task_id', '')}`；执行状态：{row.get('execution_status', '')}；退出码：{row.get('returncode')}。")
        if row.get('workdir'):
            blocks.append(f"执行目录：`{row['workdir']}`")
        if isinstance(command, str):
            blocks.append(fenced(command, 'bash'))
        elif parameters:
            blocks.append(fenced(json.dumps(parameters, ensure_ascii=False, indent=2), 'json'))
        elif row.get('parameters_omitted'):
            blocks.append('调用参数超出材料窗口，未提供完整参数；不能据此直接复现。')
        for key, label in (('stdout', '实际标准输出'), ('stderr', '实际标准错误'), ('error', '工具错误')):
            if row.get(key):
                blocks.extend([label + ('（节选）' if row.get('output_excerpt') else ''), fenced(row[key], 'text')])
        if row.get('output_file'):
            blocks.append(f"完整执行日志位置：`{row['output_file']}`")
    for source in evidence.get('source_files', []):
        blocks.extend([f"### 源码：`{source['path']}`\n\n从第 {source['start_line']} 行开始"
                       + ('（节选；不保证代码完整）' if source.get('excerpt') else '（采集到文件末尾）'),
                       fenced(source['content'], Path(source['path']).suffix.lstrip('.'))])
        if source.get('symbols'):
            blocks.append('函数／类定位：' + '；'.join(f"`{row['name']}`：第 {row['line']}–{row['end_line']} 行"
                                                   for row in source['symbols']))
    if not blocks:
        return '\n\n## 技术证据\n\n未采集到可引用的命令、输出或源码；不补造复现过程。\n'
    return '\n\n## 技术证据与源码附录\n\n' + '\n\n'.join(blocks) + '\n'


def save_writeup(path: Path, body: str) -> Path:
    """原样保存报告 Markdown；其中的 exp.py 仅为代码正文，不提取、不执行。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(body, encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
