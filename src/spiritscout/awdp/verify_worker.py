"""在独立 bubblewrap 中解包最终提交件并执行冻结的断言/unittest；仅使用标准库。"""
from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
import subprocess
import sys
from pathlib import Path

TEST_RUNNER = r'''
import ast, contextlib, io, json, os, sys, traceback, types, unittest
from pathlib import Path
root = Path(sys.argv[1])
os.chdir(root)
os.environ['AWDP_ROOT'] = str(root)
sys.path.insert(0, str(root))
module = types.ModuleType('__main__')
module.__file__ = sys.argv[2]
capture = io.StringIO()
assertions = []
exception_checks = []
def record_assertion(line):
    assertions.append(line)
def record_exception_check(line):
    exception_checks.append(line)
class TrackAssertions(ast.NodeTransformer):
    def visit_Assert(self, node):
        marker = ast.Expr(ast.Call(ast.Name('_awdp_record_assertion', ast.Load()),
                                  [ast.Constant(node.lineno)], []))
        return [ast.copy_location(marker, node), node]
    def visit_Try(self, node):
        # 标准负向断言：无异常时末尾 assert False 失败，指定异常被捕获时检查成立。
        # 仅计实际进入的有类型 handler；不推测任意分支、空函数或单纯打印的含义。
        terminal = node.body[-1] if node.body else None
        expected = (isinstance(terminal, ast.Assert) and isinstance(terminal.test, ast.Constant)
                    and terminal.test.value is False)
        node = self.generic_visit(node)
        if expected:
            for handler in node.handlers:
                if handler.type is not None:
                    marker = ast.Expr(ast.Call(ast.Name('_awdp_record_exception_check', ast.Load()),
                                              [ast.Constant(handler.lineno)], []))
                    handler.body.insert(0, ast.copy_location(marker, handler))
        return node
module._awdp_record_assertion = record_assertion
module._awdp_record_exception_check = record_exception_check
try:
    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
        tree = ast.parse(Path(sys.argv[2]).read_bytes(), filename=sys.argv[2])
        tree = ast.fix_missing_locations(TrackAssertions().visit(tree))
        exec(compile(tree, sys.argv[2], 'exec'), module.__dict__)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        result = (unittest.TextTestRunner(stream=capture, verbosity=2).run(suite)
                  if suite.countTestCases() else unittest.TestResult())
    output = {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'expected_failures': len(result.expectedFailures),
        'unexpected_successes': len(result.unexpectedSuccesses), 'log': capture.getvalue()[-7000:]}
    if result.testsRun == 0 and (assertions or exception_checks):
        output.update(run=1, format='assert_script', assertions=len(assertions),
                      expected_exception_checks=len(exception_checks))
except AssertionError as exc:
    # 顶层断言脚本也可作为一个用例；只有实际执行过 assert 才认定断言失败。
    output = {'run': 1 if assertions else 0, 'failures': 1 if assertions else 0,
              'errors': 0 if assertions else 1, 'format': 'assert_script',
              'assertions': len(assertions), 'log': (capture.getvalue() + traceback.format_exc())[-7000:]}
except BaseException as exc:
    output = {'run': 0, 'errors': 1, 'log': (capture.getvalue() + traceback.format_exc())[-7000:]}
if len(sys.argv) > 3 and sys.argv[3] == 'command' and hasattr(module, 'probe'):
    output['command_result'] = {'returncode': module.probe.returncode,
        'actual_stdout': module.probe.stdout[-2000:], 'stderr': module.probe.stderr[-1000:],
        'expected_stdout': module._awdp_expected_stdout}
print(json.dumps(output))
'''


def manifest(root):
    result = {}
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('补丁生成了非普通文件：' + str(path.relative_to(root)))
        result[path.relative_to(root).as_posix()] = {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': stat.S_IMODE(mode)}
    return result


def run(config):
    root, inputs = Path.cwd() / 'app', Path(config['inputs'])
    before = manifest(root)
    expected = before
    result = {'environment': {'python': sys.version.split()[0], 'platform': sys.platform,
                              'deployment_root': str(root)}, 'tests': []}
    if config['patched']:
        if config.get('format') == 'tar_gzip':
            extract = runpy.run_path(str(inputs / 'package.py'))['extract_package']
            with (inputs / 'patch.tar.gz').open('rb') as source:
                entry = extract(source, Path.cwd() / 'package', config['entrypoint'])
            command, cwd = [str(entry), str(root)], entry.parent
        else:
            command, cwd = ['sh', str(inputs / 'patch.sh'), str(root)], root
        patch = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=config['timeout'])
        after = manifest(root)
        result['application'] = {'returncode': patch.returncode, 'log': (patch.stdout + patch.stderr)[-3000:],
                                 'format': config.get('format', 'shell_script'),
                                 'entrypoint': config.get('entrypoint', 'patch.sh'),
                                 'cwd': str(cwd), 'deployment_root_override': str(root)}
        expected = dict(before)
        for item in config['mapping']:
            target = item['target']
            if target not in before or before[target]['sha256'] != item['before_sha256']:
                raise ValueError('部署映射与原件不一致：' + target)
            expected[target] = dict(before[target], sha256=item['after_sha256'])
        result['exact_changes'] = after == expected
        # 重跑也必须不改变文件内容或权限。
        repeated = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=config['timeout'])
        result['idempotent'] = repeated.returncode == 0 and manifest(root) == after
        if patch.returncode or not result['exact_changes'] or not result['idempotent']:
            return result
    for index, test in enumerate(config['tests']):
        try:
            child = subprocess.run([sys.executable, '-I', '-c', TEST_RUNNER, str(root), str(inputs / f'{index}.py'),
                                    test.get('input_format', 'python')],
                                   capture_output=True, text=True, timeout=config['timeout'],
                                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            item = json.loads(child.stdout) if child.returncode == 0 else {'run': 0, 'errors': 1}
            if child.returncode:
                item['log'] = (child.stdout + child.stderr)[-5000:]
        except (subprocess.TimeoutExpired, ValueError) as exc:
            item = {'run': 0, 'errors': 1, 'log': str(exc)[-5000:]}
        result['tests'].append(dict(item, name=test['name'], kind=test['kind'],
                                   input_format=test.get('input_format', 'python')))
    # 测试可创建夹具，但不能在测试中偷偷修源码或删除原文件来制造通过。
    result['test_sources_unchanged'] = all(
        (root / name).is_file() and not (root / name).is_symlink()
        and hashlib.sha256((root / name).read_bytes()).hexdigest() == value['sha256']
        and stat.S_IMODE((root / name).stat().st_mode) == value['mode']
        for name, value in expected.items())
    return result


def main(config):
    try:
        result = run(config)
    except Exception as exc:
        result = {'error': f'{type(exc).__name__}: {exc}'}
    Path(config['report']).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main(json.loads(sys.argv[1]))
