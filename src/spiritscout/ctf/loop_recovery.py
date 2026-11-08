"""重复结果后的显式行动复核；同一模型选择动作，不预设解法或终止调查。"""
from __future__ import annotations

import copy
import json
from typing import Any

NAME = 'revise_step'


def activate(state: dict[str, Any], *, repeat_requests: bool = False) -> bool:
    reason = state.get('review_due') or {}
    same_result = reason.get('same_result_count', 0) >= 2 and (
        state.get('last_evidence_request') or {}).get('count', 0) >= 2
    same_failure = reason.get('failed') and reason.get('same_failure_count', 0) >= 2
    # 修补场景中的目录时间戳等会变化；参数持续重复也值得复核，但不宣称结果相同。
    repeated_request = repeat_requests and reason.get('same_request_count', 0) >= 3
    repeated_issue = repeat_requests and reason.get('failed') and reason.get('same_issue_count', 0) >= 2
    if same_result or same_failure or repeated_request or repeated_issue:
        state.setdefault('loop_recovery', {'trigger': copy.deepcopy(reason)})
    return bool(state.get('loop_recovery'))


def tool_schema(tools: list[dict[str, Any]]) -> dict[str, Any]:
    names = [tool['function']['name'] for tool in tools if tool['function']['name'] not in {NAME, 'tool_invoke'}]
    return {'type': 'function', 'function': {'name': NAME,
        'description': '重复结果后重新对照原始要求，再选择并执行一个工具动作；可有依据重试，不替模型决定解法。',
        'parameters': {'type': 'object', 'properties': {
            'requirement': {'type': 'string', 'maxLength': 800,
                'description': '当前原始目标/附件说明中的相关要求，不能只复述自己的上一轮计划'},
            'adjustment': {'type': 'string', 'maxLength': 800,
                'description': '结果与要求的差距、下一动作如何处理；若原样重试，说明条件变化或必要对照依据'},
            'name': {'type': 'string', 'enum': names, 'description': '自主选择下一工具；不清楚参数可选 tool_lookup'},
            'arguments': {'type': 'object', 'description': '该工具的完整参数，授权与执行契约不变'}},
            'required': ['requirement', 'adjustment', 'name', 'arguments']}}}


def message(state: dict[str, Any]) -> dict[str, Any] | None:
    if not state.get('loop_recovery'):
        return None
    return {'role': 'user', '_context_contract': True, '_light_kind': 'loop_recovery', 'content': (
        '当前进入重复结果的行动复核：对照原始用户目标和附件说明，指出当前差距，'
        '直接使用当前提供的真实工具和完整参数，不再调用 revise_step 或猜测工具名。'
        '先前自己的计划可能有误，不能把它当作题目要求。'
        '参数重复而输出不同也可能是正常轮询；说明实际变化与继续执行的依据。'
        '动作仍由你决定；可核对原文、修正方法、补验证、登记已有证据或说明缺口，'
        '有明确条件变化/必要对照依据时也可原样重试。真实工具的契约和授权不变。\n'
        + json.dumps(state['loop_recovery'], ensure_ascii=False))}
