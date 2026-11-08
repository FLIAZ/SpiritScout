"""最终赛事补丁包的离线双环境回归及不可由沙箱改写的哈希回执。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shlex
from pathlib import Path

from spiritscout.awdp.builder import _patched_bytes
from spiritscout.awdp.originals import (
    catalog,
    load_record,
    private_dir,
    resolve_ref,
    save_record,
    write_private,
)
from spiritscout.awdp.patches import (
    _member_path,
    _open_regular,
    _validate_archive,
    _validate_script,
)
from spiritscout.tools.sandbox import offline_execution
from spiritscout.tools.sandbox_paths import open_directory

SCHEMA = {'type': 'function', 'function': {'name': 'verify_patch',
    'description': 'AWDP 实际验证最终补丁包。Python 用 code/code_file；Ruby/Node 等优先用 command 参数数组与 expected_stdout，免写 Python 包装。工具已解包准备原件与修后目录，不要在测试中重建应用。断网执行最终入口，检查精确差异/权限/幂等。functional 修前修后均通过，security 修前断言失败、修后通过；运行错误/跳过/超时为未验证。回执绑定最终包与原件，不证明服务器部署或重启。',
    'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {
        'archive': {'type': 'string', 'description': 'build_patch 返回的完整 patch.tar.gz/update.tar.gz 或兼容裸脚本路径'},
        'source_prefix': {'type': 'string', 'description': '可选：原归档中实际部署的子目录，如 bundle；默认整个原件目录，不自动猜测'},
        'timeout': {'type': 'integer', 'minimum': 5, 'maximum': 30},
        'tests': {'type': 'array', 'minItems': 2, 'maxItems': 8, 'items': {'type': 'object',
            'additionalProperties': False, 'properties': {
                'name': {'type': 'string'}, 'kind': {'type': 'string', 'enum': ['functional', 'security'],
                    'description': 'functional 仅测原本合法行为，修前修后都须通过；本次新增约束/拒绝非法值放 security，修前断言失败且修后通过。两类不要混在一个测试组。'},
                'command': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 64,
                    'description': '直接在部署根执行的程序参数数组，如 ["ruby","-I","lib","-e","原生检查代码"]。调用真实应用并打印观测值，与 code/code_file 互斥；不用时省略或 []。程序须正常退出；预期异常由原生代码捕获后打印结果，不用退出错误当反例。'},
                'expected_stdout': {'type': 'string', 'maxLength': 2000,
                    'description': 'command 必填：期望标准输出（忽略末尾换行）。工具生成断言；security 修前输出不符、修后匹配。code/code_file 模式不使用。'},
                'code': {'type': 'string', 'description': '仅 Python 含 assert/unittest 的脚本；不接受 JavaScript/Ruby，不要 unittest.main。code/code_file/command 三选一。工具已准备两套程序；cwd/导入路径/AWDP_ROOT 即各自部署根。只测试 cwd 中真实程序，不自行解包/拷贝/重建应用，不用旧工作区路径。'},
                'code_file': {'type': 'string', 'description': '本题 workdir、/tmp、artifacts 中已有的 Python 测试文件；读取后冻结并预检，不放入提交包。未使用的 code/code_file 省略或填空字符串。'}},
            # 扁平 schema 兼容本地约束解码器，互斥关系由 normalize_tests 校验。
            'required': ['name', 'kind']}}},
            'required': ['archive', 'tests']}}}


class TestInputError(ValueError):
    """可恢复的测试输入错误；稳定分类用于识别改动转义但未解决根因的重试。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__('；'.join(
            f'{item["name"]}: {item["exception"]} 第 {item["line"]} 行：{item["message"]}' for item in errors)
            + '。测试尚未执行，补丁尚未验证。code/code_file 仅解析 Python，其他语言原生代码用 '
              'command 参数数组（如 node/ruby -e）与 expected_stdout；不用写 Python 包装或手动解包。'
              'Python 测试修复语法后可用 code_file 传入。'
              '不要重建补丁包来解决测试参数错误。')


