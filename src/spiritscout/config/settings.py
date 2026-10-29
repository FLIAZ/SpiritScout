"""配置加载与校验。

- YAML 为主，支持 ``${ENV_VAR}`` 展开（通用机制；模型密钥不用它——
  密钥在 Web 直填加密入密钥库，合规 G8）；
- 环境变量覆盖：``SPIRITSCOUT_<SECTION>__<KEY>`` 双下划线分层；
- 加载即校验（pydantic），缺失/非法直接报 ConfigurationError。
"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.models import AgentType, RunMode

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(obj: Any) -> Any:
    """递归展开环境变量；缺失时明确报错，不制造空地址或空凭据。"""
    if isinstance(obj, str):
        def resolve(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigurationError(f"配置引用的环境变量未设置: {name}")
            return os.environ[name]
        return _ENV_PATTERN.sub(resolve, obj)
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
        suffix = key[len(prefix) + 1 :]
        if "__" not in suffix:
            continue  # 密码、Redis URL 等专用环境变量不是配置树覆盖
        path = suffix.lower().split("__")
        node = data
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                break
        else:
            node[path[-1]] = value
    return data


# ---------------------------------------------------------------- 子配置


class ValidatedConfig(BaseModel):
    """未知字段和非有限数值立即报错，避免拼写错误静默失效。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WorkspaceConfig(ValidatedConfig):
    root: str = Field(default=".spiritscout", description="工作区根目录（SQLite/审计/报告产出）")
    # Web 会话的工作区在基础根目录内部；沙箱遮蔽需连基础根一起（vault 密钥
    # 库/兄弟会话/审计都在里面），仅遮蔽会话自身目录不足以隔离
    guard_root: str = Field(default="", description="控制面根目录（沙箱遮蔽用）：空 = 不额外遮蔽")


