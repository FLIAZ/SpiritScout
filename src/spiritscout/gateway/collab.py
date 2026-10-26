"""多模型投票、辩论、级联及角色路由。

投票使用不同模型独立判断；分歧或模型不足时返回 review。调用顺序执行，
避免同时占用多个端点；协作结果由调用方处理，不直接改变漏洞状态。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from spiritscout.config.settings import ModelEntry
from spiritscout.gateway.gateway import ChatResponse, ModelGateway

# 判定结果规范化取值
VERDICTS = ("pass", "reject", "review")

# 异构角色 → 任务类型（4.9.3-6 × 4.9.2 路由表）
ROLE_TASK_MAP: dict[str, str] = {
    "planner": "meta_planning",   # 规划：旗舰推理模型
    "executor": "execution",      # 执行：中档快速模型
    "verifier": "verification",   # 核验：严谨模型 + 规则引擎
}

_JUDGE_SYSTEM = (
    "你是灵枢 SpiritScout 的多模型协作判定员。对给定安全判定议题独立给出结论，"
    "只输出 JSON：{\"verdict\": \"pass|reject|review\", \"confidence\": 0~1, "
    "\"reason\": \"一句话依据\"}。拿不准一律 review，不得臆断。\nROLE: collab_judge"
)


@dataclass
class JudgeResult:
    """单模型判定结果。"""

    verdict: str = "review"          # pass / reject / review
    confidence: float = 0.0
    reason: str = ""
    raw: str = ""
    model: str = ""


@dataclass
class VoteResult:
    """投票模式结果。"""

    decision: str                    # 一致结论；分歧 → "review"
    agreed: bool
    votes: list[JudgeResult] = field(default_factory=list)
    note: str = ""

    @property
    def models_used(self) -> list[str]:
        return [v.model for v in self.votes]


@dataclass
class DebateResult:
    """辩论模式结果。"""

    decision: str
    converged: bool                  # 两方自行收敛（未动用仲裁）
    rounds: int
    transcript: list[dict[str, Any]] = field(default_factory=list)
    arbiter_model: str = ""


@dataclass
class CascadeResult:
    """级联模式结果。"""

    response: ChatResponse           # 最终采用的应答
    escalated: bool
    confidence: float                # 首棒模型自报置信度
    first_model: str
    final_model: str


def parse_judgment(text: str, model: str = "") -> JudgeResult:
    """解析判定输出：优先 JSON（嵌套/fence 容错），退化关键词扫描（字段不臆造）。"""
    from spiritscout.gateway.backends import extract_json

    raw = text or ""
    try:
        data = extract_json(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict in VERDICTS:
            return JudgeResult(
                verdict=verdict,
                confidence=min(max(float(data.get("confidence", 0.0)), 0.0), 1.0),
                reason=str(data.get("reason", ""))[:300],
                raw=raw, model=model,
            )
    lowered = raw.lower()
    if any(k in lowered for k in ("reject", "拒绝", "误报", "不通过")):
        verdict = "reject"
    elif any(k in lowered for k in ("review", "复核", "不确定", "存疑")):
        verdict = "review"
    elif any(k in lowered for k in ("pass", "通过", "成立", "确认")):
        verdict = "pass"
    else:
        verdict = "review"           # 无法解析 → 复核（保守默认，不臆断）
    return JudgeResult(verdict=verdict, raw=raw, model=model)


class CollabGateway:
    """多模型协作入口（组合 ModelGateway 使用，复用其路由/限流/计量/审计）。"""

    def __init__(self, gateway: ModelGateway):
        self.gateway = gateway
        self.registry = gateway.registry
        self.cfg = gateway.config

    # ------------------------------------------------------------ 投票

    async def vote(
        self,
        subject: str,
        *,
        context: dict[str, Any] | None = None,
        min_models: int | None = None,
        task_type: str = "verification",
    ) -> VoteResult:
        """关键判定投票：≥min_models 个不同模型独立判定，一致才通过。

        适用面按 6.10 限定为不可逆/高影响判定；常规执行请走单模型路由。
        """
        n = max(min_models or self.cfg.vote_min_models, 2)
        models = self.registry.top_models(n, task_type=task_type)
        if len(models) < n:
            # 显式降级：模型不足无法形成独立多判 → 复核（留痕，不静默放行）
            result = VoteResult(
                decision="review", agreed=False, votes=[],
                note=f"注册模型数 {len(models)} 不足投票下限 {n}，降级人工复核",
            )
            self.gateway.audit.log("model.vote", "collab", {
                "decision": result.decision, "agreed": False, "note": result.note})
            return result
        votes = [
            await self._judge(entry, subject, context)
            for entry in models            # 顺序调用（模块docstring：确定性+限流友好）
        ]
        verdict_set = {v.verdict for v in votes}
        agreed = len(verdict_set) == 1
        decision = votes[0].verdict if agreed else "review"
        result = VoteResult(decision=decision, agreed=agreed, votes=votes,
                            note="" if agreed else "模型判定分歧，降级复核")
        self.gateway.audit.log("model.vote", "collab", {
            "decision": decision, "agreed": agreed,
            "models": result.models_used,
            "verdicts": [v.verdict for v in votes],
            "confidences": [v.confidence for v in votes],
        })
        return result

    # ------------------------------------------------------------ 辩论

    async def debate(
        self,
        subject: str,
        *,
        context: dict[str, Any] | None = None,
        max_rounds: int = 2,
        task_type: str = "verification",
    ) -> DebateResult:
        """重大分歧辩论：两模型背对背论证 → 收敛；不收敛 → 第三方仲裁 → 人工。

        max_rounds ≥1；首轮双方互不见面（独立立论），次轮起可见对方上轮论点。
        """
        models = self.registry.top_models(3, task_type=task_type)
        if len(models) < 2:
            result = DebateResult(decision="review", converged=False, rounds=0,
                                  transcript=[])
            self.gateway.audit.log("model.debate", "collab", {
                "decision": "review", "note": "可用模型不足 2 个，无法辩论，降级人工"})
            return result
        side_a, side_b = models[0], models[1]
        arbiter = models[2] if len(models) > 2 else None
        transcript: list[dict[str, Any]] = []
        last: dict[str, JudgeResult] = {}

        for round_no in range(1, max_rounds + 1):
            for label, entry, peer_key in (("A", side_a, "B"), ("B", side_b, "A")):
                peer = last.get(peer_key)
                argument = await self._argue(
                    entry, subject, context, round_no,
                    peer_argument=peer.reason if peer else "",
                    peer_verdict=peer.verdict if peer else "",
                )
                last[label] = argument
                transcript.append({
                    "round": round_no, "side": label, "model": entry.model,
                    "verdict": argument.verdict, "confidence": argument.confidence,
                    "argument": argument.reason,
                })
            if last["A"].verdict == last["B"].verdict:
                result = DebateResult(decision=last["A"].verdict, converged=True,
                                      rounds=round_no, transcript=transcript)
                self.gateway.audit.log("model.debate", "collab", {
                    "decision": result.decision, "converged": True,
                    "rounds": round_no,
                    "models": [side_a.model, side_b.model]})
                return result

        # 不收敛：第三方仲裁（无第三方 → 直接人工复核，不强行二选一）
        if arbiter is not None:
            ruling = await self._arbitrate(arbiter, subject, context, transcript)
            decision = ruling.verdict
            arbiter_model = arbiter.model
        else:
            decision, arbiter_model = "review", ""
        result = DebateResult(decision=decision, converged=False,
                              rounds=max_rounds, transcript=transcript,
                              arbiter_model=arbiter_model)
        self.gateway.audit.log("model.debate", "collab", {
            "decision": decision, "converged": False, "rounds": max_rounds,
            "arbiter": arbiter_model,
            "models": [side_a.model, side_b.model]})
        return result

    # ------------------------------------------------------------ 级联

    async def cascade(
        self,
        messages: list[dict[str, str]],
        *,
        task_type: str = "execution",
        confidence_threshold: float | None = None,
    ) -> CascadeResult:
        """便宜模型先答，置信度 < 阈值升级旗舰复判（6.10：级联升级）。"""
        threshold = (confidence_threshold
                     if confidence_threshold is not None
                     else self.cfg.cascade_confidence_threshold)
        first = self.registry.cheapest_for("simple")
        resp = await self.gateway.chat(messages, model=first.model,
                                       task_type=task_type)
        confidence = parse_judgment(resp.content).confidence
        if confidence >= threshold:
            self.gateway.audit.log("model.cascade", "collab", {
                "escalated": False, "confidence": confidence, "model": first.model})
            return CascadeResult(response=resp, escalated=False,
                                 confidence=confidence,
                                 first_model=first.model, final_model=first.model)
        # 升级旗舰复判（带上首棒结论供参考，不盲从）
        flagship = self.registry.route("meta_planning")
        escalated_messages = list(messages) + [{
            "role": "user",
            "content": f"初级模型({first.model})给出的低置信度结论（confidence="
                       f"{confidence:.2f}），请独立复判并给出最终结论：\n{resp.content[:2000]}",
        }]
        final = await self.gateway.chat(escalated_messages,
                                        model=flagship.model,
                                        task_type="meta_planning")
        self.gateway.audit.log("model.cascade", "collab", {
            "escalated": True, "confidence": confidence,
            "from": first.model, "to": flagship.model})
        return CascadeResult(response=final, escalated=True, confidence=confidence,
                             first_model=first.model, final_model=flagship.model)

    # ------------------------------------------------------------ 异构角色

    async def chat_for_role(
        self, role: str, messages: list[dict[str, str]], **kw: Any
    ) -> ChatResponse:
        """规划/执行/核验三角色异构模型调度（与 4.9.2 路由表联动）。"""
        try:
            task_type = ROLE_TASK_MAP[role]
        except KeyError as exc:
            from spiritscout.core.exceptions import ConfigurationError
            raise ConfigurationError(
                f"未知协作角色: {role}（可用: {sorted(ROLE_TASK_MAP)}）") from exc
        return await self.gateway.chat(messages, task_type=task_type, **kw)

    # ------------------------------------------------------------ 内部

    async def _judge(
        self, entry: ModelEntry, subject: str, context: dict[str, Any] | None
    ) -> JudgeResult:
        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": json.dumps(
                {"subject": subject, "context": context or {}}, ensure_ascii=False)},
        ]
        resp = await self.gateway.chat(messages, model=entry.model,
                                       task_type="verification")
        return parse_judgment(resp.content, entry.model)

    async def _argue(
        self, entry: ModelEntry, subject: str, context: dict[str, Any] | None,
        round_no: int, *, peer_argument: str, peer_verdict: str,
    ) -> JudgeResult:
        user: dict[str, Any] = {
            "subject": subject, "context": context or {}, "round": round_no,
            "instruction": "独立立论" if round_no == 1 else
                           "针对对方论点反驳或修正己方结论",
        }
        if round_no > 1:
            user["peer"] = {"verdict": peer_verdict, "argument": peer_argument[:1000]}
        resp = await self.gateway.chat(
            [{"role": "system", "content": _JUDGE_SYSTEM},
             {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
            model=entry.model, task_type="verification")
        return parse_judgment(resp.content, entry.model)

    async def _arbitrate(
        self, entry: ModelEntry, subject: str, context: dict[str, Any] | None,
        transcript: list[dict[str, Any]],
    ) -> JudgeResult:
        resp = await self.gateway.chat(
            [{"role": "system", "content": _JUDGE_SYSTEM},
             {"role": "user", "content": json.dumps({
                 "subject": subject, "context": context or {},
                 "instruction": "你是仲裁员：阅读双方辩论记录后给出最终判定；"
                                "证据不足一律 review。",
                 "transcript": transcript,
             }, ensure_ascii=False)}],
            model=entry.model, task_type="meta_planning")
        return parse_judgment(resp.content, entry.model)
