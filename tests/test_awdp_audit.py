"""AWDP 源码扫描、技能接入与实际 patch.sh 应用；不调用模型或在线规则。"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_single_executor import context

from spiritscout.awdp.builder import build_patch
from spiritscout.awdp.package import extract_package, pack, read_package
from spiritscout.awdp.patches import validate_patch
from spiritscout.awdp.scan_worker import run, snapshot
from spiritscout.awdp.scanner import _source_path, read_report
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.flags import FlagManager
from spiritscout.ctf.single import SingleCTFAgent
from spiritscout.resources.assistant import validate_skill
from spiritscout.resources.runtime import SkillRuntime
from spiritscout.resources.store import ResourceStore, builtin_skills
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.sandbox import sandbox_available, sandbox_exec
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolSpec


class PatchBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'sandbox' / 'one'
        self.work.mkdir(parents=True)
        self.attachments = self.root / 'attachments'
        self.attachments.mkdir()

    def original(self, kind, content=b'old\n'):
        name = 'bundle/src/app.py'
        if kind == 'folder':
            path = self.attachments / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        elif kind == 'file':
            name, path = 'app.py', self.attachments / 'app.py'
            path.write_bytes(content)
        elif kind == 'zip':
            path = self.attachments / 'source.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr(name, content)
        else:
            path = self.attachments / ('source.' + kind)
            mode = {'tar.gz': 'w:gz', 'tar.xz': 'w:xz', 'tar.bz2': 'w:bz2'}[kind]
            with tarfile.open(path, mode) as archive:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return name, path

    def build(self, original, attachment, content=b'fixed\n', **kwargs):
        (self.work / 'fixed.py').write_bytes(content)
        mapping = {'source': original, 'patched': 'fixed.py'}
        if 'target' in kwargs:
            mapping['target'] = kwargs.pop('target')
        return build_patch(self.root, 'one', [str(attachment)], {'files': [mapping], **kwargs})

    def apply(self, result, *args):
        if result['format'] == 'shell_script':
            return subprocess.run(['sh', result['archive'], *map(str, args)], capture_output=True, check=False)
        with tempfile.TemporaryDirectory(dir=self.root) as unpacked:
            with Path(result['archive']).open('rb') as source:
                entry = extract_package(source, Path(unpacked) / 'package', result['entrypoint'])
            return subprocess.run([str(entry), *map(str, args)], cwd=entry.parent,
                                  capture_output=True, check=False)

    def test_all_attachment_formats_preserve_paths_and_permissions_and_allow_reapply(self):
        for kind in ('zip', 'tar.gz', 'tar.bz2', 'tar.xz', 'file', 'folder'):
            with self.subTest(kind=kind):
                name, attachment = self.original(kind)
                result = self.build(name, attachment)
                self.assertFalse(result['executed'])
                target = self.root / ('deploy-' + kind) / name
                target.parent.mkdir(parents=True)
                target.write_bytes(b'old\n')
                target.chmod(0o640)
                applied = self.apply(result, self.root / ('deploy-' + kind))
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertEqual(target.read_bytes(), b'fixed\n')
                self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                self.assertEqual(self.apply(result, self.root / ('deploy-' + kind)).returncode, 0)
                published = validate_patch(self.root, 'one', result['archive'], result['files'], [str(attachment)])
                self.assertEqual(published.path.name, 'patch.tar.gz')
                self.assertEqual(published.path.read_bytes(), Path(result['archive']).read_bytes())

    def test_competition_layouts_and_legacy_script_apply_without_extra_arguments(self):
        name, attachment = self.original('zip')
        for index, options in enumerate(({}, {'entrypoint': 'update.sh'},
                {'entrypoint': 'update.sh', 'package_dir': 'update'}, {'format': 'shell_script'})):
            with self.subTest(options=options):
                deploy = self.root / f'deploy-{index}'
                deploy.mkdir()
                (deploy / 'app.py').write_bytes(b'old\n')
                (deploy / 'app.py').chmod(0o640)
                result = self.build(name, attachment, target='app.py', default_root=str(deploy), **options)
                data = Path(result['archive']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), result['sha256'])
                self.assertEqual(self.build(name, attachment, target='app.py', default_root=str(deploy),
                                            **options)['sha256'], result['sha256'])
                if result['format'] == 'tar_gzip':
                    members = read_package(io.BytesIO(data))
                    prefix = options.get('package_dir', '')
                    prefix = prefix + '/' if prefix else ''
                    self.assertEqual(set(members), {result['entrypoint'], prefix + 'files/' + name})
                    self.assertEqual(members[result['entrypoint']][1], 0o755)
                    self.assertNotIn(b'\r', members[result['entrypoint']][0])
                    self.assertEqual(members[prefix + 'files/' + name][1], 0o644)
                applied = self.apply(result)
                self.assertEqual(applied.returncode, 0, applied.stderr)
                self.assertEqual((deploy / 'app.py').read_bytes(), b'fixed\n')
                self.assertEqual((deploy / 'app.py').stat().st_mode & 0o777, 0o640)
                published = validate_patch(self.root, 'one', result['archive'], result['files'], [str(attachment)])
                self.assertEqual(published.path.name, Path(result['archive']).name)
                self.assertEqual(published.path.read_bytes(), data)

    def test_invalid_layout_and_modified_payload_are_rejected(self):
        name, attachment = self.original('zip')
        for options in ({'format': 'zip'}, {'entrypoint': '../patch.sh'}, {'package_dir': '../update'},
                        {'package_dir': '/update'}, {'package_dir': 'a/b'},
                        {'format': 'shell_script', 'package_dir': 'update'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.build(name, attachment, **options)
        result = self.build(name, attachment)
        path = Path(result['archive'])
        members = read_package(io.BytesIO(path.read_bytes()))
        members['files/' + name] = (b'corrupted payload\n', 0o644)
        path.write_bytes(pack(members))
        deploy = self.root / 'deploy'
        target = deploy / name
        target.parent.mkdir(parents=True)
        target.write_bytes(b'old\n')
        self.assertNotEqual(self.apply(result, deploy).returncode, 0)
        self.assertEqual(target.read_bytes(), b'old\n')

    def test_explicit_deployment_mapping_and_quoted_payload_default_root(self):
        name, attachment = self.original('zip')
        deploy = self.root / "deploy ' $(touch should-not-run)"
        deploy.mkdir()
        (deploy / 'app.py').write_bytes(b'old\n')
        content = b'$(touch should-not-run)\n`touch nope`\n\x00\xff\n'
        result = self.build(name, attachment, content, target='app.py', default_root=str(deploy))
        applied = self.apply(result)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((deploy / 'app.py').read_bytes(), content)
        self.assertEqual(sorted(p.name for p in deploy.iterdir()), ['app.py'])
        self.assertEqual(result['files'], [name])

    def test_wrong_source_path_returns_actual_attachment_names_without_guessing(self):
        name, attachment = self.original('zip')
        with self.assertRaises(ValueError) as caught:
            self.build('source/app.py', attachment)
        self.assertIn(name, str(caught.exception))
        self.assertIn('patched', str(caught.exception))
        with self.assertRaisesRegex(ValueError, 'source 必须是原附件成员相对路径'):
            self.build('/tmp/source/app.py', attachment)

    def test_rejects_mismatched_second_file_before_changing_first(self):
        paths = []
        for name in ('a', 'b'):
            path = self.attachments / name
            path.write_text('old')
            paths.append(str(path))
            (self.work / name).write_text('fixed')
        result = build_patch(self.root, 'one', paths,
            {'files': [{'source': name, 'patched': name} for name in ('a', 'b')]})
        deploy = self.root / 'deploy'
        deploy.mkdir()
        (deploy / 'a').write_text('old')
        (deploy / 'b').write_text('unexpected')
        self.assertNotEqual(self.apply(result, deploy).returncode, 0)
        self.assertEqual((deploy / 'a').read_text(), 'old')
        self.assertEqual((deploy / 'b').read_text(), 'unexpected')

    def test_corrupt_source_archive_is_an_actionable_validation_error(self):
        name, attachment = self.original('tar.gz')
        attachment.write_bytes(b'broken archive')
        with self.assertRaisesRegex(ValueError, '原始附件'):
            self.build(name, attachment)

    def test_unchanged_traversal_cross_topic_and_symlink_are_rejected(self):
        name, attachment = self.original('file')
        with self.assertRaisesRegex(ValueError, '相同'):
            self.build(name, attachment, b'old\n')
        for target in ('../escape', '/tmp/escape', 'x\ny'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.build(name, attachment, target=target)
        (self.work / 'linked').symlink_to(attachment)
        for raw in ('linked', '../two/app.py', '/etc/passwd'):
            with self.subTest(path=raw), self.assertRaises((ValueError, OSError)):
                build_patch(self.root, 'one', [str(attachment)], {'files': [{'source': name, 'patched': raw}]})
        result = self.build(name, attachment)
        deploy = self.root / 'deploy'
        deploy.mkdir()
        (deploy / name).symlink_to(attachment)
        self.assertNotEqual(self.apply(result, deploy).returncode, 0)
        self.assertEqual(attachment.read_bytes(), b'old\n')

    def test_clean_copy_regression_uses_actual_script_applied_program(self):
        original = ('from pathlib import Path\nimport sys\n'
                    'base = Path(sys.argv[1]).resolve()\n'
                    'target = (base / sys.argv[2]).resolve()\n'
                    'print(target.read_text())\n').encode()
        fixed = original.replace(b'print(target.read_text())',
            b'if not target.is_relative_to(base):\n    sys.exit(2)\nprint(target.read_text())')
        name, attachment = self.original('zip', original)
        result = self.build(name, attachment, fixed)
        deploy = self.root / 'deploy'
        with zipfile.ZipFile(attachment) as archive:
            archive.extractall(deploy)  # 固定测试夹具，非用户归档。
        data = deploy / 'data'
        data.mkdir()
        (data / 'normal.txt').write_text('normal')
        (deploy / 'secret.txt').write_text('fixture secret')
        def read(filename):
            return subprocess.run([sys.executable, str(deploy / name), str(data), filename],
                                  capture_output=True, check=False)
        self.assertIn(b'fixture secret', read('../secret.txt').stdout)
        self.assertEqual(self.apply(result, deploy).returncode, 0)
        self.assertEqual(read('../secret.txt').returncode, 2)
        self.assertEqual(read('normal.txt').stdout.strip(), b'normal')
        self.assertEqual(read('normal.txt').returncode, 0)


class ScannerContractTests(unittest.TestCase):
    def test_engine_errors_and_timeout_are_not_clean_scan_results(self):
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, output = root / 'app.py', root / 'scan'
                source.write_text('print(1)')
                output.mkdir()
                def invoke(command, **kwargs):
                    if timed_out:
                        raise subprocess.TimeoutExpired(command, 5)
                    kwargs['stdout'].write(json.dumps({'results': [], 'paths': {'scanned': []},
                        'errors': [{'message': 'parser failed', 'type': 'ParseError'}]}).encode())
                    return SimpleNamespace(returncode=0)
                with patch('spiritscout.awdp.scan_worker.shutil.which', return_value='/fixture/semgrep'), \
                        patch('spiritscout.awdp.scan_worker.subprocess.run', side_effect=invoke):
                    result = run({'directory': str(output), 'path': str(source), 'rules': {'rules': []}, 'timeout': 5})
                self.assertEqual(result['status'], 'failed' if timed_out else 'partial')
                self.assertFalse(result['coverage']['complete'])
                self.assertEqual(result['timed_out'], timed_out)
                self.assertTrue(result['errors'])

    def test_missing_engine_has_explicit_unavailable_report(self):
        with tempfile.TemporaryDirectory() as tmp, patch('spiritscout.awdp.scan_worker.shutil.which', return_value=None):
            result = run({'directory': tmp, 'path': tmp, 'rules': {'rules': []}, 'timeout': 5})
            self.assertEqual(result['status'], 'unavailable')
            self.assertTrue(result['errors'])
            self.assertTrue((Path(tmp) / 'report.json').exists())

    def test_inventory_records_skips_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, out = root / 'source', root / 'output'
            src.mkdir()
            out.mkdir()
            (src / 'app.py').write_text('print(1)')
            (src / 'settings.yml').write_text('config')
            (src / 'linked.py').symlink_to(src / 'app.py')
            (src / 'big.py').write_bytes(b'x' * (1024 * 1024 + 1))
            mapping, skipped = snapshot(src, out)
            self.assertEqual([r['path'] for r in mapping.values()], [str(src / 'app.py')])
            self.assertEqual({r['reason'] for r in skipped}, {'unsupported_extension', 'symlink', 'file_size_limit'})

    def test_paths_cannot_read_other_topic_reports_or_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sandbox' / 'two').mkdir(parents=True)
            with self.assertRaises(ValueError):
                _source_path(root, 'one', str(root / 'sandbox' / 'two'))
            with self.assertRaises(ValueError):
                read_report(root, 'one', 'artifacts/awdp-scans/two/run/report.json', 0, 6)


class AgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_and_skills_are_awdp_scoped_and_respect_disabled_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResourceStore(Path(tmp) / 'resources')
            runtime = SkillRuntime(store)
            key = 'awdp-source-audit'
            data = dict(builtin_skills()[key])
            validate_skill(data)
            data['enabled'] = False
            store.save('skill', key, data, 0)
            for mode in (RunMode.CTF, RunMode.AWDP):
                ctx = context(mode)
                ctx.builtin_ctx = {'workspace_root': tmp}
                ctx.gateway.skill_runtime = runtime
                agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=mode == RunMode.AWDP))
                tools = {s['function']['name'] for s in agent.tool_schemas}
                self.assertEqual('source_scan' in tools, mode == RunMode.AWDP)
                self.assertEqual('build_patch' in tools, mode == RunMode.AWDP)
                self.assertEqual('source_context' in tools, mode == RunMode.AWDP)
                self.assertEqual('edit_source' in tools, mode == RunMode.AWDP)
                task = Task(task_id='one', task_type='ctf_single', agent_type=agent.agent_type, target='',
                            payload={'challenge': {'id': 'one'}})
                state = {'seed': []}
                agent._seed_messages(task, None, None, state)
                if mode == RunMode.AWDP:
                    text = str(state['messages'])
                    # 启用技能的正文可引用其他技能名，禁用项不能出现在可选目录或实际载入项。
                    directory = next(message['content'].split('\n', 1)[1] for message in state['messages']
                                     if message['content'].startswith('AWDP 可选参考目录'))
                    self.assertNotIn(key, [item['skill_id'] for item in json.loads(directory)])
                    loaded_refs = [message.get('_skill_ref', '') for message in state['messages']]
                    self.assertFalse(any(ref.startswith(key + '@') for ref in loaded_refs))
                    self.assertTrue(any(ref.startswith('awdp-fix-patterns@') for ref in loaded_refs))
                    self.assertIn('awdp-scan-triage', text)
                    loaded = await agent._run_tool_call(task, SimpleNamespace(route_id='r', name='solve'), state,
                        {'name': 'skill_open', 'arguments': {'skill_id': 'awdp-patch-delivery'}})
                    self.assertTrue(loaded.ok, loaded.text)
                    self.assertIn('default_root', json.loads(loaded.text)['content'])
                    invalid = await agent._run_tool_call(task, SimpleNamespace(route_id='r', name='solve'), state,
                        {'name': 'edit_source', 'arguments': {'action': 'patch', 'patch': '# wrong contract'}})
                    self.assertFalse(invalid.ok)
                    self.assertIn('edits', json.loads(invalid.text)['parameters']['required'])
            disabled = dict(builtin_skills()['awdp-fix-patterns'], enabled=False)
            store.save('skill', 'awdp-fix-patterns', disabled, 0)
            state = {'seed': []}
            agent._seed_messages(task, None, None, state)
            self.assertFalse(any(message.get('_skill_ref', '').startswith('awdp-fix-patterns@')
                                 for message in state['messages']))

    @unittest.skipUnless(sandbox_available(), '需要真实 bubblewrap 语法预检')
    async def test_generated_script_can_be_submitted_with_explanation(self):
        fixture = PatchBuilderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        name, attachment = fixture.original('folder')
        (fixture.work / 'fixed.py').write_text('fixed\n')
        ctx = context()
        ctx.builtin_ctx = {'workspace_root': str(fixture.root)}
        ctx.tools = ToolRegistry(None)
        ctx.tools.register_builtin('sandbox_exec', sandbox_exec,
            ToolSpec(name='sandbox_exec', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
        agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
        task = Task(task_id='one', task_type='ctf_single', agent_type=agent.agent_type, target='',
                    payload={'challenge': {'id': 'one', 'attachments': [str(attachment)]}})
        route = SimpleNamespace(route_id='r', name='solve')
        result = await agent._run_tool_call(task, route, {}, {'name': 'tool_invoke', 'arguments': {
            'name': 'build_patch', 'arguments': {'files': [{'source': name, 'patched': 'fixed.py'}]}}})
        self.assertTrue(result.ok, result.text)
        generated = json.loads(result.text)
        submitted = await agent._tool_submit_fix(task, route, {'archive': generated['archive'],
            'files': generated['files'], 'description': '静态修复候选；未运行应用回归；保留正常接口。'})
        self.assertTrue(submitted.ok, submitted.text)
        self.assertIn('未运行应用回归', submitted.final_text)
        self.assertIn('未执行补丁', submitted.final_text)

    @unittest.skipUnless(sandbox_available() and (Path(sys.executable).parent / 'semgrep').exists(),
                         '需要现有 Linux/bubblewrap 与可选 Semgrep')
    async def test_real_sandbox_multilanguage_scan_and_stable_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'sandbox' / 'one' / 'source'
            (source / 'tests' / 'vendor').mkdir(parents=True)
            samples = {
                'app.py': 'from flask import request, render_template_string, send_file\nimport os, pickle\n'
                    'def bad(db):\n    value = request.args.get("input")\n    os.system(value) # nosemgrep\n'
                    '    db.execute(value)\n    pickle.loads(value)\n    send_file(value)\n',
                'safe.py': 'from flask import request\nimport subprocess\ndef safe(db):\n'
                    '    value = request.args.get("input")\n    db.execute("SELECT ?", (value,))\n'
                    '    subprocess.run(["echo", value], shell=False)\n',
                'app.php': '<?php\n$v = $_GET["input"];\nsystem($v);\nunserialize($v);\nmysqli_query($db, $v);\n',
                'app.js': 'const cp = require("child_process"); const fs = require("fs");\n'
                    'function bad(req, db) { const v = req.query.input; cp.exec(v); db.query(v); fs.readFileSync(v); }\n',
                'app.ts': 'function bad(req: any) { const v = req.body; eval(v); }\n',
                'App.java': 'class App { void bad(Request req, Statement stmt) throws Exception {\n'
                    'String v = req.getParameter("input"); Runtime.getRuntime().exec(v); stmt.executeQuery(v); } }\n',
                'app.go': 'package app\nimport "os/exec"\nfunc bad(r Request, db DB) {\n'
                    'v := r.FormValue("input")\nexec.Command("sh", "-c", v)\ndb.Query(v)\n}\n',
                'app.rb': 'def bad\n v = params[:input]\n eval(v)\n Marshal.load(v)\nend\n',
                'app.c': '#include <string.h>\nvoid bad(char *v) { char out[8]; strcpy(out, v); }\n',
            }
            for name, body in samples.items():
                (source / 'tests' / 'vendor' / name).write_text(body)
            (source / '.semgrepignore').write_text('*\n')
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
            ctx = context()
            ctx.builtin_ctx = {'workspace_root': tmp, '_run_mode': RunMode.AWDP, '_lightweight_local': True}
            ctx.tools = ToolRegistry(None)
            ctx.tools.register_builtin('sandbox_exec', sandbox_exec, ToolSpec(
                name='sandbox_exec', kind=ToolKind.BUILTIN, risk=RiskLevel.READONLY))
            agent = SingleCTFAgent(ctx, flag_manager=FlagManager(local_only=True, fix_mode=True))
            task = Task(task_id='one', task_type='ctf_single', agent_type=agent.agent_type, target='',
                        payload={'challenge': {'id': 'one'}})
            route = SimpleNamespace(route_id='r', name='solve')
            result = await agent._run_tool_call(task, route, {}, {'name': 'tool_invoke', 'arguments': {
                'name': 'source_scan', 'arguments': {'action': 'scan', 'path': 'source', 'limit': 1}}})
            self.assertTrue(result.ok, result.text)
            page = json.loads(result.text)
            self.assertEqual(page['status'], 'completed', result.text)
            self.assertEqual(page['coverage']['scanned_files'], len(samples))
            self.assertEqual(page['error_count'], 0)
            self.assertFalse(page['coverage']['complete'])  # ignore 配置单列为未扫描文件。
            items = page['items'][:]
            report = page['report']
            while page['has_more']:
                result = await agent._run_tool_call(task, route, {}, page['next_call'])
                self.assertTrue(result.ok, result.text)
                page = json.loads(result.text)
                self.assertEqual(page['report'], report)
                items.extend(page['items'])
            rules = json.loads((Path(__file__).parents[1] / 'src/spiritscout/awdp/data/audit_rules.json').read_text())
            self.assertEqual({i['rule_id'] for i in items}, {r['id'] for r in rules['rules']})
            self.assertEqual(len({i['id'] for i in items}), len(items))
            self.assertTrue(all(i['verification_status'] == 'unverified' for i in items))
            self.assertFalse(any(i['path'].endswith('safe.py') for i in items))
            for item in items:
                self.assertEqual(item['sha256'], before[Path(item['path'])])
            self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})
            self.assertTrue(Path(page['raw_report']).is_file())
            self.assertEqual(len(list((root / 'artifacts' / 'awdp-scans' / 'one').iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
