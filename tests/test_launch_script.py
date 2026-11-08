"""启动脚本参数分发与独立部署的会话保护回归，不启动真实服务。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('bash'), '需要 Bash')
class LaunchScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='spiritscout launch ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1]
        shutil.copy2(source / 'launch.sh', self.root / 'launch.sh')
        (self.root / 'scripts').mkdir()
        shutil.copy2(source / 'scripts/run_ollama_light.sh',
                     self.root / 'scripts/run_ollama_light.sh')
        python = self.root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''\
            import json
            import os
            import sys
            print(json.dumps({
                'args': sys.argv[1:],
                'cwd': os.getcwd(),
                'cost_map': os.environ.get('LITELLM_LOCAL_MODEL_COST_MAP'),
            }))
            if sys.argv[1:2] == ['scripts/setup_ollama_light.py']:
                sys.exit(int(os.environ.get('FIXTURE_SETUP_EXIT', '0')))
            '''), encoding='utf-8')
        python.chmod(0o755)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        ss = bin_dir / 'ss'
        ss.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8')
        ss.chmod(0o755)
        self.env = dict(os.environ, PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
        self.env.pop('WEBCONSOLE_PORT', None)
        self.env.pop('FIXTURE_SETUP_EXIT', None)
        self.env['LITELLM_LOCAL_MODEL_COST_MAP'] = 'False'
        profile = self.root / '.spiritscout-ollama'
        profile.mkdir()
        (profile / 'system.yaml').write_text('fixture: true\n', encoding='utf-8')
        (profile / 'models.yaml').write_text('fixture: true\n', encoding='utf-8')

    def launch(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['bash', str(self.root / 'launch.sh'), *args],
            cwd=self.root.parent, env=self.env, capture_output=True, text=True,
            timeout=10, check=False,
        )

    def calls(self, result: subprocess.CompletedProcess[str]) -> list[dict]:
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()
                if line.startswith('{')]

    def running_session(self, workspace: str) -> None:
        session = self.root / workspace / 'sessions/fixture'
        session.mkdir(parents=True)
        (session / 'meta.json').write_text('{"status": "running"}', encoding='utf-8')

    def test_normal_start_keeps_config_and_argument_boundaries(self) -> None:
        for args in [(), ('start', '--host', '127.0.0.1', '--config', 'custom config.yaml')]:
            with self.subTest(args=args):
                call, = self.calls(self.launch(*args))
                self.assertEqual(call['args'], [
                    '-m', 'spiritscout', '--config', 'configs/system.yaml', *args[1:],
                ])
                self.assertEqual(call['cwd'], str(self.root))

    def test_offline_selects_light_profile_and_consumes_flag(self) -> None:
        for args in [('start', '--offline'), ('--offline',),
                     ('--offline', 'start', '--port', '8101')]:
            with self.subTest(args=args):
                call, = self.calls(self.launch(*args))
                extra = ['--port', '8101'] if '--port' in args else []
                self.assertEqual(call['args'], [
                    '-m', 'spiritscout', '--config', '.spiritscout-ollama/system.yaml', *extra,
                ])
                self.assertEqual(call['cost_map'], 'True')
                self.assertEqual(call['cwd'], str(self.root))

    def test_missing_light_config_initializes_before_start(self) -> None:
        for name in ('system.yaml', 'models.yaml'):
            with self.subTest(name=name):
                path = self.root / '.spiritscout-ollama' / name
                path.unlink()
                setup, start = self.calls(self.launch('start', '--offline'))
                self.assertEqual(setup['args'], ['scripts/setup_ollama_light.py'])
                self.assertEqual(start['args'][-1], '.spiritscout-ollama/system.yaml')
                path.write_text('fixture: true\n', encoding='utf-8')

    def test_llama_configuration_uses_offline_profile_and_session_guard(self) -> None:
        models = self.root / '.spiritscout-ollama/models.yaml'
        content = 'models:\n  - model: fixture\n    local_runtime: llama_cpp\n'
        models.write_text(content, encoding='utf-8')
        for command in ('start', 'restart'):
            with self.subTest(command=command):
                call, = self.calls(self.launch(command, '--offline'))
                self.assertEqual(call['args'], ['-m', 'spiritscout', '--config',
                                               '.spiritscout-ollama/system.yaml'])
                self.assertEqual(call['cost_map'], 'True')
        self.assertEqual(models.read_text(encoding='utf-8'), content)
        self.assertFalse((self.root / '.spiritscout-llama').exists())
        self.running_session('.spiritscout-ollama')
        for command in ('stop', 'restart'):
            with self.subTest(command=command):
                blocked = self.launch(command, '--offline')
                self.assertEqual(blocked.returncode, 1)
                self.assertIn(f'{command} --offline 8001 --force', blocked.stderr)

    def test_removed_llama_flag_points_to_offline_without_starting(self) -> None:
        for command in ('start', 'stop', 'restart'):
            with self.subTest(command=command):
                result = self.launch(command, '--llama')
                self.assertEqual(result.returncode, 1)
                self.assertIn('统一使用 --offline', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_initialization_failure_does_not_start_console(self) -> None:
        (self.root / '.spiritscout-ollama/models.yaml').unlink()
        self.env['FIXTURE_SETUP_EXIT'] = '7'
        result = self.launch('start', '--offline')
        self.assertEqual(result.returncode, 7)
        calls = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['args'], ['scripts/setup_ollama_light.py'])

    def test_offline_stop_checks_light_sessions_and_keeps_mode_in_hint(self) -> None:
        self.running_session('.spiritscout-ollama')
        result = self.launch('stop', '8101', '--offline')
        self.assertEqual(result.returncode, 1)
        self.assertIn('stop --offline 8101 --force', result.stderr)
        normal = self.launch('stop')
        self.assertEqual(normal.returncode, 0, normal.stderr)
        self.assertIn('端口 8000', normal.stdout)
        forced = self.launch('stop', '--offline', '--force')
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertIn('端口 8001', forced.stdout)

    def test_normal_sessions_do_not_block_light_stop(self) -> None:
        self.running_session('.spiritscout')
        normal = self.launch('stop')
        self.assertEqual(normal.returncode, 1)
        light = self.launch('stop', '--offline')
        self.assertEqual(light.returncode, 0, light.stderr)
        self.assertIn('端口 8001', light.stdout)

    def test_offline_restart_reuses_profile_and_forwards_explicit_port(self) -> None:
        for extra in [(), ('8101',)]:
            with self.subTest(extra=extra):
                result = self.launch('restart', '--offline', *extra)
                call, = self.calls(result)
                port = extra[0] if extra else '8001'
                self.assertIn(f'端口 {port}', result.stdout)
                self.assertEqual(call['args'], [
                    '-m', 'spiritscout', '--config', '.spiritscout-ollama/system.yaml',
                    *(['--port', port] if extra else []),
                ])

    def test_missing_venv_fails_before_initialization(self) -> None:
        (self.root / '.venv/bin/python').unlink()
        result = self.launch('start', '--offline')
        self.assertEqual(result.returncode, 1)
        self.assertIn('缺少虚拟环境', result.stderr)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
