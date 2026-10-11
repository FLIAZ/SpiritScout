"""配置加载与校验。

- YAML 为主，支持 ``${ENV_VAR}`` 环境变量展开（api_key 只允许环境变量注入，合规 G8）；
- 环境变量覆盖：``SPIRITSCOUT_<SECTION>__<KEY>`` 双下划线分层；
- 加载即校验（pydantic），缺失/非法直接报 ConfigurationError。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.models import RunMode

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(obj: Any) -> Any:
    """递归展开字符串中的 ${ENV_VAR}。未设置的环境变量展开为空串并保留痕迹。"""
    if isinstance(obj, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def _apply_env_overrides(data: dict[str, Any], prefix: str = "SPIRITSCOUT") -> dict[str, Any]:
    """SPIRITSCOUT_BUS__BACKEND=redis 形式的环境变量覆盖。"""
    for key, value in os.environ.items():
        if not key.startswith(prefix + "_"):
            continue
        path = key[len(prefix) + 1 :].lower().split("__")
        node = data
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                break
        else:
            node[path[-1]] = value
    return data


# ---------------------------------------------------------------- 子配置


class WorkspaceConfig(BaseModel):
    root: str = Field(default=".spiritscout", description="工作区根目录（SQLite/审计/报告产出）")


class BusConfig(BaseModel):
    """环形数据总线后端（适配器决策 A1：inprocess 默认，redis/nats 可切）。"""

    backend: Literal["inprocess", "redis", "nats"] = "inprocess"
    redis_url: str = "redis://127.0.0.1:6379/0"
    nats_url: str = "nats://127.0.0.1:4222"
    queue_maxsize: int = 10000
    rate_limit_per_second: float = 1000.0   # 总线流量控制（4.2.1）


class BlackboardConfig(BaseModel):
    sqlite_path: str = ".spiritscout/blackboard.db"


class GatewayConfig(BaseModel):
    """多模型网关：预算与协作策略（4.9、6.10）。"""

    default_tier: str = "fast"
    budget_tokens: int = Field(default=2_000_000, description="运行预算 token 硬顶（超顶熔断）")
    budget_usd: float = Field(default=20.0, description="运行预算美元硬顶")
    mode_budget_tokens: dict[str, int] = Field(
        default_factory=dict,
        description="按运行模式（src/ctf/competition）的 token 硬顶（4.9.4）；空 = 仅全局预算")
    degrade_at_ratio: float = Field(
        default=0.8,
        description="用量达预算该比例即自动降级到低档模型（4.9.4 超预算自动降级）；1.0 = 不降级")
    cascade_confidence_threshold: float = 0.8   # 级联升级阈值（6.10：<0.8 升级旗舰复判）
    vote_min_models: int = 2                    # 投票最少模型数（4.9.3-2：≥2 独立判断）
    request_timeout_seconds: int = 300  # 重推理模型单次调用可达 3~4 分钟，120s 会中途掐死
    max_retries: int = 2


class ExecutorConfig(BaseModel):
    """工具执行器（CLI/Docker/MCP 三态）。"""

    backend: Literal["local", "docker"] = "local"
    docker_image: str = "kalilinux/kali-rolling:latest"
    default_timeout_seconds: int = 300
    max_output_chars: int = 20000         # HackSynth 输出窗口截断（3.2.4-9）
    ctf_network_isolation: bool = True    # CTF 模式网络隔离（硬约束 C7）
    # ---- Docker 沙箱加固（9.3 EXP 沙箱验证：最小权限 + 资源限额 + 只读根fs）
    docker_memory_mb: int = 512
    docker_cpus: float = 1.0
    docker_pids_limit: int = 256
    docker_read_only: bool = True
    docker_tmpfs: str = "/tmp:rw,noexec,nosuid,size=64m"
    docker_user: str = ""                 # 空 = 镜像默认用户；可配 "65534:65534" 进一步降权


class KnowledgeConfig(BaseModel):
    sqlite_path: str = ".spiritscout/knowledge.db"
    chroma_path: str = ".spiritscout/chroma"
    embedding_provider: Literal["hash", "openai_compatible"] = "hash"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""


class ReportConfig(BaseModel):
    template: str = "src_standard"        # 报告模板（4.6.2）
    output_format: Literal["markdown", "html"] = "markdown"
    output_dir: str = ".spiritscout/reports"


class WebConfig(BaseModel):
    """Web 控制台。"""

    host: str = "127.0.0.1"               # 默认仅回环；非回环绑定启动时打印显著警告
    port: int = 8000
    max_concurrent_sessions: int = Field(default=4, description="并发会话上限（信号量）")


class SystemConfig(BaseModel):
    """系统总配置。"""

    mode: RunMode = RunMode.SRC
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    bus: BusConfig = Field(default_factory=BusConfig)
    blackboard: BlackboardConfig = Field(default_factory=BlackboardConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    models_config_path: str = "configs/models.yaml"
    agent_models_path: str = "configs/agent_models.yaml"   # 按 Agent 模型分配表
    audit_log_path: str = ".spiritscout/audit.jsonl"
    max_routes_per_agent: int = Field(default=8, description="分支数硬上限（防分支爆炸，9.1）")
    default_routes_per_agent: int = Field(default=3, description="默认 3 条差异化路线（硬约束 C2）")
    iteration_max_rounds: int = 20

    @field_validator("default_routes_per_agent")
    @classmethod
    def _check_default_routes(cls, v: int) -> int:
        if not 1 <= v <= 8:
            raise ValueError("default_routes_per_agent 必须在 1~8（设计书 6.2）")
        return v


class ProviderEntry(BaseModel):
    """模型供应商（接入端点）：一个 base_url + 一把密钥，下挂若干模型。

    密钥属于供应商而非单个模型——同一端点的多个模型共用一条密钥记录；
    Web「模型」页先添加供应商（可一键从云端拉取可用模型列表），再从列表里
    勾选启用哪些模型，不用逐个模型重复填 base_url 与密钥。
    """

    name: str                             # 供应商名（主键，用户自取，如 ark / deepseek）
    provider: str = "openai_compatible"   # openai_compatible / anthropic_compatible（Web 端仅此两类；mock 仅供离线开发经 YAML 装载）
    base_url: str = ""
    api_key: str = ""                     # 经 ${ENV_VAR} 展开而来；mock 后端可为空
    # 端点协议：chat completions、OpenAI Responses、Anthropic Messages
    # 三协议的请求体与响应结构不同（messages 与 input/instructions、choices 与 output
    # items、usage 字段名 prompt_tokens 与 input_tokens）。auto 按 provider 推导
    # （anthropic_compatible→anthropic_messages，其余→chat_completions）；
    # 仅 o 系/gpt-5 等原生 Responses 端点需显式指定 responses
    api_format: Literal["auto", "chat_completions", "responses", "anthropic_messages"] = "auto"

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("供应商名不能为空")
        return v.strip()


class ModelEntry(BaseModel):
    """模型注册表条目（4.9.4：能力画像/成本/速率/质量评分）。

    模型 ID 即身份：注册表、编队指派、审计与预算都以它为主键；接入信息
    （provider/base_url/api_key/api_format）来自所属供应商，装载时合并进来。
    """

    model: str                            # 模型 ID（主键，也是发给端点的模型名）
    provider_ref: str = ""                # 归属供应商名（ProviderEntry.name）
    provider: str = "openai_compatible"   # 由供应商合并而来
    base_url: str = ""
    api_key: str = ""                     # 由供应商合并而来（经 ${ENV_VAR} 展开或密钥库覆盖）
    api_format: Literal["auto", "chat_completions", "responses", "anthropic_messages"] = "auto"
    tier: str = "fast"                    # flagship / fast / code / rigorous / light
    capabilities: list[str] = Field(default_factory=list)  # reasoning/code/chinese/security
    context_window: int = 0               # 上下文窗口（token）；0 = 未声明
    # 思考强度取值：由每个模型自行声明（如 low/medium/high，或某模型特有的
    # ultra）——不设固定枚举，也不假设各模型档位一致；空 = 该模型不吃思考强度。
    reasoning_effort_levels: list[str] = Field(default_factory=list)
    cost_input_per_mtok: float = 0.0
    cost_output_per_mtok: float = 0.0
    rate_limit_rpm: int = 60
    quality_score: float = 1.0            # 基准评测动态更新
    is_fallback_for: list[str] = Field(default_factory=list, description="主备关系：作为哪些模型的备（模型 ID）")

    @field_validator("model")
    @classmethod
    def _model_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("模型 ID 不能为空")
        return v.strip()

    @field_validator("context_window")
    @classmethod
    def _ctx_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("context_window 不能为负")
        return v

    @field_validator("reasoning_effort_levels")
    @classmethod
    def _levels_clean(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()]


class ModelConfigFile(BaseModel):
    """模型配置文件：供应商（接入端点）+ 模型（能力画像）两段。"""

    providers: list[ProviderEntry] = Field(default_factory=list)
    models: list[ModelEntry]


# ---------------------------------------------------------------- 加载入口


def _load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise ConfigurationError(f"配置文件不存在: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"YAML 解析失败 {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"配置文件顶层必须是对象: {p}")
    return data


def load_system_config(path: str | Path) -> SystemConfig:
    data = _expand_env(_load_yaml(path))
    data = _apply_env_overrides(data)
    try:
        return SystemConfig.model_validate(data)
    except Exception as exc:
        raise ConfigurationError(f"系统配置校验失败: {exc}") from exc


def _merge_providers(cfg: ModelConfigFile) -> list[ModelEntry]:
    """把供应商的接入信息合并进每个模型（模型条目本身不重复存 base_url/key）。"""
    providers = {p.name: p for p in cfg.providers}
    out: list[ModelEntry] = []
    for m in cfg.models:
        ref = m.provider_ref
        if ref and ref not in providers:
            raise ConfigurationError(
                f"模型 {m.model} 引用了不存在的供应商: {ref}（请先在供应商里添加）")
        p = providers.get(ref)
        out.append(m.model_copy(update={
            "provider": p.provider if p else m.provider,
            "base_url": p.base_url if p else m.base_url,
            "api_key": p.api_key if p else m.api_key,
            "api_format": (p.api_format if p and p.api_format != "auto" else m.api_format),
        }))
    return out


def load_model_config(path: str | Path) -> list[ModelEntry]:
    """装载模型注册表（供应商接入信息已合并进各模型）。"""
    data = _expand_env(_load_yaml(path))
    try:
        cfg = ModelConfigFile.model_validate(data)
    except Exception as exc:
        raise ConfigurationError(f"模型配置校验失败: {exc}") from exc
    ids = [m.model for m in cfg.models]
    if len(set(ids)) != len(ids):
        raise ConfigurationError("模型 ID 存在重复")
    names = [p.name for p in cfg.providers]
    if len(set(names)) != len(names):
        raise ConfigurationError("供应商名存在重复")
    return _merge_providers(cfg)
