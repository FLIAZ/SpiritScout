"""核心数据模型（pydantic v2）。

对应设计书：
- 4.8.1 黑板 8 层（目标与授权/资产/侦察线索/漏洞/凭据/拓扑/任务状态/经验反馈）
- 4.3.2 漏洞核验状态机（候选→初筛→复核→误报→合规→收录/拒收）
- 4.1.1 DAG 任务图谱与多轮迭代
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- enums


class RunMode(str, enum.Enum):
    """运行模式（设计书 2.2 模式化拓扑）。"""

    SRC = "src"                # 常规渗透模式
    CTF = "ctf"                # CTF 专项模式（不启用端口扫描）
    COMPETITION = "competition"  # 智能渗透赛模式（赛制自适应）


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}[self.value]


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    PRUNED = "pruned"      # 分支健康度剪枝
    CANCELLED = "cancelled"


class VulnStatus(str, enum.Enum):
    """漏洞核验状态机（4.3.2 全链路核验）。"""

    CANDIDATE = "candidate"                  # 候选（路线发现的原始线索）
    VERIFYING = "verifying"                  # 独立角色复测中
    INCONCLUSIVE = "inconclusive"            # 缺少证据或规则，不进入收录池
    SCREENED = "screened"                    # 初筛通过
    REPRODUCED = "reproduced"                # 独立复现通过（≥2 种方式交叉）
    FP_CHECKED = "fp_checked"                # 误报排查通过
    COMPLIANCE_CHECKED = "compliance_checked"  # SRC 合规判定通过
    ACCEPTED = "accepted"                    # 终审收录（中危及以上且可利用）
    REJECTED_SCREEN = "rejected_screen"      # 初筛淘汰
    REJECTED_UNREPRO = "rejected_unreproducible"  # 复现失败判误报
    REJECTED_FP = "rejected_false_positive"  # 误报排查淘汰
    REJECTED_COMPLIANCE = "rejected_compliance"   # 合规不予收录
    REJECTED_LOW = "rejected_low_severity"   # 低危仅记录不输出


class AgentType(str, enum.Enum):
    """子 Agent 类型（含三模式全谱系）。"""

    MASTER = "master"
    VERIFIER = "verifier"            # 独立危害复测
    SECRETARY = "secretary"
    BUSINESS_CONTEXT = "business_context"  # 业务流程理解与上下文建模
    RECON = "recon"                # 侦察
    PORT_SCAN = "port_scan"        # 端口扫描
    WEB_VULN = "web_vuln"          # Web 漏洞
    HOST_VULN = "host_vuln"        # 主机漏洞
    POST_EXPLOIT = "post_exploit"  # 后渗透
    CTF_WEB = "ctf_web"
    CTF_PWN = "ctf_pwn"
    CTF_REVERSE = "ctf_reverse"
    CTF_CRYPTO = "ctf_crypto"
    CTF_MISC = "ctf_misc"
    REPORT = "report"


class BlackboardLayer(str, enum.Enum):
    """黑板 8 层（4.8.1）。"""

    TARGET_AUTH = "target_auth"    # 目标与授权层
    ASSET = "asset"                # 资产层
    RECON_CLUE = "recon_clue"      # 侦察线索层
    VULN = "vuln"                  # 漏洞层
    CREDENTIAL = "credential"      # 凭据层
    TOPOLOGY = "topology"          # 拓扑层
    TASK_STATE = "task_state"      # 任务状态层
    EXPERIENCE = "experience"      # 经验反馈层


# ---------------------------------------------------------------- 授权与范围


class Scope(BaseModel):
    """授权范围（合规硬约束；默认 deny，未配置即拒绝启动 —— 合规自查 G1）。"""

    allowed_domains: list[str] = Field(default_factory=list, description="允许的域名（含子域通配 *.example.com）")
    allowed_cidrs: list[str] = Field(default_factory=list, description="允许的网段（CIDR）")
    allowed_urls: list[str] = Field(default_factory=list, description="允许的 URL 前缀")
    denied: list[str] = Field(default_factory=list, description="禁打列表（域名/IP/CIDR 网段/URL 前缀，最高优先）")
    time_window_start: datetime | None = None
    time_window_end: datetime | None = None
    max_requests_per_second: float = 10.0
    allow_sensitive_actions: bool = Field(
        default=False,
        description="启动时声明的敏感操作意向（仅作声明留痕——G5 即时审批不可"
                    "由启动参数免审，动作触发时仍需用户逐类确认）",
    )
    local_analysis: bool = Field(
        default=False,
        description="已授权分析本地工作区文件/任务附件（离线题常态；"
                    "不代表任何网络测试授权）",
    )
    notes: str = ""

    def is_empty(self) -> bool:
        """网络目标与本地分析授权都未声明才算空（纯附件任务可启动）。"""
        return not (self.allowed_domains or self.allowed_cidrs
                    or self.allowed_urls or self.local_analysis)


class Authorization(BaseModel):
    """任务授权声明（写入黑板「目标与授权层」）。"""

    auth_id: str
    mode: RunMode
    scope: Scope
    target_description: str = ""
    src_platform: str | None = Field(default=None, description="目标 SRC 平台（asrc/tsrc/bytesrc/butian/vulbox/cnvd...）")
    human_confirmed: bool = Field(default=False, description="授权信息经人工确认")
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- 黑板实体


class Asset(BaseModel):
    """资产（资产层）。"""

    asset_id: str
    asset_type: Literal["domain", "ip", "url", "service", "endpoint", "app"]
    value: str                          # 域名/IP/URL/服务标识
    port: int | None = None
    service: str | None = None
    fingerprint: dict[str, Any] = Field(default_factory=dict)
    attrs: dict[str, Any] = Field(default_factory=dict)
    source_agent: str = ""
    first_seen: datetime = Field(default_factory=utcnow)

    def dedup_key(self) -> str:
        """汇聚引擎去重键（6.1 一致性）。"""
        return f"{self.asset_type}|{self.value}|{self.port or ''}"


class Vulnerability(BaseModel):
    """漏洞（漏洞层），携带全链路核验状态机与证据链。"""

    vuln_id: str
    title: str
    vuln_type: str                     # sqli / xss / rce / ssrf / idor / upload ...
    target: str                        # 受影响资产/URL
    endpoint: str = ""
    parameter: str = ""
    severity: Severity = Severity.INFO
    cvss_score: float | None = None
    cvss_vector: str | None = None
    status: VulnStatus = VulnStatus.CANDIDATE
    confidence: float = 0.0            # 0~1，随核验阶段提升
    evidence: list[dict[str, Any]] = Field(default_factory=list, description="证据链（请求/响应/回显）")
    poc_code: str | None = None
    exp_code: str | None = None
    poc_verified: bool = False         # PoC 实际运行通过（AWE/MAPTA 证据门控）
    reproductions: list[dict[str, Any]] = Field(default_factory=list, description="复现记录（≥2 种方式）")
    verification: dict[str, Any] = Field(default_factory=dict, description="独立复测裁决、规则引用与实际执行证据")
    false_positive_reason: str | None = None
    compliance: dict[str, Any] = Field(default_factory=dict, description="合规判定：platform/decision/reason")
    found_by: str = ""
    found_at: datetime = Field(default_factory=utcnow)
    exploitable: bool = False          # 实际可利用性（收录硬条件之一）

    def dedup_key(self) -> str:
        return f"{self.vuln_type}|{self.target}|{self.endpoint}|{self.parameter}"


class Credential(BaseModel):
    """凭据（凭据层）。"""

    cred_id: str
    cred_type: Literal["password", "hash", "token", "cookie", "aksk", "private_key", "ticket"]
    principal: str                     # 用户名/账户标识
    secret_ref: str                    # 密文或安全存储引用（不明文入审计）
    source: str = ""
    scope_hint: str = ""               # 适用系统/域
    valid: bool | None = None


class Task(BaseModel):
    """任务包（主 Agent → 子 Agent）。"""

    task_id: str
    task_type: str                     # recon / dir_scan / vuln_probe / verify / ctf_solve ...
    target: str
    agent_type: AgentType
    payload: dict[str, Any] = Field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 5                  # 1（最高）~ 10
    round_no: int = 0                  # 迭代轮次
    parent_dag_node: str | None = None
    # 并发分支路线预算：主 Agent 按任务复杂度逐任务决策（1~8）；
    # None = 未指定，子 Agent 按模板自然条数执行
    max_routes: int | None = None
    # 思考强度：灵枢下发时按任务复杂度裁定，取值必须落在该任务所用模型
    # 自己声明的档位内（各模型档位不同，空 = 用端点默认档）
    reasoning_effort: str = ""
    timeout_seconds: int = 1800
    created_at: datetime = Field(default_factory=utcnow)
    result_summary: dict[str, Any] = Field(default_factory=dict)


class DAGNode(BaseModel):
    """DAG 任务图谱节点（4.1.1；节点类型对应 ATT&CK 战术 L1 层）。"""

    node_id: str
    tactic: str                        # ATT&CK 战术（TA0043/TA0001...）或自定义阶段
    technique: str | None = None       # ATT&CK 技术 ID
    title: str
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    round_created: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class RouteState(BaseModel):
    """子 Agent 内部测试路线状态（分支健康度剪枝的量化依据，AWE 资源治理）。"""

    route_id: str
    task_id: str
    name: str                          # 路线名（差异化策略标识）
    status: TaskStatus = TaskStatus.RUNNING
    progress: float = 0.0              # 0~1 进展分
    value_score: float = 0.0           # 发现价值分
    consecutive_failures: int = 0
    steps_executed: int = 0
    steps_succeeded: int = 0
    steps_failed: int = 0
    tokens_used: int = 0
    tool_calls: int = 0
    risk_score: float = 0.0            # 暴露风险分（工具/步骤申报的累计值）
    detection_signals: int = 0         # 被防护设备拦截/限流信号计数（WAF/403/429，6.5 风险分）
    step_times: list[float] = Field(default_factory=list)  # 近期步骤时间戳（monotonic，探测频率依据，上限 60 条）
    health: float = 1.0                # 健康度（6.5 公式计算结果）
    notes: list[str] = Field(default_factory=list)


class FlagCandidate(BaseModel):
    """CTF Flag 候选（4.4.6）。"""

    flag_id: str
    challenge_id: str
    value: str
    format_ok: bool = False
    submitted: bool = False
    accepted: bool | None = None
    submit_response: str = ""
    found_by: str = ""


class ExperienceEntry(BaseModel):
    """经验反馈层条目（成功/失败案例、绕过模式、误报特征回写沉淀）。"""

    entry_id: str
    kind: Literal["success_case", "failure_case", "bypass_pattern", "fp_signature"]
    content: dict[str, Any]
    source_agent: str = ""
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- 总线事件


class Event(BaseModel):
    """环形数据总线事件（4.2.1 / 6.9：携带 黑板层+数据ID+版本，消费方幂等）。"""

    topic: str                         # 主题分区：bb.<layer> / task.dispatch / flag.submit / audit.alert
    event_id: str = ""
    layer: BlackboardLayer | None = None
    data_id: str = ""
    version: int = 0
    priority: int = 5                  # 1 最高；高危事件（RCE/Flag/凭据）优先
    source: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)
