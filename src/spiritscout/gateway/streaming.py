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
        self.last = time.monotonic()

    def __call__(self, kind: str, text: str) -> None:
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
        for start in range(0, len(text), 4000):
            self.audit.log("agent.delta", self.actor, {
                "call_id": self.call["call_id"], "route": self.call.get("route_id", ""),
                "task_id": self.call.get("task_id", ""), "kind": self.kind,
                "text": text[start:start + 4000]})
