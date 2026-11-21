"""用户选定模式后，任务创建与续聊不得再由模型切换分支。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from spiritscout.config.settings import ModelEntry, SystemConfig, WorkspaceConfig
from spiritscout.core.audit import NullAuditLogger
from spiritscout.core.models import RunMode
from spiritscout.launch import LaunchRequest, prepare_launch
from spiritscout.web.api_schemas import SessionCreatePayload
from spiritscout.web.sessions import Session, SessionManager, _llm_route_intent


class SelectedModeTests(unittest.IsolatedAsyncioTestCase):
    def test_competition_writeup_defaults_and_plan_restore(self) -> None:
        self.assertFalse(SessionCreatePayload(mode='competition').generate_wp)
        with self.assertRaises(ValueError):
            SessionCreatePayload(mode='competition', generate_wp='true')
        for enabled in (False, True):
            with self.subTest(generate_wp=enabled), tempfile.TemporaryDirectory() as tmp:
                payload = SessionCreatePayload(mode='competition', generate_wp=enabled)
                plan = prepare_launch(LaunchRequest(mode=RunMode.COMPETITION,
                    mission='持续解题', generate_wp=payload.generate_wp))
                self.assertEqual(plan.generate_wp, enabled)
                root = Path(tmp)
                manager = SessionManager(SystemConfig(workspace=WorkspaceConfig(root=tmp)), [])
                session = Session(session_id='wp-option', mode='competition', target=plan.target,
                    workspace=root, plan=plan,
                    launch_request={'mode': 'competition', 'mission': plan.mission,
                                    'generate_wp': enabled},
                    execution_plan=manager._snapshot_plan(plan))
                self.assertEqual(manager._rebuild_plan(session).generate_wp, enabled)
                session.execution_plan = {}
                self.assertEqual(manager._rebuild_plan(session).generate_wp, enabled)
                session.execution_plan = manager._snapshot_plan(plan)
                del session.execution_plan['generate_wp']
                self.assertFalse(manager._rebuild_plan(session).generate_wp,
                                 'old snapshots keep the default summary behavior')

    def test_src_asset_strategy_is_strict_and_defaults_to_edge_first(self) -> None:
        self.assertEqual(SessionCreatePayload(mode="src").asset_strategy, "edge_first")
        self.assertEqual(
            SessionCreatePayload(mode="src", asset_strategy="core_first").asset_strategy,
            "core_first")
        with self.assertRaises(ValueError):
            SessionCreatePayload(mode="src", asset_strategy="fastest")

    def test_explicit_competition_needs_no_yaml_and_ignores_mission_mode_words(self) -> None:
        plan = prepare_launch(LaunchRequest(
            mode=RunMode.COMPETITION, contest_type='jeopardy',
            mission='这是靶场里的 CTF 题，平台 http://contest.test 多题跑分'))
        self.assertEqual(plan.authorization.mode, RunMode.COMPETITION)
        self.assertEqual(plan.rules.contest_type.value, 'jeopardy')
        self.assertFalse(plan.needs_mode_routing)
        self.assertFalse(plan.mode_auto)
        for contest in ('awd', 'range'):
            plan = prepare_launch(LaunchRequest(mode=RunMode.COMPETITION,
                contest_type=contest, mission='计分平台 http://contest.test，授权网段 10.0.0.0/24'))
            self.assertEqual(plan.rules.contest_type.value, contest)
        custom = prepare_launch(LaunchRequest(
            mode=RunMode.COMPETITION, contest_type='全新协作赛制-v2'))
        self.assertEqual(custom.rules.contest_type.value, '全新协作赛制-v2')

    async def test_initial_selected_mode_skips_both_classifiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
            cfg.audit_log_path = str(root / 'audit.jsonl')
            models = [ModelEntry(model='test-model')]
            manager = SessionManager(cfg, models)
            plan = prepare_launch(LaunchRequest(mode=RunMode.COMPETITION, generate_wp=True,
                contest_type='jeopardy', mission='CTF 靶场 http://contest.test'))
            session = Session(session_id='sess-selected', mode='competition',
                target=plan.target, workspace=root, launch_request={
                    'mode': 'competition', 'mode_auto': False,
                    'contest_type': 'jeopardy', 'mission': plan.mission})
            with patch('spiritscout.web.session_execution._llm_route_mode', new_callable=AsyncMock) as route, \
                 patch('spiritscout.web.session_execution._llm_route_intent', new_callable=AsyncMock) as intent, \
                 patch('spiritscout.web.session_execution._llm_extract_platform', new=AsyncMock(return_value=None)), \
                 patch('spiritscout.runtime.run_competition_engagement', new=AsyncMock(
                     return_value=SimpleNamespace(run_summary={}, verify_stats={},
                         flag_stats={}, report_path=''))) as runtime:
                await manager._run(session, cfg, plan, False, models)
                route.assert_not_awaited()
                intent.assert_not_awaited()
                runtime.assert_awaited_once()
                self.assertTrue(runtime.await_args.kwargs["generate_wp"])
            self.assertEqual(session.mode, 'competition')
            self.assertEqual(session.status, 'completed')

    async def test_resume_reads_intent_without_reclassifying_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SystemConfig(workspace=WorkspaceConfig(root=str(root)))
            cfg.audit_log_path = str(root / 'audit.jsonl')
            models = [ModelEntry(model='test-model')]
            manager = SessionManager(cfg, models)
            mission = 'CTF 单题 http://ctf.test'
            plan = prepare_launch(LaunchRequest(mode=RunMode.CTF, mission=mission))
            session = Session(session_id='sess-selected', mode='ctf', target=plan.target,
                workspace=root, launch_request={'mode': 'ctf', 'mode_auto': False,
                                                 'mission': mission}, plan=plan,
                dialogue_history=[{'role': 'user', 'content': mission},
                                  {'role': 'user', 'content': '继续'}])
            with patch('spiritscout.web.session_execution._llm_route_mode', new_callable=AsyncMock) as route, \
                 patch('spiritscout.web.session_execution._llm_route_intent', new=AsyncMock(return_value='task')) as intent, \
                 patch('spiritscout.web.session_execution._llm_extract_platform', new=AsyncMock(return_value=None)), \
                 patch('spiritscout.runtime.run_engagement', new=AsyncMock(side_effect=AssertionError('不得切换 SRC'))), \
                 patch('spiritscout.runtime.run_ctf_engagement', new=AsyncMock(
                     return_value=SimpleNamespace(run_summary={}, verify_stats={},
                         flag_stats={}, report_path=''))) as runtime:
                await manager._run(session, cfg, plan, False, models, resume=True)
                route.assert_not_awaited()
                intent.assert_awaited_once()
                runtime.assert_awaited_once()
            self.assertEqual(session.mode, 'ctf')

    async def test_intent_response_cannot_override_selected_mode(self) -> None:
        gateway = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content=json.dumps({
            'intent': 'dialogue', 'mode': 'src', 'contest_type': 'awd'}))))
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            with patch('spiritscout.web.session_assistant._conversation_gateway', return_value=gateway):
                intent = await _llm_route_intent(cfg, [ModelEntry(model='test-model')],
                    '解释一下结果', NullAuditLogger())
        self.assertEqual(intent, 'dialogue')

    def test_rebuild_dialogue_preserves_explicit_task_mode_and_contest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SystemConfig(workspace=WorkspaceConfig(root=tmp))
            manager = SessionManager(cfg, [])
            session = Session(session_id='sess-selected', mode='dialogue',
                target='http://contest.test', workspace=Path(tmp), launch_request={
                    'mode': 'competition', 'mode_auto': False, 'contest_type': 'jeopardy',
                    'mission': 'CTF 靶场 http://contest.test'})
            plan = manager._rebuild_plan(session)
            self.assertIsNotNone(plan)
            self.assertEqual(plan.authorization.mode, RunMode.COMPETITION)
            self.assertEqual(plan.rules.contest_type.value, 'jeopardy')
            self.assertFalse(plan.mode_auto)
