"""可见浏览器启动与人工接管生命周期。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.config.settings import McpServerConfig
from spiritscout.core.audit import NullAuditLogger
from spiritscout.policy.questions import QuestionBroker
from spiritscout.tools.browser_desktop import ensure_browser
from spiritscout.tools.browser_handoff import browser_lock, register_handoff


class BrowserLaunchTests(unittest.TestCase):
    def test_missing_local_chrome_starts_detached_with_persistent_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
                'spiritscout.tools.browser_desktop._ready', side_effect=[False, False, True]), patch(
                'spiritscout.tools.browser_desktop.desktop_environment',
                return_value={'DISPLAY': ':0'}), patch(
                'spiritscout.tools.browser_desktop.chrome_executable', return_value='/bin/chrome'), patch(
                'spiritscout.tools.browser_desktop.subprocess.Popen') as spawn:
            profile = Path(tmp) / 'profile'
            ensure_browser('http://127.0.0.1:9223', {'SPIRITSCOUT_CHROME_PROFILE_DIR': str(profile)})
            argv = spawn.call_args.args[0]
            self.assertIn('--user-data-dir=' + str(profile), argv)
            self.assertIn('--remote-debugging-address=127.0.0.1', argv)
            self.assertNotIn('--headless', argv)
            self.assertTrue(profile.is_dir())
            self.assertTrue(spawn.call_args.kwargs.get('start_new_session') or
                            spawn.call_args.kwargs.get('creationflags'))

    def test_existing_or_remote_browser_does_not_spawn_local_process(self) -> None:
        with patch('spiritscout.tools.browser_desktop._ready', return_value=True), patch(
                'spiritscout.tools.browser_desktop.subprocess.Popen') as spawn:
            ensure_browser('http://127.0.0.1:9223', {})
            spawn.assert_not_called()
        with patch('spiritscout.tools.browser_desktop._ready', return_value=False), patch(
                'spiritscout.tools.browser_desktop.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError, '远程'):
                ensure_browser('http://192.0.2.1:9223', {})
            spawn.assert_not_called()


class BrowserHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_answer_or_stop_minimizes_and_preserves_browser(self) -> None:
        for stop in (False, True):
            with self.subTest(stop=stop):
                registry = SimpleNamespace(register_builtin=lambda name, fn, spec: functions.update({name: fn}))
                functions = {}
                catalog = {}
                broker = QuestionBroker()
                client = SimpleNamespace(call=AsyncMock(return_value={'is_error': False, 'text': 'ok'}))
                spec = McpServerConfig(name='chrome', command='python')
                lock = browser_lock('test-endpoint')
                register_handoff(registry, catalog, spec, client,
                                 [{'name': 'browser_run_code'}], broker, NullAuditLogger(), lock)
                task = asyncio.create_task(functions['mcp__chrome__browser_login'](
                    url='https://login.example.test', question='请完成登录'))
                for _ in range(100):
                    if broker.view():
                        break
                    await asyncio.sleep(0)
                self.assertTrue(broker.view())
                self.assertTrue(lock.locked())
                self.assertFalse(task.done())
                if stop:
                    broker.cancel_all()
                else:
                    broker.answer(broker.view()[0]['question_id'], choice=0)
                result = await task
                self.assertEqual(result.ok, not stop)
                self.assertTrue(result.parsed['browser_preserved'])
                self.assertTrue(result.parsed['window_minimized'])
                self.assertFalse(result.parsed['login_verified'])
                self.assertFalse(lock.locked())
                calls = client.call.await_args_list
                self.assertIn('normal', calls[1].args[1]['code'])
                self.assertIn('minimized', calls[-1].args[1]['code'])
                self.assertNotIn('browser_close', [call.args[0] for call in calls])

    async def test_handoff_task_cancellation_still_minimizes(self) -> None:
        functions = {}
        client = SimpleNamespace(call=AsyncMock(return_value={'text': 'ok'}))
        broker = QuestionBroker()
        register_handoff(SimpleNamespace(register_builtin=lambda n, f, s: functions.update({n: f})), {},
                         McpServerConfig(name='chrome', command='python'), client,
                         [{'name': 'browser_run_code'}], broker, NullAuditLogger(), browser_lock('cancel'))
        task = asyncio.create_task(functions['mcp__chrome__browser_login'](
            url='https://login.example.test', question='请登录'))
        for _ in range(100):
            if broker.view():
                break
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn('minimized', client.call.await_args_list[-1].args[1]['code'])
