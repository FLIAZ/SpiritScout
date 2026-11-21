"""子 Agent 独立对话，可选引用实例任务，沿用原模式与审批。"""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from typing import Any

from spiritscout.agents.profiles import get_profile
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import AgentType, RunMode


def side_session_source(session: Any, actor: str, task_id: str) -> dict[str, Any]:
    """只读核对角色及可选任务归属；智能体模式在服务端拒绝。"""
    if session.mode not in {RunMode.SRC.value, RunMode.CTF.value}:
        raise SpiritScoutError("该模式不支持子 Agent 旁路会话")
    row = user_context = project = None
    if session.bb_path.exists():
        with closing(sqlite3.connect(session.bb_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if task_id:
                row = conn.execute("SELECT data,updated_by FROM bb_records WHERE layer='task_state' AND record_id=?",
                                   (task_id,)).fetchone()
            else:
                row = conn.execute("SELECT data,updated_by FROM bb_records WHERE layer='task_state' AND updated_by=? ORDER BY updated_at DESC LIMIT 1",
                                   (actor,)).fetchone()
            user_context = conn.execute("SELECT data FROM bb_records WHERE layer='target_auth' AND record_id='user-context'").fetchone()
            project = conn.execute("SELECT data FROM bb_records WHERE layer='topology' AND record_id='project-memory'").fetchone()
    if task_id and (row is None or row[1] != actor):
        raise SpiritScoutError("子 Agent 实例与任务不匹配，请刷新工作台")
    data = json.loads(row[0]) if row else {}
    # 无任务时只解析角色身份，不把恰好在执行的任务注入独立对话。
    agent = AgentType((data.get("task") or {}).get("agent_type") or actor)
    task = (data.get("task") or {}) if task_id else {}
    (task.get("payload") or {}).pop("source_context", None)
    profile = get_profile(agent)
    if (agent in {AgentType.MASTER, AgentType.SECRETARY, AgentType.REPORT}
            or session.mode not in profile.modes or profile.status != "available"):
        raise SpiritScoutError("该角色不支持旁路执行")
    return copy.deepcopy({"parent_session_id": session.session_id, "actor": actor,
                          "task_id": task_id, "agent": agent.value, "task": task,
                          "routes": data.get("routes", []) if task_id else [],
                          "summary": data.get("summary", {}) if task_id else {},
                          "user_context": json.loads(user_context[0]) if user_context else {},
                          "project_memory": json.loads(project[0]) if project else {}})
