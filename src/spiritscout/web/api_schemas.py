"""控制台 HTTP 请求模型；校验规则与路由装配分离。"""
from __future__ import annotations

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)


class StorageCleanupPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool = False
    database: StrictBool = True
    caches: StrictBool = True


class LoginPayload(BaseModel):
    """登录请求体。

    不在这里声明长度上限：Pydantic 的 422 会把原始输入回显给客户端，
    超长密码不该出现在响应里；长度在路由内手工校验。
    """

    username: str
    password: str


class ApprovalDecision(BaseModel):
    """审批裁决体：allow 必须是真布尔——字符串 "false"/"0" 等真值文本
    一律 422（安全布尔不做隐式转换）。"""

    allow: StrictBool


class QuestionAnswerPayload(BaseModel):
    """回答及显式扩围目标；拒绝把空答复或错误类型吞成已作答。

    choice=单选下标；choices=多选下标；text 在多选问题里作为补充说明。
    """

    model_config = ConfigDict(extra="forbid")
    choice: StrictInt | None = None
    choices: list[StrictInt] | None = Field(default=None, max_length=6)
    text: str = Field(default="", max_length=1000)
    add_targets: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def require_answer(self) -> QuestionAnswerPayload:
        if self.choice is not None and self.choices:
            raise ValueError("choice 与 choices 只能二选一")
        if self.choice is None and not self.choices and not self.text.strip() \
                and not self.add_targets:
            raise ValueError("请选择选项或填写答复")
        return self


class AgentSideSessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=120)
    task_id: str = Field(min_length=1, max_length=120)
    mission: str = Field(min_length=1, max_length=4000)


class SessionCreatePayload(BaseModel):
    """完整校验创建流程消费的字段，拒绝在搬附件或建会话后才发现类型错误。"""

    model_config = ConfigDict(extra="forbid")

    mode: str | None = None
    mission: str | None = None
    target: str | None = None
    scope: list[str] = Field(default_factory=list)
    proxy_plan_id: str = Field(default='direct', pattern=r'^(direct|proxy-[a-f0-9]{12})$')
    upload_ids: list[str] = Field(default_factory=list, max_length=20)
    platform: str | None = None
    flag_format: str | None = None
    multiple_answers: StrictBool = False
    ctf_type_hints: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=20)
    ctf_type_note: str = Field(default="", max_length=2000)
    challenges_yaml: str | None = None
    platform_yaml: str | None = None
    rules_yaml: str | None = None
    contest_type: str | None = None
    title: str = Field(default="", max_length=80)
    model: str = Field(default="", max_length=512)
    project_id: str = Field(default="", max_length=64)
    browser_account_ids: list[Annotated[str, Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')]] = Field(default_factory=list, max_length=6)
    no_llm: StrictBool = False
    authorized: StrictBool = True
    budget_tokens: StrictInt | None = None
    rps: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    allow_sensitive: StrictBool = False
    accept_rule_conflicts: StrictBool = False


class SessionModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="", max_length=512)


class SessionPatchPayload(BaseModel):
    """会话局部更新：用字段集合区分「未传」与「传空串」。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=80)
    project_id: str | None = Field(default=None, max_length=64)
