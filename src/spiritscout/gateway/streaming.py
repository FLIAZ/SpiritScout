"""将未接入专用回调的 Agent 输出节流写入审计，供 SSE 实时回放。"""
from __future__ import annotations

import time
from typing import Any


class AuditStream:
    def __init__(self, audit: Any, actor: str, call: dict[str, Any]):
        self.audit, self.actor, self.call = audit, actor, call
        self.kind = ""
        self.parts: list[str] = []
        self.size = 0
        self.emitted = False
        self.public_raw = ""
        self.public_sent = ""
        self.last = time.monotonic()

    def __call__(self, kind: str, text: str) -> None:
        if kind == "reset":
            self.parts.clear()
            self.size = 0
            self.kind = ""
            self.public_raw = self.public_sent = ""
            self.audit.log("agent.delta", self.actor, {
                "call_id": self.call["call_id"], "route": self.call.get("route_id", ""),
                "task_id": self.call.get("task_id", ""), "kind": "reset", "text": ""})
            return
        # 参数碎片不进入普通审计；工具参数继续走原有加密台账。
        if kind not in {"text", "reasoning"} or not text:
            return
        if kind != self.kind:
            self.flush()
            self.kind = kind
        self.parts.append(text)
        self.size += len(text)
        if self.size >= 240 or time.monotonic() - self.last >= .25:
            self.flush()

    def flush(self) -> None:
        text = "".join(self.parts)
        self.parts.clear()
        self.size = 0
        self.last = time.monotonic()
        if text:
            self.emitted = True
        # JSON 决策的 text 是机器协议，不是公开正文；完整响应会在
        # chat_decision 收尾时经 publish_progress 提取显式 progress/commentary。
        # reasoning 仍写审计供工作台实时观察，正文按 call 元数据整体过滤。
        if self.call.get("response_format") == "json" and self.kind == "text":
            return
        if self.kind == "text":
            from spiritscout.gateway.progress import parse_public_progress

            self.public_raw += text
            public = parse_public_progress(self.public_raw).get("text", "")
            text = public[len(self.public_sent):] if public.startswith(self.public_sent) else ""
            self.public_sent += text
        for start in range(0, len(text), 4000):
            self.audit.log("agent.delta", self.actor, {
                "call_id": self.call["call_id"], "route": self.call.get("route_id", ""),
                "task_id": self.call.get("task_id", ""), "kind": self.kind,
                "text": text[start:start + 4000]})
