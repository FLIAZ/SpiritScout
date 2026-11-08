"""在断网沙箱中检查冻结源码语法；不加载应用或安装依赖。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def check(path: Path, source: str) -> dict:
    suffix = Path(source).suffix.lower()
    commands = {'.rb': ['ruby', '--disable-gems', '-c'], '.php': ['php', '-n', '-l'],
                '.sh': ['sh', '-n'], '.bash': ['bash', '--noprofile', '--norc', '-n']}
    result = {'source': source, 'status': 'skipped'}
    try:
        if suffix in {'.py', '.json'}:
            data = path.read_bytes()
            if suffix == '.py':
                compile(data, source, 'exec', dont_inherit=True)
            else:
                json.loads(data)
            return {**result, 'status': 'passed', 'checker': sys.version.split()[0]}
        if suffix not in commands:
            return {**result, 'reason': '此文件类型需按项目构建/测试约定检查'}
        command = commands[suffix]
        executable = shutil.which(command[0])
        if not executable:
            return {**result, 'reason': f'本地缺少 {command[0]}，语法尚未检查'}
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}
        run = subprocess.run([executable, *command[1:], str(path)], env=env,
                             capture_output=True, timeout=8)
        log = (run.stdout + run.stderr).decode('utf-8', errors='replace')
        return {**result, 'status': 'passed' if run.returncode == 0 else 'failed',
                'checker': command, 'returncode': run.returncode, 'log': log[-3000:]}
    except (SyntaxError, ValueError, UnicodeError, RecursionError) as exc:
        return {**result, 'status': 'failed', 'error': str(exc)[:3000]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**result, 'status': 'error', 'error': str(exc)[:1000]}


def main():
    inputs, report = map(Path, sys.argv[1:])
    items = json.loads((inputs / 'inputs.json').read_text())
    results = [check(inputs / item['file'], item['source']) for item in items]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({'files': results}, ensure_ascii=False))


if __name__ == '__main__':
    main()
