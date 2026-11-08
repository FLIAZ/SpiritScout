"""轻量 AWDP 源码扫描契约；结果可分页回读，扫描不直接登记漏洞。"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from spiritscout.awdp.patches import _open_regular
from spiritscout.tools.sandbox_paths import (
    open_directory,
    sandbox_directory,
    sandbox_tmp_directory,
)

SCHEMA = {'type': 'function', 'function': {'name': 'source_scan',
    'description': 'AWDP 专用离线源码扫描：scan 用本地 Semgrep 规则收集候选，不执行源码、不自动修复；read 分页读已有报告，不重扫。覆盖 Python/PHP/JS/TS/Java/Go/Ruby/C/C++ 的部分高危入口/危险操作。结果含原路径、行号、源码哈希、扫描错误和缺口；Agent 必须结合源码核对真实危害。',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'action': {'type': 'string', 'enum': ['scan', 'read']},
        'path': {'type': 'string', 'description': 'scan 的实际源码文件/目录，限本题附件、workdir、/tmp、artifacts；压缩包先安全解包'},
        'report': {'type': 'string', 'description': 'read 使用 scan 返回的 report 路径'},
        'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12},
        'timeout': {'type': 'integer', 'minimum': 5, 'maximum': 180, 'description': '本次扫描秒数，默认 60；超时保留缺口，不当成零命中'}},
        'required': ['action']}}}


def _source_path(root: Path, challenge: str, raw: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > 4096 or '\0' in raw:
        raise ValueError('需要源码 path')
    path, work, temporary = Path(raw), root / sandbox_directory(challenge), root / sandbox_tmp_directory(challenge)
    allowed = [root / 'attachments', root / 'artifacts', work]
    if not path.is_absolute():
        path = work / path
    elif path.is_relative_to('/tmp') and not path.is_relative_to(root):
        path = temporary / path.relative_to('/tmp')
        allowed.append(temporary)
    if '..' in path.parts or not any(path.is_relative_to(base) for base in allowed):
        raise ValueError('源码路径不在本题工作目录或附件中')
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError('源码路径不能经过符号链接')
    if not path.is_file() and not path.is_dir():
        raise ValueError('源码文件或目录不存在')
    if path.is_relative_to(temporary):
        return str(Path('/tmp') / path.relative_to(temporary))
    return str(path)


def read_report(root: Path, challenge: str, raw: str, offset: int, limit: int) -> dict:
    if not isinstance(raw, str) or not raw:
        raise ValueError('需要已有 report 路径')
    path = Path(raw)
    if path.is_absolute():
        path = path.relative_to(root)
    base = Path('artifacts') / 'awdp-scans' / sandbox_directory(challenge).name
    if not path.is_relative_to(base) or path.name != 'report.json':
        raise ValueError('只能读取本题 source_scan 返回的 report.json')
    with _open_regular(root, path) as source:
        content = source.read(32 * 1024 * 1024 + 1)
    if len(content) > 32 * 1024 * 1024:
        raise ValueError('扫描报告过大，请按目录缩小扫描范围')
    data = json.loads(content)
    findings = data.pop('findings')
    page, size = [], 0
    for item in findings[offset:offset + limit]:
        # 分页不截断单条定位信息；大型原文保留在 raw_report。
        size += len(json.dumps(item, ensure_ascii=False))
        if page and size > 7000:
            break
        page.append(item)
    errors = data.pop('errors', [])
    coverage = data.get('coverage') or {}
    engine_skipped = coverage.get('engine_skipped') or []
    coverage.update(engine_skipped=engine_skipped[:12], engine_skipped_count=len(engine_skipped))
    data['coverage'] = coverage
    next_offset = offset + len(page)
    result = {**data, 'ok': data['status'] in {'completed', 'partial'}, 'report': str(root / path),
        'items': page, 'total': len(findings), 'offset': offset, 'next_offset': next_offset,
        'has_more': next_offset < len(findings),
        'errors': [json.dumps(item, ensure_ascii=False)[:800] for item in errors[:6]], 'error_count': len(errors),
        'errors_partial': len(errors) > 6}
    if result['has_more']:
        result['next_call'] = {'name': 'source_scan', 'arguments': {
            'action': 'read', 'report': result['report'], 'offset': next_offset, 'limit': limit}}
    return result


async def scan(agent, task, route, arguments):
    from spiritscout.ctf.agents import _ToolOutcome

    try:
        if not isinstance(arguments, dict) or set(arguments) - set(SCHEMA['function']['parameters']['properties']):
            raise ValueError('source_scan 参数不符合契约')
        args = dict(arguments)
        action, offset, limit, timeout = (args.get('action'), args.get('offset', 0),
                                        args.get('limit', 6), args.get('timeout', 60))
        if (action not in {'scan', 'read'} or type(offset) is not int or offset < 0
                or type(limit) is not int or not 1 <= limit <= 12
                or type(timeout) is not int or not 5 <= timeout <= 180):
            raise ValueError('action=scan/read，offset≥0，limit=1–12，timeout=5–180')
        workspace = (agent.ctx.builtin_ctx or {}).get('workspace_root')
        if not workspace:
            raise ValueError('缺少会话工作区')
        root, challenge, metadata = Path(workspace).resolve(), agent.challenge_id(task), {}
        if action == 'scan':
            path = _source_path(root, challenge, args.get('path'))
            relative = Path('artifacts') / 'awdp-scans' / sandbox_directory(challenge).name / os.urandom(8).hex()
            with open_directory(root, relative, create=True):
                pass
            directory = root / relative
            request = {'path': path, 'directory': str(directory), 'timeout': timeout,
                       'rules': json.loads(Path(__file__).with_name('data').joinpath('audit_rules.json').read_text())}
            script = Path(__file__).with_name('scan_worker.py').read_text()
            command = 'python3 -I -c ' + shlex.quote(script) + ' ' + shlex.quote(json.dumps(request, ensure_ascii=False))
            result = await agent.ctx.tools.call('sandbox_exec', {'code': command, 'language': 'shell',
                'action': 'analysis', 'timeout': timeout + 15, 'log_name': 'source_scan'}, **agent._tool_ctx(task))
            metadata = {'invocation_id': result.invocation_id, 'output_file': result.parsed.get('output_file')}
            report_path = str(directory / 'report.json')
            if not result.ok or result.returncode:
                return _ToolOutcome(ok=False, metadata=metadata,
                    text=json.dumps({'ok': False, 'report': report_path, 'status': 'failed',
                        'error': (result.error or result.stderr or result.stdout)[-1800:],
                        'note': '扫描未完成，不能按零命中处理；若已有报告可用 read 回读。'}, ensure_ascii=False))
        else:
            report_path = args.get('report')
        data = read_report(root, challenge, report_path, offset, limit)
        agent.ctx.audit.log('awdp.source_scan', agent.agent_id, {'task_id': task.task_id,
            'route_id': route.route_id, 'action': action, 'report': data['report'],
            'status': data['status'], 'findings': data['total']})
        return _ToolOutcome(ok=data['ok'], metadata=metadata, text=json.dumps(data, ensure_ascii=False),
                            note='源码扫描候选；可利用性尚未确认')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _ToolOutcome(ok=False, text='源码扫描失败：' + str(exc)[:600])
