"""在实际工具沙箱中按需查询本机用法，不把整套 CLI/API 文档塞进提示词。"""
from __future__ import annotations

import re
import shlex

GUIDANCE = (
    '优先复用现场已有的成熟 CLI 和 Python 库。tool_lookup 只查灵枢工具，不能查库函数。'
    '外部 CLI 用 tool_help(kind=cli, name=命令名) 读本机帮助；Python 用 '
    'tool_help(kind=python, name=完整模块.函数或类.方法) 查实际签名与文档，再由 shell/python 调用。'
    '参数或属性报错时先查实际用法，依据错误修改；相同参数重试需有文件、环境或输入变化依据。'
)

SCHEMA = {'type': 'function', 'function': {
    'name': 'tool_help', 'description': (
        '按需读取实际沙箱内的外部 CLI 帮助或 Python API 签名、文档。'
        'name 如 jadx、pwndbg 或 pwn.cyclic_find、z3.IntNumRef.as_long。'
        '不安装依赖，不执行目标函数；导入模块及帮助命令仍在原工具沙箱内执行。'),
    'parameters': {'type': 'object', 'properties': {
        'kind': {'type': 'string', 'enum': ['cli', 'python']},
        'name': {'type': 'string', 'description': 'CLI 可执行名或 Python 完整点分名称'},
        'help_flag': {'type': 'string', 'enum': ['--help', '-h'], 'description': '仅 CLI；默认 --help'},
    }, 'required': ['kind', 'name']}}}

_PYTHON = r'''
import ast, importlib, importlib.metadata, inspect, json, sys
from pathlib import Path
name = NAME
parts = name.split('.')

def candidates():
    # 包名、导入名和装饰器 traceback 路径常不同；仅定位本机源码定义，不批量导入子模块。
    roots = [parts[0]]
    try:
        tops = importlib.metadata.distribution(parts[0]).read_text('top_level.txt') or ''
        roots += [line for line in tops.splitlines() if line.isidentifier()]
    except importlib.metadata.PackageNotFoundError:
        pass
    found, scanned, used = [], 0, 0
    symbol = parts[-1]
    for root_name in dict.fromkeys(roots):
        try:
            root = importlib.import_module(root_name)
        except (ImportError, AttributeError):
            continue
        if hasattr(root, symbol):
            found.append(root_name + '.' + symbol)
        for directory in getattr(root, '__path__', []):
            base = Path(directory)
            for path in base.rglob('*.py'):
                scanned += 1
                if scanned > 500 or used > 16 * 1024 * 1024 or len(found) >= 6:
                    return list(dict.fromkeys(found))[:6], True
                try:
                    with path.open('rb') as stream:
                        raw = stream.read(256 * 1024 + 1)
                    used += len(raw)
                    if len(raw) > 256 * 1024 or symbol.encode() not in raw:
                        continue
                    tree = ast.parse(raw)
                except (OSError, SyntaxError, ValueError):
                    continue
                relative = path.relative_to(base).with_suffix('').parts
                if relative[-1] == '__init__':
                    relative = relative[:-1]
                module = '.'.join((root_name, *relative))
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
                        found.append(module + '.' + symbol)
                    if isinstance(node, ast.ClassDef):
                        for child in node.body:
                            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == symbol:
                                found.append(module + '.' + node.name + '.' + symbol)
    return list(dict.fromkeys(found))[:6], False

try:
    obj = None
    for end in range(len(parts), 0, -1):
        module_name = '.'.join(parts[:end])
        try:
            obj = importlib.import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name != module_name and not module_name.startswith(str(error.name) + '.'):
                raise
            continue
        for part in parts[end:]:
            obj = getattr(obj, part)
        break
    if obj is None:
        raise ModuleNotFoundError(name)
    try:
        signature = str(inspect.signature(obj))
    except (TypeError, ValueError):
        signature = None
    doc = inspect.getdoc(obj) or ''
    result = {'name': name, 'signature': signature, 'doc': doc[:2200],
              'doc_partial': len(doc) > 2200, 'module': getattr(obj, '__module__', None)}
    print(json.dumps(result, ensure_ascii=False))
except (ImportError, AttributeError) as error:
    names, partial = candidates()
    print(json.dumps({'name': name, 'error': type(error).__name__ + ': ' + str(error),
        'candidates_from_local_package': names, 'search_partial': partial,
        'next_calls': [{'name': 'tool_help', 'arguments': {'kind': 'python', 'name': candidate}} for candidate in names],
        'notice': '目标名称未解析；候选来自本机导出或源码，请按候选查询实际签名，不猜模块路径或重写基础算法。'},
        ensure_ascii=False))
    sys.exit(1)
'''


async def lookup(agent, task, route, state, args):
    from spiritscout.ctf.agents import _ToolOutcome

    name, kind = args.get('name'), args.get('kind')
    if not isinstance(name, str) or len(name) > 200:
        return _ToolOutcome(ok=False, text='name 需要本机命令名或 Python 点分名称，最多 200 字符')
    if kind == 'python' and all(part.isidentifier() for part in name.split('.')):
        language, key, code = 'python', 'code', _PYTHON.replace('NAME', repr(name), 1)
    elif kind == 'cli' and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.+-]*', name):
        flag = args.get('help_flag', '--help')
        if flag not in ('--help', '-h'):
            return _ToolOutcome(ok=False, text='help_flag 仅支持 --help 或 -h')
        language, key, code = 'shell', 'command', shlex.join([name, flag])
    else:
        return _ToolOutcome(ok=False, text='kind 需要 cli/python；CLI 只接受可执行名，Python 需要点分名称')
    # 共用既有执行、权限、超时、完整日志和审计，避免在宿主导入目标模块。
    result = await agent._tool_sandbox(task, route, state, language,
                                     {key: code, 'action': 'analysis', 'timeout': 20})
    result.note = '本机工具用法：' + name
    result.metadata['help_target'] = {'kind': kind, 'name': name}
    return result