def normalize_tests(root: Path, challenge: str, tests: list[dict]) -> list[dict]:
    normalized, errors = [], []
    for test in tests:
        provided = ({key for key in ('code', 'code_file', 'command')
                     if key in test and test[key] != ([] if key == 'command' else '')}
                    if isinstance(test, dict) else set())
        if (not isinstance(test, dict) or set(test) - {'name', 'kind', 'code', 'code_file', 'command', 'expected_stdout'}
                or not isinstance(test.get('name'), str) or not 1 <= len(test['name']) <= 100
                or test.get('kind') not in {'functional', 'security'}
                or len(provided) != 1):
            raise ValueError('测试组需要 name、kind；code/code_file 二选一，或使用 command + expected_stdout；不混用非空输入')
        code = test.get('code')
        if 'command' in provided:
            command, expected = test['command'], test.get('expected_stdout')
            if (not isinstance(command, list) or not 1 <= len(command) <= 64
                    or any(not isinstance(part, str) or '\0' in part for part in command) or not command[0]
                    or not isinstance(expected, str) or len(expected) > 2000):
                raise ValueError('command 需要非空程序参数数组与字符串 expected_stdout')
            code = ('import subprocess\n'
                f'_awdp_expected_stdout = {expected!r}\n'
                f'probe = subprocess.run({command!r}, capture_output=True, text=True)\n'
                'if probe.returncode != 0:\n'
                '    raise RuntimeError(f"测试命令未正常退出 {probe.returncode}: stdout={probe.stdout[-1000:]!r}; stderr={probe.stderr[-1000:]!r}")\n'
                'assert probe.stdout.rstrip("\\r\\n") == _awdp_expected_stdout, '
                'f"实际输出: {probe.stdout!r}; stderr: {probe.stderr[-1000:]}"\n')
        elif 'expected_stdout' in test and test['expected_stdout'] != '':
            raise ValueError(f'测试组 {test["name"]}：expected_stdout 只用于 command，不能与 code/code_file 混用。'
                'code 只接受 Python；若是 Ruby/Node 原生代码，放入 command 的 ruby/node -e 参数，'
                '移除 code/code_file。工具已准备修前修后的部署根，无需手动解包或重建应用。')
        if 'code_file' in provided:
            try:
                code = _patched_bytes(root, challenge, test['code_file'], limit=64000).decode('utf-8')
            except FileNotFoundError as exc:
                raise ValueError(f'测试文件尚未创建：{test["code_file"]}；先写入本题工作目录，再传真实路径。') from exc
            except UnicodeDecodeError as exc:
                raise ValueError('code_file 必须为 UTF-8 Python 测试文件') from exc
        if not isinstance(code, str) or not 1 <= len(code) <= 16000:
            raise ValueError('Python 测试代码须为 1–16000 字符')
        try:
            # 仅编译，不在宿主执行；此时不创建验证目录或应用补丁。
            compile(code, f'test:{test["name"]}', 'exec', dont_inherit=True)
        except (SyntaxError, ValueError, RecursionError) as exc:
            errors.append({'name': test['name'], 'kind': test['kind'], 'exception': type(exc).__name__,
                           'line': getattr(exc, 'lineno', 0), 'message': getattr(exc, 'msg', str(exc))})
        normalized.append({'name': test['name'], 'kind': test['kind'], 'code': code,
                           'input_format': 'command' if 'command' in provided else 'python'})
    if errors:
        raise TestInputError(errors)
    return normalized


