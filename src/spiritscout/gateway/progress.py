"""面向用户的阶段进展；只转发公开正文，不读取推理或另发模型请求。"""
from __future__ import annotations

import json
import re
from typing import Any

from spiritscout.tools.invocation import current_execution_scope

NATIVE_PROGRESS_GUIDANCE = (
    "\n阶段进展：开始一段子任务、得到重要观察、遇到阻碍或改变方向时，"
    "先用一到三句公开正文向用户说明正在验证什么、已确认什么、尚缺什么以及下一步。"
    "说明应随本次正常工具决策一起输出，不要只给工具调用，也不要重复工具参数、"
    "原始响应或内部推理；没有新结果时明确仍在验证，不编造进度百分比。")


def publish_progress(audit: Any, actor: str, content: str, **context: Any) -> None:
    """将模型本次已有的公开说明保存为可回放事件，归属到实际任务／路线。"""
    if audit is None:
        return
    text = str(content or '').strip()
    if text.startswith(('{', '[', '```')):
        try:
            data = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', text))
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        text = '\n\n'.join(dict.fromkeys(value.strip() for key in ('progress', 'commentary')
                                         if isinstance(value := data.get(key), str) and value.strip()))
    if not text:
        return
    scope = current_execution_scope.get()
    detail = {key: getattr(scope, key, '') for key in ('task_id', 'route_id', 'step_id', 'turn')}
    detail.update(context)
    if not detail.get('task_id'):
        return
    audit.log('agent.progress', actor, {**detail, 'text': text[:2400]})
