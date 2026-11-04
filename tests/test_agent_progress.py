"""公开进展复用现有模型正文，不暴露推理或执行协议。"""
from __future__ import annotations

import unittest
from unittest.mock import Mock

from spiritscout.gateway.progress import publish_progress
from spiritscout.tools.invocation import ExecutionScope, current_execution_scope


class AgentProgressTests(unittest.TestCase):
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
