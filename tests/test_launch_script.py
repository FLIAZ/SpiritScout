"""启动脚本参数分发、会话保护与服务退出回归，不调用模型。"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch


class StartupDisplayTests(unittest.TestCase):
    def test_logo_colors_and_redirected_logs(self) -> None:
        from spiritscout import __version__
        from spiritscout.startup import StartupDisplay

        cases = (
            (True, False, 80, "xterm", ""),
            (True, False, 80, "xterm-256color", "truecolor"),
            (True, True, 80, "xterm-256color", "truecolor"),
            (False, False, 80, "xterm", ""),
            (True, False, 40, "xterm", ""),
            (True, False, 68, "xterm", ""),
            (True, False, 69, "xterm", ""),
            (True, False, 80, "dumb", "truecolor"),
        )
        for terminal, no_color, width, term, colorterm in cases:
            with self.subTest(terminal=terminal, no_color=no_color, width=width,
                              term=term, colorterm=colorterm):
                stream = io.StringIO()
                stream.isatty = lambda: terminal
                env = {"TERM": term, "COLORTERM": colorterm, "WT_SESSION": "fixture"}
                if no_color:
                    env["NO_COLOR"] = "1"
                with patch.dict(os.environ, env, clear=True), patch(
                        "shutil.get_terminal_size", return_value=os.terminal_size((width, 24))):
                    display = StartupDisplay(stream)
                    display.start()
                    header = re.sub(r"\033\[[0-9;]*m", "", stream.getvalue())
                    for line in header.splitlines():
                        columns = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
                                      for c in line)
                        self.assertLessEqual(columns, width)
                    display.phase("读取配置")
                    display.phase("启动 HTTP 服务")
                    display.ready("127.0.0.1", 8000, auth_enabled=False)
                output = stream.getvalue()
                colored = terminal and not no_color and term != "dumb"
                self.assertIn(__version__, output)
                self.assertIn("http://127.0.0.1:8000", output)
                self.assertNotIn("█", output)
                self.assertNotIn("─", header)
                compact = not terminal or width < 69 or term == "dumb"
                self.assertEqual("SPIRITSCOUT" in header, compact)
                self.assertEqual("\033[" in output, colored)
                self.assertEqual("38;2;" in output, colored and colorterm == "truecolor")
                self.assertEqual(output.count("\r\033[2K"), 2 if colored else 0)
                if not colored:
                    self.assertNotIn("\r", output)
                    self.assertIn("读取配置…\n", output)
                    self.assertIn("启动 HTTP 服务…\n", output)

    def test_occupied_port_is_not_reported_as_ready_or_cleanly_stopped(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            script = textwrap.dedent("""\
                import sys
                from fastapi import FastAPI
                from spiritscout.startup import StartupDisplay, serve
                app = FastAPI()
                app.state.auth_enabled = False
                display = StartupDisplay()
                display.start()
                display.phase("启动 HTTP 服务")
                serve(app, display=display, host="127.0.0.1", port=int(sys.argv[1]),
                      log_level="warning")
            """)
            result = subprocess.run([sys.executable, "-c", script, str(port)],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("HTTP 服务已监听", result.stdout)
            self.assertNotIn("控制台已停止", result.stdout)
            self.assertIn("[错误]", result.stderr)

    def test_actual_binding_and_access_policy_are_not_misrepresented(self) -> None:
        from spiritscout.startup import StartupDisplay

        for host, address, auth in (("::1", "[::1]", False),
                                    ("0.0.0.0", "127.0.0.1", False),
                                    ("127.0.0.1", "127.0.0.1", True)):
            with self.subTest(host=host, auth=auth):
                stream = io.StringIO()
                StartupDisplay(stream).ready(host, 8012, auth_enabled=auth, warming=True)
                output = stream.getvalue()
                self.assertIn(f"http://{address}:8012", output)
                self.assertIn("首次任务会等待预热完成", output)
                self.assertIn("Ctrl+C 安全停止控制台", output)
                if auth or host == "0.0.0.0":
                    self.assertNotIn("仅本机", output)


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


@unittest.skipUnless(sys.platform == 'linux', '验证本机 POSIX 信号退出')
class ServerShutdownTests(unittest.TestCase):
    def test_sigint_with_open_event_stream_reaches_lifespan_cleanup(self) -> None:
        # 通过产品入口启动真实 Uvicorn，保留一个永不终态的 SSE 消费者。
        # 原实现会一直等 HTTP 排空，永远无法进入 lifespan 收尾。
        with tempfile.TemporaryDirectory(prefix='spirit shutdown ') as temp:
            root = Path(temp)
            marker = root / 'closed'
            listener = socket.socket()
            self.addCleanup(listener.close)
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            script = textwrap.dedent('''\
                import sys
                from contextlib import asynccontextmanager
                from pathlib import Path
                from types import SimpleNamespace
                from unittest.mock import patch
                from fastapi import FastAPI
                from fastapi.responses import StreamingResponse
                from spiritscout import __main__ as entry
                from spiritscout.web.events import stream_audit

                marker = Path(sys.argv[1])
                fd = int(sys.argv[2])
                @asynccontextmanager
                async def lifespan(app):
                    try:
                        yield
                    finally:
                        marker.write_text('cleanup complete')
                app = FastAPI(lifespan=lifespan)
                app.state.auth_enabled = False
                app.state.updater = SimpleNamespace(configure_launch=lambda *args: None)
                @app.get('/events')
                async def events():
                    return StreamingResponse(stream_audit(marker.parent / 'audit.jsonl'),
                                             media_type='text/event-stream')
                cfg = SimpleNamespace(models_config_path='unused', web=SimpleNamespace(
                    host='127.0.0.1', port=0, max_concurrent_sessions=1))
                from spiritscout.startup import serve as run
                def serve(app, **kwargs):
                    return run(app, fd=fd, **kwargs)
                # 隔离配置和模型预热，HTTP 服务、信号和事件流均使用真实实现。
                with patch.object(entry, '_find_dotenv', return_value=None), \
                     patch.object(entry, 'load_system_config', return_value=cfg), \
                     patch.object(entry, 'load_model_config', return_value=[]), \
                     patch('spiritscout.web.create_app', return_value=app), \
                     patch('spiritscout.startup.serve', side_effect=serve), \
                     patch.dict(sys.modules, {'litellm': SimpleNamespace()}):
                    entry.main([])
                ''')
            with (root / 'output').open('w+') as output:
                process = subprocess.Popen(
                    [sys.executable, '-c', script, str(marker), str(listener.fileno())],
                    pass_fds=(listener.fileno(),), stdout=output, stderr=output)
                try:
                    client = socket.create_connection(('127.0.0.1', port), timeout=15)
                    self.addCleanup(client.close)
                    client.sendall(b'GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n')
                    response = b''
                    while b': hb\n\n' not in response:
                        chunk = client.recv(4096)
                        self.assertTrue(chunk, 'SSE 未成功建立')
                        response += chunk
                    self.assertIn(b'200 OK', response)
                    output.seek(0)
                    banner = output.read()
                    self.assertIn(f'http://127.0.0.1:{port}', banner)
                    self.assertIn('HTTP 服务已监听', banner)
                    started = time.monotonic()
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=12)
                    self.assertLess(time.monotonic() - started, 12)
                    self.assertEqual(marker.read_text(), 'cleanup complete')
                    self.assertIn(process.returncode, (0, -signal.SIGINT))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    output.seek(0)
                    if not marker.exists():
                        self.fail('服务未执行退出清理：' + output.read())
                    self.assertEqual(process.returncode, 0)
                    output.seek(0)
                    self.assertNotIn('KeyboardInterrupt', output.read())


if __name__ == '__main__':
    unittest.main()
