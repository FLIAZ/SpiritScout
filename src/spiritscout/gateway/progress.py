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


# 放在动态任务消息中，系统提示和工具 schema 保持原前缀；仅标注语义边界。
EXPLORATION_PROGRESS_GUIDANCE = (
    '探索展示：仅开始新意图、改变验证方法或更新尝试结论时，在本次已有公开说明中输出 '
    '{"progress":"简短进展","exploration":{"intent":"要确认的问题","attempt":"验证方法",'
    '"from":"转向前的尝试标题（可省）","reason":"依据（可省）","state":"running"}}。'
    'intent/attempt 用简短稳定标题，同一问题和方法沿用标题；请求、脚本、读取、重试属于其内部执行，'
    '不要逐步建节点。其余回合照常说明并沿用当前尝试，无需重复结构。state 可为 '
    'running/blocked/supported/refuted/exhausted/closed，描述公开验证状态，不输出内部推理；'
    '不为绘图增加调用，不改变原工具决策格式；JSON 决策已有字段保持不变。')



def ensure_exploration_progress(state: dict[str, Any]) -> None:
    """旧会话只在末尾追加一次展示约定，保留已缓存历史前缀。"""
    if state.get('exploration_display_v1'):
        return
    messages = state.setdefault('messages', [])
    if not any(EXPLORATION_PROGRESS_GUIDANCE in str(message.get('content') or '')
               for message in messages if message.get('role') == 'user'):
        messages.append({'role': 'user', 'content': EXPLORATION_PROGRESS_GUIDANCE,
                         '_context_contract': True})
    state['exploration_display_v1'] = True


def public_exploration(value: Any) -> dict[str, str] | None:
    """只接受模型明确声明的公开层级，不分析正文猜意图或提升为事实。"""
    if not isinstance(value, dict):
        return None
    if any(not isinstance(value.get(key), str) or not value[key].strip()
           or len(value[key]) > 100 for key in ('intent', 'attempt')):
        return None
    result = {key: value[key].strip() for key in ('intent', 'attempt')}
    for key, limit in [('from', 100), ('reason', 400)]:
        if isinstance(value.get(key), str) and value[key].strip():
            result[key] = value[key].strip()[:limit]
    if isinstance(value.get('state'), str) and value['state'] in {'running', 'blocked', 'supported', 'refuted', 'exhausted', 'closed'}:
        result['state'] = value['state']
    return result


def publish_progress(audit: Any, actor: str, content: str, **context: Any) -> None:
    """将模型本次已有的公开说明保存为可回放事件，归属到实际任务／路线。"""
    if audit is None:
        return
    text = str(content or '').strip()
    exploration = None
    if text.startswith(('{', '[', '```')):
        try:
            data = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', text))
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        exploration = public_exploration(data.get('exploration'))
        text = '\n\n'.join(dict.fromkeys(value.strip() for key in ('progress', 'commentary')
                                         if isinstance(value := data.get(key), str) and value.strip()))
    if not text:
        return
    scope = current_execution_scope.get()
    detail = {key: getattr(scope, key, '') for key in ('task_id', 'route_id', 'step_id', 'turn')}
    detail.update(context)
    if not detail.get('task_id'):
        return
    audit.log('agent.progress', actor, {**detail, 'text': text[:2400],
                                     **({'exploration': exploration} if exploration else {})})
