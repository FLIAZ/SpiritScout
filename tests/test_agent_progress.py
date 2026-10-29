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