def prepare(root: Path, challenge: str, attachments: list[str], args: dict) -> tuple[dict, Path]:
    if set(args) - {'archive', 'tests', 'source_prefix', 'timeout'}:
        raise ValueError('verify_patch 参数不符合契约')
    tests = args.get('tests')
    if not isinstance(tests, list) or not 2 <= len(tests) <= 8:
        raise ValueError('提供 2–8 个测试组，含 functional 和 security')
    tests = normalize_tests(root, challenge, tests)
    if {test['kind'] for test in tests} != {'functional', 'security'}:
        raise ValueError('需要正常功能与安全边界两类测试')
    if len({test['name'] for test in tests}) != len(tests):
        raise ValueError('测试组名称不能重复')
    timeout = args.get('timeout', 15)
    if not isinstance(timeout, int) or not 5 <= timeout <= 30:
        raise ValueError('timeout 必须为 5–30 秒')
    artifact = _patched_bytes(root, challenge, args.get('archive'), limit=4 * 1024 * 1024)
    digest = hashlib.sha256(artifact).hexdigest()
    try:
        build = load_record(root, challenge, 'builds', digest)
    except FileNotFoundError as exc:
        raise ValueError('verify_patch 支持本题 build_patch 生成且未改写的最终提交件；包/脚本修改后须重新构建验证') from exc
    output_format = build.get('format', 'shell_script')
    entrypoint = build.get('entrypoint', 'patch.sh')
    if output_format == 'tar_gzip':
        _validate_archive(io.BytesIO(artifact), root, challenge, digest,
                          tuple(item['source'] for item in build['mapping']), attachments)
    else:
        _validate_script(io.BytesIO(artifact))
    original = catalog(root, challenge, attachments)
    prefix = args.get('source_prefix', '')
    prefix = _member_path(prefix) + '/' if prefix else ''
    reference = 'verify-' + os.urandom(12).hex()
    directory = private_dir(challenge) / 'runs' / reference
    source_map = {item['source']: item for item in build['mapping']}
    deployment = {}
    for entry in original['files']:
        if not entry['source'].startswith(prefix):
            continue
        target = entry['source'][len(prefix):]
        if entry['source'] in source_map:
            mapping = source_map[entry['source']]
            if mapping['before_sha256'] != entry['sha256']:
                raise ValueError('构建后原始附件已变化，请重新审计/构建')
            target = mapping['target']
        if target in deployment:
            raise ValueError('部署映射冲突：' + target)
        deployment[target] = entry
    if not deployment or set(source_map) - {entry['source'] for entry in deployment.values()}:
        raise ValueError('source_prefix 没有覆盖全部修改文件')
    for phase in ('before', 'after'):
        job = directory / phase
        for target, entry in deployment.items():
            _, data = resolve_ref(root, challenge, entry['source_ref'], original)
            dest = job / 'sandbox/app' / target
            write_private(root, dest, data)
            with open_directory(root, dest.parent) as fd:
                os.chmod(dest.name, entry['mode'], dir_fd=fd, follow_symlinks=False)
        write_private(root, job / 'attachments' / ('patch.tar.gz' if output_format == 'tar_gzip' else 'patch.sh'), artifact)
        write_private(root, job / 'attachments/package.py', Path(__file__).with_name('package.py').read_bytes())
        for index, test in enumerate(tests):
            write_private(root, job / 'attachments' / f'{index}.py', test['code'].encode())
    receipt = {'verification_ref': reference, 'patch_sha256': digest,
        'format': output_format, 'entrypoint': entrypoint, 'default_root': build['default_root'],
        'invocation': 'sandbox_deployment_root_override',
        'catalog_ref': original['catalog_ref'], 'original_sha256': original['sha256'],
        'tests_sha256': hashlib.sha256(json.dumps(tests, sort_keys=True).encode()).hexdigest(),
        'mapping': build['mapping'], 'tests': tests, 'timeout': timeout, 'source_prefix': prefix,
        'network': 'disabled', 'status': 'inconclusive'}
    return receipt, directory


def classify(before: dict, after: dict, tests: list[dict]) -> str:
    for phase in (before, after):
        values = phase.get('tests', [])
        if phase.get('error') or len(values) != len(tests) or not phase.get('test_sources_unchanged'):
            return 'inconclusive'
        for item, test in zip(values, tests, strict=True):
            if (item.get('name') != test['name'] or item.get('kind') != test['kind']
                    or not isinstance(item.get('run'), int) or item['run'] <= 0
                    or any(item.get(key, 0) for key in ('errors', 'skipped', 'expected_failures', 'unexpected_successes'))):
                return 'inconclusive'
    if (after.get('application', {}).get('returncode') != 0
            or not after.get('exact_changes') or not after.get('idempotent')):
        return 'failed'
    for item in before['tests']:
        if item['kind'] == 'functional' and item.get('failures') != 0:
            return 'inconclusive'
        if item['kind'] == 'security' and not item.get('failures', 0):
            return 'inconclusive'
    return 'failed' if any(item.get('failures') != 0 for item in after['tests']) else 'passed'


