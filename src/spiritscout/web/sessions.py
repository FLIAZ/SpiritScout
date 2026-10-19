"""Web 控制台会话管理器（会话隔离的任务操作台）。

隔离模型：每会话独立工作区 ``<workspace>/sessions/<id>/`` 与独立
SystemConfig 副本（黑板 DB / 知识库 / 审计 / 报告路径全部重定向，互不共享）；
同进程 ``asyncio.Task`` 驱动三模式 engagement（runtime 无模块级单例，
天然支持并发，D4）。

``meta.json`` 持久化会话元数据 → 进程重启后扫描 sessions/ 目录重建注册表，
历史会话离线可查；重启时发现 running 残留标记 interrupted（显式而非猜测）。
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError

from spiritscout.agents.ring import RingLedger
from spiritscout.config.settings import ModelEntry, SystemConfig
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import RunMode
from spiritscout.core.pause import PauseGate
from spiritscout.gateway.usage import UsageSnapshot as _UsageSnapshot
from spiritscout.gateway.usage import read_usage
from spiritscout.launch import LaunchPlan, LaunchRequest, prepare_launch
from spiritscout.policy.approvals import ApprovalBroker
from spiritscout.policy.questions import QuestionBroker
from spiritscout.web import agentcfg, vault


def _bound_model(cfg: SystemConfig, models: list[ModelEntry],
                 agent: str = "master") -> str:
    """编队页指派给该 Agent 的模型（须仍在注册表中）。

    对话链路（语义路由/灵枢应答）的说话人是灵枢本人——与运行中的
    AgentBoundGateway 同一绑定语义：空指派 = 不干预（注册表顺序首个
    兜底）；指派的条目已从注册表移除（改过名/删过行）= 同样不干预。
    """
    try:
        assigned = agentcfg.read_assignments(cfg.agent_models_path).get(agent, "")
    except (SpiritScoutError, OSError, ValueError):
        return ""
    return assigned if any(m.model == assigned for m in models) else ""


def _conversation_gateway(cfg: SystemConfig, models: list[ModelEntry], audit: AuditLogger) -> Any:
    """路由与空闲对话也进入同一会话用量台账，运行期继续回灌该快照。"""
    from spiritscout.gateway import ModelGateway, ModelRegistry

    gateway = ModelGateway(ModelRegistry(models), config=cfg.gateway, audit=audit)
    from spiritscout.gateway.usage import bind_usage

    bind_usage(gateway, Path(cfg.workspace.root) / "usage.json")
    return gateway


async def _llm_route_mode(cfg: SystemConfig, models: list[ModelEntry],
                          mission: str, heuristic: RunMode,
                          audit: AuditLogger,
                          history: list[dict[str, str]] | None = None
                          ) -> tuple[RunMode | None, str]:
    """LLM 语义路由（兼任开工预检）：一次调用裁决「模式 + 意图」。

    意图四分类（判断完全在提示词内，后端不写死规则）：
    - task：明确的可执行任务（有具体目标/题目/清晰的行动指令）→ 开工派工；
    - task_with_rules：任务与规则同时下发 → 规则入库后开工；
    - rules：规则/流程/手册说明（含占位凭证），尚无题目或目标 →
      灵枢确认规则并等待，绝不派工（手册里的示例地址不是目标）；
    - dialogue：普通对话/提问 → 灵枢回复，不开工。

    history 为此前的对话往来（续传轮传入）：指代性输入（「继续」「按你
    说的办」）要结合上文才能判准是接着聊还是接着干。
    - 网关调用失败 → 抛出：等同模型预检失败，会话不开工并明示原因；
    - 应答解析失败 → 返回 (None, "task")：按装配层结构化兜底走。
    低思考档提示：分类是小判断，不值得重推理（模型未声明低档时忽略）。
    """
    from spiritscout.gateway.backends import extract_json

    gateway = _conversation_gateway(cfg, models, audit)
    try:
        resp = await gateway.chat(
            [{"role": "system", "content": (
                "你是灵枢的任务路由与意图判读。通读用户输入，判断两件事："
                "属于哪类安全测试分支、用户此刻的意图类型，只输出 JSON。"
                "recent_chat 是此前的对话记录（可能为空）——输入是「继续」"
                "「按你说的办」这类指代性说法时，先看上文再判：接着当前话题"
                "聊是 dialogue，上文是未竟任务且输入是推进指令则是 task。"
                "ROLE: mode_router")},
             {"role": "user", "content": json.dumps({
                 "mission": mission[:3000],
                 "recent_chat": [
                     {"role": str(e.get("role")),
                      "content": str(e.get("content"))[:300]}
                     for e in (history or [])[-6:]],
                 "branches": {
                     "src": "授权安全测试/渗透：对站点、系统或业务做侦察→漏洞"
                            "探测→核验→报告（含 SRC 漏洞挖掘场景）",
                     "ctf": "CTF 解题：目标是具体赛题、以夺取 flag 为目的",
                     "competition": "渗透赛/攻防对抗：有赛制、排名、AWD 等规则"},
                 "intent_types": {
                     "task": "明确的可执行任务：有具体目标/题目/清晰的行动指令",
                     "task_with_rules": "任务与规则同时下发：既有目标或行动指令，"
                                        "又附带流程/约束/API 约定等执行规则",
                     "rules": "规则/流程/手册/约定：讲的是「应该怎么做」——接口"
                              "清单、步骤、约束、终止条件，或凭证为占位符；"
                              "没有可直接执行的具体目标或题目",
                     "dialogue": "普通对话/提问/闲聊：无需派工，灵枢正常回复"},
                 "fallback_guess": heuristic.value,
                 "answer": '{"mode": "src|ctf|competition", '
                           '"intent": "task|task_with_rules|rules|dialogue", '
                           '"rationale": "一句话"}',
             }, ensure_ascii=False)}],
            task_type="classification", response_format="json",
            reasoning_effort="low", model=_bound_model(cfg, models),
            timeout_seconds=min(45, cfg.gateway.request_timeout_seconds))
    except Exception as exc:
        audit.log("system.lifecycle", "mode_router", {
            "action": "model_preflight_failed", "error": str(exc)[:300]})
        raise SpiritScoutError(
            f"模型不可用，会话未开工：{exc}。请在「模型」页检查接入配置"
            f"与密钥后重新发起") from exc
    try:
        data = extract_json(resp.content)
        mode = RunMode(str((data or {}).get("mode", "")).strip().lower())
    except (ValueError, TypeError) as exc:
        audit.log("decision.change", "mode_router", {
            "action": "llm_mode_route_fallback",
            "heuristic": heuristic.value, "reason": f"应答不可解析: {exc}"[:160]})
        return None, "task"
    intent = str((data or {}).get("intent", "task")).strip().lower()
    if intent not in ("task", "task_with_rules", "rules", "dialogue"):
        intent = "task"
    audit.log("decision.change", "mode_router", {
        "action": "llm_mode_route", "llm_mode": mode.value,
        "intent": intent, "heuristic": heuristic.value,
        "rationale": str((data or {}).get("rationale", ""))[:200]})
    return mode, intent


async def _llm_extract_platform(cfg: SystemConfig, models: list[ModelEntry],
                                mission: str, audit: AuditLogger
                                ) -> dict[str, Any] | None:
    """平台档案抽取：任务文本若描述了托管平台（跑分/CTF）的 API 文档
    （接口清单/字段/凭证/限额），把它机械转录为可执行的平台档案 JSON。

    独立小调用（与语义路由同一条对话链路）。判定「是否描述了平台」完全
    在提示词内交给模型；转录必须照抄文档事实，不得发明接口/字段。失败、
    不适用或校验不过 → None（任务照常按赛题清单执行，不阻断开工）。
    """
    from spiritscout.ctf.platform import parse_platform_profile
    from spiritscout.gateway.backends import extract_json

    gateway = _conversation_gateway(cfg, models, audit)
    try:
        resp = await gateway.chat(
            [{"role": "system", "content": (
                "你是平台对接档案抽取器。用户任务文本里可能贴了某个托管平台"
                "（跑分/CTF 平台）的使用说明与 API 文档。先判断：文本是否"
                "描述了平台 API（HTTP 接口清单、鉴权方式、提交约定、并发"
                "限额）？是 → 机械转录为 JSON 档案；不是（普通题目描述、"
                "附件题、单站授权测试等）→ 返回 {\"platform\": null}。"
                "转录纪律：只抄写文档中出现过的事实（URL、方法、字段名、"
                "占位值、凭证原值），不得发明文档未出现的接口或字段；"
                "接口用不到的就不写；没有把握的字段宁可省略。"
                "ROLE: platform_extractor")},
             {"role": "user", "content": json.dumps({
                 "mission": mission[:16000],
                 "answer_shape": {
                     "platform": {
                         "name": "平台名",
                         "base_url": "API 根地址（文档给出的域名）",
                         "headers": {"凭证头名": "文档原文中的凭证值"},
                         "ops": {
                             "precheck": {"method": "GET", "url": "健康检查完整 URL（如有）"},
                             "list": {"method": "GET|POST", "path": "相对路径或完整 URL",
                                      "query": {}, "body": {},
                                      "items_field": "应答中题目数组的点路径（如 data.challenges；顶层数组则留空）"},
                             "start": {"method": "…", "path": "…", "query": {}, "body": {},
                                       "addr_field": "应答中容器地址字段的点路径（如 container_addr）"},
                             "close": {"method": "…", "path": "…", "query": {}, "body": {}},
                             "submit": {"method": "…", "path": "…", "query": {}, "body": {},
                                        "accepted_field": "应答中「是否判对」字段的点路径（如 correct）",
                                        "duplicate_code": "重复提交的业务错误码（如 duplicate）"},
                             "hint": {"method": "…", "path": "…", "query": {}, "body": {}},
                         },
                         "limits": {"max_active": "并发容器上限（文档写了几就填几）"},
                         "challenge_fields": {
                             "id": "列表应答里的题号字段（必填）",
                             "title": "标题字段（没有就省略）",
                             "container_addr": "容器地址字段（有就填）",
                             "is_completed": "该题是否完成的字段（有就填）",
                             "（其余对解题有用的事实字段也映射进来，内部名自取英文名）": "字段路径"},
                     },
                 },
                 "rules": [
                     "题号/地址占位写 {challenge}，flag 值占位写 {flag}（例：query {\"unique_code\": \"{challenge}\"}，body {\"flag\": \"{flag}\"}）",
                     "base_url 与 path 拼成完整地址；文档给的是完整 URL 就直接写 url 字段",
                     "challenge_fields 的值是该字段在列表应答里的点路径",
                 ],
             }, ensure_ascii=False)}],
            task_type="classification", response_format="json",
            reasoning_effort="low", model=_bound_model(cfg, models),
            timeout_seconds=min(90, cfg.gateway.request_timeout_seconds))
    except Exception as exc:  # noqa: BLE001 —— 抽取失败不阻断开工
        audit.log("system.lifecycle", "platform_extractor", {
            "action": "platform_extract_failed", "error": str(exc)[:200]})
        return None
    try:
        data = extract_json(resp.content)
    except Exception:  # noqa: BLE001 —— 应答不可解析按「无平台」处理
        data = None
    raw = (data or {}).get("platform") if isinstance(data, dict) else None
    if not isinstance(raw, dict) or not raw:
        audit.log("decision.change", "platform_extractor", {
            "action": "platform_not_found",
            "rationale": str((data or {}).get("rationale", "")
                             if isinstance(data, dict) else "")[:200]})
        return None
    try:
        profile = parse_platform_profile(raw)
    except ValueError as exc:
        audit.log("decision.change", "platform_extractor", {
            "action": "platform_profile_invalid", "error": str(exc)[:200]})
        return None
    audit.log("decision.change", "platform_extractor", {
        "action": "platform_profile_extracted", "profile": profile.redacted()})
    return profile.to_dict()


# ---------------------------------------------------------------- 会话自动命名
# 与 ChatGPT 用 mini 级模型给会话起标题同一套路：旁路小调用、低思考档、
# 短输出短超时、失败静默降级（展示回退 mission 首行）；命名失败绝不影响
# 会话开工与执行。资产的「来源项目」分类与会话名共用这一次调用产出。

_NAMING_SYSTEM = (
    "你是安全测试平台的登记员，负责给测试会话归档。只输出 JSON。"
    "name：会话短名（4~16 个字，中文，概括测试对象与目的，如"
    "「BiliBili 登录越权测试」「某靶场 SQL 注入」）；"
    "mission 也可能根本不是测试任务（如问候、闲聊、提问咨询）：这时"
    "name 如实概括对话主题（如「问候」「系统用法咨询」），不得照着"
    "示例编造不存在的测试对象或比赛，campaign 给空串；"
    "campaign：本次测试归属的项目名称——厂商 SRC、比赛或漏洞平台"
    "（如「BiliBili SRC」「2026 某CTF」）——同一次调用顺便确定资产归类。"
    "existing_campaigns 是平台上已有的项目清单：本次任务若属于其中某个"
    "项目，campaign 必须原样复用该名称（一字不改）；确实判断不了归属时"
    "campaign 给空串。answer: {\"name\": \"...\", \"campaign\": \"...\"}。"
    "ROLE: namer")

_NAMING_MAX_TOKENS = 1024    # 思考型模型的思考额度也从这里扣：给足余量，
                             # 否则思考耗尽额度正文为空，命名静默失败


def _naming_model_candidates(cfg: SystemConfig, models: list[ModelEntry]) -> list[str]:
    """命名候选链：用户在「模型」页排的降级顺序；空链 = 跟随灵枢指派。

    顺序完全由用户决定，后端不发明兜底规则；链上 ID 已不在注册表
    （改名/删行）时跳过，整链失效才回到默认（编队页灵枢指派，再退
    注册表首项——与网关空指派同语义）。
    """
    names = {m.model for m in models}
    try:
        configured = agentcfg.read_naming_chain(cfg.agent_models_path)
    except (SpiritScoutError, OSError, ValueError):
        configured = []
    chain = [mid for mid in configured if mid in names]
    if chain:
        return chain
    bound = _bound_model(cfg, models, "master")
    if bound:
        return [bound]
    return [models[0].model] if models else []


async def _llm_name(cfg: SystemConfig, models: list[ModelEntry], audit: AuditLogger,
                    *, mission: str, target: str, mode: str,
                    campaigns: list[str] | None = None) -> dict[str, str] | None:
    """按用户排的候选链调用：前面的调不动/应答不成 JSON 就换下一个。

    campaign 携带已有项目清单让分类自然收敛：模型能对应上就复用既名
    （大小写/空格变体归一到既有名称），对应不上才提出新名。
    """
    from spiritscout.gateway.backends import extract_json
    from spiritscout.gateway.gateway import output_lost

    known = [str(name) for name in (campaigns or []) if str(name).strip()][:40]
    candidates = _naming_model_candidates(cfg, models)
    if not candidates:
        return None
    messages = [
        {"role": "system", "content": _NAMING_SYSTEM},
        {"role": "user", "content": json.dumps({
            "mission": mission[:2000], "target": target, "mode": mode,
            "existing_campaigns": known,
        }, ensure_ascii=False)}]
    try:
        gateway = _conversation_gateway(cfg, models, audit)
    except Exception as exc:  # noqa: BLE001 —— 命名是锦上添花，任何失败不阻断主流程
        audit.log("session.naming", "namer", {
            "action": "failed", "mode": mode, "error": str(exc)[:300]})
        return None
    for candidate in candidates:
        try:
            resp = await gateway.chat(
                messages,
                task_type="classification", response_format="json",
                reasoning_effort="low", model=candidate,
                max_tokens=_NAMING_MAX_TOKENS,
                timeout_seconds=min(30, cfg.gateway.request_timeout_seconds))
            if output_lost(resp):
                # 思考型模型可能把输出额度全耗在思考上、正文为空或被端点
                # 掐断：加大余量对同一候选原样重试一次（与决策通道
                # llm.output_retry 同一语义），而不是把「思考吃光额度」
                # 误判成「应答不成 JSON」换下一个候选
                audit.log("session.naming", "namer", {
                    "action": "output_retry", "mode": mode,
                    "model": candidate,
                    "finish_reason": resp.finish_reason,
                    "content_chars": len(resp.content or "")})
                resp = await gateway.chat(
                    messages,
                    task_type="classification", response_format="json",
                    reasoning_effort="low", model=candidate,
                    max_tokens=_NAMING_MAX_TOKENS * 4,
                    timeout_seconds=min(30, cfg.gateway.request_timeout_seconds))
        except Exception as exc:  # noqa: BLE001 —— 该模型调不动：换链上用户排的下一个
            audit.log("session.naming", "namer", {
                "action": "model_unavailable", "mode": mode,
                "model": candidate, "error": str(exc)[:200]})
            continue
        try:
            data = extract_json(resp.content)
        except ValueError as exc:
            audit.log("session.naming", "namer", {
                "action": "unparsed", "mode": mode,
                "model": candidate, "error": str(exc)[:200]})
            continue            # 应答不成 JSON：同样换下一个候选再试
        if not isinstance(data, dict):
            data = {}
        name = str(data.get("name") or "").strip().strip("\"'。").replace("\n", " ")[:40]
        campaign = str(data.get("campaign") or "").strip().strip("\"'。")[:80]
        if campaign:
            folded = campaign.casefold().replace(" ", "")
            for existing in known:
                if existing.casefold().replace(" ", "") == folded:
                    campaign = existing
                    break
        audit.log("session.naming", "namer", {
            "action": "named", "mode": mode, "name": name, "campaign": campaign,
            "model": candidate})
        return {"name": name, "campaign": campaign}
    audit.log("session.naming", "namer", {
        "action": "failed", "mode": mode, "models": candidates})
    return None


_BACKFILL_TASK: asyncio.Task | None = None
_BACKFILL_BATCH = 60     # 单次触发处理的会话数上限（按最早观测先来先办）


def schedule_campaign_backfill(cfg: SystemConfig, models: list[ModelEntry]) -> int:
    """为「已产出资产但尚无项目分类」的历史会话后台补名。

    返回本次纳入的会话数（0 = 无需处理/已在运行/不可执行）。补名全程
    旁路：按任务原文分组去重（同文本只调一次模型），结果复用到组内全部
    会话；任何失败都不影响响应，判不了归属的会话下次触发再试。
    """
    global _BACKFILL_TASK
    if _BACKFILL_TASK is not None and not _BACKFILL_TASK.done():
        return 0
    if not models:
        return 0
    from spiritscout.resources.store import ResourceStore, resource_root

    try:
        store = ResourceStore(resource_root(cfg), cfg.resources.freshness_hours)
        pending = store.sessions_without_campaign(limit=_BACKFILL_BATCH)
    except (OSError, sqlite3.Error, SpiritScoutError):
        return 0
    if not pending:
        return 0
    _BACKFILL_TASK = asyncio.create_task(
        _campaign_backfill(cfg, models, store, pending),
        name="spiritscout-campaign-backfill")
    return len(pending)


async def _campaign_backfill(cfg: SystemConfig, models: list[ModelEntry],
                             store: Any, session_ids: list[str]) -> None:
    """逐组补名：同任务原文只调一次模型，结果复用到组内全部会话。"""
    audit = AuditLogger(cfg.audit_log_path)
    sessions_dir = Path(cfg.workspace.root) / "sessions"
    groups: dict[str, dict[str, Any]] = {}
    for sid in session_ids:
        try:
            meta = json.loads(
                (sessions_dir / sid / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            store.set_session_campaign(sid, "")     # 元数据不可读：不再重复尝试
            continue
        launch = meta.get("launch_request") if isinstance(meta.get("launch_request"), dict) else {}
        request = meta.get("request") if isinstance(meta.get("request"), dict) else {}
        mission = str(launch.get("mission") or request.get("mission") or "").strip()
        target = str(meta.get("target") or "")
        if not mission and not target:
            store.set_session_campaign(sid, "")     # 无任务信息：没有可命名的内容
            continue
        group = groups.setdefault(mission or f"target:{target}", {
            "mission": mission, "target": target,
            "mode": str(meta.get("mode") or ""), "ids": []})
        group["ids"].append(sid)
    named_sessions = attached = 0
    try:
        for group in groups.values():
            campaigns = [row["name"] for row in store.campaigns()["items"]]
            result = await _llm_name(
                cfg, models, audit, mission=group["mission"],
                target=group["target"], mode=group["mode"], campaigns=campaigns)
            if not result or not result.get("campaign"):
                continue        # 调用失败/判不了归属：不落库，下次触发再补
            for sid in group["ids"]:
                attached += store.set_session_campaign(sid, result["campaign"])
            named_sessions += len(group["ids"])
    except (OSError, sqlite3.Error) as exc:
        audit.log("session.naming", "namer", {
            "action": "backfill_failed", "error": str(exc)[:300]})
    audit.log("session.naming", "namer", {
        "action": "backfill_done", "scanned": len(session_ids),
        "groups": len(groups), "named_sessions": named_sessions,
        "updated_observations": attached})


TERMINAL_STATES = frozenset({"completed", "failed", "stopped", "interrupted"})

# 会话对话史封顶（条数）：用户↔灵枢的完整往来，跨续传与重启保留。
# 超出只裁最旧的——大厂网页对话形态：会话末尾随时可接着聊
DIALOGUE_HISTORY_MAX = 40


def _self_view(cfg: SystemConfig) -> str:
    """对话通道的系统自况：编队名册 + MCP 配置清单。

    对话分支不建 infra、不做 MCP 握手——接入状态如实写成「已配置、
    开工时握手、由专项智能体调用」，避免灵枢对自身系统一无所知、
    凭通用模型常识答出「我没有连接任何 MCP 工具」。
    """
    from spiritscout.agents.profiles import PROFILES

    roster = "、".join(
        f"{p.name}（{p.duty.split('：')[0]}）"
        for p in PROFILES
        if p.status == "available" and p.key.value != "master")
    servers = [s for s in cfg.mcp_servers if s.enabled]
    if servers:
        mcp = (f"系统已配置 {len(servers)} 台 MCP 服务器："
               + "、".join(f"{s.name}（{s.transport}）" for s in servers)
               + "。MCP 工具在正式开工时握手接入，按角色权限并入对应专项"
                 "智能体的工具目录、由它们调用——你（灵枢）负责元决策与"
                 "调度，不直接持有这些工具。")
    else:
        mcp = "系统当前未配置 MCP 服务器（configs/system.yaml 的 mcp_servers 段）。"
    return (f"【系统自况】你是灵枢（SpiritScout）多智能体安全测试系统的主"
            f"智能体，麾下专项编队：{roster}。{mcp}"
            "用户问及系统自身能力时按此如实回答。")


def _remember_reply(session: Session, reply: str) -> None:
    """灵枢应答落对话史（封顶保留）；用户消息由 /messages 端点随发随落。"""
    session.dialogue_history.append(
        {"role": "assistant", "content": (reply or "")[:4000]})
    del session.dialogue_history[:-DIALOGUE_HISTORY_MAX]


async def _master_reply(cfg: SystemConfig, models: list[ModelEntry],
                        user_text: str, audit, *, kind: str,
                        history: list[dict[str, str]] | None = None) -> str:
    """灵枢对非派工输入的应答（dialogue 正常对话 / rules 规则确认）。

    history 为此前的对话往来（不含本条 user_text）——会话终态后继续发
    消息时灵枢带着上文接着聊，与运行中直达通道共用同一份对话史。
    LLM 不可用时回退固定话术——用户至少能看到确定的反馈。
    """
    system = ("你是灵枢。用中文写一段简短、自然的回复。\n" + _self_view(cfg)
              if kind == "dialogue" else
              "你是灵枢。用户刚下发了执行规则/流程手册（不是具体题目或目标）。"
              "写一段简短确认：记住了哪些关键约定（3~5 条要点），正在等待"
              "用户提供什么（题目/目标/真实凭证）。")
    messages = [{"role": "system", "content": system},
                *[{"role": str(e.get("role")), "content": str(e.get("content"))[:2000]}
                  for e in (history or [])[-20:]],
                {"role": "user", "content": user_text[:4000]}]
    try:
        gateway = _conversation_gateway(cfg, models, audit)
        resp = await gateway.chat(
            messages,
            task_type="meta_planning", max_tokens=1024,
            model=_bound_model(cfg, models),
            timeout_seconds=min(60, cfg.gateway.request_timeout_seconds))
        text = (resp.content or "").strip()
        if text:
            return text[:2500]
    except (SpiritScoutError, OSError, ValueError, TypeError) as exc:
        # 应答失败回退固定话术，输入已持久化不丢
        audit.log("master.reply_failed", "master", {"error": str(exc)[:200]})
    if kind == "rules":
        return ("已收到执行规则并完整记录：灵枢将严格按该约定行事。"
                "请下发具体题目或目标（以及真实凭证），我再开工。")
    return ("已收到你的消息。当前没有可执行的任务指令——下发目标或题目"
            "后我立即安排。")


def _preflight_models(cfg: SystemConfig, models: list[ModelEntry],
                      audit: AuditLogger) -> None:
    """开工前预检（静态，毫秒级）：模型注册表非空。

    连通性不再用 LLM 调用证明——重推理端点上一次「就绪检查」曾花 10s+，
    多题并发时是纯浪费；语义路由分支由路由调用兼任预检，静态路由分支的
    连通性由首个真实模型调用证明（失败在时间线上即时可见）。
    """
    if not models:
        audit.log("system.lifecycle", "preflight", {
            "action": "model_preflight_failed", "error": "模型注册表为空"})
        raise SpiritScoutError(
            "模型注册表为空，会话未开工。请在「模型」页配置至少一个模型后重试")



def _coerce_meta(data: dict[str, Any]) -> dict[str, Any]:
    """元数据字段类型收敛：旧版本/手改文件里类型漂移的字段拉回正型。"""
    def _dict(v: Any) -> dict[str, Any]:
        return v if isinstance(v, dict) else {}

    def _str(v: Any) -> str:
        return v if isinstance(v, str) else ("" if v is None else str(v))

    def _float(v: Any, default: float = 0.0) -> float:
        try:
            result = float(v)
            return result if math.isfinite(result) else default
        except (TypeError, ValueError, OverflowError):
            return default

    def _opt_float(v: Any) -> float | None:
        try:
            return None if v is None or not math.isfinite(float(v)) else float(v)
        except (TypeError, ValueError, OverflowError):
            return None

    def _list(v: Any) -> list:
        return v if isinstance(v, list) else []

    status = data.get("status", "interrupted")
    if not isinstance(status, str) or status not in TERMINAL_STATES | {"created", "running"}:
        status = "interrupted"
    summary = dict(_dict(data.get("summary")))
    if "run" in summary:
        summary["run"] = _dict(summary["run"])
    return {
        **data,
        "session_id": _str(data.get("session_id")),
        "mode": _str(data.get("mode")),
        "target": _str(data.get("target")),
        "title": _str(data.get("title")),
        "auto_title": _str(data.get("auto_title")),
        "request": _dict(data.get("request")),
        "launch_request": _dict(data.get("launch_request")),
        "standing_rules": _str(data.get("standing_rules")),
        "opinions": [e for e in _list(data.get("opinions"))
                     if isinstance(e, dict)],
        "dialogue_history": [
            e for e in _list(data.get("dialogue_history"))
            if isinstance(e, dict) and e.get("role") and e.get("content")],
        "created_at": _float(data.get("created_at")),
        "started_at": _opt_float(data.get("started_at")),
        "finished_at": _opt_float(data.get("finished_at")),
        "summary": summary,
        "error": _str(data.get("error")),
        "status": status,
        "phase": _str(data.get("phase")) or status,
        "paused": data.get("paused") is True,
        "excluded_seconds": max(0.0, _float(data.get("excluded_seconds"))),
    }


@dataclass
class Session:
    """一次渗透任务会话（会话隔离单元）。"""

    session_id: str
    mode: str
    target: str
    workspace: Path
    title: str = ""                                          # 会话名（可重命名；空 = 自动摘要）
    # 小模型自动命名（创建后台生成；展示优先级：用户重命名 > auto_title > mission 首行）
    auto_title: str = ""
    request: dict[str, Any] = field(default_factory=dict)   # 启动参数（展示用）
    status: str = "created"      # created/running/completed/failed/stopped/interrupted
    phase: str = "queued"        # 细粒度启动/执行阶段，避免把启动等待误报成业务覆盖
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    # 用户下发的执行规则（rules/task_with_rules 意图入库）：随每次开工
    # 下发到灵枢与各智能体；跨轮次与续传保留
    standing_rules: str = ""
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    # 高风险动作即时确认队列（G5 交互形态；进程内状态，会话终态即失效）
    approvals: ApprovalBroker = field(
        default_factory=ApprovalBroker, repr=False, compare=False)
    # 模型发起的用户询问队列（ask_user 工具后端；问题与选项由模型撰写）
    questions: QuestionBroker = field(
        default_factory=QuestionBroker, repr=False, compare=False)
    # 环形协同台账：子 Agent 已尝试登记 + 灵枢督导建议（进程内，随会话消亡）
    ring: RingLedger = field(
        default_factory=RingLedger, repr=False, compare=False)
    # 用户会话中发来的意见与附件记录（进程内收件箱；灵枢在最近的决策点
    # 取走消费——派发期督导或轮末复盘，消费动作落审计事件）
    user_inbox: list[dict[str, Any]] = field(
        default_factory=list, repr=False, compare=False)
    # 启动计划（进程内持有，续传复用同一授权/赛题载荷）
    plan: Any = field(default=None, repr=False, compare=False)
    # 原始启动参数（含赛题/赛制 YAML，落 meta.json）——控制台重启后据此
    # 确定性重建启动计划，续传不因重启被拦
    launch_request: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False)
    # 用户历史意见（最新在后，封顶保留；拓扑图与状态面展示用）
    opinions: list[dict[str, Any]] = field(
        default_factory=list, repr=False, compare=False)
    # 会话对话史（用户↔灵枢，最新在后）：首条即任务原文，续传轮以末尾
    # 为新输入，灵枢每次开口都带着它——大厂网页对话形态，会话任意时刻
    # 可从末尾接着聊。落 meta.json，跨重启保留；封顶见 DIALOGUE_HISTORY_MAX
    dialogue_history: list[dict[str, str]] = field(
        default_factory=list, repr=False, compare=False)
    # 续传前的终态（续传失败时恢复，原成果不丢）
    resume_prev_status: str | None = field(
        default=None, repr=False, compare=False)
    # 暂停门 + 用时扣账 + 执行间隙检测（进程内，随会话任务共持；
    # paused 态持久化到 meta.json，控制台重启后续传仍保持暂停）
    pause_gate: PauseGate = field(
        default_factory=PauseGate, repr=False, compare=False)

    # ---- 路径（隔离边界）
    @property
    def bb_path(self) -> Path:
        return self.workspace / "bb.db"

    @property
    def audit_path(self) -> Path:
        return self.workspace / "audit.jsonl"

    @property
    def flags_path(self) -> Path:
        return self.workspace / "flags.json"

    @property
    def meta_path(self) -> Path:
        return self.workspace / "meta.json"

    def model_usage(self) -> dict[str, Any]:
        """列表、状态页与仪表盘共用的用量视图；损坏快照回退到已保存汇总。"""
        live = read_usage(self.workspace / "usage.json")
        if live is not None:
            return live
        run = self.summary.get("run") if isinstance(self.summary, dict) else None
        stored = run.get("model_usage") if isinstance(run, dict) else None
        try:
            usage = _UsageSnapshot.model_validate(stored).model_dump()
        except ValidationError:
            usage = {}
        if usage.get("call_series"):
            from spiritscout.gateway.cache import cache_summary

            usage["cache"] = cache_summary(usage["call_series"])
        return usage

    def report_path(self) -> Path | None:
        """最新报告文件（未生成 → None）。"""
        reports = self.workspace / "reports"
        if not reports.is_dir():
            return None
        candidates = sorted(reports.glob("*.md"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0] if candidates else None

    def display_title(self) -> str:
        """会话名：用户重命名优先，其次小模型自动命名，最后回到任务首行。"""
        if self.title:
            return self.title
        if self.auto_title:
            return self.auto_title
        mission = str(self.request.get("mission") or "").strip()
        if mission:
            first = mission.splitlines()[0].strip()
            return first[:42] + ("…" if len(first) > 42 else "")
        return self.target or self.session_id

    def brief(self, *, include_summary: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": self.session_id, "mode": self.mode,
            "title": self.display_title(), "renamed": bool(self.title),
            "target": self.target, "status": self.status,
            "phase": self.phase,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "error": self.error,
            "paused": self.pause_gate.paused,
            "excluded_seconds": round(self.pause_gate.excluded_seconds, 1),
        }
        # 仪表盘总览的轻量指标（从运行摘要提取，不走完整 summary）
        run = (self.summary or {}).get("run", {}) if isinstance(self.summary, dict) else {}
        usage = self.model_usage()
        out["tokens"] = usage.get("used_tokens", 0)
        out["cost_usd"] = usage.get("used_usd", 0.0)
        # 运行中实时用量优先：网关逐笔持久化的 usage.db（兼容旧 usage.json）（列表/仪表盘随轮询跳动）
        out["rounds"] = run.get("rounds_executed", 0)
        # Flag 统计：CTF 结果在 summary["flags"]（运行摘要本身不带 flag_stats）；
        # 面板口径统一为 total/submitted/accepted/rejected
        flags = run.get("flag_stats") or (
            self.summary.get("flags") if isinstance(self.summary, dict) else None)
        if flags:
            flags = {**flags, "total": flags.get("total", flags.get("extracted", 0))}
        out["flag_stats"] = flags
        if include_summary:
            out["summary"] = self.summary
        return out


class SessionManager:
    """会话注册表与生命周期（创建/启动/停止/删除/重启重建）。

    任务创建走「创建队列 + 监督协程」：HTTP 端点只入队，监督协程（在应用
    生命周期内启动、不隶属任何请求作用域）负责 spawn 会话任务。原因：
    starlette TestClient/ASGI 栈会在请求返回时取消请求任务组内的 asyncio
    任务——若会话任务在端点内直接 create_task，响应一发出就会被误取消
    （实测 t≈响应完成时刻，CancelledError 经 langgraph 传播）。
    """

    def __init__(self, cfg: SystemConfig, models: list[ModelEntry],
                 *, max_concurrent: int = 4):
        self._cfg = cfg
        self._models = models
        self._sem = asyncio.Semaphore(max_concurrent)
        self._sessions: dict[str, Session] = {}
        self._queue: asyncio.Queue[
            tuple[Session, SystemConfig, LaunchPlan, bool, list[ModelEntry], bool]] = \
            asyncio.Queue()
        self._supervisor: asyncio.Task | None = None
        self._naming_tasks: set[asyncio.Task] = set()   # 会话自动命名（后台旁路）
        self.root = Path(cfg.workspace.root) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._scan()

    # ------------------------------------------------------------ 注册表

    def _scan(self) -> None:
        """启动重建：加载历史会话；running 残留 → interrupted（进程已死）。

        损坏或缺关键元的 meta.json 隔离改名（meta.corrupt-<时刻>.json）并
        告警——不静默吞掉也不自动删，用户数据误判可人工找回；类型漂移的
        字段（旧版本/手改文件）按正型收敛，不让一个坏字段炸掉整个注册表。
        """
        for meta in sorted(self.root.glob("*/meta.json")):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._quarantine_meta(meta, f"解析失败: {exc}")
                continue
            if not isinstance(data, dict) or not str(data.get("session_id") or ""):
                self._quarantine_meta(meta, "不是键值映射或缺 session_id")
                continue
            try:
                data = _coerce_meta(data)
                session = Session(
                    session_id=data["session_id"], mode=data["mode"],
                    target=data["target"], workspace=meta.parent,
                    title=data["title"],
                    auto_title=data["auto_title"],
                    request=data["request"],
                    launch_request=data["launch_request"],
                    opinions=data["opinions"],
                    dialogue_history=data["dialogue_history"],
                    status=data.get("status", "interrupted"),
                    phase=data.get("phase", "interrupted"),
                    created_at=data["created_at"],
                    started_at=data["started_at"],
                    finished_at=data["finished_at"],
                    summary=data["summary"], error=data["error"],
                    standing_rules=data["standing_rules"],
                )
                session.pause_gate.restore(data["paused"], data["excluded_seconds"])
            except (TypeError, ValueError) as exc:
                self._quarantine_meta(meta, f"字段类型异常: {exc}")
                continue
            # 暂停态与扣账跨重启恢复：静默置门（不补记审计）
            if session.status not in TERMINAL_STATES:
                session.status = "interrupted"
                session.phase = "interrupted"
                session.finished_at = session.finished_at or time.time()
                self._persist(session)
            self._sessions[session.session_id] = session

    def _quarantine_meta(self, meta: Path, reason: str) -> None:
        """损坏元数据隔离：改名留证 + 标准错误告警（不静默、不删除）。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = meta.with_name(f"meta.corrupt-{stamp}.json")
        serial = 0
        while target.exists():                  # 同秒多份损坏不互相覆盖
            serial += 1
            target = meta.with_name(f"meta.corrupt-{stamp}-{serial}.json")
        try:
            meta.rename(target)
        except OSError as exc:
            print(f"[spiritscout] 会话元数据损坏且无法隔离（{reason}）: {exc}",
                  file=sys.stderr)
            return
        print(f"[spiritscout] 会话元数据损坏已隔离：{meta.parent.name}"
              f"（{reason}）→ {target.name}", file=sys.stderr)

    def _persist(self, session: Session) -> None:
        payload = json.dumps({
            "session_id": session.session_id, "mode": session.mode,
            "target": session.target, "title": session.title,
            "auto_title": session.auto_title,
            "request": session.request,
            "launch_request": session.launch_request,
            "standing_rules": session.standing_rules,
            "opinions": session.opinions[-8:],
            "dialogue_history": session.dialogue_history[-DIALOGUE_HISTORY_MAX:],
            "status": session.status, "phase": session.phase,
            "created_at": session.created_at,
            "started_at": session.started_at, "finished_at": session.finished_at,
            "summary": session.summary, "error": session.error,
            "paused": session.pause_gate.paused,
            "excluded_seconds": round(session.pause_gate.excluded_seconds, 1),
        }, ensure_ascii=False, indent=2)
        # 原子替换：写半截崩进程不留损坏的 meta.json（损坏文件扫描时会被隔离）
        tmp = session.meta_path.with_name("meta.json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(session.meta_path)

    def list(self) -> list[Session]:
        # 先复制再遍历：/api/sessions 等同步端点在线程池执行，与本事件
        # 循环的创建/删除并发时会撞「dictionary changed size」
        return sorted(self._sessions.copy().values(),
                      key=lambda s: s.created_at, reverse=True)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def _set_phase(self, session: Session, phase: str,
                   audit: AuditLogger | None = None) -> None:
        """持久化用户可见阶段；审计存在时同步留痕。"""
        if session.phase == phase:
            return
        session.phase = phase
        self._persist(session)
        if audit is not None:
            audit.log("system.lifecycle", "runtime", {
                "action": "phase_changed", "phase": phase})

    # ------------------------------------------------------------ 生命周期

    def _session_cfg(self, session: Session, budget_tokens: int | None) -> SystemConfig:
        """会话级配置副本：全部状态路径重定向到会话工作区（隔离核心）。"""
        cfg = self._cfg.model_copy(deep=True)
        ws = session.workspace
        # 控制面根目录：会话工作区在基础工作区内部，沙箱遮蔽需连基础根
        # 一起（vault 密钥库/兄弟会话/审计都在里面），只遮会话目录不够
        cfg.workspace.guard_root = str(self._cfg.workspace.root)
        try:
            cfg.mode = RunMode(session.mode)
        except ValueError:
            # 对话/规则展示态会话（session.mode=dialogue/rules）：配置副本
            # 只服务回复/审计路径，不派工——借用主配置模式，别让 RunMode
            # 构造崩掉续传（续传是否派工由语义路由重新裁决）
            cfg.mode = self._cfg.mode
        from spiritscout.resources.store import ResourceStore, resource_root

        cfg.resources.root = str(resource_root(self._cfg))
        resources = ResourceStore(cfg.resources.root, cfg.resources.freshness_hours)
        cfg.mcp_servers = resources.effective_mcp(cfg.mcp_servers)
        cfg.workspace.root = str(ws)
        # 经验库跨会话共享：不随会话工作区重定向（显式配置优先）
        cfg.experience.root = self._cfg.experience.root or str(self._cfg.workspace.root)
        cfg.blackboard.sqlite_path = str(session.bb_path)
        cfg.knowledge.sqlite_path = str(ws / "kb.db")
        cfg.knowledge.chroma_path = str(ws / "chroma")
        cfg.audit_log_path = str(session.audit_path)
        cfg.report.output_dir = str(ws / "reports")
        if budget_tokens:
            cfg.gateway.budget_tokens = budget_tokens
        return cfg

    # ------------------------------------------------------------ 监督协程

    def start(self) -> None:
        """启动监督协程（幂等；应用 lifespan startup 或直接使用时懒启动）。"""
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(
                self._supervise(), name="spiritscout-supervisor")

    async def _supervise(self) -> None:
        """从创建队列取会话并 spawn 执行任务（不隶属任何请求作用域）。"""
        while True:
            session, cfg, plan, no_llm, models, resume = await self._queue.get()
            try:
                if resume:
                    if session.status != "running":
                        continue        # 续传排队期间状态被并发改动（如再次停止）
                elif session.status != "created":
                    continue            # 排队期间已被停止/删除
                session.status = "running"
                session.phase = "starting"
                session.started_at = session.started_at or time.time()
                self._persist(session)
                session.task = asyncio.create_task(
                    self._run(session, cfg, plan, no_llm, models, resume),
                    name=f"spiritscout-{session.session_id}",
                )
                session.task.add_done_callback(
                    lambda task, current=session: self._settle_task(current, task))
                if session.status != "running":
                    session.task.cancel()   # spawn 瞬间被并发 stop：立即取消
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 监督器不能因单会话退出
                session.status = "failed"
                session.phase = "failed"
                session.error = f"启动监督失败：{type(exc).__name__}: {exc}"[:500]
                session.finished_at = time.time()
                self._persist(session)
            finally:
                self._queue.task_done()

    def _settle_task(self, session: Session, task: asyncio.Task) -> None:
        """兜住执行协程 try 块之外的异常，禁止会话永久留在 running。"""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            exc = None
        if session.status in TERMINAL_STATES:
            return
        session.status = "failed" if exc is not None else "stopped"
        session.phase = session.status
        if exc is not None:
            session.error = f"执行任务异常退出：{type(exc).__name__}: {exc}"[:500]
        session.finished_at = time.time()
        self._persist(session)

    async def aclose(self) -> None:
        """应用关停：停监督协程，再停所有未终态会话（幂等）。"""
        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
        for session in list(self._sessions.values()):
            if session.status not in TERMINAL_STATES:
                await self.stop(session.session_id)

    async def create(self, payload: dict[str, Any], plan: LaunchPlan, *,
                     staged_files: list[Path] | None = None) -> Session:
        """创建会话并入队启动（payload 已经 app 层合规校验；plan 由 launch 装配）。

        staged_files：用户任务下达前暂存的附件——在此移入会话工作区并注入
        赛题载荷（必须在入队前完成，会话任务可能即刻开工）。
        """
        # 热生效：Web 端改模型注册表后，新会话立即用新配置；
        # 加载失败显式报错（不静默退回旧配置）。先于建目录，失败不留痕。
        from spiritscout.config import load_model_config
        models = vault.apply_keys(
            load_model_config(self._cfg.models_config_path))
        if not models:
            raise SpiritScoutError(
                f"模型注册表为空: {self._cfg.models_config_path}（"
                "请在 Web「模型」面板或配置文件中至少注册一个云端模型）")
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        session = Session(
            session_id=session_id,
            mode=str(payload["mode"]), target=plan.target,
            workspace=self.root / session_id,
            title=str(payload.get("title") or "").strip()[:80],
            request={k: v for k, v in payload.items()
                     if k not in ("challenges_yaml", "rules_yaml", "upload_ids")},
            created_at=time.time(),
        )
        session.workspace.mkdir(parents=True, exist_ok=False)
        session.plan = plan
        session.launch_request = {k: v for k, v in payload.items()
                                  if k != "upload_ids"}
        # 对话史首条：用户的任务原文即这段会话的第一句话（与会话页首颗
        # 气泡同源），此后的续传对话从这里接着长
        if plan.mission:
            session.dialogue_history.append(
                {"role": "user", "content": plan.mission[:4000]})
        self._sessions[session.session_id] = session
        self._persist(session)

        try:
            cfg = self._session_cfg(session, payload.get("budget_tokens"))
            session.approvals = ApprovalBroker(AuditLogger(cfg.audit_log_path))
            session.questions = QuestionBroker(AuditLogger(cfg.audit_log_path))
            session.ring = RingLedger(AuditLogger(cfg.audit_log_path))
            session.pause_gate = PauseGate(AuditLogger(cfg.audit_log_path))
            no_llm = bool(payload.get("no_llm", False))
            # 暂存附件材料化：移入工作区 attachments/ + 注入无附件的赛题载荷
            # （方向判类/附件分析工具/题面视图直接可用）+ 种入灵枢收件箱
            if staged_files:
                dest_dir = session.workspace / "attachments"
                dest_dir.mkdir(parents=True, exist_ok=True)
                audit = AuditLogger(cfg.audit_log_path)
                records: list[dict[str, Any]] = []
                for src in staged_files:
                    dest = dest_dir / src.name
                    n = 2
                    while dest.exists():            # 同名不覆盖
                        dest = dest_dir / f"{src.stem}-{n}{src.suffix}"
                        n += 1
                    shutil.move(str(src), str(dest))
                    records.append({"kind": "attachment", "name": dest.name,
                                    "path": str(dest),
                                    "size": dest.stat().st_size})
                    audit.log("user.attachment", "user", {
                        "name": dest.name, "size": records[-1]["size"],
                        "path": str(dest)})
                session.user_inbox.extend(records)
                for ch in plan.challenges:
                    if not ch.get("attachments"):
                        ch["attachments"] = [r["path"] for r in records]
                for updir in {p.parent for p in staged_files}:
                    shutil.rmtree(updir, ignore_errors=True)
            self.start()        # 直接使用（非 Web lifespan）时懒启动监督协程
            await self._queue.put((session, cfg, plan, no_llm, models, False))
        except Exception as exc:
            # 登记后中途失败（配置构建/附件搬运/入队）：落终态再原样抛出——
            # 「created」既非终态也无法续传，留下就成了删不掉也开不了的僵尸
            session.status = "failed"
            session.phase = "failed"
            session.error = f"会话创建中途失败（{type(exc).__name__}）"[:500]
            session.finished_at = time.time()
            try:
                self._persist(session)
            except Exception:  # noqa: BLE001 —— 落盘失败不遮蔽原始异常
                pass
            raise
        if not no_llm:
            # 命名是旁路增强：不阻塞入队，失败静默（展示回退 mission 首行）
            self._schedule_naming(session, models, plan)
        return session

    def _schedule_naming(self, session: Session,
                         models: list[ModelEntry], plan: LaunchPlan) -> None:
        """创建后立刻后台命名：小模型给「会话短名 + 归属项目」，不阻塞开工。"""
        if not (plan.mission or plan.target):
            return
        if not models:
            return              # 没有模型可调（创建链路已保证注册表非空，兜底）
        task = asyncio.create_task(
            self._name_session(session, models, plan),
            name=f"spiritscout-naming-{session.session_id}")
        self._naming_tasks.add(task)
        task.add_done_callback(self._naming_tasks.discard)

    async def _name_session(self, session: Session,
                            models: list[ModelEntry], plan: LaunchPlan) -> None:
        from spiritscout.resources.store import ResourceStore, resource_root

        # 命名是平台旁路调用：计量与审计都走全局台账（与开工前路由预检、
        # 历史补名同一口径），不混入该会话——否则会话页的「模型消耗」背上
        # 命名开销（一句问候也被记几百 token），时间线还多出一条莫名的
        # classification 调用
        audit = AuditLogger(self._cfg.audit_log_path)
        try:
            try:
                store: ResourceStore | None = ResourceStore(
                    resource_root(self._cfg), self._cfg.resources.freshness_hours)
                campaigns = [row["name"] for row in store.campaigns()["items"]]
            except (OSError, sqlite3.Error):
                store, campaigns = None, []
            result = await _llm_name(
                self._cfg, models, audit, mission=plan.mission or "",
                target=plan.target, mode=session.mode, campaigns=campaigns)
            if not result or self._sessions.get(session.session_id) is not session:
                return          # 调用失败，或会话已被删除/替换：不落库不回写
            if result.get("name"):
                session.auto_title = result["name"]
                self._persist(session)
            if result.get("campaign") and store is not None:
                updated = store.set_session_campaign(
                    session.session_id, result["campaign"])
                audit.log("session.naming", "namer", {
                    "action": "campaign_attached",
                    "campaign": result["campaign"], "observations": updated})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 命名旁路绝不影响会话主流程
            audit.log("session.naming", "namer", {
                "action": "error",
                "error": f"{type(exc).__name__}: {exc}"[:300]})

    def _rebuild_plan(self, session: Session, *, mission: str | None = None,
                      extra_scope: list[str] | None = None) -> LaunchPlan | None:
        """控制台重启后从持久化的启动参数重建计划（同一输入 → 同一装配）。

        mission/extra_scope 供续传真任务的计划重建覆盖：新任务消息替换
        原任务原文，既有授权范围并入 extra_scope（用户追加目标时不缩
        既有边界——授权只增不减，禁打列表仍是唯一硬边界）。
        """
        req = session.launch_request or {}
        if not req:
            return None
        try:
            # 自动模式且尚未创建黑板，说明旧进程在真正开工前中断；应按原始
            # 用户意图重新判定，而不是把当时的临时兜底模式永久钉死。
            untouched_auto = bool(req.get("mode_auto")) and not session.bb_path.exists()
            try:
                mode_override = RunMode(session.mode)
            except ValueError:
                # 对话/规则等展示态不是业务分支：交回语义路由重新裁决
                # （对话会话重启后续传此前会在此摔成「缺少启动信息」）
                mode_override = None
            plan = prepare_launch(LaunchRequest(
                mode=None if untouched_auto else mode_override,
                target=req.get("target") or None,
                extra_scope=[*(req.get("scope") or []), *(extra_scope or [])],
                platform=req.get("platform") or None,
                allow_sensitive=bool(req.get("allow_sensitive", False)),
                rps=float(req.get("rps", 10.0)),
                flag_format=req.get("flag_format") or None,
                challenges_yaml=req.get("challenges_yaml") or None,
                platform_yaml=req.get("platform_yaml") or None,
                rules_yaml=req.get("rules_yaml") or None,
                accept_rule_conflicts=bool(req.get("accept_rule_conflicts", False)),
                mission=mission if mission is not None
                        else (req.get("mission") or None),
                has_uploads=(session.workspace / "attachments").is_dir(),
            ))
        except (SpiritScoutError, TypeError, ValueError):
            return None
        # 工作区里用户上传过的附件重新挂到无附件的赛题上（与创建时同规则）
        att = session.workspace / "attachments"
        if att.is_dir() and plan.challenges:
            files = sorted(str(p) for p in att.iterdir() if p.is_file())
            if files:
                for ch in plan.challenges:
                    if not ch.get("attachments"):
                        ch["attachments"] = files
        return plan

    def _cancel_stale_tasks(self, session: Session) -> int:
        """续传前归档上一次被中断的黑板任务。

        进程停止时子 Agent 已不存在，黑板中仍为 pending/running 的任务不可能
        继续执行。将其原位更新为 cancelled，避免状态页和后续报告把历史残留
        当成当前活跃任务。
        """
        if not session.bb_path.exists():
            return 0
        from spiritscout.blackboard import Blackboard
        from spiritscout.core.models import AgentType, BlackboardLayer

        active = {"pending", "dispatched", "running"}
        changed = 0
        bb = Blackboard(session.bb_path, bus=None)
        try:
            for rec in bb.query(BlackboardLayer.TASK_STATE, limit=10000):
                data = rec["data"]
                if "dag_snapshot" in data:
                    dirty = False
                    for node in data.get("dag_snapshot", []):
                        if node.get("status") in active:
                            node["status"] = "cancelled"
                            dirty = True
                    if dirty:
                        bb.write(
                            BlackboardLayer.TASK_STATE, rec["record_id"], data,
                            agent_type=AgentType.MASTER,
                            updated_by="session-resume",
                            expected_version=rec["version"],
                        )
                    continue
                task = data.get("task") or {}
                if task.get("status") not in active:
                    continue
                task["status"] = "cancelled"
                for route in data.get("routes", []):
                    if route.get("status") in active:
                        route["status"] = "cancelled"
                summary = data.setdefault("summary", {})
                summary["interrupted"] = True
                summary["reason"] = "上一次会话在任务终态写入前停止"
                bb.write(
                    BlackboardLayer.TASK_STATE, rec["record_id"], data,
                    agent_type=AgentType.MASTER,
                    updated_by="session-resume",
                    dedup_key=f"task|{rec['record_id']}",
                    expected_version=rec["version"],
                )
                changed += 1
        finally:
            bb.close()
        if changed:
            AuditLogger(session.audit_path).log(
                "system.lifecycle", "session-resume", {
                    "action": "stale_tasks_cancelled", "count": changed})
        return changed

    async def resume(self, session_id: str) -> Session:
        """续传：终态会话从对话末尾接着进行（同一工作区/审计/Flag 库）。

        复用启动计划——进程内持有则直接用；控制台重启过则从 meta.json 的
        原始启动参数确定性重建。这一轮的输入是对话史末尾的用户消息，
        是否开工、进哪个分支由 _run 的语义路由结合上下文裁决；
        CTF 已破题的赛题不再重解，未破题的与用户意见一起进入新一轮决策环。
        """
        from spiritscout.config import load_model_config
        session = self._sessions.get(session_id)
        if session is None:
            raise SpiritScoutError(f"会话不存在: {session_id}")
        if session.status not in TERMINAL_STATES:
            raise SpiritScoutError("会话仍在运行中，意见将直接进入当前决策环")
        if session.plan is None:
            session.plan = self._rebuild_plan(session)
        if session.plan is None:
            raise SpiritScoutError(
                "该会话缺少启动信息（由不支持续传的旧版本创建），无法续传——"
                "请基于现有报告与审计新开任务")
        # 开工前中断的自动模式会在 _rebuild_plan 中重新确定装配模式；
        # 对话/规则会话没有开工记录，展示态模式保持不动——新消息是否
        # 开工、进哪个分支由 _run 的语义路由重新裁决
        if not session.bb_path.exists() \
                and str((session.summary or {}).get("intent") or "") \
                not in ("dialogue", "rules"):
            session.mode = session.plan.authorization.mode.value
            session.target = session.plan.target
        self._cancel_stale_tasks(session)
        models = vault.apply_keys(load_model_config(self._cfg.models_config_path))
        if not models:
            raise SpiritScoutError(
                f"模型注册表为空: {self._cfg.models_config_path}（请在「模型」页检查）")
        session.resume_prev_status = session.status
        # 配置副本与通道先于状态翻转构建——构建失败会话保持原终态，
        # 不会卡出「运行中/等待调度」的僵尸态
        cfg = self._session_cfg(session, session.request.get("budget_tokens"))
        session.approvals = ApprovalBroker(AuditLogger(cfg.audit_log_path))
        session.questions = QuestionBroker(AuditLogger(cfg.audit_log_path))
        session.ring = RingLedger(AuditLogger(cfg.audit_log_path))
        # 暂停门换新（带本轮审计器），暂停态与已扣账的暂停/间隙时长跨续传
        # 保留：用户没手动继续前重开的任务在检查点挂起等待；时长丢失会让
        # 用时展示把此前暂停的时段重新计入
        was_paused = session.pause_gate.paused
        was_excluded = session.pause_gate.excluded_seconds
        session.pause_gate = PauseGate(AuditLogger(cfg.audit_log_path))
        session.pause_gate.restore(was_paused, was_excluded)
        no_llm = bool(session.request.get("no_llm", False))
        session.status = "running"
        session.phase = "queued"
        session.finished_at = None
        session.error = ""
        self._persist(session)
        self.start()
        await self._queue.put((session, cfg, session.plan, no_llm, models, True))
        return session

    async def _run(self, session: Session, cfg: SystemConfig,
                   plan: LaunchPlan, no_llm: bool,
                   models: list[ModelEntry], resume: bool = False) -> None:
        """会话执行体：按模式分发 runtime 入口，落终态与摘要。

        resume=True 为续传运行：同一工作区/审计/Flag 库重开一轮，
        模式不再改判（会话模式已定），失败时恢复原终态（原成果不丢）。
        """
        try:
            async with self._sem:
                # 导入、模式解析和信号量等待都必须在故障隔离范围内；任何一步
                # 出错都落终态，不能留下只有 meta.json 的假 running 会话。
                from spiritscout.runtime import (
                    run_competition_engagement,
                    run_ctf_engagement,
                    run_engagement,
                )

                try:
                    mode = RunMode(session.mode)
                except ValueError:
                    # 对话/规则收尾落下的展示态模式（dialogue/rules）：
                    # 兜底取装配模式，真实模式由下方语义路由按消息重新裁决
                    mode = plan.authorization.mode
                # 开工即有声：先落一条事件让用户立刻看到灵枢动起来了
                # （随后的模型调用期间时间线不再静默）
                audit = AuditLogger(cfg.audit_log_path)
                # 用户首条消息（任务/提问原文）先于开工事件落时间线——会话页
                # 第一颗完整气泡，与后续 /messages 的消息气泡同构。只在首跑
                # 记录：续传与对话消息已由 /messages 端点即时落过，重放会重复
                if not resume and plan.mission:
                    audit.log("user.message", "user", {"content": plan.mission})
                audit.log("system.lifecycle", "runtime", {
                    "action": "session_kickoff", "resume": resume})
                routed = None
                intent = "task"
                # 路由对象消息：续传轮取对话史末尾的用户消息——大厂网页
                # 对话形态，会话从上次结束的地方接着说，新消息就是这一轮
                # 的输入，不再把原任务整跑一遍；无对话史（旧会话/接口直调
                # 续传）回退原任务原文
                prev_intent = str((session.summary or {}).get("intent") or "")
                last_user_text = next(
                    (str(e.get("content") or "") for e in
                     reversed(session.dialogue_history)
                     if e.get("role") == "user"), "")
                resume_turn = bool(resume and last_user_text)
                route_mission = last_user_text if resume_turn else plan.mission
                # 路由带对话上文：指代性输入（「继续」「按你说的办」）
                # 要结合上文才能判准是接着聊还是接着干
                route_history = session.dialogue_history[:-1] \
                    if resume_turn else None
                if not no_llm and models:
                    if route_mission and (
                            (not resume and plan.needs_mode_routing)
                            or resume_turn):
                        # 语义路由兼任开工预检（开工前唯一模型调用，调用成功
                        # 即模型全链可用），不为预检单付一次串行等待
                        self._set_phase(session, "mode_routing", audit)
                        routed, intent = await _llm_route_mode(
                            cfg, models, route_mission, mode, audit,
                            history=route_history)
                    else:
                        self._set_phase(session, "model_preflight", audit)
                        _preflight_models(cfg, models, audit)
                # 任务+规则同来：规则入库后照常开工——把带目标的任务弹回
                # 「等待确认」同样是把用户输入拒之门外
                if intent == "task_with_rules" and route_mission:
                    session.standing_rules = route_mission
                # 意图分流（LLM 裁决，后端只搬运结果）：
                # dialogue/rules 不派工——灵枢回复后收尾，规则持久化，
                # 等用户下发真正的任务（发送消息即带现场续传）
                if intent in ("rules", "dialogue") and route_mission:
                    session.mode = intent      # 展示态：对话/规则，不冒充业务分支
                    if intent == "rules":
                        session.standing_rules = route_mission
                        if isinstance(session.launch_request, dict) \
                                and session.launch_request.get("mission"):
                            session.launch_request["mission"] = ""
                    self._persist(session)
                    self._set_phase(session, "master_reply", audit)
                    reply = await _master_reply(
                        cfg, models, route_mission, audit,
                        kind="rules" if intent == "rules" else "dialogue",
                        history=session.dialogue_history[:-1])
                    audit.log("master.reply", "master", {"text": reply[:4000]})
                    _remember_reply(session, reply)
                    # 收件箱消费语义：本轮已答复的消息移出（对话史已留痕，
                    # 运行中直达通道不会重复吃到旧消息）
                    session.user_inbox[:] = [
                        m for m in session.user_inbox
                        if not (m.get("kind") == "message"
                                and m.get("text") == route_mission)]
                    session.status = "completed"
                    session.phase = "completed"
                    session.finished_at = time.time()
                    session.summary = {
                        "note": ("已记录执行规则，等待题目/目标下发"
                                 if intent == "rules" else
                                 "已回复用户；未识别到可执行任务"),
                        "intent": intent}
                    self._persist(session)
                    return
                # 续传轮来了真任务：以新消息为任务意图重建启动计划（原计划
                # 可能是对话装配或旧任务），既有授权范围并入 extra_scope——
                # 用户追加目标时不缩既有边界；指代性续作（「继续」）提取不
                # 出新目标时，靠并入的既有范围照常开工
                if resume_turn and intent in ("task", "task_with_rules"):
                    old_scope = plan.authorization.scope
                    merged = [*old_scope.allowed_domains,
                              *old_scope.allowed_cidrs,
                              *old_scope.allowed_urls]
                    try:
                        new_plan = self._rebuild_plan(
                            session, mission=route_mission, extra_scope=merged)
                        if new_plan is None:
                            # 旧版本会话无持久化启动参数：退回进程内原始请求
                            base = plan.source_request or LaunchRequest(
                                mission=route_mission)
                            new_plan = prepare_launch(LaunchRequest(
                                **{**base.__dict__, "mission": route_mission,
                                   "extra_scope": [*base.extra_scope, *merged]}))
                    except SpiritScoutError as exc:
                        # 新消息仍缺装配要素：不硬拒——灵枢说明缺口，
                        # 会话留在对话态等用户补齐
                        self._set_phase(session, "master_reply", audit)
                        reply = await _master_reply(
                            cfg, models,
                            f"{route_mission}\n\n（装配提示：{exc}）",
                            audit, kind="dialogue",
                            history=session.dialogue_history[:-1])
                        audit.log("master.reply", "master",
                                  {"text": reply[:4000]})
                        _remember_reply(session, reply)
                        session.status = "completed"
                        session.phase = "completed"
                        session.finished_at = time.time()
                        session.summary = {
                            "note": "任务缺目标/赛题等装配要素，灵枢已说明；"
                                    "补齐后发送即自动开工",
                            "intent": prev_intent}
                        self._persist(session)
                        return
                    plan = new_plan
                    mode = plan.authorization.mode
                    session.mode = mode.value
                    session.target = plan.target
                    cfg.mode = mode
                    if isinstance(session.launch_request, dict):
                        session.launch_request["mission"] = route_mission
                    self._persist(session)
                if routed is not None and routed != mode and plan.source_request:
                    try:
                        plan = prepare_launch(LaunchRequest(**{
                            **plan.source_request.__dict__, "mode": routed}))
                        mode = routed
                        session.mode = routed.value
                        session.target = plan.target
                        cfg.mode = routed
                    except SpiritScoutError as exc:
                        # 改判缺必需输入（如判渗透赛但无赛制 YAML）：
                        # 不静默跟随——留痕并按兜底执行
                        audit.log("decision.change", "mode_router", {
                            "action": "llm_mode_route_rejected",
                            "llm_mode": routed.value, "kept": mode.value,
                            "reason": str(exc)[:300]})
                # CTF/渗透赛无显式平台档案时做一次抽取：任务文本若描述了托管
                # 平台的 API 文档（跑分/CTF 平台），把档案落进本次运行并按平台
                # 列题/开容器/回收/提交；没描述则是零影响的小调用。抽到的
                # 档案回写启动参数（续传/重建计划不再重抽）
                if (mode in (RunMode.CTF, RunMode.COMPETITION)
                        and plan.platform_profile is None
                        and not no_llm and models and plan.mission):
                    self._set_phase(session, "platform_profile", audit)
                    found = await _llm_extract_platform(
                        cfg, models, plan.mission, audit)
                    if found:
                        plan.platform_profile = found
                        if isinstance(session.launch_request, dict):
                            session.launch_request["platform_yaml"] = yaml.dump(
                                found, allow_unicode=True, sort_keys=False)
                        self._persist(session)
                        # 平台宿主并入授权域（平台 API 由装配层直连；纯
                        # 托管题任务在目标尚未下发时也能通过启动门）
                        host = urlparse(str(found.get("base_url") or "")).hostname or ""
                        if host and host not in plan.authorization.scope.allowed_domains:
                            plan.authorization.scope.allowed_domains.append(host)
                # 真任务缺任何授权对象（范围空）：不派工也不硬拒——让会话
                # 以 ScopeViolation 摔死在启动门，续传还会再摔一次。灵枢
                # 向用户说明缺口，补齐后发送即自动开工
                if intent in ("task", "task_with_rules") \
                        and plan.authorization.scope.is_empty():
                    self._set_phase(session, "master_reply", audit)
                    reply = await _master_reply(
                        cfg, models,
                        f"{route_mission}\n\n（装配提示：任务中未识别到任何"
                        "授权目标——域名/网段/URL 均为空，无法开工。请向用户"
                        "说明需要提供什么：SRC 给目标地址，CTF 给赛题链接或"
                        "附件）", audit, kind="dialogue",
                        history=session.dialogue_history[:-1])
                    audit.log("master.reply", "master", {"text": reply[:4000]})
                    _remember_reply(session, reply)
                    session.status = "completed"
                    session.phase = "completed"
                    session.finished_at = time.time()
                    session.summary = {
                        "note": "任务未识别到授权目标，灵枢已向用户说明；"
                                "补齐后发送即自动开工",
                        "intent": prev_intent or "task"}
                    self._persist(session)
                    return
                self._set_phase(session, "runtime_starting", audit)
                agent_models = agentcfg.read_assignments(cfg.agent_models_path)
                self._set_phase(session, "executing", audit)
                # 执行间隙检测（合盖待机/Ctrl+Z/VM 快照）：墙钟心跳缺口，
                # 全平台一致；用时扣账与时间线标记由门内完成
                session.pause_gate.start_gap_watch()
                if mode == RunMode.CTF:
                    result = await run_ctf_engagement(
                        cfg=cfg, models=models,
                        authorization=plan.authorization,
                        challenges=plan.challenges,
                        flag_patterns=plan.flag_patterns,
                        enable_llm_planning=not no_llm,
                        agent_models=agent_models,
                        approval_gate=session.approvals.ask,
                        ring=session.ring,
                        user_inbox=session.user_inbox,
                        question_broker=session.questions,
                        standing_rules=session.standing_rules,
                        pause_gate=session.pause_gate,
                        dialogue_history=session.dialogue_history,
                        platform_profile=plan.platform_profile,
                        resume=resume)
                elif mode == RunMode.COMPETITION:
                    result = await run_competition_engagement(
                        cfg=cfg, models=models,
                        authorization=plan.authorization,
                        rules=plan.rules, challenges=plan.challenges,
                        enable_llm_planning=not no_llm,
                        agent_models=agent_models,
                        approval_gate=session.approvals.ask,
                        ring=session.ring,
                        user_inbox=session.user_inbox,
                        question_broker=session.questions,
                        standing_rules=session.standing_rules,
                        pause_gate=session.pause_gate,
                        dialogue_history=session.dialogue_history,
                        platform_profile=plan.platform_profile)
                else:
                    result = await run_engagement(
                        cfg=cfg, models=models,
                        authorization=plan.authorization,
                        target=plan.target,
                        enable_llm_planning=not no_llm,
                        mission=plan.mission,
                        agent_models=agent_models,
                        approval_gate=session.approvals.ask,
                        ring=session.ring,
                        user_inbox=session.user_inbox,
                        question_broker=session.questions,
                        standing_rules=session.standing_rules,
                        pause_gate=session.pause_gate,
                        dialogue_history=session.dialogue_history)
                session.status = "completed"
                session.phase = "completed"
                session.summary = {
                    "run": result.run_summary,
                    "verify": result.verify_stats,
                    "flags": result.flag_stats,
                    "report": result.report_path,
                }
                # 终场用量以台账为准：收尾的经验提炼与报告生成发生在运行
                # 快照（usage_report）之后，快照口径会漏记这几笔调用
                live = session.model_usage()
                if live and isinstance(result.run_summary.get("model_usage"), dict):
                    result.run_summary["model_usage"] = live
        except asyncio.CancelledError:
            # stop() 主动取消：runtime finally 已关闭 infra；落 stopped 终态后
            # 正常结束（不向上 re-raise——任务目的即被终止）
            session.status = "stopped"
            session.phase = "stopped"
        except Exception as exc:  # noqa: BLE001 —— 会话故障隔离：单会话失败不影响其他会话
            if resume and session.resume_prev_status:
                # 续传未开工/中途失败：恢复原终态，原成果与摘要不丢
                session.status = session.resume_prev_status
                session.phase = session.status
                session.error = f"续传未完成：{type(exc).__name__}: {exc}"[:500]
            else:
                session.status = "failed"
                session.phase = "failed"
                session.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            await session.pause_gate.stop_gap_watch()
            session.finished_at = time.time()
            self._persist(session)

    async def stop(self, session_id: str) -> Session | None:
        """幂等停止：取消执行任务（数据保留）；已终态直接返回。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.approvals.cancel_all()
        session.questions.cancel_all()
        task = session.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if session.status not in TERMINAL_STATES:
            session.status = "stopped"
            session.phase = "stopped"
            session.finished_at = time.time()
            self._persist(session)
        return session

    async def set_paused(self, session_id: str, paused: bool) -> Session | None:
        """暂停/继续切换（幂等）：仅运行中生效并即时持久化。

        暂停是协作式的——灵枢图节点与子 Agent 步骤在各自检查点挂起，
        在途动作跑完才停；直达对话与审批不经此门，照常可用。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.status == "running":
            if paused:
                session.pause_gate.pause()
            else:
                session.pause_gate.resume()
            self._persist(session)
        return session

    def rename(self, session_id: str, title: str) -> Session | None:
        """重命名会话（任意状态可改；空串 = 恢复自动摘要名）。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.title = title.strip()[:80]
        self._persist(session)
        return session

    async def delete(self, session_id: str, *, confirm: bool = False) -> Session | None:
        """删除会话：仅终态可删且需 confirm；连工作区目录一并移除。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.status not in TERMINAL_STATES:
            raise SpiritScoutError(
                f"会话 {session_id} 仍在 {session.status}，请先停止再删除")
        if not confirm:
            raise SpiritScoutError("删除会话需显式 confirm=true（连工作区一并移除）")
        self._sessions.pop(session_id, None)
        shutil.rmtree(session.workspace, ignore_errors=True)
        return session
