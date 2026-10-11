"""模型注册表的文件读写（Web 模型管理）。

文件分两段：``providers``（供应商：base_url + 密钥 + 协议）与 ``models``
（模型：能力画像/档位/思考强度）。一个供应商下挂多个模型，接入信息不重复存。

安全边界（合规 G8）：密钥不以任何形式落进 YAML。Web 直填的密钥加密存入
本机密钥库（见 vault 模块），装载时按供应商名注入内存 ModelEntry；
本模块读写给前端的永远不含密钥，API 绝不回传。
写回用 PyYAML 规范化（手写注释不保留），原子替换防半写损坏。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from spiritscout.config.settings import ModelEntry, ProviderEntry
from spiritscout.core.exceptions import SpiritScoutError

_HEADER = """\
# 模型注册表（设计书 4.9.4，硬约束 C9：OpenAI/Anthropic 兼容 API）。
# 本文件可被 Web 控制台「模型」面板写回（手写注释会被规范化；完整注释模板见
# configs/models.example.yaml）。密钥不落本文件（G8）——在 Web「模型」页直填，
# 加密存入本机密钥库。
"""

_CLOUD_PROVIDERS = ("openai_compatible", "anthropic_compatible")


def normalize_api_key(raw: str) -> str:
    """密钥不落 YAML：api_key 字段只接受空值；直填密钥走 api_key_value（入密钥库）。"""
    if (raw or "").strip():
        raise SpiritScoutError(
            "密钥不写进配置文件（G8）——请用 api_key_value 字段直填"
            "（加密存入本机密钥库）")
    return ""


# ------------------------------------------------------------ 读


def read_config(path: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读原始配置（api_key 保持 ${VAR} 占位符，不展开）→ (providers, models)。"""
    p = Path(path)
    if not p.exists():
        return [], []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    providers = data.get("providers") or []
    models = data.get("models") or []
    if not isinstance(providers, list) or not isinstance(models, list):
        raise SpiritScoutError(f"模型配置文件 providers/models 必须是列表: {p}")
    return ([x for x in providers if isinstance(x, dict)],
            [m for m in models if isinstance(m, dict)])


def read_providers(path: str | Path) -> list[dict[str, Any]]:
    return read_config(path)[0]


def read_entries(path: str | Path) -> list[dict[str, Any]]:
    return read_config(path)[1]


# ------------------------------------------------------------ 校验 / 写


def _validate(providers: list[dict[str, Any]],
              models: list[dict[str, Any]]) -> None:
    names: set[str] = set()
    for raw in providers:
        try:
            p = ProviderEntry.model_validate(raw)
        except Exception as exc:
            raise SpiritScoutError(
                f"供应商条目校验失败（{raw.get('name', '?')}）: {exc}") from exc
        if p.provider not in (*_CLOUD_PROVIDERS, "mock"):
            raise SpiritScoutError(
                f"不支持的供应商类型（{p.name}）: {p.provider}——"
                "Web 端仅配置云端兼容端点（openai_compatible / anthropic_compatible）")
        if p.provider in _CLOUD_PROVIDERS and not p.base_url.strip():
            raise SpiritScoutError(f"供应商缺少 base_url: {p.name}")
        if p.name in names:
            raise SpiritScoutError(f"供应商名重复: {p.name}")
        names.add(p.name)

    seen: set[str] = set()
    for entry in models:
        try:
            e = ModelEntry.model_validate(entry)
        except Exception as exc:
            raise SpiritScoutError(
                f"模型条目校验失败（{entry.get('model', '?')}）: {exc}") from exc
        if e.provider_ref and e.provider_ref not in names:
            raise SpiritScoutError(
                f"模型 {e.model} 引用了不存在的供应商: {e.provider_ref}")
        if e.model in seen:
            raise SpiritScoutError(f"模型 ID 重复: {e.model}")
        seen.add(e.model)


def write_config(path: str | Path, providers: list[dict[str, Any]],
                 models: list[dict[str, Any]]) -> None:
    """校验后原子写回（tmp + replace，防半写损坏）。"""
    _validate(providers, models)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {"providers": providers, "models": models}
    text = _HEADER + yaml.safe_dump(body, allow_unicode=True, sort_keys=False,
                                    width=120)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".models-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------ 供应商


