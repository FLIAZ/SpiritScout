"""环形协同底座（AWE 环形对等节点）：子 Agent 间实时共享进度与已尝试
路线登记 + 灵枢督导通道。

- **已尝试登记（attempt ledger）**：每次网络探测按「方法+URL+参数形状」
  归一化登记结果价值；后续 Agent 发现同一入口已被同伴尝试过且无产出时，
  跳过深入——环形节点互不等候、互不重复做功。
- **督导通道（steering）**：灵枢按实时进度为各子 Agent 写下方向建议，
  子 Agent 在下一次 LLM 决策的上下文中消费（LLM 提议、规则放行：
  建议是上下文而非指令，执行仍走原确定性通道）。

进程内状态，随会话消亡（协调语义不落盘；路线笔记已各自持久化）。
本模块的确定性行为只有三件：登记、查重、投递。
"""

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

    def steer(self, agent_key: str, hint: str, *, rationale: str = "") -> None:
        """灵枢写下对某子 Agent 的方向建议（新的覆盖旧的——只保留当前有效思路）。"""
        self._steering[agent_key] = {
            "hint": hint[:300], "rationale": rationale[:200],
            "ts": time.time(),
        }
        self._audit.log("ring.steer", "ring", {
            "agent": agent_key, "hint": hint[:300], "rationale": rationale[:200]})

    def steering_for(self, agent_key: str) -> dict | None:
        return self._steering.get(agent_key)

    # ------------------------------------------------------------ 展示

    def snapshot(self) -> dict[str, Any]:
        """当前台账快照（/state 折入，前端呈现共享与督导状态）。"""
        return {
            "attempts": len(self._attempts),
            # 先复制再遍历（/state 在线程池执行，避开并发登记/写督导）
            "steering": {k: dict(v) for k, v in self._steering.copy().items()},
        }