def issues(reports: dict) -> list[str]:
    result = []
    for phase, report in reports.items():
        if report.get('error'):
            result.append(f'{phase}: 验证环境/进程错误：{report["error"]}')
        if phase == 'after' and 'application' in report:
            if report['application'].get('returncode') != 0:
                result.append('after: 最终脚本应用失败，需核对实际部署映射和执行日志')
            if not report.get('exact_changes'):
                result.append('after: 实际文件内容/权限变化与构建映射不符')
            if not report.get('idempotent'):
                result.append('after: 重复应用脚本失败或产生额外变化')
        if report.get('test_sources_unchanged') is False:
            result.append(f'{phase}: 测试修改了原始程序文件，不能作为修补证据。'
                '工具已准备真实部署目录；删除测试中写入/复制/重建应用的代码，仅调用该目录中的程序。')
        for test in report.get('tests', []):
            label = f'{phase}/{test["name"]}'
            native = test.get('command_result')
            detail = (json.dumps(native, ensure_ascii=False) if native else test.get('log', '')[-800:])
            if not test.get('run') and not test.get('errors'):
                result.append(label + ': 没有执行有效测试；需要实际执行 assert 或 unittest.TestCase.test_*，打印 PASS 不算测试')
            if test.get('errors'):
                hint = ('command 模式由工具比较 stdout 生成断言，不需要另写 Python。'
                    '修前修后命令都须正常退出；输出实际观测值，预期业务异常自行捕获，'
                    '不要 process.exit(1)/exit 1/raise 来表示输出不符。修前输出不匹配 expected_stdout 才是断言反例。'
                    if test.get('input_format') == 'command' else
                    'cwd/导入路径已是干净部署根，Python 应通过 import 调用原应用。'
                    '测试的 __file__ 指测试文件；exec(open(源码).read()) 会复用它并改变应用的相对路径语义。'
                    '不要改应用常量或猜 attachments 子目录来使测试通过。')
                result.append(label + ': 测试报错，不能当漏洞复现或通过。' + hint + '实际错误：'
                    + detail)
            if test.get('skipped') or test.get('expected_failures'):
                result.append(label + ': 存在跳过或预期失败，不计通过')
            if test.get('failures') and (phase == 'after' or test['kind'] == 'functional'):
                hint = ('原生命令输出与 expected_stdout 不符；对照 actual_stdout 核对逐字格式（含空格/引号）和调用参数。'
                        'functional 必须修前修后均通过；输出格式错误不需要重建补丁。' if native else
                        '原件的正常用例尚未通过。functional 必须修前修后均通过；'
                        '若失败断言属于本次新增约束/拒绝非法输入，应保留断言并移入 security 组，'
                        'functional 只测约定内原本正常的行为。否则核对测试调用、加载路径和环境。'
                        if phase == 'before' else '核对实际断言结果，修补须保持约定内行为并满足边界。')
                result.append(label + ': 断言失败；' + hint + '实际错误：' + detail)
            if phase == 'before' and test['kind'] == 'security' and not test.get('failures') and not test.get('errors'):
                result.append(label + ': 原件没有被安全断言证伪，尚不能证明此用例复现了漏洞')
    return result


def failure_profile(result: dict) -> list[str]:
    """复核用的失败类别，不等同于测试指纹或认定没有进展。"""
    categories = set()
    for phase, report in result.get('results', {}).items():
        if report.get('error'):
            categories.add(f'{phase}:environment_error')
        for test in report.get('tests', []):
            prefix = f'{phase}:{test.get("kind", "unknown")}'
            if test.get('errors'):
                categories.add(prefix + (':command_error' if test.get('command_result') else ':test_error'))
            if test.get('failures') and (phase == 'after' or test.get('kind') == 'functional'):
                categories.add(prefix + (':stdout_mismatch' if test.get('command_result') else ':assertion_failed'))
            if not test.get('run'):
                categories.add(prefix + ':no_checks')
            if test.get('skipped') or test.get('expected_failures'):
                categories.add(prefix + ':skipped')
            if phase == 'before' and test.get('kind') == 'security' and not test.get('failures'):
                categories.add(prefix + ':no_counterexample')
    return sorted(categories)


def failure_signature(result: dict) -> str:
    """输入语法错误按根因聚合；执行回执只归一随机编号，保留真实结果差异。"""
    if result.get('input_error') == 'test_syntax':
        # 改空白/引号后同一测试依旧不能编译，不能算验证取得进展。
        material = {'input_error': 'test_syntax', 'tests': [
            {key: item.get(key) for key in ('name', 'kind', 'exception')}
            for item in result.get('test_errors', [])]}
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    material = {key: result.get(key) for key in
                ('status', 'patch_sha256', 'original_sha256', 'tests_sha256', 'results')}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True)
    reference = result.get('verification_ref')
    if isinstance(reference, str) and reference:
        encoded = encoded.replace(reference, '<current-verification>')
    return hashlib.sha256(encoded.encode()).hexdigest()


