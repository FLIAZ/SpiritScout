"""供应商与模型配置的读取、校验和原子保存。

YAML 只存接入配置；密钥由本机密钥库按供应商注入，API 不返回密钥。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from spiritscout.config.settings import ModelEntry, ProviderEntry
from spiritscout.core.exceptions import ConfigurationError, SpiritScoutError

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


def canonical_provider(provider: dict[str, Any]) -> dict[str, Any]:
    """请求体 → 规范供应商条目：只用已声明字段重新序列化（api_key 置空）。

    未知字段不透传——请求体原样写盘会让污染字段进 YAML、读接口回吐；
    密钥永远不在这里（G8），直填密钥走 api_key_value 由调用方入密钥库。
    """
    try:
        validated = ProviderEntry.model_validate(
            {k: v for k, v in provider.items() if k in ProviderEntry.model_fields})
    except Exception as exc:
        raise SpiritScoutError(f"供应商条目校验失败: {exc}") from exc
    data = validated.model_dump()
    data["api_key"] = normalize_api_key(str(data.get("api_key") or ""))
    return data


def canonical_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """请求体 → 规范模型条目：只用已声明字段重新序列化。

    模型接口收到密钥字段直接拒绝（密钥属于供应商，走供应商的
    api_key_value）；其余未知字段丢弃，不进 YAML、不回传。
    """
    if "api_key_value" in entry:
        raise SpiritScoutError(
            "密钥属于供应商：请在模型所属供应商里设置密钥"
            "（api_key_value，加密入密钥库）——模型接口不接受密钥字段")
    if str(entry.get("api_key") or "").strip():
        raise SpiritScoutError(
            "密钥不写进配置文件（G8）——请用供应商的 api_key_value 字段直填")
    try:
        validated = ModelEntry.model_validate(
            {k: v for k, v in entry.items() if k in ModelEntry.model_fields})
    except Exception as exc:
        raise SpiritScoutError(f"模型条目校验失败: {exc}") from exc
    data = validated.model_dump()
    data.pop("api_key", None)      # 密钥属于供应商，模型条目不落密钥字段
    return data


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
        provider = next((p for p in providers if p.get("name") == e.provider_ref), {})
        resolved = e.model_copy(update={
            "provider": provider.get("provider", e.provider),
            "api_format": provider.get("api_format") if provider.get("api_format", "auto") != "auto" else e.api_format})
        try:
            resolved.validate_cache_transport()
        except ConfigurationError as exc:
            raise SpiritScoutError(str(exc)) from exc
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
    provider = canonical_provider(provider)
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
                 original_model: str | None = None,
                 assignments_path: str | Path | None = None) -> list[dict[str, Any]]:
    """新增（original_model=None）或更新模型；ID 冲突/不存在抛 SpiritScoutError。"""
    entry = canonical_entry(entry)   # 密钥字段拒绝、未知字段丢弃、规范化
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
    if original_model and original_model != entry["model"]:
        entries = [dict(e, is_fallback_for=[entry["model"] if m == original_model else m
                    for m in e.get("is_fallback_for", [])]) for e in entries]
    if assignments_path and original_model and original_model != entry["model"]:
        from spiritscout.web.agentcfg import migrate_model, read_assignments

        table = read_assignments(assignments_path)
        # 配置校验先完成；写盘异常回滚编队，避免静默留悬空引用。
        _validate(providers, entries)
        migrate_model(assignments_path, original_model, entry["model"])
        try:
            write_config(path, providers, entries)
        except BaseException:
            from spiritscout.web.agentcfg import write_assignments
            write_assignments(assignments_path, table)
            raise
    else:
        write_config(path, providers, entries)
    return entries


def add_entries(path: str | Path, provider_ref: str,
                model_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
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
        entries.append({"model": mid, "provider_ref": provider_ref,
                        "capabilities": [], "context_window": 0,
                        "reasoning_effort_levels": [], "is_fallback_for": []})
        existing.add(mid)
        added.append(mid)
    write_config(path, providers, entries)
    return entries, added


def delete_entry(path: str | Path, model: str, *, assignments_path: str | Path | None = None) -> list[dict[str, Any]]:
    providers, entries = read_config(path)
    from spiritscout.web.agentcfg import read_assignments, read_naming_chain

    users = [k for k, v in read_assignments(assignments_path).items() if v == model] if assignments_path else []
    chain = [m for m in read_naming_chain(assignments_path) if m == model] if assignments_path else []
    backups = [e["model"] for e in entries if model in e.get("is_fallback_for", [])]
    if users or chain or backups:
        raise SpiritScoutError(f"模型仍被引用，请先迁移：编队 {users}，命名链 {chain}，备用关系 {backups}")
    remaining = [e for e in entries if e.get("model") != model]
    if len(remaining) == len(entries):
        raise SpiritScoutError(f"模型不存在: {model}")
    write_config(path, providers, remaining)
    return remaining
