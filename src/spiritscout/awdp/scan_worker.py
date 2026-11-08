"""在既有沙箱中运行本地 Semgrep；只解析源码，不导入或执行附件。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

EXTENSIONS = {'.py': 'python', '.php': 'php', '.js': 'javascript', '.jsx': 'javascript',
              '.ts': 'typescript', '.tsx': 'typescript', '.java': 'java', '.go': 'go',
              '.rb': 'ruby', '.c': 'c', '.h': 'c', '.cc': 'cpp', '.cpp': 'cpp', '.hpp': 'cpp'}
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 4096
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def _save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def snapshot(source: Path, directory: Path) -> tuple[dict, list[dict]]:
    """扁平快照避免附件自带 ignore 配置隐藏文件；保留逐文件原路径和内容哈希。"""
    mapping, skipped, total, count = {}, [], 0, 0
    targets = directory / 'targets'
    targets.mkdir()
    if source.is_file():
        candidates = iter([source])
    else:
        def walk():
            def error(exc):
                skipped.append({'path': str(exc.filename), 'reason': 'unreadable_directory'})

            for current, dirs, files in os.walk(source, followlinks=False, onerror=error):
                dirs.sort()
                for name in list(dirs):
                    path = Path(current) / name
                    if path.is_symlink() or name == '.git' or path == directory.parent:
                        dirs.remove(name)
                        skipped.append({'path': str(path), 'reason': 'symlink_or_scan_metadata'})
                yield from (Path(current) / name for name in sorted(files))
        candidates = walk()
    for path in candidates:
        count += 1
        if count > MAX_FILES:
            skipped.append({'path': str(source), 'reason': 'inventory_limit', 'remaining_unknown': True})
            break
        suffix = path.suffix.lower()
        reason = ('symlink' if path.is_symlink() else 'unsupported_extension' if suffix not in EXTENSIONS else '')
        if reason:
            skipped.append({'path': str(path), 'reason': reason})
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('not_regular')
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError('file_size_limit')
            if total + len(data) > MAX_TOTAL_BYTES:
                skipped.append({'path': str(source), 'reason': 'total_size_limit', 'remaining_unknown': True})
                break
            total += len(data)
            # 不复制 .semgrepignore、构建脚本配置或凭据；默认忽略规则不会漏掉 tests/vendor 内源码。
            name = f'{len(mapping):05d}{suffix}'
            (targets / name).write_bytes(data)
            mapping['targets/' + name] = {'path': str(path), 'language': EXTENSIONS[suffix],
                'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        except (OSError, ValueError) as exc:
            skipped.append({'path': str(path), 'reason': str(exc)[:300]})
    return mapping, skipped


def run(args: dict) -> dict:
    directory = Path(args['directory'])
    source = Path(args['path'])
    report = {'scanner': 'semgrep', 'engine': 'community', 'status': 'unavailable',
        'source': str(source), 'findings': [], 'errors': [], 'coverage': {'complete': False},
        'limitations': ['结果仅为候选，不能证明可利用或修复有效；零命中不代表安全。',
            '本地规则只覆盖部分入口及危险操作；社区引擎不提供本工具所需的跨文件完整调用链。',
            '鉴权/业务逻辑、运行配置、动态反射与未匹配框架由 Agent 继续审计。']}
    report['rules_sha256'] = hashlib.sha256(json.dumps(args['rules'], sort_keys=True).encode()).hexdigest()
    executable = shutil.which('semgrep')
    if not executable:
        report['errors'] = [{'message': '沙箱未安装 semgrep；安装可选 .[audit] 后可用，本次没有完成扫描。'}]
        _save(directory / 'report.json', report)
        return report
    mapping, skipped = snapshot(source, directory)
    _save(directory / 'inventory.json', {'files': mapping, 'skipped': skipped})
    report['coverage'] = {'eligible_files': len(mapping), 'scanned_files': 0,
        'skipped_files': len(skipped), 'inventory': str(directory / 'inventory.json'), 'complete': False}
    if not mapping:
        report.update(status='no_targets', errors=[{'message': '没有可扫描源码；压缩包需先在本题 workdir 安全解包。'}])
        _save(directory / 'report.json', report)
        return report
    _save(directory / 'rules.json', args['rules'])
    raw_path = directory / 'semgrep.json'
    command = [executable, 'scan', '--config', str(directory / 'rules.json'), '--oss-only',
        '--metrics=off', '--disable-version-check', '--disable-nosem', '--no-git-ignore',
        '--no-rewrite-rule-ids', '--jobs=1', '--timeout=8', '--max-memory=512',
        '--max-target-bytes=' + str(MAX_FILE_BYTES), '--json', '--quiet', 'targets']
    environment = {**os.environ, 'SEMGREP_SEND_METRICS': 'off', 'SEMGREP_ENABLE_VERSION_CHECK': '0',
        'SEMGREP_SETTINGS_FILE': str(directory / 'settings.yml')}
    environment.pop('SEMGREP_APP_TOKEN', None)
    timed_out = False
    with raw_path.open('wb') as output, (directory / 'stderr.log').open('wb') as errors:
        try:
            process = subprocess.run(command, cwd=directory, env=environment, stdout=output,
                stderr=errors, timeout=args['timeout'], check=False)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            timed_out, returncode = True, -1
    report.update(returncode=returncode, timed_out=timed_out, raw_report=str(raw_path),
                  stderr=str(directory / 'stderr.log'), command=command, status='failed')
    try:
        if raw_path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('原始扫描报告过大，需缩小扫描范围；完整原件已保留')
        raw = json.loads(raw_path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or not isinstance(raw.get('results'), list):
            raise ValueError('扫描器返回的 JSON 缺少 results')
        report['version'] = raw.get('version')
        report['errors'] = raw.get('errors') or []
        scanned = {str(Path(p)) for p in (raw.get('paths') or {}).get('scanned', [])}
        missed = sorted(set(mapping) - scanned)
        report['coverage'].update(scanned_files=len(set(mapping) & scanned),
            engine_skipped=missed, complete=not missed and not skipped and not report['errors'] and returncode == 0)
        for index, item in enumerate(raw['results']):
            key = str(Path(item['path']))
            if key not in mapping:
                report['errors'].append({'message': '扫描结果路径不在输入快照中', 'path': key})
                continue
            extra = item.get('extra') or {}
            start, end = int(item['start']['line']), int(item['end']['line'])
            lines = (directory / key).read_text(encoding='utf-8', errors='replace').splitlines()
            report['findings'].append({'id': f'finding-{index + 1}', 'rule_id': item['check_id'],
                **mapping[key], 'start_line': start, 'end_line': end,
                'severity': extra.get('severity'), 'message': extra.get('message'),
                'metadata': extra.get('metadata') or {},
                'snippet': '\n'.join(lines[max(0, start - 1):min(end, start + 19)])[:2400],
                'snippet_partial': end - start >= 20 or sum(len(s) for s in lines[max(0, start - 1):end]) > 2400,
                'dataflow_available': bool(extra.get('dataflow_trace')),
                'raw_result_index': index, 'verification_status': 'unverified'})
        if returncode == 0 and not timed_out:
            report['status'] = 'completed' if not report['errors'] and not missed else 'partial'
        report['coverage']['complete'] = report['coverage']['complete'] and not report['errors']
    except (OSError, ValueError, TypeError, KeyError) as exc:
        report['errors'].append({'message': '扫描报告不完整或无法解析：' + str(exc)[:500]})
    _save(directory / 'report.json', report)
    return report


if __name__ == '__main__':
    request = json.loads(sys.argv[1])
    try:
        result = run(request)
        print(json.dumps({'status': result['status'], 'report': str(Path(request['directory']) / 'report.json')}))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error': str(error)[:500]}))
        sys.exit(1)
