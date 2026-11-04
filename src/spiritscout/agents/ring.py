"""子 Agent 协同台账：共享尝试记录、进展和督导意见。

尝试记录用于提示同伴已有结果；是否复查、换向或结束由模型决定。"""

from __future__ import annotations

import time
from typing import Any

from spiritscout.core.attempts import AttemptLedger, exact_key
from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, BlackboardLayer

# 判定「无产出」的价值阈值：低于此值的已完成尝试对同伴构成避让信号
ZERO_OUTCOME = 0.1


def attempt_key(method: str, url: str, params: dict[str, Any] | None = None) -> str:
    """完整方法、目标与参数的摘要键；仅表达精确相同，不推断意图。"""
    return exact_key(method.upper(), url, params)



class RingLedger:
    """会话内环形共享台账：已尝试登记 + 督导建议（全部子 Agent 与灵枢共持一份）。"""

    def __init__(self, audit: AuditLogger | None = None):
        self._audit = audit or NullAuditLogger()
        self._attempts: dict[str, dict] = {}
        self.exact = AttemptLedger()
        self._steering: dict[str, dict] = {}       # agent_key → 最新督导建议
        self._task_steering: dict[str, dict] = {}
        self._steer_sequence = 0
        self._instructions: dict[str, dict] = {}
        self._blackboard: Any = None

    def bind_blackboard(self, blackboard: Any) -> None:
        self._blackboard = blackboard
        rows = blackboard.query(BlackboardLayer.TOPOLOGY, limit=10000, data_filter=("kind", "instruction"))
        for row in sorted(rows, key=lambda item: item["data"].get("ts", 0)):
            record = {key: value for key, value in row["data"].items() if key != "kind"}
            self._instructions.setdefault(record["id"], record)
            if not record.get("superseded"):
                if record.get("task_id"):
                    self._task_steering[record["task_id"]] = record
                else:
                    self._steering[record["agent"]] = record
        for record in self._instructions.values():
            self._persist_instruction(record)

    def _persist_instruction(self, record: dict) -> None:
        if self._blackboard is not None:
            data = {"kind": "instruction", **record}
            if self._blackboard.read(BlackboardLayer.TOPOLOGY, record["id"]) != data:
                self._blackboard.write(BlackboardLayer.TOPOLOGY, record["id"], data,
                                       agent_type=AgentType.MASTER, updated_by="ring")

    # ------------------------------------------------------------ 已尝试登记

    def lookup_attempt(self, key: str) -> dict | None:
        return self._attempts.get(key)

    def should_avoid(self, key: str, *, agent_id: str) -> dict | None:
        """兼容的低价值历史查询；结果只供参考，执行入口不据此跳过整个步骤。"""
        rec = self._attempts.get(key)
        if rec is None or rec["agent_id"] == agent_id:
            return None
        return rec if rec["outcome"] < ZERO_OUTCOME else None

    def record_attempt(self, key: str, *, agent_id: str, agent_name: str,
                       action: str, outcome: float, note: str) -> None:
        """登记一次已执行尝试（后写不覆盖高价值先写：保留更有信息量的记录）。"""
        old = self._attempts.get(key)
        if old is not None and old["outcome"] >= outcome:
            return
        self._attempts[key] = {
            "agent_id": agent_id, "agent_name": agent_name, "action": action,
            "outcome": round(outcome, 3), "note": note[:200],
            "ts": time.time(),
        }

    # ------------------------------------------------------------ 督导通道

    def steer(self, agent_key: str, hint: str, *, rationale: str = "",
              task_id: str = "", supersedes: list[str] | None = None,
              source_id: str = "") -> str:
        """新指令排队保留；只有明确替代同一收件人的旧指令才使其失效。"""
        if source_id:
            existing = next((record for record in self._instructions.values()
                             if record.get("source_id") == source_id), None)
            if existing is not None:
                return existing["id"]
        for identifier in supersedes or []:
            old = self._instructions.get(identifier)
            if old and old["agent"] == agent_key and old["task_id"] == task_id:
                old["superseded"] = True
                self._persist_instruction(old)
        record = {"id": new_id("steer"), "agent": agent_key,
                  "task_id": task_id, "hint": hint, "rationale": rationale,
                  "feedback": {}, "ts": time.time(), "source_id": source_id}
        self._instructions[record["id"]] = record
        if task_id:
            self._task_steering[task_id] = record
        else:
            self._steering[agent_key] = record
        self._persist_instruction(record)
        self._audit.log("ring.steer", "ring", dict(record))
        return record["id"]

    def steerings_for(self, agent_key: str, *, task_id: str = "") -> list[dict]:
        """公共指令与本任务的未撤回指令同时可见，各路线独立反馈。"""
        return [{**record, "feedback": {key: dict(value) for key, value in record["feedback"].copy().items()
                                       if not task_id or value["task_id"] == task_id}}
                for record in list(self._instructions.values())
                if record["agent"] == agent_key and not record.get("superseded")
                and (not record["task_id"] or record["task_id"] == task_id)]

    def steering_for(self, agent_key: str, *, task_id: str = "") -> dict | None:
        records = self.steerings_for(agent_key, task_id=task_id)
        return records[-1] if records else None

    def steering_feedback(self, agent_key: str, *, task_id: str, route_id: str,
                          instruction_id: str, status: str, detail: str = "") -> bool:
        """记录投递事实或模型自报结果，不把接收指令冒充已执行。"""
        if status not in {"delivered", "accepted", "completed", "blocked"}:
            return False
        record = self._instructions.get(instruction_id)
        if (record is None or record.get("superseded") or record["agent"] != agent_key
                or (record["task_id"] and record["task_id"] != task_id)):
            return False
        key = f"{task_id}|{route_id}"
        previous = record["feedback"].get(key)
        # 每轮重送不覆盖模型反馈，也不重复产生“已接收”事件。
        if status == "delivered" and previous is not None:
            return True
        feedback = {"task_id": task_id, "route_id": route_id,
                    "status": status, "detail": detail[:600]}
        record["feedback"][key] = feedback
        self._persist_instruction(record)
        self._audit.log("ring.steer_feedback", agent_key, {
            "instruction_id": instruction_id, **feedback})
        return True

    # ------------------------------------------------------------ 展示

    def snapshot(self) -> dict[str, Any]:
        """当前台账快照（/state 折入，前端呈现共享与督导状态）。"""
        return {
            "attempts": len(self._attempts),
            "exact_attempts": self.exact.snapshot(),
            # 先复制再遍历（/state 在线程池执行，避开并发登记/写督导）
            "steering": {k: {**v, "feedback": dict(v["feedback"])}
                         for k, v in self._steering.copy().items()},
            "task_steering": {k: {**v, "feedback": dict(v["feedback"])}
                              for k, v in self._task_steering.copy().items()},
        }