async def verify_patch(agent, task, route, args: dict) -> dict:
    root = Path(agent.ctx.builtin_ctx['workspace_root']).resolve()
    challenge = agent.challenge_id(task)
    receipt, directory = await asyncio.to_thread(prepare, root, challenge,
        list(agent.challenge_of(task).get('attachments') or []), args)
    worker = Path(__file__).with_name('verify_worker.py').read_text(encoding='utf-8')
    receipt['worker_sha256'] = hashlib.sha256(worker.encode()).hexdigest()
    with _open_regular(root, directory / 'after/attachments/package.py') as stream:
        receipt['package_worker_sha256'] = hashlib.sha256(stream.read()).hexdigest()
    reports = {}
    for phase in ('before', 'after'):
        job = root / directory / phase
        config = {'inputs': str(job / 'attachments'), 'report': str(job / 'artifacts/report.json'),
            'patched': phase == 'after', 'mapping': receipt['mapping'],
            'format': receipt['format'], 'entrypoint': receipt['entrypoint'],
            # 源码已冻结在只读附件；命令仅传元数据，避免多组/中文长测试超过系统 argv 限制。
            'tests': [{key: test[key] for key in ('name', 'kind', 'input_format')} for test in receipt['tests']],
            'timeout': receipt['timeout']}
        command = 'python3 -I -c ' + shlex.quote(worker) + ' ' + shlex.quote(json.dumps(config))
        context = {**agent._tool_ctx(task), 'workspace_root': str(job), 'workspace_scope': None,
                   'guard_root': agent._tool_ctx(task).get('guard_root') or str(root), '_dedup_bypass': True}
        token = offline_execution.set(True)
        try:
            result = await agent.ctx.tools.call('sandbox_exec', {'code': command, 'language': 'shell',
                'timeout': min(300, receipt['timeout'] * (len(receipt['tests']) + 2) + 10),
                'action': 'analysis', 'log_name': 'verify_patch'}, **context)
        finally:
            offline_execution.reset(token)
        if not result.ok or result.returncode != 0:
            reports[phase] = {'error': result.error or result.stderr or '验证进程未正常完成',
                              'returncode': result.returncode}
        else:
            try:
                with _open_regular(root, directory / phase / 'artifacts/report.json') as stream:
                    reports[phase] = json.loads(stream.read(512 * 1024))
            except (OSError, ValueError) as exc:
                reports[phase] = {'error': str(exc)}
    receipt['reports'] = reports
    receipt['status'] = classify(reports['before'], reports['after'], receipt['tests'])
    save_record(root, challenge, 'verifications', receipt['verification_ref'], receipt)
    public = Path('artifacts') / ('awdp-' + receipt['verification_ref'] + '.json')
    # 公开报告与工具返回统一用 results；保留旧 reports 字段兼容已有读取者。
    write_private(root, public, json.dumps({**receipt, 'results': reports}, ensure_ascii=False, indent=2).encode())
    compact = {phase: dict(report, tests=[dict(item, log='' if item.get('command_result') else item.get('log', '')[-1000:])
               for item in report.get('tests', [])]) for phase, report in reports.items()}
    return {'ok': receipt['status'] == 'passed', 'executed': True, 'status': receipt['status'],
        'issues': issues(reports), 'verification_ref': receipt['verification_ref'],
        'patch_sha256': receipt['patch_sha256'], 'original_sha256': receipt['original_sha256'],
        'format': receipt['format'], 'entrypoint': receipt['entrypoint'],
        'tests_sha256': receipt['tests_sha256'], 'report': str(root / public), 'results': compact,
        'note': 'passed 绑定整个最终提交件，表示实际解包应用后这些测试通过且正常功能保持；'
                '使用部署根参数覆盖为干净副本，不证明实际服务器路径/命令白名单/服务重启或未测路径。'
                'failed/inconclusive 请阅读反例/环境错误并决定修法或补充测试；提交时可携带 verification_ref。'}


def check_receipt(root: Path, challenge: str, reference: str, patch_sha: str, attachments: list[str]) -> dict:
    receipt = load_record(root, challenge, 'verifications', reference)
    if receipt['patch_sha256'] != patch_sha:
        raise ValueError('verification_ref 对应另一份提交件；归档/脚本/修复文件变化后请重新 verify_patch')
    current = catalog(root, challenge, attachments)
    if current['sha256'] != receipt['original_sha256']:
        raise ValueError('验证后原始附件已变化；旧回执不能证明当前补丁')
    return receipt
