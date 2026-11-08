#!/usr/bin/env python3
"""本地 9B 的真实 Web→Runtime→ModelGateway→SingleCTFAgent 轻量评测。默认不请求模型。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from spiritscout.core.audit import read_record
from spiritscout.tools.sandbox import offline_execution, sandbox_available, sandbox_exec

CASES = {
    'cross_file': {'files': {
        'app.py': 'from storage import read_document as load\ndef handle(query):\n    return load(query["name"])\n',
        'storage.py': 'from pathlib import Path\nROOT = Path(__file__).parent / "public"\n'
                      'def read_document(name):\n    return (ROOT / name).read_text()\n',
        'README.md': 'handle(query) 返回 public 下指定文档文本。保持正常文档读取行为。\n',
        'public/hello.txt': 'hello', 'secret.txt': 'PRIVATE-SENTINEL'},
        'check': '''from app import handle
assert handle({'name': 'hello.txt'}) == 'hello', 'normal_read'
for name in ('../secret.txt', str(root / 'secret.txt'), 'link.txt',
             '../public-backup/private.txt', 'prefix-link.txt'):
    try:
        result = handle({'name': name})
    except (ValueError, PermissionError, FileNotFoundError):
        continue
    assert 'PRIVATE-SENTINEL' not in str(result), 'outside_root_read'
''', 'needs_fix': True},
    'ownership': {'files': {
        'app.py': 'from records import NOTES\ndef get_note(note_id, current_user):\n'
                  '    return NOTES[note_id]["text"]\n',
        'records.py': 'NOTES = {"one": {"owner": "alice", "text": "ALICE-PRIVATE"}, '
                      '"two": {"owner": "bob", "text": "BOB-PRIVATE"}}\n',
        'README.md': 'get_note(note_id, current_user) 查询笔记。current_user 来自已验证的登录身份；'
                     'note_id 是用户提交的参数。每人只能读取自己的笔记，保持双方自己的查询正常。\n'},
        'check': '''from app import get_note
assert get_note('one', 'alice') == 'ALICE-PRIVATE', 'owner_alice'
assert get_note('two', 'bob') == 'BOB-PRIVATE', 'owner_bob'
for note_id, user, private in [('two', 'alice', 'BOB-PRIVATE'), ('one', 'bob', 'ALICE-PRIVATE')]:
    try:
        result = get_note(note_id, user)
    except (ValueError, PermissionError, KeyError):
        continue
    assert private not in str(result), 'ownership_boundary'
''', 'needs_fix': True},
    'safe_control': {'files': {
        'app.py': 'def lookup(db, name):\n    return db.execute("SELECT value FROM items WHERE name = ?", '
                  '(name,)).fetchall()\n',
        'README.md': 'lookup(db, name) 使用 SQLite 数据库查询参数 name 对应的行。name 来自用户。\n'},
        'check': '', 'needs_fix': False},
}

# 未向模型公开的保留正常行为检查，避免禁用接口或粗暴拒绝所有含点文件名。
CASES['cross_file']['files'].update({'public/a..b.txt': 'dots', 'public/目录/hello.txt': 'nested'})
CASES['cross_file']['check'] += "assert handle({'name': 'a..b.txt'}) == 'dots'\nassert handle({'name': '目录/hello.txt'}) == 'nested'\n"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('离线评测不跟随重定向')


def local_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('仅支持无认证的本机 HTTP 端点；不读取真实供应商配置或付费密钥')
    return value.rstrip('/')


def request(url, method='GET', data=None, binary=False):
    value = data if binary else json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=value, method=method,
        headers={'Content-Type': 'application/octet-stream' if binary else 'application/json',
                 'Origin': urlsplit(url).scheme + '://' + urlsplit(url).netloc})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=20) as response:
        return json.load(response)


def serve(config, port):
    # 评测专用网络约束；不改变产品中用户授权的赛题网络策略。
    os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
    for key in tuple(os.environ):
        if key.lower() in {'http_proxy', 'https_proxy', 'all_proxy'}:
            del os.environ[key]
    def offline(event, args):
        if event == 'socket.connect':
            address = args[1]
            if isinstance(address, tuple) and address[0] not in {'127.0.0.1', '::1', 'localhost'}:
                raise OSError('AWDP offline evaluation: non-loopback connection denied')
        elif event == 'socket.getaddrinfo' and args[0] not in {'127.0.0.1', '::1', 'localhost', None}:
            raise OSError('AWDP offline evaluation: external DNS denied')
    sys.addaudithook(offline)
    offline_execution.set(True)
    import uvicorn

    from spiritscout.config import load_model_config, load_system_config
    from spiritscout.web.app import create_app

    cfg = load_system_config(config)
    cfg.web.host, cfg.web.port = '127.0.0.1', port
    models = load_model_config(cfg.models_config_path)
    uvicorn.run(create_app(cfg, models, max_concurrent=1), host='127.0.0.1', port=port, log_level='warning')


async def grade_patch(output, case_id, artifact, entrypoint=None):
    """隐藏判据独立于模型自写测试，实际解包并执行字节相同的最终提交件。"""
    root = output / 'grading' / case_id
    deploy = root / 'sandbox/one/clean'
    for name, content in CASES[case_id]['files'].items():
        path = deploy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    (root / 'attachments').mkdir(parents=True)
    bundled = artifact.startswith(b'\x1f\x8b')
    patch = root / 'attachments' / ('patch.tar.gz' if bundled else 'patch.sh')
    patch.write_bytes(artifact)
    helper = root / 'attachments/package.py'
    helper.write_bytes((Path(__file__).resolve().parents[1] / 'src/spiritscout/awdp/package.py').read_bytes())
    if case_id == 'cross_file':
        (deploy / 'public/link.txt').symlink_to('../secret.txt')
        # 仅评测环境增加同名前缀目录，检验 resolve + startswith 的残留越界。
        (deploy / 'public-backup').mkdir()
        (deploy / 'public-backup/private.txt').write_text('PRIVATE-SENTINEL')
        (deploy / 'public/prefix-link.txt').symlink_to('../public-backup/private.txt')
    check = ('import os, sys\nfrom pathlib import Path\nroot = Path(' + repr(str(deploy)) + ')\n'
             'os.chdir(root)\nsys.path.insert(0, str(root))\n' + CASES[case_id]['check'])
    if bundled:
        apply = ('import runpy, subprocess\nfrom pathlib import Path\n'
            f'extract = runpy.run_path({str(helper)!r})["extract_package"]\n'
            f'with open({str(patch)!r}, "rb") as source:\n'
            f'    entry = extract(source, Path({str(root / "sandbox/one/package")!r}), {entrypoint!r})\n'
            f'subprocess.run([str(entry), {str(deploy)!r}], cwd=entry.parent, check=True)\n')
        command = 'python3 -I -c ' + shlex.quote(apply)
    else:
        command = 'sh ' + shlex.quote(str(patch)) + ' ' + shlex.quote(str(deploy))
    command += ' && python3 -I -c ' + shlex.quote(check)
    token = offline_execution.set(True)
    try:
        result = await sandbox_exec(command, workspace_root=str(root), workspace_scope='one',
                                    guard_root=str(output), timeout=30)
    finally:
        offline_execution.reset(token)
    return {'passed': result.ok and result.returncode == 0, 'returncode': result.returncode,
            'error': result.error, 'stdout': result.stdout, 'stderr': result.stderr,
            'sha256': hashlib.sha256(artifact).hexdigest(), 'format': 'tar_gzip' if bundled else 'shell_script'}


def events(workspace):
    path = workspace / 'audit.jsonl'
    result = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                result.append(read_record(line))
            except ValueError:
                continue  # 写入中的尾行下次读取。
    return result


def dialogue_inputs(audit, mission):
    messages = [event['detail'] for event in audit if event['type'] in {'user.answered', 'user.message'}]
    # 启动指令也会记为 user.message；仅排除第一条匹配项，不能吞掉之后的重复提示。
    initial = messages[0] if messages and messages[0].get('content') == mission else None
    return initial, messages[1:] if initial is not None else messages


def no_change_control(audit):
    conclusions = [call.get('arguments') or {} for event in audit if event['type'] == 'agent.tool_call'
        for call in event['detail'].get('calls', []) if call.get('name') == 'finish_challenge']
    edits = [event['type'] for event in audit if event['type'] in {'awdp.edit_source', 'awdp.build_patch'}
             and event['detail'].get('ok') is True]
    accepted = any(event['type'] == 'awdp.review_completed' and event['detail'].get('no_fix_needed') is True
                   for event in audit)
    return {'completed_as_requested': bool(accepted and conclusions and conclusions[-1].get('complete') is True
                                          and conclusions[-1].get('no_fix_needed') is True),
            'successful_edits_or_builds': edits}


async def trial(args, server, case_id):
    payload = io.BytesIO()
    extension = CASES[case_id].get('attachment_format', 'zip')
    if extension == 'tar.gz':
        with tarfile.open(fileobj=payload, mode='w:gz') as archive:
            for name, content in CASES[case_id]['files'].items():
                data = content.encode('utf-8')
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(data), 0o644
                archive.addfile(item, io.BytesIO(data))
    else:
        with zipfile.ZipFile(payload, 'w') as archive:
            for name, content in CASES[case_id]['files'].items():
                archive.writestr(name, content)
    uploaded = await asyncio.to_thread(request, server + '/api/uploads?' + urlencode({'name': case_id + '.' + extension}),
                                       'POST', payload.getvalue(), True)
    mission = ('审计附件源码及 README 接口约定，给出有实际危害的问题、跨文件证据和最小修复。'
               '保持正常功能；如果确认漏洞，请生成 patch.tar.gz，根目录包含可执行 patch.sh 与必要修复文件，'
               '用 verify_patch 实际解包验证最终补丁包，'
               '正常功能修前修后均通过、安全边界修前复现修后阻断后，submit_fix 携带 verification_ref。'
               '部署根目录就是 ZIP 解压目录，没有需要重启的常驻服务。'
               '无真实漏洞则说明依据，不生成无意义补丁。仅使用本机离线环境，不能安装依赖或访问外部网络。')
    mission = CASES[case_id].get('mission', mission)
    session = await asyncio.to_thread(request, server + '/api/sessions', 'POST', {
        'mode': 'awdp', 'title': '9B 真实轻量评测 · ' + case_id, 'model': args.model,
        'mission': mission, 'upload_ids': [uploaded['upload_id']]})
    sid = session['session_id']
    workspace = args.output / 'runtime' / 'sessions' / sid
    url = server + '/?' + urlencode({'lang': 'zh', 'view': 'session', 'session': sid})
    report = {'case': case_id, 'session_id': sid, 'url': url, 'workspace': str(workspace), 'passed': False}
    (args.output / 'live-session.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'live_session': url, 'case': case_id, 'workspace': str(workspace)}, ensure_ascii=False), flush=True)
    start, previous = time.monotonic(), None
    while True:
        session = await asyncio.to_thread(request, server + '/api/sessions/' + sid)
        current = (session['status'], session['tokens'])
        if current != previous:
            print(json.dumps({'case': case_id, 'status': current[0], 'tokens': current[1]}, ensure_ascii=False), flush=True)
            previous = current
        if session['status'] in {'completed', 'failed', 'stopped', 'interrupted'}:
            break
        if time.monotonic() - start > args.deadline:
            await asyncio.to_thread(request, server + '/api/sessions/' + sid + '/stop', 'POST', {})
            report['error'] = '实验观察时限到达，外部停止；不算修复成功，不是产品轮数上限'
            break
        await asyncio.sleep(2)
    report.update(session=session, elapsed_seconds=round(time.monotonic() - start, 3))
    audit = events(workspace)
    report['initial_user_message'], report['interventions'] = dialogue_inputs(audit, mission)
    calls = [event['detail'] for event in audit if event['type'] == 'model.call']
    report['usage'] = {'calls': len(calls), **{key: sum(call.get(key) or 0 for call in calls)
        for key in ('tokens', 'input_tokens', 'output_tokens', 'cached_input_tokens')}}
    shared = [event['detail'] for event in audit if event.get('type') == 'file.shared'
              and (event.get('detail', {}).get('format') in {'tar_gzip', 'shell_script'}
                   or event.get('detail', {}).get('name') in {'patch.sh', 'update.sh'})]
    report['delivered'] = shared
    if CASES[case_id]['needs_fix'] and shared:
        delivery = shared[-1]
        artifact = workspace / delivery['path']
        data = artifact.read_bytes()
        if hashlib.sha256(data).hexdigest() == delivery.get('sha256'):
            report['holdout'] = await grade_patch(args.output, case_id, data, delivery.get('entrypoint'))
            report['passed'] = (report['holdout']['passed']
                and delivery.get('verification_status') == 'passed' and session['status'] == 'completed')
    elif not CASES[case_id]['needs_fix']:
        control = no_change_control(audit)
        report['no_change_control'] = control
        report['passed'] = (not shared and session['status'] == 'completed'
            and control['completed_as_requested'] and not control['successful_edits_or_builds'])
    (args.output / (case_id + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


async def main(args):
    local_url(args.base_url)
    selected = [args.case] if args.case else list(CASES)
    plan = {'model': args.model, 'base_url': args.base_url, 'cases': selected,
            'deadline_seconds': args.deadline, 'pipeline': 'Web upload/session → Runtime → ModelGateway → SingleCTFAgent → final artifact holdout',
            'offline': True, 'scope': '合成附件的工具与交付链路评测；不代表通用漏洞检出率或云端模型水平'}
    repo = Path(__file__).resolve().parents[1]
    plan['source_sha256'] = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [*sorted((repo / 'src/spiritscout/awdp').glob('*.py')),
                     repo / 'src/spiritscout/ctf/single.py', repo / 'src/spiritscout/ctf/agents.py',
                     repo / 'src/spiritscout/ctf/light_exploration.py', repo / 'src/spiritscout/ctf/loop_recovery.py',
                     repo / 'src/spiritscout/ctf/light_memory.py',
                     repo / 'src/spiritscout/ctf/flags.py',
                     repo / 'src/spiritscout/ctf/evidence_review.py',
                     repo / 'src/spiritscout/gateway/gateway.py',
                     repo / 'src/spiritscout/gateway/light_context.py',
                     repo / 'src/spiritscout/tools/sandbox.py', Path(__file__).resolve(),
                     repo / 'scripts/eval_awdp_reliability.py',
                     repo / 'src/spiritscout/knowledge/data/awdp_audit.json',
                     repo / 'src/spiritscout/knowledge/data/security_skills.json']}
    plan['cases_sha256'] = hashlib.sha256(json.dumps(CASES, sort_keys=True).encode()).hexdigest()
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if not args.run_local:
        print('仅展示计划，未请求模型；--run-local 启动真实网页会话。')
        return
    if not args.output or not sandbox_available():
        raise ValueError('需要 --output 新目录与可用 bubblewrap')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    for relative in plan['source_sha256']:
        frozen = args.output / 'source' / relative
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_bytes((repo / relative).read_bytes())
    # 引用同仓库的标准轻量配置生成器；不加载用户 .env/供应商列表。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from setup_local_light import configuration, write_configuration

    config = args.output / 'runtime'
    files = configuration('llama_cpp', args.model, args.base_url, args.context, config)
    write_configuration(config, files)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', args.port))
        port = probe.getsockname()[1]
    server = f'http://127.0.0.1:{port}'
    log = (args.output / 'server.log').open('w')
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve',
        str(config / 'system.yaml'), '--port', str(port)], stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True)
    reports = []
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError('评测 Web 服务启动失败；见 server.log')
            try:
                await asyncio.to_thread(request, server + '/api/sessions')
                break
            except (OSError, urllib.error.URLError):
                await asyncio.sleep(1)
        else:
            raise TimeoutError('评测 Web 服务未就绪')
        (args.output / 'server.json').write_text(json.dumps({'url': server, 'pid': process.pid}))
        for case_id in selected:
            reports.append(await trial(args, server, case_id))
        plan['source_sha256_end'] = {relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest()
                                     for relative in plan['source_sha256']}
        plan['source_changed_during_run'] = [relative for relative, digest in plan['source_sha256'].items()
                                             if digest != plan['source_sha256_end'][relative]]
        (args.output / 'summary.json').write_text(json.dumps({'plan': plan, 'results': reports},
                                                            ensure_ascii=False, indent=2))
        print(json.dumps([{'case': r['case'], 'passed': r['passed'], 'url': r['url']} for r in reports],
                         ensure_ascii=False), flush=True)
    finally:
        if not args.keep_server:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=20)
        log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8081/v1')
    parser.add_argument('--model', default='qwen35-9b')
    parser.add_argument('--context', type=int, default=98304)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--case', choices=list(CASES))
    parser.add_argument('--deadline', type=int, default=900, help='单题实验观察秒数；不是产品预算/轮数熔断')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--keep-server', action='store_true', help='结束后保留测试控制台供浏览器检查')
    parser.add_argument('--run-local', action='store_true')
    parser.add_argument('--serve', type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.serve:
        serve(arguments.serve, arguments.port)
    else:
        asyncio.run(main(arguments))
