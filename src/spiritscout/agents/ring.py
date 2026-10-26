"""子 Agent 协同台账：共享尝试记录、进展和督导意见。

尝试记录用于提示同伴已有结果；是否复查、换向或结束由模型决定。"""

from __future__ import annotations

import time
from typing import Any

from spiritscout.core.audit import AuditLogger, NullAuditLogger

# 判定「无产出」的价值阈值：低于此值的已完成尝试对同伴构成避让信号
ZERO_OUTCOME = 0.1


def attempt_key(method: str, url: str, params: dict[str, Any] | None = None) -> str:
    """探测尝试的归一化键：方法 + URL + 参数形状（键=值截断，排序去序）。"""
    shape = ""
    if params:
        shape = "|".join(sorted(f"{k}={str(v)[:32]}" for k, v in params.items()))[:200]
    return f"{method.upper()} {url} [{shape}]"


class RingLedger:
    """会话内环形共享台账：已尝试登记 + 督导建议（全部子 Agent 与灵枢共持一份）。"""

    def __init__(self, audit: AuditLogger | None = None):
        self._audit = audit or NullAuditLogger()
        self._attempts: dict[str, dict] = {}
        self._steering: dict[str, dict] = {}       # agent_key → 最新督导建议
        self._task_steering: dict[str, dict] = {}
        self._steer_sequence = 0

    # ------------------------------------------------------------ 已尝试登记

    def lookup_attempt(self, key: str) -> dict | None:
        return self._attempts.get(key)

    def should_avoid(self, key: str, *, agent_id: str) -> dict | None:
        """同伴已完成的无产出尝试 → 避让（返回登记条目）；否则 None。

        同一 Agent 自己的历史尝试不触发避让（其路线内去重自行负责）；
        有产出的尝试不避让（有价值痕迹值得各自复核）。
        """
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
              task_id: str = "") -> None:
        """仅替换同一收件任务的旧指令；未指定任务表示该类型的公共指令。"""
        self._steer_sequence += 1
        record = {
            "id": f"steer-{self._steer_sequence}", "agent": agent_key,
            "task_id": task_id, "hint": hint[:2000], "rationale": rationale[:600],
            "feedback": {},
            "ts": time.time(),
        }
        if task_id:
            self._task_steering[task_id] = record
        else:
            self._steering[agent_key] = record
        self._audit.log("ring.steer", "ring", dict(record))

    def steerings_for(self, agent_key: str, *, task_id: str = "") -> list[dict]:
        """公共指令与本任务指令同时可见，别题指令不可见。"""
        return [{**r, "feedback": {k: dict(v) for k, v in r["feedback"].copy().items()
                                    if not task_id or v["task_id"] == task_id}}
                for r in (self._steering.get(agent_key),
                                  self._task_steering.get(task_id))
                if r is not None and r["agent"] == agent_key]

    def steering_for(self, agent_key: str, *, task_id: str = "") -> dict | None:
        records = self.steerings_for(agent_key, task_id=task_id)
        return records[-1] if records else None

    def steering_feedback(self, agent_key: str, *, task_id: str, route_id: str,
                          instruction_id: str, status: str, detail: str = "") -> bool:
        """记录投递事实或模型自报结果，不把接收指令冒充已执行。"""
        if status not in {"delivered", "accepted", "completed", "blocked"}:
            return False
        record = next((r for r in (self._steering.get(agent_key),
                                  self._task_steering.get(task_id))
                       if r and r["agent"] == agent_key and r["id"] == instruction_id), None)
        if record is None:
            return False
        key = f"{task_id}|{route_id}"
        previous = record["feedback"].get(key)
        # 每轮重送不覆盖模型反馈，也不重复产生“已接收”事件。
        if status == "delivered" and previous is not None:
            return True
        feedback = {"task_id": task_id, "route_id": route_id,
                    "status": status, "detail": detail[:600]}
        record["feedback"][key] = feedback
        self._audit.log("ring.steer_feedback", agent_key, {
            "instruction_id": instruction_id, **feedback})
        return True

    # ------------------------------------------------------------ 展示

    def snapshot(self) -> dict[str, Any]:
        """当前台账快照（/state 折入，前端呈现共享与督导状态）。"""
        return {
            "attempts": len(self._attempts),
            # 先复制再遍历（/state 在线程池执行，避开并发登记/写督导）
            "steering": {k: {**v, "feedback": dict(v["feedback"])}
                         for k, v in self._steering.copy().items()},
            "task_steering": {k: {**v, "feedback": dict(v["feedback"])}
                              for k, v in self._task_steering.copy().items()},
        }
