"""模型注册表的文件读写（Web 模型管理）。

安全边界（合规 G8）：密钥不落盘。落盘的 api_key 只允许 ``${ENV_VAR}``
占位符形式；Web 直填的密钥只保存在本进程的内存保管库（控制台重启即
失效，需重新填写——这是「不落盘」的代价，无人值守部署请用环境变量
通道）。本模块读写给前端的永远是 YAML 原文（占位符不展开），已解析
的密钥只存在于内存 ModelEntry 中，API 绝不回传。
写回用 PyYAML 规范化（手写注释不保留），原子替换防半写损坏。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from spiritscout.config.settings import ModelEntry
from spiritscout.core.exceptions import SpiritScoutError

_HEADER = """\
# 模型注册表（设计书 4.9.4，硬约束 C9：OpenAI/Anthropic 兼容 API）。
# 本文件可被 Web 控制台「模型」面板写回（手写注释会被规范化；完整注释模板见
# configs/models.example.yaml）。api_key 仅允许 ${ENV_VAR} 注入（合规 G8）。
"""

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def normalize_api_key(raw: str) -> str:
    """把用户输入规范为 ${VAR} 占位符（空留空；已是 ${} 形式原样）。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if _ENV_REF.match(raw):
        return raw
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
        return "${" + raw + "}"
    raise SpiritScoutError(
        "api_key 只允许填环境变量名（如 SPIRITSCOUT_LLM_API_KEY）"
        "或 ${SPIRITSCOUT_LLM_API_KEY} 形式（G8）；"
        "直填密钥请用 api_key_value 字段（仅存内存，不落盘）")


# ------------------------------------------------------------ 内存密钥保管库
# Web 直填的密钥只活在本 dict 里（按 alias 索引），永不写盘、API 永不回传
_MEMORY_KEYS: dict[str, str] = {}


def set_memory_key(alias: str, key: str) -> None:
    """登记 Web 直填密钥（空串 = 清除）。"""
    if key:
        _MEMORY_KEYS[alias] = key
    else:
        _MEMORY_KEYS.pop(alias, None)


def drop_memory_key(alias: str) -> None:
    _MEMORY_KEYS.pop(alias, None)


def rename_memory_key(old_alias: str, new_alias: str) -> None:
    if old_alias != new_alias and old_alias in _MEMORY_KEYS:
        _MEMORY_KEYS[new_alias] = _MEMORY_KEYS.pop(old_alias)


def apply_memory_keys(models: list[ModelEntry]) -> list[ModelEntry]:
    """内存密钥覆盖到加载后的注册表（内存直填优先于环境变量占位符）。"""
    return [m.model_copy(update={"api_key": _MEMORY_KEYS[m.alias]})
            if m.alias in _MEMORY_KEYS else m for m in models]


def key_status(entry: dict[str, Any]) -> dict[str, Any]:
    """从原始条目提取 key 状态（不展开、不回传密钥本身）。

    key_source：memory（Web 直填，仅内存）/ env（环境变量）/ none（未配置）。
    """
    api_key = str(entry.get("api_key") or "")
    m = _ENV_REF.match(api_key)
    env = m.group(1) if m else ""
    alias = str(entry.get("alias") or "")
    if alias in _MEMORY_KEYS:
        source = "memory"
    elif env and os.environ.get(env):
        source = "env"
    else:
        source = "none"
    return {
        "key_env": env,
        "key_source": source,
        "key_configured": source != "none",
        "key_is_placeholder": bool(m) or not api_key,
    }


def read_entries(path: str | Path) -> list[dict[str, Any]]:
    """读原始 YAML 条目（api_key 保持 ${VAR} 占位符，不展开）。"""
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    models = data.get("models", [])
    if not isinstance(models, list):
        raise SpiritScoutError(f"模型配置文件 models 必须是列表: {p}")
    return [m for m in models if isinstance(m, dict)]


def _validate(entries: list[dict[str, Any]]) -> None:
    aliases: set[str] = set()
    for entry in entries:
        try:
            e = ModelEntry.model_validate(entry)
        except Exception as exc:
            raise SpiritScoutError(
                f"模型条目校验失败（{entry.get('alias', '?')}）: {exc}") from exc
        if e.provider not in ("openai_compatible", "anthropic_compatible"):
            raise SpiritScoutError(
                f"不支持的模型提供商（{e.alias}）: {e.provider}——"
                "Web 端仅配置云端兼容端点（openai_compatible / anthropic_compatible）")
        if not e.model.strip():
            raise SpiritScoutError(
                f"模型条目缺少 model 名称（{e.alias}）：云端提供商必须填写"
                "模型名或推理端点 ID")
        alias = e.alias
        if alias in aliases:
            raise SpiritScoutError(f"模型 alias 重复: {alias}")
        aliases.add(alias)


def write_entries(path: str | Path, entries: list[dict[str, Any]]) -> None:
    """校验后原子写回（tmp + replace，防半写损坏）。"""
    _validate(entries)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = _HEADER + yaml.safe_dump(
        {"models": entries}, allow_unicode=True, sort_keys=False, width=120)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".models-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def upsert_entry(path: str | Path, entry: dict[str, Any], *,
                 original_alias: str | None = None) -> list[dict[str, Any]]:
    """新增（original_alias=None）或更新；alias 冲突/不存在抛 SpiritScoutError。"""
    entry = dict(entry, api_key=normalize_api_key(str(entry.get("api_key") or "")))
    entries = read_entries(path)
    if original_alias is None:
        if any(e.get("alias") == entry.get("alias") for e in entries):
            raise SpiritScoutError(f"模型 alias 已存在: {entry.get('alias')}")
        entries.append(entry)
    else:
        for i, e in enumerate(entries):
            if e.get("alias") == original_alias:
                if (entry.get("alias") != original_alias
                        and any(x.get("alias") == entry.get("alias") for x in entries)):
                    raise SpiritScoutError(f"模型 alias 已存在: {entry.get('alias')}")
                entries[i] = entry
                break
        else:
            raise SpiritScoutError(f"模型不存在: {original_alias}")
    write_entries(path, entries)
    return entries


def delete_entry(path: str | Path, alias: str) -> list[dict[str, Any]]:
    entries = read_entries(path)
    remaining = [e for e in entries if e.get("alias") != alias]
    if len(remaining) == len(entries):
        raise SpiritScoutError(f"模型不存在: {alias}")
    write_entries(path, remaining)
    return remaining
