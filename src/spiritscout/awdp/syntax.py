"""模型 build_patch 入口：先检查冻结字节，再生成交付归档。"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path

from spiritscout.awdp.builder import prepare_build, write_build
from spiritscout.awdp.originals import private_dir, write_private
from spiritscout.awdp.patches import _open_regular
from spiritscout.tools.sandbox import offline_execution


async def build_checked_patch(agent, task, route, args: dict) -> dict:
    root = Path(agent.ctx.builtin_ctx['workspace_root']).resolve()
    challenge = agent.challenge_id(task)
    plan = await asyncio.to_thread(prepare_build, root, challenge,
        list(agent.challenge_of(task).get('attachments') or []), args)
    directory = private_dir(challenge) / 'runs' / ('syntax-' + os.urandom(8).hex())
    inputs = directory / 'attachments'
    items = []
    for index, (record, payload) in enumerate(zip(plan.records, plan.payloads, strict=True)):
        name = str(index) + Path(record['source']).suffix
        write_private(root, inputs / name, payload)
        items.append({'source': record['source'], 'file': name})
    write_private(root, inputs / 'inputs.json', json.dumps(items).encode())
    worker = Path(__file__).with_name('syntax_worker.py').read_text()
    report = directory / 'artifacts/report.json'
    command = shlex.join(['python3', '-I', '-c', worker, str(root / inputs), str(root / report)])
    context = {**agent._tool_ctx(task), 'workspace_root': str(root / directory), 'workspace_scope': None,
               'guard_root': agent._tool_ctx(task).get('guard_root') or str(root), '_dedup_bypass': True}
    token = offline_execution.set(True)
    try:
        execution = await agent.ctx.tools.call('sandbox_exec', {'code': command, 'language': 'shell',
            'action': 'analysis', 'timeout': min(300, len(items) * 8 + 15), 'log_name': 'patch_syntax'}, **context)
    finally:
        offline_execution.reset(token)
    if not execution.ok or execution.returncode:
        return {'ok': False, 'executed': False, 'status': 'syntax_check_error',
                'error': (execution.error or execution.stderr or execution.stdout)[-2000:],
                'note': '语法检查未完成，未生成提交包；按错误修复检查环境后重试。'}
    with _open_regular(root, report) as stream:
        files = json.load(stream)['files']
    checks = {'files': files, 'complete': all(item['status'] == 'passed' for item in files)}
    if any(item['status'] in {'failed', 'error'} for item in files):
        return {'ok': False, 'executed': False, 'status': 'invalid_source', 'syntax_checks': checks,
                'error': '修复源码未通过语法检查，未生成提交包。',
                'note': '按文件与行号修改工作副本，再 build_patch；不要把语法错误当成漏洞测试失败。'}
    result = await asyncio.to_thread(write_build, plan)
    return {**result, 'syntax_checks': checks}
