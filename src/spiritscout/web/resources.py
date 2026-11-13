"""资源控制台路由：管理对话隔离于测试会话，所有变更显式版本校验。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    ValidationError,
)

from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.resources.archives import MAX_UPLOAD, parse_skill_archive
from spiritscout.resources.assistant import ResourceAssistant, validate_skill
from spiritscout.resources.catalog import mcp_catalog, skill_catalog
from spiritscout.resources.store import ResourceStore, redact_credentials, validate_mcp


class ResourceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict[str, Any]
    expected_version: int = Field(default=0, ge=0)


class SkillFileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=100_000)
    expected_version: int = Field(ge=0)


class ExaSettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key_value: SecretStr | None = None
    enabled: StrictBool = True
    clear_key: StrictBool = False
    expected_version: int = Field(default=0, ge=0)


class FofaSettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: SecretStr | None = None
    api_key_value: SecretStr | None = None
    enabled: StrictBool = False
    clear_credentials: StrictBool = False
    expected_version: int = Field(default=0, ge=0)


class ResourceMessage(BaseModel):
    message: str = Field(min_length=1, max_length=16000)


class ConfirmChange(BaseModel):
    confirm: StrictBool


def public_mcp(config: Any, version: int = 0, origin: str = "config") -> dict[str, Any]:
    data = config.model_dump()
    for key in ("headers", "env"):
        data[key + "_keys"] = list(data.pop(key))
    return {
        "id": config.name,
        "version": version,
        "origin": origin,
        "data": redact_credentials(data),
    }


def mount_resources(
    app: FastAPI, cfg: Any, store: ResourceStore, assistant: ResourceAssistant,
) -> None:
    def fail(exc: Exception) -> HTTPException:
        if isinstance(exc, ValidationError):
            errors = [
                {
                    "field": ".".join(str(part) for part in item["loc"]),
                    "message": item["msg"],
                }
                for item in exc.errors(include_input=False)
            ]
            return HTTPException(
                422,
                {
                    "message": "配置校验失败："
                    + "；".join(
                        item["field"] + " " + item["message"] for item in errors
                    ),
                    "errors": errors,
                },
            )
        return HTTPException(
            409 if isinstance(exc, SpiritScoutError) else 422,
            str(exc)[:600],
        )

    def exa_settings_view() -> dict[str, Any]:
        from spiritscout.tools.web_search import effective_search

        row = next((item for item in store.settings("search") if item["id"] == "exa"), None)
        selected, key = effective_search(cfg.search, store)
        stored = bool(row and row["data"].get("api_key"))
        return {"version": row["version"] if row else 0, "enabled": selected.enabled,
                "key_configured": bool(key), "stored_key": stored,
                "key_source": "saved" if stored else "environment" if key else "none",
                "allowed_modes": ["src", "ctf"], "competition_allowed": False,
                "billing_policy": "free_first_paid_only_for_blockers"}

    def fofa_settings_view() -> dict[str, Any]:
        import json
        import os

        from spiritscout.web import vault

        row = next((item for item in store.settings("asset_provider")
                    if item["id"] == "fofa"), None)
        saved = vault.get_key(cfg.asset_intelligence.fofa.vault_key_name)
        try:
            saved_data = json.loads(saved) if saved else {}
        except (TypeError, ValueError):
            saved_data = {}
        environment = bool(os.environ.get(cfg.asset_intelligence.fofa.email_env, "").strip()
                           and os.environ.get(cfg.asset_intelligence.fofa.api_key_env, "").strip())
        stored = bool(saved_data.get("email") and saved_data.get("key"))
        enabled = bool((row or {}).get("data", {}).get(
            "enabled", cfg.asset_intelligence.fofa.enabled))
        return {
            "version": row["version"] if row else 0,
            "enabled": enabled,
            "key_configured": stored or environment,
            "stored_credentials": stored,
            "key_source": "saved" if stored else "environment" if environment else "none",
            "provider": "fofa",
            "candidate_policy": "external_candidates_require_scope_dns_http_verification",
        }

    @app.get("/api/resources/search/exa")
    def exa_settings() -> dict[str, Any]:
        return exa_settings_view()

    @app.put("/api/resources/search/exa")
    async def save_exa_settings(request: Request) -> dict[str, Any]:
        # 单独捕获校验错误并移除 input，避免非法载荷将密钥原文回显。
        try:
            payload = ExaSettingsChange.model_validate(await request.json())
            value = payload.api_key_value.get_secret_value().strip() if payload.api_key_value else ""
            if len(value) > 4096 or any(ord(char) < 32 for char in value):
                raise ValueError("密钥格式不正确，请粘贴 Exa 控制台生成的 API Key")
            if payload.clear_key and value:
                raise ValueError("清除密钥时不能同时填写新密钥")
            row = next((item for item in store.settings("search") if item["id"] == "exa"), None)
            data = dict(row["data"] if row else {})
            if payload.clear_key:
                data.pop("api_key", None)
            elif value:
                data["api_key"] = value
            data["enabled"] = payload.enabled
            store.save("search", "exa", data, payload.expected_version)
            return exa_settings_view()
        except (ValueError, TypeError, SpiritScoutError) as exc:
            raise fail(exc) from exc

    @app.get("/api/resources/assets/fofa")
    def fofa_settings() -> dict[str, Any]:
        return fofa_settings_view()

    @app.put("/api/resources/assets/fofa")
    async def save_fofa_settings(request: Request) -> dict[str, Any]:
        import json

        from spiritscout.web import vault

        try:
            payload = FofaSettingsChange.model_validate(await request.json())
            email = payload.email.get_secret_value().strip() if payload.email else ""
            key = (payload.api_key_value.get_secret_value().strip()
                   if payload.api_key_value else "")
            if any(ord(char) < 32 for char in email + key) or len(email) > 320 or len(key) > 4096:
                raise ValueError("FOFA 凭据格式不正确")
            if payload.clear_credentials and (email or key):
                raise ValueError("清除凭据时不能同时填写新凭据")
            if bool(email) != bool(key):
                raise ValueError("FOFA 邮箱与 API Key 必须同时填写")
            row = next((item for item in store.settings("asset_provider")
                        if item["id"] == "fofa"), None)
            data = dict(row["data"] if row else {})
            data["enabled"] = payload.enabled
            store.save("asset_provider", "fofa", data, payload.expected_version)
            if payload.clear_credentials:
                vault.drop_key(cfg.asset_intelligence.fofa.vault_key_name)
            elif email and key:
                vault.set_key(cfg.asset_intelligence.fofa.vault_key_name,
                              json.dumps({"email": email, "key": key}, allow_nan=False))
            cfg.asset_intelligence.fofa.enabled = payload.enabled
            return fofa_settings_view()
        except (ValueError, TypeError, SpiritScoutError) as exc:
            raise fail(exc) from exc

    @app.get("/api/resources/mcp")
    def mcp_list(
        q: str = Query("", max_length=200), enabled: bool | None = None,
        transport: Literal["", "stdio", "http", "sse"] = "",
        source: Literal["", "config", "managed"] = "",
        risk: Literal["", "readonly", "intrusive", "destructive"] = "",
        agent: str = Query("", max_length=100),
        added_from: date | None = None, added_to: date | None = None,
        date_state: Literal["all", "known", "unknown"] = "all",
        sort: Literal["added_desc", "added_asc", "name_asc", "updated_desc"] = "added_desc",
        offset: int = Query(0, ge=0), limit: int = Query(40, ge=1, le=100),
    ) -> dict[str, Any]:
        overrides = {row["id"]: row for row in store.settings("mcp")}
        configured = {item.name for item in cfg.mcp_servers}
        items = []
        for item in store.effective_mcp(cfg.mcp_servers):
            row = overrides.get(item.name, {})
            items.append({**public_mcp(item, row.get("version", 0),
                                      "managed" if row else "config"),
                          "created_at": (None if item.name in configured else
                                         row.get("data", {}).get("created_at")),
                          "updated_at": row.get("updated_at")})
        try:
            result = mcp_catalog(items, q=q, enabled=enabled, transport=transport,
                                 source=source, risk=risk, agent=agent, added_from=added_from,
                                 added_to=added_to, date_state=date_state, sort=sort,
                                 offset=offset, limit=limit)
        except ValueError as exc:
            raise fail(exc) from exc
        return {**result, "activation": "新启动会话生效；不会热替换正在执行的 MCP 连接"}

    @app.get("/api/resources/mcp/{name}/config")
    def mcp_detail(name: str) -> dict[str, Any]:
        config = next((item for item in store.effective_mcp(cfg.mcp_servers)
                       if item.name == name), None)
        if config is None:
            raise HTTPException(404, "MCP 不存在或已删除")
        row = next((item for item in store.settings("mcp") if item["id"] == name), {})
        return public_mcp(config, row.get("version", 0), "managed" if row else "config")

    @app.delete("/api/resources/mcp/{name}")
    def mcp_delete(name: str, expected_version: int = Query(..., ge=0)) -> dict[str, Any]:
        try:
            store.delete_mcp(name, expected_version,
                             configured=any(item.name == name for item in cfg.mcp_servers))
            return {"id": name, "deleted": True}
        except KeyError as exc:
            raise HTTPException(404, "MCP 不存在或已删除") from exc
        except SpiritScoutError as exc:
            raise fail(exc) from exc

    @app.put("/api/resources/mcp/{name}")
    def mcp_save(name: str, payload: ResourceChange) -> dict[str, Any]:
        try:
            existing = next(
                (
                    item
                    for item in store.effective_mcp(cfg.mcp_servers)
                    if item.name == name
                ),
                None,
            )
            data = {
                **(existing.model_dump() if existing else {}),
                **payload.data,
                "name": name,
            }
            spec = validate_mcp(data)
            version = store.save(
                "mcp", name, spec.model_dump(), payload.expected_version
            )
            return public_mcp(spec, version, "managed")
        except (ValueError, SpiritScoutError) as exc:
            raise fail(exc) from exc

    @app.get("/api/resources/skills")
    def skill_list(
        q: str = Query("", max_length=200),
        enabled: bool | None = None,
        domain: str = Query("", max_length=100),
        tier: Literal["", "core", "domain", "reference"] = "",
        source: Literal["", "builtin", "archive", "manual"] = "",
        added_from: date | None = None,
        added_to: date | None = None,
        date_state: Literal["all", "known", "unknown"] = "all",
        sort: Literal["relevance", "added_desc", "added_asc", "name_asc", "updated_desc"] = "relevance",
        offset: int = Query(0, ge=0),
        limit: int = Query(40, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return skill_catalog(store.skills(), q=q, enabled=enabled, domain=domain, tier=tier,
                                 source=source, added_from=added_from, added_to=added_to,
                                 date_state=date_state, sort=sort, offset=offset, limit=limit)
        except ValueError as exc:
            raise fail(exc) from exc

    @app.post("/api/resources/skills/import")
    async def skill_import(request: Request) -> dict[str, Any]:
        # 流式限制请求体，避免先把任意大的上传整体读入内存。
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_UPLOAD:
                raise HTTPException(413, "压缩包不得超过 20 MiB")
            content.extend(chunk)

        def import_archive() -> dict[str, Any]:
            items, warnings = parse_skill_archive(bytes(content))
            return {"items": store.import_skills(items), "warnings": warnings}

        try:
            return await run_in_threadpool(import_archive)
        except (ValueError, SpiritScoutError) as exc:
            raise fail(exc) from exc

    @app.get("/api/resources/skills/{key}")
    def skill_detail(key: str) -> dict[str, Any]:
        item = next(
            (
                row
                for row in store.skills(key, include_body=True)
                if row["skill_id"] == key
            ),
            None,
        )
        if not item:
            raise HTTPException(404, "Skill 不存在")
        from spiritscout.resources.skill_search import metadata

        return {**item, **metadata(item)}

    @app.put("/api/resources/skills/{key}")
    def skill_save(key: str, payload: ResourceChange) -> dict[str, Any]:
        try:
            original = next(
                (
                    row
                    for row in store.skills(key, include_body=True)
                    if row["skill_id"] == key
                ),
                {},
            )
            data = {
                k: v
                for k, v in {**original, **payload.data, "skill_id": key}.items()
                if k not in {"version", "origin", "created_at", "updated_at", "source_kind"}
            }
            validate_skill(data)
            version = store.save("skill", key, data, payload.expected_version)
            return {"version": version, "skill_id": key}
        except (ValueError, SpiritScoutError) as exc:
            raise fail(exc) from exc

    @app.put("/api/resources/skills/{key}/file")
    def skill_file_save(key: str, payload: SkillFileChange) -> dict[str, Any]:
        original = skill_detail(key)
        try:
            if payload.expected_version != original["version"]:
                raise SpiritScoutError("Skill 已被其他操作更新，请保留草稿并重新读取后再保存")
            files = dict(original.get("files") or {"SKILL.md": original["body"]})
            if payload.path not in files:
                raise HTTPException(404, "文件不在该 Skill 的目录内")
            files[payload.path] = payload.content
            data = {k: v for k, v in original.items()
                    if k not in {"version", "origin", "created_at", "updated_at", "source_kind"}}
            data["files"] = files
            validate_skill(data)
            version = store.save("skill", key, data, payload.expected_version)
            return {"skill_id": key, "path": payload.path, "version": version}
        except (ValueError, SpiritScoutError) as exc:
            raise fail(exc) from exc

    @app.delete("/api/resources/skills/{key}")
    def skill_delete(key: str, expected_version: int = Query(..., ge=0)) -> dict[str, Any]:
        try:
            store.delete_skill(key, expected_version)
            return {"deleted": key}
        except KeyError as exc:
            raise HTTPException(404, "Skill 不存在") from exc
        except SpiritScoutError as exc:
            raise fail(exc) from exc

    @app.get("/api/assets")
    def assets(
        q: str = Query("", max_length=200),
        host: str = Query("", max_length=300),
        project: str = Query("", max_length=64),
        unclassified: int = Query(0, ge=0, le=1),
        kind: str = Query("", max_length=16),
        offset: int = Query(0, ge=0),
        limit: int = Query(40, ge=1, le=100),
    ) -> dict[str, Any]:
        if project and unclassified:
            raise HTTPException(422, "project 与 unclassified 互斥")
        if kind not in {"", "asset", "finding"}:
            raise HTTPException(422, "kind 只能是空、asset 或 finding")
        return store.assets(q, offset, limit, host=host, project=project,
                            unclassified=bool(unclassified), kind=kind)

    @app.get("/api/assets/{key}/history")
    def asset_history(key: str, offset: int = Query(0, ge=0)) -> dict[str, Any]:
        return store.history(key, offset)

    @app.post("/api/assets/sync")
    async def asset_sync() -> dict[str, Any]:
        """只导入历史会话的黑板资产；不触发命名/归类（归属由用户决定）。"""
        def scan() -> tuple[int, int]:
            count, errors = 0, 0
            for path in (Path(cfg.workspace.root) / "sessions").glob("sess-*"):
                try:
                    count += store.import_session(path, full=True)
                except (OSError, ValueError, sqlite3.Error):
                    errors += 1
            return count, errors

        count, errors = await run_in_threadpool(scan)
        return {"scanned_records": count, "failed_sessions": errors}

    @app.get("/api/resources/{kind}/chat")
    def chat_view(kind: Literal["mcp", "skill"]) -> dict[str, Any]:
        return assistant.view(kind)

    @app.post("/api/resources/{kind}/chat")
    async def chat_message(
        kind: Literal["mcp", "skill"], payload: ResourceMessage
    ) -> dict[str, Any]:
        try:
            return await assistant.message(kind, payload.message)
        except (ValueError, TypeError, SpiritScoutError, OSError) as exc:
            raise fail(exc) from exc

    @app.post("/api/resources/proposals/{key}/apply")
    async def apply_proposal(key: str, payload: ConfirmChange) -> dict[str, Any]:
        if not payload.confirm:
            raise HTTPException(422, "需要确认具体变更")
        try:
            assistant.apply(key)
        except ValueError as exc:
            raise fail(exc) from exc
        return {"state": "applying"}
