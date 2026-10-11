"""Agent 模型分配表读写：Web 编队页 → configs/agent_models.yaml。

分配语义：key = AgentType 值（master/recon/web_vuln/...），value = 模型注册表
里的模型 ID；空表/缺 key = 跟随网关档位路由（4.9.1，现状行为）。
校验：模型 ID 必须在注册表中存在（Web 编队页给出下拉，服务端仍独立校验，
不静默接受失效模型——失效模型在运行期会导致路由失败且原因难查）。
写回：tmp + os.replace 原子替换（与 modelcfg 同约定）。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from spiritscout.agents.roster import ROSTER
from spiritscout.core.exceptions import SpiritScoutError

_HEADER = "# Agent 模型分配表——由 Web「编队」页维护，可手工编辑。\n"

_VALID_KEYS = {r["key"] for r in ROSTER}


def read_assignments(path: str | Path) -> dict[str, str]:
    """读取分配表；文件不存在/为空 → {}（全跟随档位路由）。"""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("assignments", {}), dict):
        raise SpiritScoutError(f"Agent 模型分配表格式错误: {p}")
    return {str(k): str(v) for k, v in (data.get("assignments") or {}).items()
            if v}


def write_assignments(path: str | Path, assignments: dict[str, str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    body = yaml.safe_dump({"assignments": assignments},
                          allow_unicode=True, sort_keys=False)
    tmp.write_text(_HEADER + body, encoding="utf-8")
    os.replace(tmp, p)


def set_assignment(path: str | Path, agent_key: str, model: str,
                   valid_models: set[str]) -> dict[str, str]:
    """设置/清除一个 Agent 的模型分配，返回新表。模型为空串 = 清除。"""
    if agent_key not in _VALID_KEYS:
        raise SpiritScoutError(f"未知 Agent: {agent_key}")
    if model and model not in valid_models:
        raise SpiritScoutError(
            f"模型不存在: {model}（请先在「模型」页注册）")
    table = read_assignments(path)
    if model:
        table[agent_key] = model
    else:
        table.pop(agent_key, None)
    write_assignments(path, table)
    return table
