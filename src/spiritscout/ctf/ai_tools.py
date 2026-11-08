"""普通与轻量 CTF 共用的离线 AI 证据工具；参数/执行仍走既有审计与沙箱。"""
from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

from spiritscout.tools.sandbox_paths import sandbox_directory, sandbox_tmp_directory

SCHEMA = {'type': 'function', 'function': {'name': 'ai_inspect',
    'description': '离线 AI 分析：environment 查实际沙箱 Python/依赖版本；artifact 分页查看 NPY/NPZ、PyTorch ZIP、pickle 操作码或 safetensors 元数据，不加载模型或执行附件；compare 计算两个已保存数值 NPY 的全数组扰动范数与范围，不判断是否解题。无需联网，结果含来源摘要和续读参数。',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'operation': {'type': 'string', 'enum': ['environment', 'artifact', 'compare']},
        'path': {'type': 'string', 'description': '本题真实附件、workdir、artifacts 或持久 /tmp 文件；compare 的候选 NPY'},
        'reference': {'type': 'string', 'description': 'compare 的原始 NPY；必须相同形状，不自动广播'},
        'member': {'type': 'string', 'description': 'artifact 中 ZIP 内实际返回的 .npy/.pkl 条目名，不解压'},
        'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 32}}, 'required': ['operation']}}}


def _path(raw, root, challenge):
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise ValueError('需要本题真实文件路径')
    working, temporary = root / sandbox_directory(challenge), root / sandbox_tmp_directory(challenge)
    path = (Path(raw) if Path(raw).is_absolute() else working / raw).resolve()
    allowed = (root / 'attachments', root / 'artifacts', working)
    if Path(raw).is_relative_to('/tmp') and not any(path.is_relative_to(base) for base in allowed):
        path = (temporary / Path(raw).relative_to('/tmp')).resolve()
        if not path.is_relative_to(temporary) or not path.is_file():
            raise ValueError('本题 /tmp 文件不存在或超出范围')
        return str(Path('/tmp') / path.relative_to(temporary))
    if not any(path.is_relative_to(base) for base in allowed) or not path.is_file():
        raise ValueError('文件不存在或不在本题附件、workdir、artifacts 中')
    return str(path)


async def inspect(agent, task, route, arguments):
    from spiritscout.ctf.agents import _ToolOutcome

    try:
        if not isinstance(arguments, dict) or set(arguments) - set(SCHEMA['function']['parameters']['properties']):
            raise ValueError('参数必须符合 ai_inspect 契约')
        args = dict(arguments)
        if args.get('operation') not in {'environment', 'artifact', 'compare'}:
            raise ValueError('operation 必须为 environment/artifact/compare')
        args.setdefault('offset', 0)
        args.setdefault('limit', 8)
        if type(args['offset']) is not int or args['offset'] < 0 or type(args['limit']) is not int or not 1 <= args['limit'] <= 32:
            raise ValueError('offset >= 0，limit 为 1–32 整数')
        if 'member' in args and (not isinstance(args['member'], str) or len(args['member']) > 2048):
            raise ValueError('member 需为实际条目名')
        workspace = (agent.ctx.builtin_ctx or {}).get('workspace_root')
        if not workspace:
            raise ValueError('缺少会话工作区')
        root = Path(workspace).resolve()
        if args['operation'] != 'environment':
            args['path'] = _path(args.get('path'), root, agent.challenge_id(task))
        if args['operation'] == 'compare':
            args['reference'] = _path(args.get('reference'), root, agent.challenge_id(task))
        script = Path(__file__).with_name('ai_worker.py').read_text()
        command = 'python3 -I -c ' + shlex.quote(script) + ' ' + shlex.quote(json.dumps(args, ensure_ascii=False))
        result = await agent.ctx.tools.call('sandbox_exec', {'code': command, 'language': 'shell',
            'action': 'analysis', 'timeout': 45, 'log_name': 'ai_inspect'}, **agent._tool_ctx(task))
        if not result.ok or result.returncode:
            raise ValueError('AI 检查未完成：' + (result.error or result.stderr)[-500:])
        try:
            data = json.loads(result.stdout)
        except ValueError as error:
            raise ValueError('AI 回执不是完整 JSON；原始日志：' + str(result.parsed.get('output_file'))) from error
        if data.get('has_more'):
            data['next_call'] = {'name': 'ai_inspect', 'arguments': {**arguments, 'offset': data['next_offset']}}
        text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        return _ToolOutcome(ok=data.get('ok') is True, text=text, note='离线 AI 检查：' + args['operation'],
            metadata={'invocation_id': result.invocation_id, 'output_file': result.parsed.get('output_file'),
                      'observation': {'digest': hashlib.sha256(text.encode()).hexdigest(), 'preview': text,
                                      'structured': True, 'partial': args['operation'] != 'compare',
                                      'empty': data.get('items') == []}})
    except (OSError, ValueError, TypeError) as error:
        return _ToolOutcome(ok=False, text=str(error))