def upsert_provider(path: str | Path, provider: dict[str, Any], *,
                    original_name: str | None = None) -> list[dict[str, Any]]:
    """新增（original_name=None）或更新供应商；重名/不存在抛 SpiritScoutError。"""
    provider = dict(provider,
                    api_key=normalize_api_key(str(provider.get("api_key") or "")))
    providers, models = read_config(path)
    if original_name is None:
        if any(p.get("name") == provider.get("name") for p in providers):
            raise SpiritScoutError(f"供应商已存在: {provider.get('name')}")
        providers.append(provider)
    else:
        for i, p in enumerate(providers):
            if p.get("name") == original_name:
                if (provider.get("name") != original_name
                        and any(x.get("name") == provider.get("name")
                                for x in providers)):
                    raise SpiritScoutError(f"供应商已存在: {provider.get('name')}")
                providers[i] = provider
                break
        else:
            raise SpiritScoutError(f"供应商不存在: {original_name}")
        if provider.get("name") != original_name:
            # 供应商改名：名下模型的引用跟着改（模型归属不悬空）
            new_name = str(provider.get("name"))
            models = [dict(m, provider_ref=new_name)
                      if m.get("provider_ref") == original_name else m
                      for m in models]
    write_config(path, providers, models)
    return providers


def delete_provider(path: str | Path, name: str) -> list[dict[str, Any]]:
    """删除供应商；名下仍有模型时拒绝（避免模型悬空）。"""
    providers, models = read_config(path)
    remaining = [p for p in providers if p.get("name") != name]
    if len(remaining) == len(providers):
        raise SpiritScoutError(f"供应商不存在: {name}")
    used = [str(m.get("model")) for m in models if m.get("provider_ref") == name]
    if used:
        raise SpiritScoutError(
            f"供应商 {name} 下还有 {len(used)} 个模型（{', '.join(used[:5])}"
            f"{'…' if len(used) > 5 else ''}），请先删除这些模型")
    write_config(path, remaining, models)
    return remaining


# ------------------------------------------------------------ 模型


def upsert_entry(path: str | Path, entry: dict[str, Any], *,
                 original_model: str | None = None) -> list[dict[str, Any]]:
    """新增（original_model=None）或更新模型；ID 冲突/不存在抛 SpiritScoutError。"""
    entry = dict(entry)
    entry.pop("api_key", None)      # 密钥属于供应商，模型条目不带密钥字段
    providers, entries = read_config(path)
    if original_model is None:
        if any(e.get("model") == entry.get("model") for e in entries):
            raise SpiritScoutError(f"模型已存在: {entry.get('model')}")
        entries.append(entry)
    else:
        for i, e in enumerate(entries):
            if e.get("model") == original_model:
                if (entry.get("model") != original_model
                        and any(x.get("model") == entry.get("model") for x in entries)):
                    raise SpiritScoutError(f"模型已存在: {entry.get('model')}")
                entries[i] = entry
                break
        else:
            raise SpiritScoutError(f"模型不存在: {original_model}")
    write_config(path, providers, entries)
    return entries


def add_entries(path: str | Path, provider_ref: str, model_ids: list[str], *,
                tier: str = "fast") -> tuple[list[dict[str, Any]], list[str]]:
    """批量启用某供应商下的模型（从云端拉取的可用列表里勾选）。

    返回 (全部模型, 本次新增的模型 ID)；已存在的跳过（不覆盖既有画像设置）。
    """
    providers, entries = read_config(path)
    if not any(p.get("name") == provider_ref for p in providers):
        raise SpiritScoutError(f"供应商不存在: {provider_ref}")
    existing = {str(e.get("model")) for e in entries}
    added: list[str] = []
    for raw_id in model_ids:
        mid = str(raw_id).strip()
        if not mid or mid in existing:
            continue
        entries.append({"model": mid, "provider_ref": provider_ref, "tier": tier,
                        "capabilities": [], "context_window": 0,
                        "reasoning_effort_levels": [], "is_fallback_for": []})
        existing.add(mid)
        added.append(mid)
    write_config(path, providers, entries)
    return entries, added


def delete_entry(path: str | Path, model: str) -> list[dict[str, Any]]:
    providers, entries = read_config(path)
    remaining = [e for e in entries if e.get("model") != model]
    if len(remaining) == len(entries):
        raise SpiritScoutError(f"模型不存在: {model}")
    write_config(path, providers, remaining)
    return remaining
