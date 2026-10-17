"""Agent 模型分配表读写：Web 编队页 → configs/agent_models.yaml。

分配语义：key = AgentType 值（master/recon/web_vuln/...），value = 模型注册表
里的模型 ID；空表/缺 key = 跟随网关档位路由（4.9.1，现状行为）。
同文件还存 naming（会话命名候选链，模型页维护的降级顺序）——两段一起
读、一起原子写回，防互相覆盖。
校验：模型 ID 必须在注册表中存在（Web 编队页给出下拉，服务端仍独立校验，
不静默接受失效模型——失效模型在运行期会导致路由失败且原因难查）。
写回：tmp + os.replace 原子替换（与 modelcfg 同约定）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from spiritscout.agents.profiles import PROFILES
from spiritscout.core.exceptions import SpiritScoutError

_HEADER = "# Agent 模型分配表——由 Web「编队」页维护（命名候选链由「模型」页维护），可手工编辑。\n"

_VALID_KEYS = {profile.key.value for profile in PROFILES if profile.configurable}


def _read_doc(path: str | Path) -> dict[str, Any]:
    """读整份文档（assignments + naming）：写回时两段互相保留的单一事实源。"""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SpiritScoutError(f"Agent 模型分配表格式错误: {p}")
    return data


def _write_doc(path: str | Path, assignments: dict[str, str],
               naming: list[str]) -> None:
    """整文件原子写回：分配表与命名链一次落盘（naming 为空不落键）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    doc: dict[str, Any] = {
        "assignments": {str(k): str(v) for k, v in assignments.items()
                        if v and str(k) in _VALID_KEYS}}
    if naming:
        doc["naming"] = list(naming)
    tmp.write_text(_HEADER + yaml.safe_dump(doc, allow_unicode=True,
                                            sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, p)


def read_assignments(path: str | Path) -> dict[str, str]:
    """读取分配表；文件不存在/为空 → {}（全跟随档位路由）。"""
    data = _read_doc(path)
    if not isinstance(data.get("assignments", {}), dict):
        raise SpiritScoutError(f"Agent 模型分配表格式错误: {path}")
    return {str(k): str(v) for k, v in (data.get("assignments") or {}).items()
            if v and str(k) in _VALID_KEYS}


def write_assignments(path: str | Path, assignments: dict[str, str]) -> None:
    """写分配表；命名链原样保留（同一文件两段，一起读一起写）。"""
    _write_doc(path, dict(assignments), read_naming_chain(path))


def read_naming_chain(path: str | Path) -> list[str]:
    """会话命名候选链（用户在「模型」页排的降级顺序）；缺文件/缺键 → []。"""
    raw = _read_doc(path).get("naming") or []
    if not isinstance(raw, list):
        raise SpiritScoutError(f"命名候选链格式错误: {path}")
    chain: list[str] = []
    for item in raw:
        mid = str(item).strip()
        if mid and mid not in chain:
            chain.append(mid)
    return chain


def set_naming_chain(path: str | Path, models: list[str],
                     valid_models: set[str]) -> list[str]:
    """整链写回：顺序即降级顺序（完全由用户决定），逐项校验在注册表中。"""
    chain: list[str] = []
    for item in models:
        mid = str(item).strip()
        if not mid:
            continue
        if mid not in valid_models:
            raise SpiritScoutError(
                f"模型不存在: {mid}（请先在「模型」页注册）")
        if mid not in chain:
            chain.append(mid)
    _write_doc(path, read_assignments(path), chain)
    return chain


def migrate_model(path: str | Path, old: str, new: str) -> None:
    """模型改名后同步迁移指派与命名链（modelcfg 改名流程共用）。"""
    table = read_assignments(path)
    chain = read_naming_chain(path)
    _write_doc(path,
               {key: (new if value == old else value)
                for key, value in table.items()},
               [new if mid == old else mid for mid in chain])


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