class BusConfig(ValidatedConfig):
    """环形数据总线后端（适配器决策 A1：inprocess 默认，redis/nats 可切）。"""

    backend: Literal["inprocess", "redis", "nats"] = "inprocess"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_url_env: str = "SPIRITSCOUT_REDIS_URL"
    namespace: str = Field(default="spiritscout", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    connect_timeout_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    shared_request_rps: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    nats_url: str = "nats://127.0.0.1:4222"
    queue_maxsize: int = Field(default=10000, gt=0)
    rate_limit_per_second: float = Field(default=1000.0, gt=0)   # 总线流量控制


class BlackboardConfig(ValidatedConfig):
    sqlite_path: str = ".spiritscout/blackboard.db"


class GatewayConfig(ValidatedConfig):
    """多模型网关：预算与协作策略。"""

    budget_tokens: int = Field(default=2_000_000, description="Token 用量参考值，仅计量不熔断")
    budget_usd: float = Field(default=20.0, description="费用参考值，仅计量不熔断")
    mode_budget_tokens: dict[str, int] = Field(
        default_factory=dict,
        description="按模式记录的 Token 参考值，不作为执行上限")
    degrade_at_ratio: float = Field(
        default=0.8,
        description="兼容旧配置；当前不按消耗自动降级模型")
    cascade_confidence_threshold: float = 0.8   # 级联升级阈值（6.10：<0.8 升级旗舰复判）
    vote_min_models: int = 2                    # 投票最少模型数（4.9.3-2：≥2 独立判断）
    request_timeout_seconds: int = Field(default=300, gt=0)  # 重推理模型单次调用可达 3~4 分钟，120s 会中途掐死
    max_retries: int = 2
    stable_snapshot_prefix: bool = True  # 新决策段保留稳定事实前缀，历史不重写
    shared_ctf_prefix: bool = False  # 共享布局实验关闭，真实复杂对照尚未证明命中收益
    compact_decision_patches: bool = False  # 无损补丁短编码，真实模型对照通过后再启用
    remember_endpoint_capabilities: bool = True  # 只复用已成功的可选参数兼容回退


class ExecutorConfig(ValidatedConfig):
    """工具执行器（CLI/Docker/MCP 三态）。"""

    backend: Literal["local", "docker"] = "local"
    docker_image: str = "kalilinux/kali-rolling:latest"
    default_timeout_seconds: int = Field(default=300, gt=0)
    max_output_chars: int = Field(default=20000, gt=0)         # HackSynth 输出窗口截断（3.2.4-9）
    http_max_body_bytes: int = Field(default=8 * 1024 * 1024, gt=0)  # HTTP 解压后正文上限，与展示窗口独立
    ctf_network_isolation: bool = True    # CTF 模式网络隔离（硬约束 C7）
    # ---- Docker 沙箱加固（9.3 EXP 沙箱验证：最小权限 + 资源限额 + 只读根fs）
    docker_memory_mb: int = Field(default=512, gt=0)
    docker_cpus: float = Field(default=1.0, gt=0)
    docker_pids_limit: int = Field(default=256, gt=0)
    docker_read_only: bool = True
    docker_tmpfs: str = "/tmp:rw,noexec,nosuid,size=64m"
    docker_user: str = ""                 # 空 = 镜像默认用户；可配 "65534:65534" 进一步降权


class KnowledgeConfig(ValidatedConfig):
    sqlite_path: str = ".spiritscout/knowledge.db"
    chroma_path: str = ".spiritscout/chroma"
    embedding_provider: Literal["hash", "openai_compatible"] = "hash"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""


class ReportConfig(ValidatedConfig):
    template: str = "src_standard"        # 报告模板
    output_format: Literal["markdown", "html"] = "markdown"
    output_dir: str = ".spiritscout/reports"


class WebConfig(ValidatedConfig):
    """Web 控制台。"""

    host: str = "127.0.0.1"               # 默认仅回环
    port: int = Field(default=8000, ge=1, le=65535)
    max_concurrent_sessions: int = Field(default=4, ge=1, description="并发会话上限（信号量）")
    remote_auth_required: bool = Field(
        default=True, description="非回环绑定必须配置管理密码，否则拒绝启动")
    password_env: str = Field(
        default="SPIRITSCOUT_SERVER_PASSWORD",
        description="旧版管理密码环境变量；启动时导入管理员库（用户名默认 spiritscout）")
    admin_username_env: str = Field(
        default="SPIRITSCOUT_ADMIN_USERNAME",
        description="管理员用户名所在环境变量；用于启动时导入/恢复凭据")
    admin_password_env: str = Field(
        default="SPIRITSCOUT_ADMIN_PASSWORD",
        description="管理员密码所在环境变量；优先级高于旧版 password_env")


class McpServerConfig(ValidatedConfig):
    """外部 MCP 服务器接入（工具以 mcp__<名>__<工具> 并入智能体工具目录）。

    transport=stdio：本地子进程（如项目 venv 的 ``idalib-mcp --stdio``、
    自带 Yakit 引擎的 ``yak mcp``）；
    transport=http：Streamable HTTP 单端点（如 Yakit 引擎
    ``--transport streamable_http`` 的 ``/mcp``）；
    transport=sse：旧版 SSE 端点（如 Burp 官方扩展的 ``/sse``）。
    """

    name: str
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str = ""                     # stdio：可执行命令
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""                         # http：MCP 端点
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = Field(default=180.0, gt=0)                # 单次工具调用硬顶（秒）
    default_risk: Literal["readonly", "intrusive", "destructive"] = "intrusive"
    risk_by_action: bool = Field(default=False, description="按模型声明的本次行为审批，普通浏览器操作不因工具能力而升级风险")
    tool_risk_overrides: dict[str, Literal["readonly", "intrusive", "destructive"]] = Field(
        default_factory=dict,
        description="宿主可信配置的单工具风险映射，仅精确匹配远端工具名；不支持通配放行")
    internet_search: bool = Field(default=False, description="公开网页搜索服务；智能体模式不连接")
    scope_enforcement: Literal["required", "best_effort", "none"] = Field(
        default="required",
        description="旧配置兼容字段，仅影响目标元数据展示；测试边界统一由模型上下文判断")
    target_params: list[str] = Field(
        default_factory=lambda: [
            "url", "urls", "target", "targets", "host", "ip", "domain", "endpoint"],
        description="MCP 工具目标参数元数据，不用于后端授权拦截")
    allowed_agents: list[AgentType] = Field(
        default_factory=list, description="额外授权的执行角色；空列表沿用原有角色工具范围")
    enabled: bool = True

    @field_validator("allowed_agents")
    @classmethod
    def validate_allowed_agents(cls, value: list[AgentType]) -> list[AgentType]:
        supported = {AgentType.VERIFIER, AgentType.BUSINESS_CONTEXT, AgentType.CTF_WEB, AgentType.CTF_PWN,
                     AgentType.CTF_REVERSE, AgentType.CTF_CRYPTO, AgentType.CTF_MISC}
        if any(role not in supported for role in value):
            raise ValueError("自定义 MCP 目前支持 verifier、business_context 和 ctf_* 执行角色；其他角色没有动态工具调用循环")
        return list(dict.fromkeys(value))


class SecretaryConfig(ValidatedConfig):
    """只读参谋按灵枢请求工作，不默认每回合增加调用。"""

    enabled: bool = True


class ResourcesConfig(ValidatedConfig):
    """共享目录路径在会话隔离前固定；历史观测不携带授权。"""

    root: str = ""
    freshness_hours: float = Field(default=24, gt=0)
    npm_registry: str = Field(default="https://registry.npmjs.org", pattern=r"^https?://")


class ExperienceConfig(ValidatedConfig):
    """经验库（跨会话自我进化）：会话收尾提炼实战洞察，后续会话开工召回。"""

    root: str = Field(default="", description="经验库目录（含 experience.db）；空=跟随工作区")
    distill_timeout_seconds: int = Field(
        default=45, gt=0, description="收尾提炼单次 LLM 调用硬顶（秒），仅终态路径使用")
    recall_limit: int = Field(default=6, gt=0, le=20, description="会话开工注入的经验条数上限")


class SearchConfig(ValidatedConfig):
    """公开资料检索；凭据从加密资源库或环境变量读取，不进入模型上下文。"""

    enabled: bool = False
    api_url: str = "https://api.exa.ai/search"
    api_key_env: str = "EXA_API_KEY"
    timeout_seconds: float = Field(default=25, gt=0, le=120)
    cache_ttl_seconds: int = Field(default=21600, ge=1)
    cache_max_entries: int = Field(default=2000, ge=1)


class SystemConfig(ValidatedConfig):
    """系统总配置。"""

    mode: RunMode = RunMode.SRC
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    bus: BusConfig = Field(default_factory=BusConfig)
    blackboard: BlackboardConfig = Field(default_factory=BlackboardConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    secretary: SecretaryConfig = Field(default_factory=SecretaryConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    experience: ExperienceConfig = Field(default_factory=ExperienceConfig)
    mcp_servers: list[McpServerConfig] = Field(
        default_factory=list,
        description="外部 MCP 服务器（stdio/http），工具并入智能体工具目录")
    models_config_path: str = "configs/models.yaml"
    agent_models_path: str = "configs/agent_models.yaml"   # 按 Agent 模型分配表
    audit_log_path: str = ".spiritscout/audit.jsonl"
    max_routes_per_agent: int = Field(default=8, ge=1, le=8, description="分支数硬上限（防分支爆炸，9.1）")
    default_routes_per_agent: int = Field(default=3, description="默认 3 条差异化路线（硬约束 C2）")
    # 不设迭代回合顶：继续/收手由主 Agent 按态势判断，用户随时可停

    @field_validator("default_routes_per_agent")
    @classmethod
    def _check_default_routes(cls, v: int) -> int:
        if not 1 <= v <= 8:
            raise ValueError("default_routes_per_agent 必须在 1~8（设计书 6.2）")
        return v


class ProviderEntry(ValidatedConfig):
    """模型供应商（接入端点）：一个 base_url + 一把密钥，下挂若干模型。

    密钥属于供应商而非单个模型——同一端点的多个模型共用一条密钥记录；
    Web「模型」页先添加供应商（可一键从云端拉取可用模型列表），再从列表里
    勾选启用哪些模型，不用逐个模型重复填 base_url 与密钥。
    """

    name: str                             # 供应商名（主键，用户自取，如 ark / deepseek）
    provider: str = "openai_compatible"   # openai_compatible / anthropic_compatible（Web 端仅此两类；mock 仅供离线开发经 YAML 装载）
    base_url: str = ""
    api_key: str = ""                     # 由密钥库注入（Web 直填加密入库）；YAML 不落密钥，mock 后端可为空
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
        if "/" in v:
            raise ValueError("供应商名不能包含 /（与接口路径标识冲突）")
        return v.strip()


class ModelEntry(ValidatedConfig):
    """模型注册表条目（4.9.4：能力画像/成本/速率/质量评分）。

    配置身份由供应商与模型 ID 共同组成；端点模型名保持原样。接入信息
    （provider/base_url/api_key/api_format）来自所属供应商，装载时合并进来。
    """

    @property
    def key(self) -> str:
        """配置唯一标识；端点请求仍使用原始 model。"""
        from spiritscout.config.model_identity import model_key
        return model_key(self)

    model: str                            # 发给端点的原始模型 ID
    provider_ref: str = ""                # 归属供应商名（ProviderEntry.name）
    provider: str = "openai_compatible"   # 由供应商合并而来
    base_url: str = ""
    api_key: str = ""                     # 由供应商合并而来（密钥库注入）
    api_format: Literal["auto", "chat_completions", "responses", "anthropic_messages"] = "auto"
    capabilities: list[str] = Field(default_factory=list)  # reasoning/code/chinese/security
    context_window: int = 0               # 上下文窗口（token）；0 = 未声明
    # 单次输出上限（token）：0 = 请求不带 max_tokens，由端点按模型自身设定——
    # 上限只在模型想继续说的时候决定掐在哪，设小了思考型模型会把额度耗在
    # 思考上、正文被截成半截 JSON；声明值仅作模型档案的显式覆盖
    max_output_tokens: int = 0
    # 思考强度取值：由每个模型自行声明（如 low/medium/high，或某模型特有的
    # ultra）——不设固定枚举，也不假设各模型档位一致；空 = 该模型不吃思考强度。
    reasoning_effort_levels: list[str] = Field(default_factory=list)
    prompt_caching: Literal["provider_default", "anthropic_ephemeral"] = "provider_default"
    cost_cached_input_per_mtok: float | None = Field(default=None, ge=0)
    cost_cache_write_per_mtok: float | None = Field(default=None, ge=0)
    cost_input_per_mtok: float = Field(default=0.0, ge=0)
    cost_output_per_mtok: float = Field(default=0.0, ge=0)
    rate_limit_rpm: int = Field(default=60, gt=0)
    quality_score: float = 1.0            # 基准评测动态更新
    is_fallback_for: list[str] = Field(default_factory=list, description="主备关系：作为哪些模型配置的备（旧裸名仅唯一时兼容）")

    def validate_cache_transport(self) -> None:
        """显式缓存标记只在已声明的兼容协议上启用。"""
        anthropic = self.api_format == "anthropic_messages" or (
            self.api_format == "auto" and self.provider == "anthropic_compatible")
        if self.prompt_caching == "anthropic_ephemeral" and not anthropic:
            raise ConfigurationError(f"模型 {self.model}：显式 Anthropic 缓存需要 anthropic_messages 协议")

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

    @field_validator("max_output_tokens")
    @classmethod
    def _max_out_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_output_tokens 不能为负")
        return v

    @field_validator("reasoning_effort_levels")
    @classmethod
    def _levels_clean(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()]


class ModelConfigFile(ValidatedConfig):
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
    if isinstance(data.get("gateway"), dict) and "default_tier" in data["gateway"]:
        data["gateway"].pop("default_tier")
        warnings.warn("gateway.default_tier 已废弃，请在编队页指派模型", UserWarning, stacklevel=2)
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
    for entry in out:
        entry.validate_cache_transport()
    return out


_TIER_WARNED = False    # 弃用提示只说一次：热路径每次请求都重新装载
                         # models.yaml，逐次警告会在终端刷屏


def load_model_config(path: str | Path) -> list[ModelEntry]:
    """装载模型注册表（供应商接入信息已合并进各模型）。"""
    global _TIER_WARNED
    data = _expand_env(_load_yaml(path))
    for entry in data.get("models", []) if isinstance(data.get("models"), list) else []:
        if isinstance(entry, dict) and "tier" in entry:
            entry.pop("tier")
            if not _TIER_WARNED:
                _TIER_WARNED = True
                warnings.warn("模型 tier 已废弃，请在编队页指派模型",
                              UserWarning, stacklevel=2)
    try:
        cfg = ModelConfigFile.model_validate(data)
    except Exception as exc:
        raise ConfigurationError(f"模型配置校验失败: {exc}") from exc
    ids = [m.key for m in cfg.models]
    if len(set(ids)) != len(ids):
        raise ConfigurationError("模型 ID 存在重复")
    names = [p.name for p in cfg.providers]
    if len(set(names)) != len(names):
        raise ConfigurationError("供应商名存在重复")
    return _merge_providers(cfg)
