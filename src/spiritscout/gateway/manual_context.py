"""会话手动压缩请求：持久化意图，由各上下文所有者在调用边界消费。"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from spiritscout.core.models import BlackboardLayer

REQUEST_KEY = "manual-context-request"
RESULT_KIND = "manual-context-result"


def archive_boundary(messages: list[dict[str, Any]]) -> int:
    """为手动请求固定边界，保留最近四次助手回复和所有后续工具结果。"""
    turns = [i for i, message in enumerate(messages) if message.get("role") == "assistant"]
    return turns[-4] if len(turns) > 4 else 0


def excerpt_view(messages: list[dict[str, Any]], boundary: int) -> list[dict[str, Any]]:
    """无历史读取工具的走访上下文只节选旧正文，完整记录仍由所有者保留。"""
    view = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if (2 <= index < boundary and not message.get("_context_contract")
                and message.get("role") in {"assistant", "tool"}
                and isinstance(content, str) and len(content) > 800):
            message = {**message, "content": content[:400] + "\n[早期观察已节选；完整内容见执行记录]\n" + content[-250:]}
        view.append(message)
    return view


def pending_request(blackboard: Any, consumed_id: str = "") -> dict[str, Any] | None:
    if not callable(getattr(blackboard, "read", None)):
        return None
    request = blackboard.read(BlackboardLayer.TOPOLOGY, REQUEST_KEY)
    if isinstance(request, dict) and request.get("request_id") and request["request_id"] != consumed_id:
        return {k: request[k] for k in ("request_id", "requested_at") if k in request}
    return None


def record_result(blackboard: Any, request: dict[str, Any], consumer: str,
                  before: int, after: int, *, audit: Any = None, actor: str = "") -> dict[str, Any]:
    """只记录发送视图的离线估值，不把整理请求误报成所有 Agent 均已完成。"""
    result = {"kind": RESULT_KIND, "request_id": request["request_id"], "consumer": consumer,
              "estimated_tokens_before": before, "estimated_tokens_after": after,
              "changed": after < before, "completed_at": datetime.now(UTC).isoformat()}
    key = hashlib.sha256(consumer.encode()).hexdigest()[:24]
    blackboard.write(BlackboardLayer.TOPOLOGY, f"manual-context-result:{key}", result,
                     updated_by=actor or "context")
    if audit is not None:
        audit.log("context.manual_compacted", actor or "context", result)
    return result
