"""公开进展复用现有模型正文，不暴露推理或执行协议。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from spiritscout.gateway.progress import parse_public_progress, publish_progress
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope


class AgentProgressTests(unittest.TestCase):
    def test_inline_discovery_json_is_hidden_across_each_possible_stream_boundary(self):
        from spiritscout.core.audit import NullAuditLogger
        from spiritscout.gateway.progress import PublicProgressEmitter

        value = {'key': 'service', 'intent': '核对服务', 'title': '发现输入条件',
                 'basis': '原始响应', 'importance': 3, 'confidence': 'observed',
                 'kind': 'discovery', 'evidence_refs': ['exec-one']}
        raw = '已确认服务入口。\n' + json.dumps({'discovery': value}, ensure_ascii=False)
        for split in range(1, len(raw)):
            audit = NullAuditLogger()
            emitter = PublicProgressEmitter(audit, 'cloud', task_id='t', inline_discovery=True)
            for chunk in (raw[:split], raw[split:]):
                emitter('text', chunk)
                emitter.flush()
            text = ''.join(r['detail']['text'] for r in audit.records)
            self.assertEqual(text, '已确认服务入口。', f'分块位置 {split}')
        result = publish_progress(audit, 'cloud', json.dumps({'discovery': value}), task_id='t')
        self.assertEqual(result['discovery_key'], 'service')

    def test_scored_discovery_only_publishes_sanitized_display_metadata(self):
        from spiritscout.gateway.progress import publish_discovery

        audit = Mock()
        value = {'key': 'identity', 'intent': '确认身份来源', 'title': '身份来自会话对象',
                 'basis': '响应对照显示身份随会话对象变化', 'importance': 4, 'confidence': 'observed',
                 'kind': 'discovery', 'evidence_refs': ['exec-one'], 'analysis': '私有推理'}
        result = publish_discovery(audit, 'ctf', value, task_id='t', route_id='r', turn=6)
        self.assertTrue(result['ok'])
        self.assertEqual(audit.log.call_args.args[0], 'agent.discovery')
        detail = audit.log.call_args.args[2]
        self.assertEqual(detail['importance'], 4)
        self.assertEqual(detail['confidence'], 'observed')
        self.assertNotIn('analysis', detail)
        for changes in ({'importance': True}, {'importance': 6}, {'evidence_refs': []}, {'key': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                publish_discovery(audit, 'ctf', {**value, **changes}, task_id='t')
        self.assertTrue(publish_discovery(audit, 'ctf', {**value, 'importance': 0,
                        'confidence': 'hypothesis', 'evidence_refs': []}, task_id='t')['ok'])

    def test_json_decision_can_report_discovery_without_an_extra_model_round(self):
        audit = Mock()
        discovery = {'key': 'service', 'intent': '核对服务', 'title': '响应存在版本线索',
                     'basis': '实际响应头与页面提供了对应线索', 'importance': 3, 'confidence': 'observed',
                     'kind': 'discovery', 'evidence_refs': ['exec-one'], 'private': '内部数据'}
        publish_progress(audit, 'scanner', json.dumps({'progress': '继续核实服务',
                         'decision': 'wait', 'discovery': discovery}), task_id='scan', turn=3)
        events = {call.args[0]: call.args[2] for call in audit.log.call_args_list}
        self.assertEqual(events['agent.discovery']['importance'], 3)
        self.assertEqual(events['agent.progress']['text'], '继续核实服务')
        self.assertNotIn('private', events['agent.discovery'])
        self.assertNotIn('decision', events['agent.progress'])
        audit.reset_mock()
        publish_progress(audit, 'unscoped', json.dumps({'discovery': discovery}))
        self.assertEqual(audit.log.call_args.args[0], 'agent.discovery_rejected')

    def test_discovery_reporting_appends_one_short_contract_without_changing_cached_prefix(self):
        import copy

        from spiritscout.gateway.progress import (
            INLINE_DISCOVERY_GUIDANCE,
            ensure_discovery_reporting,
            native_discovery_reminder,
        )

        state = {'messages': [{'role': 'system', 'content': '稳定系统提示'},
                              {'role': 'user', 'content': '原始题目'},
                              {'role': 'assistant', 'content': '已有发现'}]}
        prefix = copy.deepcopy(state['messages'])
        ensure_discovery_reporting(state)
        first = copy.deepcopy(state['messages'])
        for _ in range(4):
            ensure_discovery_reporting(state)
        self.assertEqual(state['messages'], first)
        self.assertEqual(state['messages'][:-1], prefix)
        self.assertEqual(state['messages'][-1]['content'], INLINE_DISCOVERY_GUIDANCE)
        old = {'messages': [*prefix, {'role': 'user', 'content': '旧版登记工具契约'}],
               'discovery_reporting_v1': True, 'discovery_reporting_v2': True,
               'observations': [{'result': '已有原始观察'}]}
        before = copy.deepcopy(old['messages'])
        ensure_discovery_reporting(old)
        self.assertEqual(old['messages'][:-1], before)
        self.assertTrue(old['discovery_reporting_v3'])
        reminder = native_discovery_reminder(old)
        old['discovery_reported_keys'] = ['stable-key']
        self.assertEqual(native_discovery_reminder(old), reminder)
        self.assertLess(len(reminder), 100)
        from spiritscout.gateway.context import estimate_input_tokens
        self.assertLess(estimate_input_tokens([{'role': 'user', 'content': INLINE_DISCOVERY_GUIDANCE}]), 650)

    def test_public_formats_match_browser_fixtures_and_persist_explicit_semantics(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/public_progress.json').read_text())
        for case in cases:
            with self.subTest(case=case['name']):
                expected = {key: case[key] for key in ('text', 'exploration') if key in case}
                self.assertEqual(parse_public_progress(case['content']), expected)
                audit = Mock()
                result = publish_progress(audit, 'worker', case['content'], task_id='t', turn=1)
                if case['text']:
                    self.assertEqual(result['text'], case['text'])
                    self.assertEqual(result.get('exploration'), case.get('exploration'))
                else:
                    audit.log.assert_not_called()

    def test_exploration_contract_is_short_enough_to_keep_attention_on_the_task(self) -> None:
        from spiritscout.gateway.progress import (
            EXPLORATION_PROGRESS_GUIDANCE,
            FORMAT_GUIDANCE,
        )

        self.assertLess(len(EXPLORATION_PROGRESS_GUIDANCE), 520)
        self.assertLess(len(FORMAT_GUIDANCE), 90)
        self.assertIn('JSON 只供建图', EXPLORATION_PROGRESS_GUIDANCE)
        self.assertIn('不能代替正文', EXPLORATION_PROGRESS_GUIDANCE)
        self.assertIn('JSON 不进入用户正文', FORMAT_GUIDANCE)

    def test_structured_decision_only_publishes_public_progress_with_task_scope(self) -> None:
        audit = Mock()
        token = current_execution_scope.set(ExecutionScope(
            task_id='task-a', route_id='route-a', turn=2, step_id='stage-a'))
        try:
            publish_progress(audit, 'worker-a',
                             '{"progress":"已确认匿名请求被拒绝，下一步检查认证后的访问。",'
                             '"analysis":"内部分析", "actions":[{"command":"secret"}]}')
        finally:
            current_execution_scope.reset(token)
        event, actor, detail = audit.log.call_args.args
        self.assertEqual((event, actor), ('agent.progress', 'worker-a'))
        self.assertEqual(detail['task_id'], 'task-a')
        self.assertEqual(detail['turn'], 2)
        self.assertNotIn('内部分析', detail['text'])
        self.assertNotIn('secret', detail['text'])

    def test_native_progress_is_explicitly_associated_and_invalid_json_is_not_published(self) -> None:
        audit = Mock()
        publish_progress(audit, 'worker-b', '登录仍需人工确认。', task_id='task-b', route_id='r', turn=3)
        self.assertEqual(audit.log.call_args.args[2]['route_id'], 'r')
        audit.reset_mock()
        for content in ('{"progress":"未完', '{"analysis":"隐藏分析"}', '["内部执行数据"]', ''):
            publish_progress(audit, 'worker-b', content, task_id='task-b')
        audit.log.assert_not_called()


    def test_recovered_conversation_appends_contract_once_without_rewriting_prefix(self):
        import copy

        from spiritscout.gateway.progress import (
            EXPLORATION_PROGRESS_GUIDANCE,
            ensure_exploration_progress,
        )

        state = {'messages': [{'role': 'system', 'content': '稳定系统提示'},
                              {'role': 'user', 'content': '原任务'},
                              {'role': 'assistant', 'content': '已有公开说明'}]}
        before = copy.deepcopy(state['messages'])
        ensure_exploration_progress(state)
        self.assertEqual(state['messages'][:-1], before)
        self.assertEqual(state['messages'][-1]['content'], EXPLORATION_PROGRESS_GUIDANCE)
        ensure_exploration_progress(state)
        self.assertEqual(len(state['messages']), len(before) + 1)
        fresh = {'messages': [{'role': 'user', 'content': '题目\n' + EXPLORATION_PROGRESS_GUIDANCE}]}
        ensure_exploration_progress(fresh)
        self.assertEqual(len(fresh['messages']), 1)

    def test_public_semantic_boundary_is_preserved_without_private_fields(self):
        audit = Mock()
        publish_progress(audit, 'worker',
                         '{"progress":"对照响应后改用授权账号。",'
                         '"exploration":{"intent":"验证访问边界","attempt":"授权账号对照",'
                         '"from":"匿名请求","reason":"匿名请求被拒绝","state":"running",'
                         '"analysis":"内部推理","actions":["执行参数"]}}', task_id='t')
        detail = audit.log.call_args.args[2]
        self.assertEqual(detail['exploration']['intent'], '验证访问边界')
        self.assertEqual(detail['exploration']['from'], '匿名请求')
        self.assertNotIn('内部推理', str(detail))
        self.assertNotIn('执行参数', str(detail))

    def test_malformed_semantic_metadata_does_not_break_public_progress(self):
        audit = Mock()
        publish_progress(audit, 'worker', '{"progress":"仍在验证","exploration":[]}', task_id='t')
        self.assertEqual(audit.log.call_args.args[2]['text'], '仍在验证')
        self.assertNotIn('exploration', audit.log.call_args.args[2])
        publish_progress(audit, 'worker', '{"progress":"仍在验证",'
                         '"exploration":{"intent":"核对边界","attempt":"对照响应","state":[]}}', task_id='t')
        self.assertNotIn('state', audit.log.call_args.args[2]['exploration'])

    def test_milestones_keep_public_basis_and_ignore_private_or_incomplete_fields(self):
        from spiritscout.gateway.progress import public_exploration

        value = {'intent': '核对边界', 'attempt': '账号对照', 'milestone': {
            'kind': 'discovery', 'title': '两个账号拿到了相同记录', 'basis': '响应中的记录标识一致，尚需核对归属',
            'next': '验证该记录是否属于其他账号', 'evidence_refs': ['topology:observed', 'topology:observed', 3],
            'analysis': '内部分析', 'actions': ['执行参数']}}
        semantic = public_exploration(value)
        self.assertEqual(semantic['milestone']['evidence_refs'], ['topology:observed'])
        self.assertEqual(semantic['milestone']['next'], '验证该记录是否属于其他账号')
        self.assertNotIn('内部分析', str(semantic))
        self.assertNotIn('执行参数', str(semantic))
        for invalid in ({'kind': 'breakthrough', 'title': '无依据'},
                        {'kind': 'tool_success', 'title': '请求成功', 'basis': '200'},
                        {'kind': [], 'title': '错误类型', 'basis': '无效'}):
            self.assertNotIn('milestone', public_exploration({**value, 'milestone': invalid}))

    def test_existing_v1_session_receives_new_contract_once_without_rewriting_history(self):
        from spiritscout.gateway.progress import ensure_exploration_progress

        state = {'exploration_display_v1': True, 'messages': [{'role': 'user', 'content': '旧展示约定'}]}
        ensure_exploration_progress(state)
        self.assertEqual(state['messages'][0]['content'], '旧展示约定')
        self.assertIn('milestone', state['messages'][-1]['content'])
        ensure_exploration_progress(state)
        self.assertEqual(len(state['messages']), 2)


class ProgressDecisionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_json_decision_produces_progress_without_another_model_call(self) -> None:
        from unittest.mock import AsyncMock

        from spiritscout.gateway.gateway import ChatResponse, _chat_decision_once

        audit = Mock()
        gateway = Mock(chat=AsyncMock(return_value=ChatResponse(
            content='{"progress":"入口已确认，下一步检查权限。","steps":[]}',
            model='fixture', prompt_tokens=0, completion_tokens=0, cost_usd=0, latency_ms=0)))
        token = current_execution_scope.set(ExecutionScope(task_id='task-real', route_id='r'))
        try:
            await _chat_decision_once(gateway, [], actor='worker', audit=audit)
        finally:
            current_execution_scope.reset(token)
        gateway.chat.assert_awaited_once()
        self.assertEqual(audit.log.call_args.args[0], 'agent.progress')
