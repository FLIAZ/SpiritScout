"""子 Agent 工作台旁路：引用真实实例任务，独立会话沿用原模式与审批。"""
from __future__ import annotations

import copy
import json
import sqlite3
from typing import Any

from spiritscout.agents.profiles import get_profile
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import AgentType, RunMode


def side_session_source(session: Any, actor: str, task_id: str) -> dict[str, Any]:
    """只读核对任务归属；智能体模式在服务端拒绝，不依赖隐藏按钮。"""
    if session.mode not in {RunMode.SRC.value, RunMode.CTF.value}:
        raise SpiritScoutError("该模式不支持子 Agent 旁路会话")
    if not session.bb_path.exists():
        raise SpiritScoutError("尚无可用于旁路会话的子 Agent 任务")
    with sqlite3.connect(f"file:{session.bb_path}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT data,updated_by FROM bb_records WHERE layer='task_state' AND record_id=?",
                           (task_id,)).fetchone()
        user_context = conn.execute("SELECT data FROM bb_records WHERE layer='target_auth' AND record_id='user-context'").fetchone()
        project = conn.execute("SELECT data FROM bb_records WHERE layer='topology' AND record_id='project-memory'").fetchone()
    if row is None or row[1] != actor:
        raise SpiritScoutError("子 Agent 实例与任务不匹配，请刷新工作台")
    data = json.loads(row[0])
    task = data.get("task") or {}
    (task.get("payload") or {}).pop("source_context", None)
    agent = AgentType(task["agent_type"])
    profile = get_profile(agent)
    if (agent in {AgentType.MASTER, AgentType.SECRETARY, AgentType.REPORT}
            or session.mode not in profile.modes or profile.status != "available"):
        raise SpiritScoutError("该角色不支持旁路执行")
    return copy.deepcopy({"parent_session_id": session.session_id, "actor": actor,
                          "task_id": task_id, "agent": agent.value, "task": task,
                          "routes": data.get("routes", []), "summary": data.get("summary", {}),
                          "user_context": json.loads(user_context[0]) if user_context else {},
                          "project_memory": json.loads(project[0]) if project else {}})
