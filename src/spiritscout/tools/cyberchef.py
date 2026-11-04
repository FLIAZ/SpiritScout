"""官方 CyberChef Node API：离线沙箱执行，完整字节产物与配方留存。"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from spiritscout.core.models import RunMode
from spiritscout.tools.invocation import current_invocation
from spiritscout.tools.sandbox import _kill_group, _read_capped, sandbox_available
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

MAX_BYTES = 16 * 1024 * 1024
NAMES = frozenset({'cyberchef_operations', 'cyberchef_bake'})

def runtime_paths() -> tuple[Path, Path]:
    """只查找已安装的运行时；不在工具调用时联网安装。"""
    explicit = os.environ.get('SPIRITSCOUT_CYBERCHEF_ROOT')
    roots = [Path(explicit)] if explicit else [
        Path('/opt/cyberchef'), Path(__file__).resolve().parents[3] / '.tools/cyberchef']
    root = next((p.resolve() for p in roots
                 if (p / 'node_modules/cyberchef/src/node/index.mjs').is_file()), None)
    if root is None:
        raise ValueError('CyberChef 未安装：运行 python scripts/setup_cyberchef.py，或重建 Docker 镜像')
    node = (os.environ.get('SPIRITSCOUT_CYBERCHEF_NODE')
            or next((str(p) for p in (root / 'bin/node', root / 'node_modules/node/bin/node')
                     if p.is_file()), None) or shutil.which('node'))
    if not node or not Path(node).is_file():
        raise ValueError('CyberChef 需要 Node.js 24–26；可设置 SPIRITSCOUT_CYBERCHEF_NODE')
    return root, Path(node).resolve()


def _input_bytes(workspace: Path, input: str | None, input_file: str | None,
                 offset: int, length: int | None) -> bytes:
    if (input is None) == (input_file is None):
        raise ValueError('input 与 input_file 必须且只能提供一个')
    if type(offset) is not int or offset < 0 or (
            length is not None and (type(length) is not int or not 0 <= length <= MAX_BYTES)):
        raise ValueError('offset 为非负字节偏移，length 为 0–16 MiB 的字节数')
    if input is not None:
        if not isinstance(input, str) or offset or length is not None:
            raise ValueError('input 必须是字符串；offset/length 仅用于 input_file')
        data = input.encode('utf-8')
    else:
        if not isinstance(input_file, str) or not input_file:
            raise ValueError('input_file 必须是本会话文件路径')
        path = Path(input_file)
        path = (path if path.is_absolute() else workspace / path).resolve()
        allowed = [workspace / name for name in ('attachments', 'sandbox', 'artifacts', 'http-responses')]
        if not any(path.is_relative_to(p) for p in allowed):
            raise ValueError('input_file 仅允许本会话附件、沙箱产物、artifacts 或 HTTP 响应原文')
        if not path.is_file():
            raise ValueError('input_file 必须是普通文件')
        with path.open('rb') as stream:
            stream.seek(offset)
            data = stream.read(MAX_BYTES + 1 if length is None else length)
        if length is not None and len(data) != length:
            raise ValueError('请求的输入范围不完整，不会静默截短')
        if offset > path.stat().st_size:
            raise ValueError('offset 超出文件长度')
    if len(data) > MAX_BYTES:
        raise ValueError('单次输入超过 16 MiB，请通过 offset/length 显式选择范围')
    return data


def _argv(runtime: Path, node: Path, job: Path, output: Path) -> list[str]:
    # 只挂载系统运行库、官方依赖和本次输入/输出；不挂载工作区、HOME、环境密钥。
    argv = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session',
            '--clearenv', '--ro-bind', '/usr', '/usr']
    for name in ('/lib', '/lib64'):
        if Path(name).exists():
            argv += ['--ro-bind', name, name]
    argv += ['--tmpfs', '/usr/local', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
             '--ro-bind', str(runtime / 'node_modules'), '/tool/runtime/node_modules',
             '--ro-bind', str(node), '/tool/node',
             '--ro-bind', str(Path(__file__).with_name('cyberchef_worker.mjs')), '/tool/worker.mjs',
             '--ro-bind', str(job), '/job', '--bind', str(output), '/output',
             '--chdir', '/job', '--setenv', 'HOME', '/tmp', '--setenv', 'LANG', 'C.UTF-8',
             '--', '/tool/node', '--max-old-space-size=512', '/tool/worker.mjs']
    return argv


async def _run(mode: str, *, workspace_root: str = '', input: str | None = None,
               input_file: str | None = None, offset: int = 0, length: int | None = None,
               recipe: list[dict[str, Any]] | None = None, query: str = '',
               limit: int = 3, timeout: float = 60,
               _run_mode: RunMode | None = None, _lightweight_local: bool = False,
               **_: Any) -> ToolResult:
    name = 'cyberchef_operations' if mode == 'operations' else 'cyberchef_bake'
    output = None
    try:
        if not workspace_root:
            raise ValueError('缺少会话 workspace_root')
        if not sandbox_available():
            raise ValueError('CyberChef 需要 Linux/bubblewrap；Windows/macOS 请使用 Docker，不在宿主降级执行')
        runtime, node = runtime_paths()
        workspace = Path(workspace_root).resolve()
        for shared in ('attachments', 'sandbox', 'artifacts', 'http-responses'):
            if (workspace / shared).is_symlink():
                raise ValueError(f'共享目录不能是符号链接: {shared}')
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
            raise ValueError('timeout 必须为 1–300 秒')
        request: dict[str, Any] = {'mode': mode}
        data = b''
        if mode == 'operations':
            if not isinstance(query, str) or not query.strip() or len(query) > 200:
                raise ValueError('query 必须为 1–200 字符的操作关键词或完整名称')
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10:
                raise ValueError('offset 为非负整数，limit 为 1–10')
            request.update(query=query, offset=offset, limit=limit)
        else:
            if not isinstance(recipe, list) or not recipe:
                raise ValueError('recipe 必须是非空的 CyberChef 配方数组')
            for step in recipe:
                if (not isinstance(step, dict) or set(step) - {'op', 'args'}
                        or not isinstance(step.get('op'), str) or not step['op']
                        or not isinstance(step.get('args', []), list)):
                    raise ValueError('每步只接受 op 名称与位置参数 args 数组；先查询 cyberchef_operations')
            data = _input_bytes(workspace, input, input_file, offset, length)
            request.update(recipe=recipe, max_output_bytes=MAX_BYTES)
            if _lightweight_local and _run_mode in (RunMode.CTF, RunMode.AWDP):
                # 仅 Ollama 小模型 × CTF 模式：猜错操作名时附带相近候选
                request['suggest_unknown_ops'] = True
        encoded = json.dumps(request, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 1024 * 1024:
            raise ValueError('配方或查询参数超过 1 MiB')
        artifacts = workspace / 'artifacts'
        artifacts.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix='cyberchef-', dir=artifacts))
        with tempfile.TemporaryDirectory(prefix='ss-cyberchef-') as temporary:
            job = Path(temporary)
            (job / 'request.json').write_text(encoded, encoding='utf-8')
            (job / 'input.bin').write_bytes(data)
            proc = await asyncio.create_subprocess_exec(
                *_argv(runtime, node, job, output), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
            readers = [asyncio.create_task(_read_capped(s, 6000)) for s in (proc.stdout, proc.stderr)]
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                streams = await asyncio.wait_for(asyncio.gather(*readers), timeout=5)
            except BaseException:
                _kill_group(proc)
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=5)
                # 等待管道 EOF 后再取消读取，避免超时/停止后残留 subprocess transport。
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.gather(*readers), timeout=5)
                for task in readers:
                    task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                raise
            if proc.returncode:
                diagnostic = b'\n'.join(s[0] for s in streams).decode('utf-8', errors='replace')
                raise ValueError(f'CyberChef 执行失败（{proc.returncode}）：{diagnostic[-5000:]}')
        invocation = current_invocation.get()
        metadata: dict[str, Any] = {
            'source_invocation_id': invocation.invocation_id if invocation else '',
            'metadata_file': str(output / 'metadata.json'),
        }
        if mode == 'operations':
            catalog = output / 'catalog.json'
            if catalog.stat().st_size > 1024 * 1024:
                raise ValueError('操作参数说明过大，请缩小查询或分页')
            metadata.update(json.loads(catalog.read_text(encoding='utf-8')))
            metadata['catalog_file'] = str(catalog)
        else:
            result = output / 'result.bin'
            if result.stat().st_size > MAX_BYTES:
                raise ValueError('输出超过 16 MiB')
            raw = result.read_bytes()
            metadata.update(json.loads((output / 'engine.json').read_text(encoding='utf-8')))
            try:
                text, utf8 = raw.decode('utf-8'), True
            except UnicodeDecodeError:
                text, utf8 = raw[:800].hex(), False
            metadata.update(input_bytes=len(data), input_sha256=hashlib.sha256(data).hexdigest(),
                            input_file=input_file, input_offset=offset,
                            output_bytes=len(raw), output_sha256=hashlib.sha256(raw).hexdigest(),
                            output_file=str(result), preview=text[:1600],
                            preview_format='utf-8' if utf8 else 'hex',
                            preview_truncated=len(text) > 1600 or (not utf8 and len(raw) > 800),
                            recipe_file=str(output / 'recipe.json'))
            (output / 'recipe.json').write_text(json.dumps(recipe, ensure_ascii=False), encoding='utf-8')
        (output / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        preview = json.dumps(metadata, ensure_ascii=False, separators=(',', ':'))
        if len(preview) > 5500:
            brief = {k: v for k, v in metadata.items() if k != 'operations'}
            brief.update(preview_truncated=True, notice='完整参数说明见 catalog_file；缩小查询或分页读取')
            preview = json.dumps(brief, ensure_ascii=False, separators=(',', ':'))
        return ToolResult(tool=name, ok=True, stdout=preview, parsed=metadata)
    except asyncio.CancelledError:
        if output is not None:
            shutil.rmtree(output, ignore_errors=True)
        raise
    except (OSError, ValueError, TypeError, KeyError, TimeoutError) as exc:
        if output is not None:
            shutil.rmtree(output, ignore_errors=True)
        timed_out = isinstance(exc, TimeoutError)
        return ToolResult(tool=name, ok=False, returncode=1, timed_out=timed_out,
                          error='CyberChef 超时，进程已终止' if timed_out else str(exc),
                          parsed={'retryable': False})


async def cyberchef_operations(**kwargs: Any) -> ToolResult:
    return await _run('operations', **kwargs)


async def cyberchef_bake(**kwargs: Any) -> ToolResult:
    return await _run('bake', **kwargs)


SPECS = {name: ToolSpec(name=name, kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY,
                       target_params=(), description='离线 CyberChef 操作查询或配方执行',
                       allowed_params=({'query', 'offset', 'limit', 'timeout'} if name == 'cyberchef_operations'
                                       else {'recipe', 'input', 'input_file', 'offset', 'length', 'timeout'}))
         for name in NAMES}
SCHEMAS = [
    {'type': 'function', 'function': {'name': 'cyberchef_operations',
        'description': '查询已安装 CyberChef 的操作名、位置参数、默认值和选项；编码解码前先查准确参数。Node API 不支持部分浏览器操作和配方流控制。',
        'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10}}, 'required': ['query']}}},
    {'type': 'function', 'function': {'name': 'cyberchef_bake',
        'description': '在断网沙箱用官方 CyberChef 执行编码/解码/哈希等配方。input 与 input_file 二选一；优先读取原文件而非手抄。完整字节存 output_file，预览可能省略或为 hex；后续脚本直接读取该文件，禁止心算或补写变量名。参数遵循 CyberChef 语义，严格解码需在 args 显式指定。',
        'parameters': {'type': 'object', 'properties': {
            'recipe': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'op': {'type': 'string'}, 'args': {'type': 'array', 'items': {}}}, 'required': ['op']}},
            'input': {'type': 'string', 'description': 'UTF-8 字符串，不去空白'},
            'input_file': {'type': 'string', 'description': '本会话附件、sandbox、artifacts 或 http-responses 中的文件路径'},
            'offset': {'type': 'integer', 'minimum': 0, 'description': '文件字节偏移；不能直接沿用 http_response_read 的字符偏移'},
            'length': {'type': 'integer', 'minimum': 0, 'maximum': MAX_BYTES, 'description': '文件字节数，省略则读至末尾'},
            'timeout': {'type': 'number', 'minimum': 1, 'maximum': 300}}, 'required': ['recipe']}}},
]
