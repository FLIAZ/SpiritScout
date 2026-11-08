"""沙箱内的离线源码导航索引；只解析快照，不导入附件或执行项目配置。"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILES = 4096
MAX_RECORDS = 40000
CODE_SUFFIXES = {'.py', '.pyi', '.php', '.js', '.jsx', '.ts', '.tsx', '.java', '.go',
                 '.rb', '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.cs', '.rs'}
DEFINITION_KINDS = {'function', 'method', 'class', 'interface', 'struct', 'enum',
                    'constructor', 'macro', 'typedef', 'prototype', 'namespace', 'trait',
                    'func', 'methodSpec', 'type', 'talias'}


def dotted(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted(node.value)
        return parent + '.' + node.attr if parent else ''
    return ''


class PythonIndex(ast.NodeVisitor):
    def __init__(self):
        self.symbols, self.calls, self.imports, self.scope = [], [], [], []

    def definition(self, node):
        qualified = '.'.join([*self.scope, node.name])
        signature = ast.unparse(node.args) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else ''
        self.symbols.append({'name': node.name, 'qualified': qualified,
            'kind': 'class' if isinstance(node, ast.ClassDef) else 'function',
            'line': min([node.lineno, *[x.lineno for x in node.decorator_list]]),
            'end_line': node.end_lineno, 'signature': signature[:500],
            'signature_partial': len(signature) > 500, 'engine': 'python_ast'})
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = definition

    def visit_Call(self, node):
        self.calls.append({'name': dotted(node.func), 'line': node.lineno,
            'caller': '.'.join(self.scope) or '<module>', 'engine': 'python_ast',
            'binding_verified': False})
        self.generic_visit(node)

    def visit_Import(self, node):
        for item in node.names:
            self.imports.append({'name': item.name, 'alias': item.asname or item.name.split('.')[0],
                'module': item.name if item.asname else item.name.split('.')[0], 'type': 'import',
                'line': node.lineno, 'scope': '.'.join(self.scope), 'level': 0})

    def visit_ImportFrom(self, node):
        for item in node.names:
            self.imports.append({'name': item.name, 'alias': item.asname or item.name,
                'module': node.module or '', 'type': 'from', 'level': node.level, 'line': node.lineno,
                'scope': '.'.join(self.scope)})


def snapshot(source: Path, directory: Path) -> tuple[list[dict], list[dict]]:
    files, skipped, total = [], [], 0
    (directory / 'files').mkdir()
    if source.is_file():
        candidates = iter([source])
    else:
        def walk():
            def error(exc):
                skipped.append({'path': str(exc.filename), 'reason': 'unreadable_directory'})

            for current, dirs, names in os.walk(source, followlinks=False, onerror=error):
                dirs.sort()
                for name in list(dirs):
                    path = Path(current) / name
                    if path.is_symlink() or name == '.git' or path == directory.parent:
                        dirs.remove(name)
                        skipped.append({'path': str(path), 'reason': 'symlink_or_index_metadata'})
                yield from (Path(current) / name for name in sorted(names))
        candidates = walk()
    for count, path in enumerate(candidates):
        if count >= MAX_FILES:
            skipped.append({'path': str(source), 'reason': 'file_count_limit', 'remaining_unknown': True})
            break
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('not_regular')
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError('file_size_limit')
            content = data.decode('utf-8')
            if '\0' in content:
                raise ValueError('binary_file')
            if total + len(data) > MAX_TOTAL_BYTES:
                skipped.append({'path': str(source), 'reason': 'total_size_limit', 'remaining_unknown': True})
                break
            total += len(data)
            file_id = f'f{len(files):04d}'
            suffix = path.suffix.lower() if path.suffix.lower() in CODE_SUFFIXES else '.txt'
            relative = 'files/' + file_id + suffix
            (directory / relative).write_bytes(data)
            files.append({'file_id': file_id, 'path': str(path), 'snapshot': relative,
                'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                'lines': len(content.splitlines(keepends=True)), 'symbols': [], 'calls': [], 'imports': [],
                'navigation': 'text_only'})
        except (OSError, ValueError) as exc:
            reason = 'non_utf8' if isinstance(exc, UnicodeDecodeError) else str(exc)[:240]
            skipped.append({'path': str(path), 'reason': reason})
    return files, skipped


def run(args: dict) -> dict:
    directory = Path(args['directory'])
    files, skipped = snapshot(Path(args['path']), directory)
    errors, count, ctags_targets = [], 0, []
    for item in files:
        path = directory / item['snapshot']
        if path.suffix in {'.py', '.pyi'}:
            try:
                visitor = PythonIndex()
                visitor.visit(ast.parse(path.read_bytes()))
                for kind in ('symbols', 'calls', 'imports'):
                    records = getattr(visitor, kind)
                    available = max(0, MAX_RECORDS - count)
                    item[kind] = records[:available]
                    count += len(item[kind])
                    if len(records) > available:
                        errors.append({'path': item['path'], 'reason': kind + '_record_limit'})
                item['navigation'] = 'python_ast'
                continue
            except (SyntaxError, ValueError, RecursionError) as exc:
                errors.append({'path': item['path'], 'reason': 'python_parse_error', 'detail': str(exc)[:300]})
        if path.suffix in CODE_SUFFIXES:
            ctags_targets.append(item['snapshot'])
    executable = shutil.which('ctags')
    engine = 'not_needed'
    if ctags_targets:
        engine = 'unavailable'
        if executable:
            (directory / 'inputs.txt').write_text('\n'.join(ctags_targets) + '\n', encoding='utf-8')
            command = [executable, '--options=NONE', '--output-format=json', '--fields=+neSlK',
                       '--sort=no', '-o', '-', '-L', 'inputs.txt']
            with (directory / 'ctags.jsonl').open('wb') as output, (directory / 'ctags.stderr').open('wb') as stderr:
                try:
                    result = subprocess.run(command, cwd=directory, stdout=output, stderr=stderr,
                                            timeout=args['timeout'], check=False)
                    engine = 'completed' if result.returncode == 0 else 'failed'
                except (OSError, subprocess.TimeoutExpired) as exc:
                    engine = 'failed'
                    errors.append({'reason': 'ctags_error', 'detail': str(exc)[:300]})
            mapping = {item['snapshot']: item for item in files}
            try:
                if (directory / 'ctags.jsonl').stat().st_size > 16 * 1024 * 1024:
                    raise ValueError('ctags_output_limit')
                with (directory / 'ctags.jsonl').open(encoding='utf-8') as stream:
                    for line in stream:
                        tag = json.loads(line)
                        item = mapping.get(tag.get('path'))
                        if tag.get('_type') != 'tag' or not item or tag.get('kind') not in DEFINITION_KINDS:
                            continue
                        if count >= MAX_RECORDS:
                            raise ValueError('symbol_record_limit')
                        if type(tag.get('line')) is not int or tag['line'] < 1:
                            continue
                        count += 1
                        item['symbols'].append({'name': tag['name'], 'qualified': '.'.join(filter(None,
                            [tag.get('scope'), tag['name']])), 'kind': tag['kind'], 'line': tag['line'],
                            'end_line': tag.get('end'), 'signature': str(tag.get('signature', ''))[:500],
                            'engine': 'ctags'})
                if engine == 'completed':
                    for name in ctags_targets:
                        mapping[name]['navigation'] = 'ctags'
            except (ValueError, OSError) as exc:
                engine = 'partial'
                errors.append({'reason': 'ctags_output_error', 'detail': str(exc)[:300]})
        if engine != 'completed':
            errors.append({'reason': 'ctags_' + engine,
                'detail': '需要带 JSON 支持的 Universal Ctags；其他文件仍可按原文回读，详见 ctags.stderr。'})
    report = {'source': args['path'], 'status': 'partial' if skipped or errors else 'completed',
        'files': files, 'skipped': skipped, 'errors': errors, 'ctags': engine,
        'coverage': {'files': len(files), 'skipped': len(skipped),
                     'symbols': sum(len(x['symbols']) for x in files), 'complete': not skipped and not errors},
        'limitations': ['这是本次源码快照；修改后需要重新 index。原文回读保留快照哈希。',
            '符号/调用/别名只是导航候选，不证明可达性、参数污点、鉴权或真实危害。',
            'Python 使用 AST；其他语言使用 Ctags 定义和词法引用，动态调用/反射/重绑定需人工核对。',
            '不执行项目、项目配置或在线解析器；UTF-8 文本可回读，跳过项见完整索引。']}
    (directory / 'index.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    return report


if __name__ == '__main__':
    value = run(json.loads(sys.argv[1]))
    print(json.dumps({'status': value['status'], 'coverage': value['coverage']}, ensure_ascii=False))
