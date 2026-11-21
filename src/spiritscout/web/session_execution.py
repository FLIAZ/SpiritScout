"""单轮 Web 会话执行：路由、续传装配与模式分发，不拥有监督任务或终态清理。"""

from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import yaml

from spiritscout.config.settings import ModelEntry, SystemConfig
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import SpiritScoutError
from spiritscout.core.models import RunMode
from spiritscout.launch import (
    LaunchPlan,
    LaunchRequest,
    declared_mode,
    extract_targets,
    prepare_launch,
)
from spiritscout.web import agentcfg
from spiritscout.web.session_assistant import (
    _llm_discover_platform,
    _llm_extract_platform,
    _llm_route_intent,
    _llm_route_mode,
    _master_reply,
    _preflight_models,
    _routed_competition_rules_yaml,
    _session_run_facts,
)
from spiritscout.web.session_models import (
    DIALOGUE_HISTORY_MAX,
    Session,
    _remember_reply,
)

if TYPE_CHECKING:
    from spiritscout.web.sessions import SessionManager


class SessionExecution:
    """只持有本轮上下文；持久状态和任务生命周期仍归 SessionManager。"""

    def __init__(
        self,
        manager: SessionManager,
        session: Session,
        cfg: SystemConfig,
        plan: LaunchPlan,
        no_llm: bool,
        models: list[ModelEntry],
        resume: bool,
    ):
        self.manager = manager
        self.session = session
        self.cfg = cfg
        self.plan = plan
        self.no_llm = no_llm
        self.models = models
        self.resume = resume

    async def run(self) -> None:
        from spiritscout.runtime import (
            run_competition_engagement,
            run_ctf_engagement,
            run_engagement,
        )

        self.run_competition_engagement = run_competition_engagement
        self.run_ctf_engagement = run_ctf_engagement
        self.run_engagement = run_engagement
        try:
            self.mode = RunMode(self.session.mode)
        except ValueError:
            # 对话/规则收尾落下的展示态模式（dialogue/rules）：
            # 兜底取装配模式，真实模式由下方语义路由按消息重新裁决
            self.mode = self.plan.authorization.mode
        # 开工即有声：先落一条事件让用户立刻看到灵枢动起来了
        # （随后的模型调用期间时间线不再静默）
        self.audit = AuditLogger(self.session.audit_path)
        # 同项目历史记忆：只作参考、不构成本次授权；范围为空时用来
        # 向用户确认要测试的目标（见下方 SRC 分支）。
        self.project_memory = self.manager._project_memory(self.session)
        # 用户首条消息（任务/提问原文）先于开工事件落时间线——会话页
        # 第一颗完整气泡，与后续 /messages 的消息气泡同构。只在首跑
        # 记录：续传与对话消息已由 /messages 端点即时落过，重放会重复
        if not self.resume and self.plan.mission:
            initial_id = next(
                (
                    entry.get("statement_id")
                    for entry in self.session.dialogue_history
                    if entry.get("role") == "user"
                ),
                "",
            )
            self.audit.log(
                "user.message",
                "user",
                {"content": self.plan.mission, "message_id": initial_id},
            )
        self.audit.log(
            "system.lifecycle",
            "runtime",
            {"action": "session_kickoff", "resume": self.resume},
        )
        if self.session.request.get("side_agent"):
            await self._run_side_agent()
            return
        if await self._route_intent():
            return
        if await self._reply_without_execution():
            return
        if await self._rebuild_followup():
            return
        self._apply_routed_mode()
        await self._discover_platform()
        if await self._confirm_scope():
            return
        await self._execute()

    async def _run_side_agent(self) -> None:
        from spiritscout.runtime import run_agent_side_session
        from spiritscout.web.conversation import message_text

        self.manager._set_phase(self.session, "executing", self.audit)

        def remember_reply(text: str) -> None:
            self.session.dialogue_history.append({"role": "assistant", "content": text})
            del self.session.dialogue_history[:-DIALOGUE_HISTORY_MAX]
            self.manager._persist(self.session)

        mission = (
            message_text(self.session.dialogue_history[-1])
            if self.resume and self.session.dialogue_history
            else self.plan.mission
        )
        while True:
            # 本轮历史已包含这些用户消息；执行中新增的消息留待下一轮回应。
            feed = list(self.session.user_inbox)
            result = await run_agent_side_session(
                cfg=self.cfg,
                models=self.models,
                authorization=self.plan.authorization,
                agent_key=self.session.request["side_agent"],
                mission=mission,
                context=self.session.request.get("side_context") or {},
                agent_models=agentcfg.read_assignments(self.cfg.agent_models_path),
                approval_gate=self.session.approvals.ask,
                question_broker=self.session.questions,
                ring=self.session.ring,
                pause_gate=self.session.pause_gate,
                dialogue_history=copy.deepcopy(self.session.dialogue_history),
                standing_rules=self.session.standing_rules,
                on_reply=remember_reply,
            )
            self.session.summary = {
                "run": result.run_summary,
                "flags": result.flag_stats,
            }
            self.session.acknowledge_user_feed(feed)
            self.manager._persist(self.session)
            if not self.session.user_inbox:
                break
            mission = "\n\n".join(
                message_text(item) for item in self.session.user_inbox
            )
        self.session.status = self.session.phase = "completed"
        return

    async def _route_intent(self) -> bool:
        self.routed = None
        self.intent = "task"
        self.routed_contest = ""
        # 路由对象消息：续传轮取对话史末尾的用户消息——大厂网页
        # 对话形态，会话从上次结束的地方接着说，新消息就是这一轮
        # 的输入，不再把原任务整跑一遍；无对话史（旧会话/接口直调
        # 续传）回退原任务原文
        self.prev_intent = str((self.session.summary or {}).get("intent") or "")
        last_user_text = next(
            (
                str(e.get("content") or "")
                for e in reversed(self.session.dialogue_history)
                if e.get("role") == "user"
            ),
            "",
        )
        self.resume_turn = bool(self.resume and last_user_text)
        self.route_mission = last_user_text if self.resume_turn else self.plan.mission
        # 路由带对话上文：指代性输入（「继续」「按你说的办」）
        # 要结合上文才能判准是接着聊还是接着干
        self.route_history = (
            self.session.dialogue_history[:-1] if self.resume_turn else None
        )
        from spiritscout.web.conversation import message_text

        self.turn_feed = list(self.session.user_inbox) if self.resume else []
        self.execution_request: dict[str, Any] = {}
        self.continuation_handled = False
        if self.turn_feed:
            self.route_mission = "\n\n".join(
                message_text(item) for item in self.turn_feed
            )
            self.resume_turn = True
            ids = {
                item.get("statement_id")
                for item in self.turn_feed
                if item.get("statement_id")
            }
            self.route_history = [
                item
                for item in self.session.dialogue_history
                if item.get("statement_id") not in ids
            ]
        self.scope_mission = (
            "\n\n".join(str(item.get("text") or "") for item in self.turn_feed)
            if self.turn_feed
            else self.route_mission
        )
        self.auto_mode = bool(
            self.plan.mode_auto or (self.session.launch_request or {}).get("mode_auto")
        )
        if await self._continue_existing():
            return True
        if not self.auto_mode:
            self.audit.log(
                "decision.change",
                "runtime",
                {
                    "action": "user_selected_mode",
                    "mode": self.mode.value,
                    "contest_type": str(
                        (self.session.launch_request or {}).get("contest_type") or ""
                    ),
                },
            )
        if not self.no_llm and self.models and not self.continuation_handled:
            if self.resume_turn and not self.auto_mode:
                self.manager._set_phase(self.session, "intent_routing", self.audit)
                self.intent = await _llm_route_intent(
                    self.cfg,
                    self.models,
                    self.route_mission,
                    self.audit,
                    history=self.route_history,
                )
            elif self.route_mission and (
                (not self.resume and self.plan.needs_mode_routing) or self.resume_turn
            ):
                # 语义路由兼任开工预检（开工前唯一模型调用，调用成功
                # 即模型全链可用），不为预检单付一次串行等待
                self.manager._set_phase(self.session, "mode_routing", self.audit)
                source = self.plan.source_request
                route_declared = declared_mode(
                    LaunchRequest(
                        mission=self.route_mission,
                        rules_yaml=(source.rules_yaml if source else None),
                        challenges_yaml=(source.challenges_yaml if source else None),
                    )
                )
                project_context = (
                    "\n".join(
                        str(self.project_memory.get(key) or "").strip()
                        for key in ("name", "description")
                    ).strip()
                    if self.project_memory
                    else ""
                )
                self.routed, self.intent, self.routed_contest = await _llm_route_mode(
                    self.cfg,
                    self.models,
                    self.route_mission,
                    self.mode,
                    self.audit,
                    history=self.route_history,
                    declared=route_declared,
                    project_context=project_context,
                )
            else:
                self.manager._set_phase(self.session, "model_preflight", self.audit)
                _preflight_models(self.cfg, self.models, self.audit)
        # 任务+规则同来：规则入库后照常开工——把带目标的任务弹回
        # 「等待确认」同样是把用户输入拒之门外
        if self.intent == "task_with_rules" and self.route_mission:
            self.session.standing_rules = self.route_mission
        # 意图分流（LLM 裁决，后端只搬运结果）：
        # dialogue/rules 不派工——灵枢回复后收尾，规则持久化，
        # 等用户下发真正的任务（发送消息即带现场续传）
        return False

    async def _continue_existing(self) -> bool:
        if (
            self.turn_feed
            and not self.no_llm
            and self.models
        ):
            lightweight_resume = self.cfg.gateway.lightweight_local and self.mode in (
                RunMode.CTF,
                RunMode.AWDP,
            )
            if lightweight_resume:
                self.manager._set_phase(self.session, "intent_routing", self.audit)
                self.intent = await _llm_route_intent(
                    self.cfg,
                    self.models,
                    self.route_mission,
                    self.audit,
                    history=self.route_history,
                )
                if self.intent == "rules":
                    self.session.standing_rules = self.route_mission
            self.manager._set_phase(self.session, "master_reply", self.audit)
            reply = await _master_reply(
                self.cfg,
                self.models,
                self.route_mission,
                self.audit,
                kind="dialogue",
                history=self.route_history,
                project_memory=self.project_memory,
                run_facts=_session_run_facts(self.session),
                session=self.session,
                execution_request=self.execution_request,
                user_feed=self.turn_feed,
                execution_only=lightweight_resume
                and self.intent in ("task", "task_with_rules"),
            )
            self.scope_mission = "\n\n".join(str(item.get("text") or "") for item in self.turn_feed)
            self.audit.log("master.reply", "master", {"text": reply})
            _remember_reply(self.session, reply)
            self.continuation_handled = True
            if not self.execution_request:
                self.session.acknowledge_user_feed(self.turn_feed)
                self.manager._persist(self.session)
                self.session.status = self.session.phase = "completed"
                self.session.summary = {
                    **self.session.summary,
                    "intent": self.intent if lightweight_resume else "dialogue",
                }
                return True
            self.route_mission = str(self.execution_request["mission"])
        return False

    async def _reply_without_execution(self) -> bool:
        if self.intent in ("rules", "dialogue") and self.route_mission:
            self.session.mode = self.intent  # 展示态：对话/规则，不冒充业务分支
            if self.intent == "rules":
                self.session.standing_rules = self.route_mission
                if isinstance(
                    self.session.launch_request, dict
                ) and self.session.launch_request.get("mission"):
                    self.session.launch_request["mission"] = ""
            self.manager._persist(self.session)
            self.manager._set_phase(self.session, "master_reply", self.audit)
            reply = await _master_reply(
                self.cfg,
                self.models,
                self.route_mission,
                self.audit,
                kind="rules" if self.intent == "rules" else "dialogue",
                history=self.session.dialogue_history[:-1],
                project_memory=self.project_memory,
                run_facts=_session_run_facts(self.session),
            )
            self.audit.log("master.reply", "master", {"text": reply[:4000]})
            _remember_reply(self.session, reply)
            # 收件箱消费语义：本轮已答复的消息移出（对话史已留痕，
            # 运行中直达通道不会重复吃到旧消息）
            reply_items = self.turn_feed or [
                m
                for m in self.session.user_inbox
                if m.get("text") == self.route_mission
            ]
            self.session.acknowledge_user_feed(reply_items)
            self.session.status = "completed"
            self.session.phase = "completed"
            self.session.finished_at = time.time()
            # 保留已有执行摘要（run/flags/report）——终态后续聊不能
            # 把已完成的执行记录冲掉，会话卡片与后续续聊都依赖它
            prior = (
                self.session.summary if isinstance(self.session.summary, dict) else {}
            )
            self.session.summary = {
                **prior,
                "note": (
                    "已记录执行规则，等待题目/目标下发"
                    if self.intent == "rules"
                    else "已回复用户；未识别到可执行任务"
                ),
                "intent": self.intent,
            }
            self.manager._persist(self.session)
            return True
        # 续传轮来了真任务：以新消息为任务意图重建启动计划（原计划
        # 可能是对话装配或旧任务），既有授权范围并入 extra_scope——
        # 用户追加目标时不缩既有边界；指代性续作（「继续」）提取不
        # 出新目标时，靠并入的既有范围照常开工
        return False

    async def _rebuild_followup(self) -> bool:
        if self.resume_turn and self.intent in ("task", "task_with_rules"):
            old_scope = self.plan.authorization.scope
            merged = [
                *old_scope.allowed_domains,
                *old_scope.allowed_cidrs,
                *old_scope.allowed_urls,
            ]
            try:
                new_plan = self.manager._rebuild_plan(
                    self.session,
                    mission=self.scope_mission
                    if self.continuation_handled
                    else self.route_mission,
                    extra_scope=merged,
                    preserve_challenges=self.continuation_handled,
                )
                if new_plan is None:
                    # 旧版本会话无持久化启动参数：退回进程内原始请求
                    base = self.plan.source_request or LaunchRequest(
                        mode=None if self.auto_mode else self.mode,
                        target=self.plan.target if self.mode == RunMode.SRC else None,
                        challenges_yaml=(
                            yaml.safe_dump(self.plan.challenges)
                            if self.plan.challenges
                            else None
                        ),
                        contest_type=str(
                            (self.session.launch_request or {}).get("contest_type")
                            or "adaptive"
                        ),
                        mission=self.route_mission,
                    )
                    new_plan = prepare_launch(
                        LaunchRequest(
                            **{
                                **base.__dict__,
                                "mission": self.scope_mission
                                if self.continuation_handled
                                else self.route_mission,
                                "extra_scope": [*base.extra_scope, *merged],
                            }
                        )
                    )
            except SpiritScoutError as exc:
                # 新消息仍缺装配要素：不硬拒——灵枢说明缺口，
                # 会话留在对话态等用户补齐
                self.manager._set_phase(self.session, "master_reply", self.audit)
                reply = await _master_reply(
                    self.cfg,
                    self.models,
                    f"{self.route_mission}\n\n（装配提示：{exc}）",
                    self.audit,
                    kind="dialogue",
                    history=self.session.dialogue_history[:-1],
                    project_memory=self.project_memory,
                    run_facts=_session_run_facts(self.session),
                )
                self.audit.log("master.reply", "master", {"text": reply[:4000]})
                _remember_reply(self.session, reply)
                self.session.status = "completed"
                self.session.phase = "completed"
                self.session.finished_at = time.time()
                prior = (
                    self.session.summary
                    if isinstance(self.session.summary, dict)
                    else {}
                )
                self.session.summary = {
                    **prior,
                    "note": "任务缺目标/赛题等装配要素，灵枢已说明；"
                    "补齐后发送即自动开工",
                    "intent": self.prev_intent,
                }
                self.manager._persist(self.session)
                return True
            self.plan = new_plan
            if self.continuation_handled:
                self.plan.mission = self.route_mission
            # 直接装配的兜底路径不经 _rebuild_plan：附件回挂补齐
            self.manager._remount_attachments(self.session, self.plan)
            self.mode = self.plan.authorization.mode
            self.session.mode = self.mode.value
            self.session.target = self.plan.target
            self.cfg.mode = self.mode
            self.session.plan = self.plan
            self.manager._persist(self.session)
        return False

    def _apply_routed_mode(self) -> None:
        if (
            self.routed is not None
            and self.routed != self.mode
            and self.plan.source_request
        ):
            try:
                overrides = {**self.plan.source_request.__dict__, "mode": self.routed}
                if self.routed == RunMode.COMPETITION and not overrides.get(
                    "rules_yaml"
                ):
                    # 模式已由模型从任务语义判为竞赛；赛制类型也是
                    # 同一次模型裁决，平台能力事实继续由平台抽取补齐。
                    overrides["rules_yaml"] = _routed_competition_rules_yaml(
                        self.routed_contest, self.route_mission
                    )
                self.plan = prepare_launch(LaunchRequest(**overrides))
                self.manager._remount_attachments(self.session, self.plan)
                self.mode = self.routed
                self.session.mode = self.routed.value
                self.session.target = self.plan.target
                self.cfg.mode = self.routed
                if isinstance(self.session.launch_request, dict):
                    self.session.launch_request["mode"] = self.routed.value
                    self.session.launch_request["rules_yaml"] = (
                        overrides.get("rules_yaml") or ""
                    )
                    self.manager._persist(self.session)
            except SpiritScoutError as exc:
                # 改判缺必需输入（如判渗透赛但无赛制 YAML）：
                # 不静默跟随——留痕并按兜底执行
                self.audit.log(
                    "decision.change",
                    "mode_router",
                    {
                        "action": "llm_mode_route_rejected",
                        "llm_mode": self.routed.value,
                        "kept": self.mode.value,
                        "contest_type": self.routed_contest,
                        "reason": str(exc)[:300],
                    },
                )
        # CTF/渗透赛无显式平台档案时先从任务原文抽取。智能体模式若
        # 原文未给 API 文档但给了赛事入口，再做有界同源只读发现，只有
        # 列题与提交接口都能引用实际证据时才接入；发现结果回写启动参数，
        # 续传/重建计划不再重复探测。

    async def _discover_platform(self) -> None:
        if (
            self.mode in (RunMode.CTF, RunMode.COMPETITION)
            and self.plan.platform_profile is None
            and self.cfg.gateway.automatic_platform_extraction
            and not self.no_llm
            and self.models
            and self.plan.mission
        ):
            self.manager._set_phase(self.session, "platform_profile", self.audit)
            found = await _llm_extract_platform(
                self.cfg,
                self.models,
                self.scope_mission if self.continuation_handled else self.plan.mission,
                self.audit,
            )
            if found and self.mode == RunMode.COMPETITION:
                from spiritscout.ctf.platform_discovery import (
                    declared_profile_is_supported,
                )

                source_mission = (
                    self.scope_mission
                    if self.continuation_handled
                    else self.plan.mission
                )
                if not declared_profile_is_supported(found, source_mission):
                    self.audit.log(
                        "decision.change",
                        "platform_discovery",
                        {
                            "action": "platform_text_extraction_rejected",
                            "rationale": "任务原文未同时提供可复核的列题与提交契约",
                        },
                    )
                    found = None
            if not found and self.mode == RunMode.COMPETITION:
                entry_urls, _ = extract_targets(
                    self.scope_mission
                    if self.continuation_handled
                    else self.plan.mission
                )
                if self.plan.target.startswith(("http://", "https://")):
                    entry_urls = [self.plan.target, *entry_urls]
                found = await _llm_discover_platform(
                    self.cfg,
                    self.models,
                    self.scope_mission
                    if self.continuation_handled
                    else self.plan.mission,
                    list(dict.fromkeys(entry_urls)),
                    self.audit,
                )
            if found:
                self.plan.platform_profile = found
                if isinstance(self.session.launch_request, dict):
                    self.session.launch_request["platform_yaml"] = yaml.dump(
                        found, allow_unicode=True, sort_keys=False
                    )
                self.manager._persist(self.session)
                # 平台宿主并入授权域（平台 API 由装配层直连；纯
                # 托管题任务在目标尚未下发时也能通过启动门）
                host = urlparse(str(found.get("base_url") or "")).hostname or ""
                if host and host not in self.plan.authorization.scope.allowed_domains:
                    self.plan.authorization.scope.allowed_domains.append(host)
        # 项目说明和历史资产只是候选事实，不能替代本次会话授权。
        # SRC 网络范围为空时在开工前即时确认；用户确认的目标通过
        # LaunchRequest.extra_scope 重建计划，执行层与主控看到同一范围。

    async def _confirm_scope(self) -> bool:
        if (
            self.mode == RunMode.SRC
            and self.intent in ("task", "task_with_rules")
            and not self.manager._has_network_scope(self.plan)
            and not self.manager._is_local_attachment_task(
                self.session, self.route_mission
            )
        ):
            asked, added, answer = await self.manager._confirm_project_scope(
                self.session,
                self.cfg,
                self.models,
                self.route_mission,
                self.plan,
                self.audit,
                self.project_memory,
                no_llm=self.no_llm,
            )
            if asked and added:
                rebuilt = self.manager._rebuild_plan(
                    self.session,
                    mission=self.route_mission,
                    extra_scope=added,
                    preserve_challenges=self.continuation_handled,
                )
                if rebuilt is None:
                    raise SpiritScoutError("用户已确认目标，但启动计划重建失败")
                self.plan = rebuilt
                self.mode = self.plan.authorization.mode
                self.session.mode = self.mode.value
                self.session.target = self.plan.target
                self.cfg.mode = self.mode
                self.manager._remount_attachments(self.session, self.plan)
                self.manager._persist(self.session)
            elif asked:
                self.session.status = "completed"
                self.session.phase = "completed"
                self.session.finished_at = time.time()
                self.session.summary = {
                    **(
                        self.session.summary
                        if isinstance(self.session.summary, dict)
                        else {}
                    ),
                    "note": "项目候选资产未获本次授权，未启动网络测试",
                    "intent": self.intent,
                    "scope_answer": answer[:300],
                }
                self.manager._persist(self.session)
                return True
        # SRC 无网络目标或附件也进入规划，由模型判断是否需要澄清；
        # 项目记忆仍仅作参考，不自动并入本次网络授权。
        if (
            self.mode != RunMode.SRC
            and self.intent in ("task", "task_with_rules")
            and self.plan.authorization.scope.is_empty()
        ):
            self.manager._set_phase(self.session, "master_reply", self.audit)
            reply = await _master_reply(
                self.cfg,
                self.models,
                f"{self.route_mission}\n\n（装配提示：任务中未识别到授权目标，"
                "请根据任务说明缺少哪些赛题链接、附件或平台信息）",
                self.audit,
                kind="dialogue",
                history=self.session.dialogue_history[:-1],
                project_memory=self.project_memory,
                run_facts=_session_run_facts(self.session),
            )
            self.audit.log("master.reply", "master", {"text": reply[:4000]})
            _remember_reply(self.session, reply)
            self.session.status = "completed"
            self.session.phase = "completed"
            self.session.finished_at = time.time()
            prior = (
                self.session.summary if isinstance(self.session.summary, dict) else {}
            )
            self.session.summary = {
                **prior,
                "note": "任务未识别到授权目标，灵枢已向用户说明；补齐后发送即自动开工",
                "intent": self.prev_intent or "task",
            }
            self.manager._persist(self.session)
            return True
        return False

    async def _execute(self) -> None:
        self.session.plan = self.plan
        self.session.execution_plan = self.manager._snapshot_plan(self.plan)
        self.manager._set_phase(self.session, "runtime_starting", self.audit)
        agent_models = agentcfg.read_assignments(self.cfg.agent_models_path)
        self.manager._set_phase(self.session, "executing", self.audit)
        # 执行间隙检测（合盖待机/Ctrl+Z/VM 快照）：墙钟心跳缺口，
        # 全平台一致；用时扣账与时间线标记由门内完成
        self.session.pause_gate.start_gap_watch()

        def on_user_feed(items: list[dict[str, Any]]) -> None:
            self.session.acknowledge_user_feed(items)
            del self.session.dialogue_history[:-DIALOGUE_HISTORY_MAX]
            self.manager._persist(self.session)

        if self.mode in (RunMode.CTF, RunMode.AWDP):
            result = await self.run_ctf_engagement(
                cfg=self.cfg,
                models=self.models,
                authorization=self.plan.authorization,
                challenges=self.plan.challenges,
                flag_patterns=self.plan.flag_patterns,
                enable_llm_planning=not self.no_llm,
                agent_models=agent_models,
                approval_gate=self.session.approvals.ask,
                ring=self.session.ring,
                user_inbox=self.session.user_inbox,
                question_broker=self.session.questions,
                standing_rules=self.session.standing_rules,
                pause_gate=self.session.pause_gate,
                dialogue_history=self.session.dialogue_history,
                on_user_feed=on_user_feed,
                platform_profile=self.plan.platform_profile,
                resume=self.resume,
                mission=self.plan.mission,
                followup_goal=self.continuation_handled,
                reopen_challenges=self.execution_request.get("reopen_challenges"),
            )
        elif self.mode == RunMode.COMPETITION:
            result = await self.run_competition_engagement(
                cfg=self.cfg,
                models=self.models,
                authorization=self.plan.authorization,
                rules=self.plan.rules,
                challenges=self.plan.challenges,
                generate_wp=self.plan.generate_wp,
                enable_llm_planning=not self.no_llm,
                agent_models=agent_models,
                approval_gate=self.session.approvals.ask,
                ring=self.session.ring,
                user_inbox=self.session.user_inbox,
                question_broker=self.session.questions,
                standing_rules=self.session.standing_rules,
                pause_gate=self.session.pause_gate,
                dialogue_history=self.session.dialogue_history,
                on_user_feed=on_user_feed,
                platform_profile=self.plan.platform_profile,
                mission=self.plan.mission,
                followup_goal=self.continuation_handled,
            )
        else:
            result = await self.run_engagement(
                cfg=self.cfg,
                models=self.models,
                authorization=self.plan.authorization,
                target=self.plan.target,
                enable_llm_planning=not self.no_llm,
                mission=self.plan.mission,
                agent_models=agent_models,
                approval_gate=self.session.approvals.ask,
                ring=self.session.ring,
                user_inbox=self.session.user_inbox,
                question_broker=self.session.questions,
                standing_rules=self.session.standing_rules,
                pause_gate=self.session.pause_gate,
                dialogue_history=self.session.dialogue_history,
                on_user_feed=on_user_feed,
                followup_goal=self.continuation_handled,
                asset_strategy=str(
                    (self.session.launch_request or {}).get("asset_strategy")
                    or "balanced"
                ),
            )
        if self.continuation_handled:
            self.session.acknowledge_user_feed(self.turn_feed)
        self.session.status = (
            "interrupted"
            if result.run_summary.get("answers_complete") is False
            else "completed"
        )
        self.session.phase = self.session.status
        self.session.summary = {
            "run": result.run_summary,
            "verify": result.verify_stats,
            "flags": result.flag_stats,
            "report": result.report_path,
        }
        # 终场用量以台账为准：收尾的经验提炼与报告生成发生在运行
        # 快照（usage_report）之后，快照口径会漏记这几笔调用
        live = self.session.model_usage()
        if live and isinstance(result.run_summary.get("model_usage"), dict):
            result.run_summary["model_usage"] = live
