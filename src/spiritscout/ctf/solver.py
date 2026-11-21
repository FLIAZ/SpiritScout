"""单个 CTF 解题回合的编排；Agent 保留工具、状态与专项覆写钩子。"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING, Any

from spiritscout.agents.sub.base import RoutePlan, StepResult
from spiritscout.blackboard.exchange import NAMES
from spiritscout.core.models import RunMode, Task
from spiritscout.ctf.conversation import _model_messages
from spiritscout.gateway.context import append_instruction_events, estimate_input_tokens
from spiritscout.gateway.progress import PublicProgressEmitter as _DeltaEmitter
from spiritscout.resources.runtime import SKILL_NAMES

if TYPE_CHECKING:
    from spiritscout.ctf.agents import CTFBaseAgent


_MSG_CHAR_BUDGET = 48000

_TOOL_RESULT_CAP = 6000

_SOLVER_TIMEOUT = 420

_TURN_PROGRESS = 0.04


class SolverTurn:
    """临时回合执行器，不复制会话状态，也不替代 Agent 的能力钩子。"""

    def __init__(self, agent: CTFBaseAgent, task: Task, route: Any, plan: RoutePlan):
        self.agent = agent
        self.task = task
        self.route = route
        self.plan = plan
        self.state: dict[str, Any] = {}
        self.boot_value = 0.0
        self.boot_note = ""

    async def run(self) -> StepResult:
        agent, task, route = self.agent, self.task, self.route
        state = agent._inv_state(task, route)
        self.state = state
        if state["concluded"] or state["stall"]:
            return StepResult(
                ok=True,
                value=0.0,
                final_note=True,
                note=state["stall"] or "本路线已收尾",
            )
        if (
            getattr(agent.flag_manager, "local_only", False)
            and not task.payload.get("followup_goal")
            and agent.challenge_id(task) in agent.flag_manager.completed_ids()
        ):
            receipt = "本题全部答案已确认，解题报告在后台生成。"
            agent._finish_accepted(task, route, state, receipt)
            return StepResult(ok=True, final_note=True, note=receipt)
        # 竞赛同题并行路线共享平台判定：整题完成后不再调用模型生成题解。
        if (
            getattr(agent.ctx, "mode", None) == RunMode.COMPETITION
            and agent.flag_manager is not None
        ):
            for flag in agent.flag_manager.accepted_flags():
                if flag.challenge_id == agent.challenge_id(
                    task
                ) and agent._submission_complete(task, flag.submit_response):
                    receipt = agent._accepted_receipt(task, flag.submit_response)
                    agent._finish_accepted(task, route, state, receipt)
                    return StepResult(ok=True, value=0.0, final_note=True, note=receipt)
        # 并行同伴夺旗 → 事实通报（不再由后端下令收尾）：本轮并行同伴的
        # Flag 获平台判定正确，每枚只通报一次；继续深挖（多 Flag 题/未竟面）
        # 还是收尾，由模型依现场决定。候选/未判定不算战果；判错的进提示词
        # 当禁提交清单。
        win_notice = agent._peer_win_notice(task, route, state)

        prepared = await self._prepare(win_notice)
        if prepared is not None:
            return prepared
        return await self._advance()

    async def _prepare(self, win_notice: str) -> StepResult | None:
        agent, task, route, plan = self.agent, self.task, self.route, self.plan
        broker = getattr(agent.ctx, "questions", None)
        if broker is not None:
            await broker.wait_for_task(task.task_id)
        state = self.state
        boot_value, boot_note = 0.0, ""
        if not state["messages"]:
            try:
                boot_value, boot_note = await agent._inv_bootstrap(task, route, state)
            except Exception as exc:  # noqa: BLE001 —— 首轮感知失败不终结调查：
                # 把障碍原样交给模型（见过现场才知道怎么绕：换入口/换工具/先读题面）
                state["seed"].append(
                    f"首轮感知失败: {type(exc).__name__}: {str(exc)[:200]}"
                    "——请换可行的入口或工具继续调查"
                )
                boot_note = f"首轮感知失败: {str(exc)[:120]}"
            if state["stall"]:
                summary = str(state["stall"])
                agent._record_conclusion(task, route, summary)
                agent.ctx.audit.log(
                    "agent.blocked",
                    agent.agent_id,
                    {
                        "route": route.name,
                        "challenge": agent.challenge_id(task),
                        "reason": summary,
                    },
                )
                agent._save_inv_state(task, route, state)
                return StepResult(
                    ok=True,
                    value=boot_value,
                    final_note=True,
                    note=agent._merge_note(boot_note, summary),
                )
            agent._seed_messages(task, route, plan, state)
            if win_notice:
                state["messages"].append({"role": "user", "content": win_notice})
        else:
            if win_notice:
                state["messages"].append({"role": "user", "content": win_notice})
            if update := agent._peer_updates(task, state):
                # 同伴动态作为新消息注入：并行 Agent 的现场即时互通
                state["messages"].append({"role": "user", "content": update})

        environment = agent._task_execution_environment(task)
        if environment and state.get("execution_environment") != environment:
            frames = [m.get("_light_task_frame") or {} for m in state["messages"] if m.get("_light_task_frame")]
            if not any(frame.get("execution_environment") == environment for frame in frames):
                # 普通云模型与旧检查点也必须在生成第一条命令前知道本题可写路径。
                # 只追加变化，不改历史前缀、不替用户重写或重放命令。
                state["messages"].append({"role": "user", "_light_kind": "execution_environment",
                    "_context_contract": True,
                    "content": "当前题执行环境（覆盖旧环境路径）：" + json.dumps(environment, ensure_ascii=False)})
            state["execution_environment"] = environment

        from spiritscout.tools.background import take_notifications

        notifications = take_notifications(
            getattr(agent.ctx, "builtin_ctx", {}).get("workspace_root", ""),
            agent.agent_id,
            agent._tool_ctx(task).get("workspace_scope", ""),
        )
        if notifications:
            state["messages"].append(
                {
                    "role": "user",
                    "_light_kind": "observation",
                    "content": "后台任务通知（工具观测，不是新的用户指令）："
                    + json.dumps(notifications, ensure_ascii=False),
                }
            )
        fragment, changed = agent._sync_world_state(state)
        if fragment:
            state["messages"].append(
                {
                    "role": "user",
                    "content": fragment,
                    **({"_light_kind": "world"} if agent._is_lightweight() else {}),
                }
            )
            agent.ctx.audit.log(
                "world_state.update",
                agent.agent_id,
                {"route": route.name, "keys": changed},
            )
        runtime = agent._skill_runtime()
        if runtime is not None and not agent._is_lightweight():
            catalog = runtime.discovery()
            if catalog != state.get("skill_catalog"):
                state["skill_catalog"] = catalog
                state["messages"].append(
                    {
                        "role": "user",
                        "_context_contract": True,
                        "content": "可用技能目录（仅元数据，选择由你决定）：\n"
                        + json.dumps(catalog, ensure_ascii=False),
                    }
                )
        self.boot_value, self.boot_note = boot_value, boot_note
        return None

    async def _advance(self) -> StepResult:
        agent, task, route, plan = self.agent, self.task, self.route, self.plan
        broker = getattr(agent.ctx, "questions", None)
        if broker is not None:
            await broker.wait_for_task(task.task_id)
        pause_gate = getattr(agent.ctx, "pause_gate", None)
        if pause_gate is not None:
            await pause_gate.wait()
        state = self.state
        boot_value, boot_note = self.boot_value, self.boot_note
        state["turn"] += 1
        turn = state["turn"]
        include_initial_system = not bool(state.get("system_initialized"))
        agent._ensure_progress_guidance(state)
        records = (
            agent.ctx.ring.steerings_for(agent.agent_type.value, task_id=task.task_id)
            if agent.ctx.ring is not None
            else []
        )
        before_instructions = len(state["messages"])
        append_instruction_events(
            state, records, consumer_key=f"{task.task_id}|{route.route_id}"
        )
        if agent._is_lightweight() and len(state["messages"]) > before_instructions:
            state["messages"][-1]["_light_kind"] = "steering"
        agent._merge_mcp_schemas()
        budget_for = getattr(agent.ctx.gateway, "context_budget", None)
        budget = budget_for(task_type="execution") if callable(budget_for) else None
        if budget is None and agent._is_lightweight():
            # 未声明容量的第三方端点保留旧整理方式，不猜测其真实窗口。
            size = len(json.dumps(state["messages"], ensure_ascii=False))
            if size > state.get("compact_at", _MSG_CHAR_BUDGET):
                agent._compact_messages(
                    state["messages"], initialized=not include_initial_system
                )
                size = len(json.dumps(state["messages"], ensure_ascii=False))
                state["compact_at"] = size + max(16000, size // 2)
                agent.ctx.audit.log(
                    "context.compacted",
                    agent.agent_id,
                    {
                        "task_id": task.task_id,
                        "route_id": route.route_id,
                        "reason": "unknown_window_legacy",
                        "chars": size,
                    },
                )
        messages: list[dict[str, Any]] = _model_messages(
            state["messages"], initialized=not include_initial_system
        )
        directives = agent._active_steering(task, route)
        emitter = _DeltaEmitter(
            agent.ctx.audit,
            agent.agent_id,
            route=route.name,
            challenge=agent.challenge_id(task),
            turn=turn,
            inline_discovery=not agent._is_lightweight(),
        )
        agent.ctx.audit.log(
            "agent.turn",
            agent.agent_id,
            {
                "task_id": task.task_id,
                "route_id": route.route_id,
                "route": route.name,
                "challenge": agent.challenge_id(task),
                "turn": turn,
                "initial_system_prompt": include_initial_system,
                "angle": plan.strategy[:160] if turn == 1 else "",
            },
        )
        from spiritscout.gateway.light_context import ContextWindowError

        try:
            messages, reminder = self._project_messages(messages, budget, directives)
            resp = await agent.ctx.gateway.chat(
                messages,
                task_type="execution",
                # 输出上限由网关按模型配置处理，避免截断思考或工具参数。
                timeout_seconds=_SOLVER_TIMEOUT,
                tools=agent._model_tool_schemas(),
                tool_choice="auto",
                preserve_tool_order=bool(
                    state.get("shared_prefix", False)
                    or (
                        agent._is_lightweight()
                        and getattr(agent.flag_manager, "fix_mode", False)
                    )
                ),
                preserve_context=True,
                on_delta=emitter
                if not agent._is_lightweight() or getattr(agent, "public_body", False)
                else (
                    lambda kind, text: emitter(kind, text) if kind != "text" else None
                )
                if getattr(agent.flag_manager, "local_only", False)
                else emitter,
                reasoning_effort=agent.task_effort(task),
            )
        except ContextWindowError as exc:
            state["stall"] = str(exc)
            state["context_blocked"] = True
            agent.ctx.audit.log(
                "context.blocked",
                agent.agent_id,
                {
                    "task_id": task.task_id,
                    "route_id": route.route_id,
                    "error": str(exc),
                },
            )
            agent._save_inv_state(task, route, state)
            return StepResult(
                ok=False, final_note=True, note="上下文无法容纳当前契约：" + str(exc)
            )
        except Exception as exc:  # noqa: BLE001 —— 单次调用失败允许下一回合重试
            emitter.flush()
            state["llm_failures"] += 1
            agent.ctx.audit.log(
                "agent.error",
                agent.agent_id,
                {
                    "route": route.name,
                    "challenge": agent.challenge_id(task),
                    "turn": turn,
                    "error": str(exc)[:300],
                },
            )
            if state["llm_failures"] >= 2:
                state["stall"] = (
                    f"LLM 连续不可用，调查停摆（无模板兜底）：{str(exc)[:120]}"
                )
            note = agent._extend_or_finish(
                plan, state, f"第 {turn} 回合：LLM 调用失败（{str(exc)[:80]}）"
            )
            agent._save_inv_state(task, route, state)
            return StepResult(
                ok=True, value=boot_value, note=agent._merge_note(boot_note, note)
            )
        emitter.flush()
        calls, lightweight = self._record_response(resp, emitter, directives, reminder)
        if pause_gate is not None:
            await pause_gate.wait()
        resolved = await self._resolve_calls(resp, calls, lightweight)
        if isinstance(resolved, StepResult):
            return resolved
        return await self._execute_calls(resolved, lightweight)

    def _project_messages(
        self,
        messages: list[dict[str, Any]],
        budget: int | None,
        directives: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        from spiritscout.gateway.light_context import ContextWindowError

        agent, task, route = self.agent, self.task, self.route
        state = self.state
        reminder = None
        from spiritscout.gateway.manual_context import (
            archive_boundary,
            pending_request,
            record_result,
        )

        compact_request = pending_request(
            agent.ctx.blackboard, state.get("manual_compaction_id", "")
        )
        before_compaction = None
        if compact_request and agent._is_lightweight():
            try:
                before_compaction = estimate_input_tokens(
                    agent._light_model_messages(task, route, copy.deepcopy(state)),
                    agent._model_tool_schemas(),
                )
            except ContextWindowError:
                before_compaction = estimate_input_tokens(
                    state["messages"], agent._model_tool_schemas()
                )
            # 留住最近四轮助手回复及其完整工具结果，旧请求不会继续归档新消息。
            state["manual_archive_before"] = archive_boundary(state["messages"])
        if agent._is_lightweight():
            messages = agent._light_model_messages(task, route, state)
        else:
            from spiritscout.agents.profiles import EVIDENCE_FIRST_GUIDANCE
            from spiritscout.gateway.normal_context import project
            from spiritscout.gateway.progress import (
                EXPLORATION_PROGRESS_GUIDANCE,
                native_discovery_reminder,
            )

            # 硬规则每轮随提醒重述：长系统提示里远处的约束在连续工具循环中
            # 容易被忽略，登录/302/扫描场景必须每轮可见。
            reminder = {
                "role": "user",
                "content": EVIDENCE_FIRST_GUIDANCE
                + "\n"
                + native_discovery_reminder(state),
            }
            if directives:
                reminder["content"] += (
                    "\n当前未完成任务指令（用户最新要求与授权优先，已完成内容不重做）："
                    + json.dumps(directives, ensure_ascii=False)
                )
            has_contract = any(
                m.get("role") == "user"
                and EXPLORATION_PROGRESS_GUIDANCE in str(m.get("content") or "")
                for m in state["messages"]
            )
            sent_contract = not has_contract
            if sent_contract:
                # 仅旧检查点或投影恰好裁掉首条契约时补一次完整契约。
                reminder["content"] = (
                    EXPLORATION_PROGRESS_GUIDANCE + "\n" + reminder["content"]
                )
            reserve = estimate_input_tokens([reminder])
            view = project(
                state,
                agent._model_tool_schemas(),
                max(1, budget - reserve) if budget is not None else None,
                force=compact_request is not None,
            )
            before_compaction = view["previous_tokens"] + reserve
            messages = [*view["messages"], reminder]
            if sent_contract:
                reminder["_context_contract"] = True
            if view["new_archived_turns"]:
                agent.ctx.audit.log(
                    "context.projected",
                    agent.agent_id,
                    {
                        "task_id": task.task_id,
                        "route_id": route.route_id,
                        "mode": "normal",
                        **{k: v for k, v in view.items() if k != "messages"},
                    },
                )
        if compact_request:
            state["manual_compaction_id"] = compact_request["request_id"]
            agent._save_inv_state(task, route, state)
            record_result(
                agent.ctx.blackboard,
                compact_request,
                f"execution:{task.task_id}:{route.route_id}",
                before_compaction,
                estimate_input_tokens(messages, agent._model_tool_schemas()),
                audit=agent.ctx.audit,
                actor=agent.agent_id,
            )
        return messages, reminder

    def _record_response(
        self,
        resp: Any,
        emitter: _DeltaEmitter,
        directives: list[dict[str, Any]],
        reminder: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        agent, task, route = self.agent, self.task, self.route
        state = self.state
        if not agent._is_lightweight():
            state["messages"].append({**reminder, "_progress_reminder": True})
        state["llm_failures"] = 0
        state["system_initialized"] = True
        agent._model_response_received(task, route, state)
        if isinstance(getattr(resp, "model", None), str) and resp.model:
            models = state.setdefault("response_models", [])
            if resp.model not in models:
                models.append(resp.model)
        for directive in directives:
            agent.ctx.ring.steering_feedback(
                agent.agent_type.value,
                task_id=task.task_id,
                route_id=route.route_id,
                instruction_id=directive["id"],
                status="delivered",
            )

        from spiritscout.gateway.progress import publish_progress

        public_body = not agent._is_lightweight() or getattr(
            agent, "public_body", False
        )
        progress = None
        if public_body:
            # 流式正文服务实时展示；完整事件仍须持久化，供探索图和刷新后回放。
            progress = publish_progress(
                agent.ctx.audit,
                agent.agent_id,
                resp.content or "",
                task_id=task.task_id,
                route_id=route.route_id,
                turn=state.get("turn", 0),
                streamed=emitter.has_public_text,
            )
        elif resp.tool_calls:
            progress = publish_progress(
                agent.ctx.audit,
                agent.agent_id,
                resp.content or "",
                task_id=task.task_id,
                route_id=route.route_id,
                turn=state.get("turn", 0),
            )
        elif not getattr(agent.flag_manager, "local_only", False):
            progress = publish_progress(
                agent.ctx.audit,
                agent.agent_id,
                resp.content or "",
                task_id=task.task_id,
                route_id=route.route_id,
                turn=state.get("turn", 0),
            )
        agent._record_public_progress(task, route, state, progress)
        calls = [c for c in (resp.tool_calls or []) if isinstance(c, dict)]
        lightweight = getattr(
            getattr(getattr(agent.ctx, "gateway", None), "config", None),
            "lightweight_local",
            False,
        ) and getattr(agent.ctx, "mode", None) in (RunMode.CTF, RunMode.AWDP)
        if calls:
            state["prose_only_turns"] = 0
            if lightweight:
                state["empty_replies"] = 0
        assistant: dict[str, Any] = {"role": "assistant", "content": resp.content or ""}
        if getattr(resp, "reasoning", "") and not lightweight:
            # 轻量 × CTF 不持久化思考：压缩器只折 content 不折 reasoning，
            # 思考随历史堆积即不可压缩的预算死重（48 次空转压缩实测）
            assistant["reasoning_content"] = resp.reasoning
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": c.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": str(c.get("name") or ""),
                        "arguments": c.get("raw")
                        or json.dumps(c.get("arguments") or {}, ensure_ascii=False),
                    },
                }
                for i, c in enumerate(calls)
            ]
        state["messages"].append(assistant)

        return calls, lightweight

    async def _recover_prose(
        self, resp: Any, calls: list[dict[str, Any]], lightweight: bool
    ) -> StepResult | list[dict[str, Any]]:
        agent, task, route, plan = self.agent, self.task, self.route, self.plan
        state = self.state
        boot_value, boot_note = self.boot_value, self.boot_note
        turn = state["turn"]
        if (
            not calls
            and getattr(agent.flag_manager, "local_only", False)
            and (lightweight or not task.payload.get("followup_goal"))
            and str(resp.content or "").strip()
        ):
            if lightweight:
                # Ollama 小模型专属补偿：叙述回合用同一工具目录重试，
                # 让模型自主选择真实动作；恢复失败只记录状态，
                # 不能把尚未执行的口述计划当作调查结论。
                recovered = await agent._forced_tool_recovery(task, route, state, turn)
                if recovered:
                    calls = recovered
                    state["prose_only_turns"] = 0
                    state["empty_replies"] = 0
                else:
                    prose_only = int(state.get("prose_only_turns") or 0) + 1
                    state["prose_only_turns"] = prose_only
                    if prose_only == 1:
                        instruction = agent._completion_instruction()
                        state["messages"].append(
                            {
                                "role": "user",
                                "_light_kind": "recovery",
                                "content": (
                                    "上一回合只输出了文字，恢复调用也没有携带工具调用。"
                                    "需要继续调查时自主选择工具并实际调用；已完成目标时按完成契约登记；"
                                    "无法完成时用 finish_challenge(complete=false, reason=具体缺口)。"
                                    f"本模式不在会话重复题解。{instruction}"
                                ),
                            }
                        )
                        note = agent._extend_or_finish(
                            plan,
                            state,
                            f"第 {turn} 回合：模型纯文本且强制恢复未果，已指明三选一",
                        )
                        agent._save_inv_state(task, route, state)
                        return StepResult(
                            ok=True,
                            value=boot_value,
                            note=agent._merge_note(boot_note, note),
                        )
            else:
                instruction = agent._completion_instruction()
                state["messages"].append(
                    {
                        "role": "user",
                        "content": "本轮文字已记录。进展说明和接力资料可供后续继续调查，"
                        "交付文档本身不表示答案已完成；依据现场证据决定下一步。"
                        + instruction
                        + "无法完成时可用 finish_challenge(complete=false) 记录具体缺口。",
                    }
                )
                note = agent._extend_or_finish(
                    plan, state, f"第 {turn} 回合：文字已记录，等待明确的后续动作或完成登记"
                )
                agent._save_inv_state(task, route, state)
                return StepResult(
                    ok=True, value=boot_value, note=agent._merge_note(boot_note, note)
                )
        return calls

    async def _resolve_calls(
        self, resp: Any, calls: list[dict[str, Any]], lightweight: bool
    ) -> StepResult | list[dict[str, Any]]:
        agent, task, route, plan = self.agent, self.task, self.route, self.plan
        state = self.state
        boot_value, boot_note = self.boot_value, self.boot_note
        turn = state["turn"]
        resolved = await self._recover_prose(resp, calls, lightweight)
        if isinstance(resolved, StepResult):
            return resolved
        calls = resolved
        if not calls:
            # 兼容普通模式的正文结论；轻量模式须在下方检查显式完成动作。
            summary = (resp.content or "").strip()
            if not summary:
                # 协议修复：正文为空且无工具调用，多见于思考型模型把输出
                # 额度耗在思考段——空回合不是结论。轻量模式先强制恢复成
                # 工具调用；恢复不了保持待执行，不能按空回复次数判定收尾
                if lightweight:
                    recovered = await agent._forced_tool_recovery(
                        task, route, state, turn
                    )
                    if recovered:
                        calls = recovered
                        state["empty_replies"] = 0
                        state["prose_only_turns"] = 0
                if not calls:
                    state["empty_replies"] = int(state.get("empty_replies") or 0) + 1
                    if state["empty_replies"] < 2:
                        empty_nudge = (
                            "上一回合没有正文或工具调用。需要继续调查时自主选择工具并实际调用；"
                            + agent._completion_instruction()
                            + "无法继续时用 finish_challenge(complete=false) 说明缺口。"
                            if lightweight
                            else "（上一回合没有收到你的输出：既没有工具调用，也没有正文。）"
                            "请继续调查；若你已判定调查结束，请写出明确结论——"
                            "拿到了什么、依据是什么、还差什么。"
                        )
                        state["messages"].append(
                            {"role": "user", "content": empty_nudge}
                        )
                        note = agent._extend_or_finish(
                            plan, state, f"第 {turn} 回合：模型空输出，已请其重述"
                        )
                        agent._save_inv_state(task, route, state)
                        return StepResult(
                            ok=True,
                            value=boot_value,
                            note=agent._merge_note(boot_note, note),
                        )
            else:
                state["empty_replies"] = 0
        if not calls and lightweight:
            # 结束调查须由模型显式调用完成契约；普通正文、空回复与恢复失败都不是完成信号。
            state["messages"].append(
                {
                    "role": "user",
                    "_light_kind": "recovery",
                    "content": (
                        "本回合仍未收到工具调用，口述计划尚未执行，任务保持进行中。"
                        "需要继续调查时实际调用工具；"
                        + agent._completion_instruction()
                        + "无法继续时调用 finish_challenge(complete=false, reason=具体缺口)，不能只输出计划代替动作。"
                    ),
                }
            )
            agent.ctx.audit.log(
                "solver.awaiting_tool_action",
                agent.agent_id,
                {
                    "task_id": task.task_id,
                    "route_id": route.route_id,
                    "turn": turn,
                    "empty_reply": not bool(str(resp.content or "").strip()),
                },
            )
            note = agent._extend_or_finish(
                plan, state, f"第 {turn} 回合：未收到工具动作，等待模型明确继续或结束"
            )
            agent._save_inv_state(task, route, state)
            return StepResult(
                ok=True, value=boot_value, note=agent._merge_note(boot_note, note)
            )
        if not calls:
            summary = (resp.content or "").strip()
            state["conclusion_text"] = summary[:2000]
            state["concluded"] = True
            if not state["conclusion_text"].strip():
                state["conclusion_text"] = "（模型未输出结论文本，最后动作为工具调用）"
            agent._record_conclusion(task, route, summary or "模型未给出结论文本")
            agent.ctx.audit.log(
                "agent.conclude",
                agent.agent_id,
                {
                    "route": route.name,
                    "challenge": agent.challenge_id(task),
                    "turn": turn,
                    "summary": summary[:2000],
                },
            )
            # 结论已随上面的 agent.conclude 单独上时间线：置位让任务级
            # 收尾（base._run_task）不再把同一段文本拼进汇总结论重复展示
            route.conclusion_published = True
            # 结论文本（最终答案与依据，常含 Markdown 表格）走长限额，
            # WriteUp 的「解题依据」段不再半途截断
            note = f"收尾（第 {turn} 回合）：{summary[:1500] or '无结论文本'}"
            agent._save_inv_state(task, route, state)
            return StepResult(
                ok=True,
                value=boot_value,
                final_note=True,
                note=agent._merge_note(boot_note, note),
            )

        return calls

    async def _execute_calls(
        self, calls: list[dict[str, Any]], lightweight: bool
    ) -> StepResult:
        agent, task, route, plan = self.agent, self.task, self.route, self.plan
        state = self.state
        boot_value, boot_note = self.boot_value, self.boot_note
        turn = state["turn"]
        # 模型可能在暂停后才返回工具提案。等放行后再发布执行开始。
        pause_gate = getattr(agent.ctx, "pause_gate", None)
        if pause_gate is not None:
            await pause_gate.wait()
        agent.ctx.audit.log(
            "agent.tool_call",
            agent.agent_id,
            {
                "task_id": task.task_id,
                "route_id": route.route_id,
                "route": route.name,
                "challenge": agent.challenge_id(task),
                "turn": turn,
                "calls": [
                    {
                        "id": c.get("id"),
                        "name": c.get("name"),
                        "arguments": c.get("arguments"),
                    }
                    for c in calls
                ],
            },
        )
        outcomes = await asyncio.gather(
            *(agent._run_tool_call(task, route, state, call) for call in calls)
        )
        total_value = 0.0
        notes: list[str] = []
        findings: list[dict[str, Any]] = []
        empty_streak = int(state.get("empty_streak") or 0)
        for idx, (call, out) in enumerate(zip(calls, outcomes)):
            text = out.text or "（无输出）"
            result_name = out.metadata.get("invoked_tool") or call.get("name")
            if idx == 0 and out.ok and empty_streak >= 3:
                # 感知标注（非规则）：连续空输出是「方法可能不成立」的事实信号，
                # 是否换路由模型自行判断——后端不截断不强制
                text += (
                    f"\n（感知：本路线已连续 {empty_streak} 次工具调用"
                    "输出为空——若当前方法可能不成立，把已排除的假设用 "
                    "note 记下后换条路；是否调整由你判断）"
                )
            state["messages"].append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "call_0",
                    # 参数契约按需查询后必须完整可读，不能把 schema JSON 截成半段。
                    "content": text
                    if (
                        result_name in SKILL_NAMES | NAMES
                        or result_name
                        in {
                            "mcp_search",
                            "http_response_read",
                            "tool_lookup",
                            "tool_help",
                            "history_read",
                            "ai_inspect",
                        }
                        or not lightweight
                        or result_name
                        in {
                            "shell",
                            "python",
                            "source_manifest",
                            "verify_patch",
                            "edit_source",
                            "build_patch",
                            "source_context",
                            "source_scan",
                        }
                    )
                    else text[:_TOOL_RESULT_CAP],
                    **(
                        {
                            "_context_contract": True,
                            "_skill_ref": out.metadata["skill_ref"],
                        }
                        if out.metadata.get("skill_ref")
                        else {"_context_contract": True}
                        if result_name == "mcp_search"
                        else {}
                    ),
                }
            )
            agent._observe_tool_result(state, call, out)
            total_value += out.value
            findings.extend(out.findings)
            if out.note:
                notes.append(out.note)
            agent.ctx.audit.log(
                "agent.tool_result",
                agent.agent_id,
                {
                    "task_id": task.task_id,
                    "route_id": route.route_id,
                    "route": route.name,
                    "challenge": agent.challenge_id(task),
                    "turn": turn,
                    "id": call.get("id"),
                    "name": call.get("name"),
                    "ok": out.ok,
                    "note": out.note[:200],
                    "result": (out.text or "")[:1200],
                    "result_truncated": len(out.text or "") > 1200,
                    **out.metadata,
                },
            )
        state["empty_streak"] = (
            (empty_streak + 1)
            if outcomes and all(o.ok and o.empty for o in outcomes)
            else 0
        )
        head = f"第 {turn} 回合"
        note = f"{head}：" + "；".join(notes) if notes else head
        terminal = next((o for o in outcomes if o.stop and o.final_text), None)
        if terminal is not None:
            summary = terminal.final_text[:600]
            agent._finish_accepted(task, route, state, summary)
            return StepResult(
                ok=True,
                value=min(boot_value + total_value, 1.0),
                progress_delta=_TURN_PROGRESS,
                findings=findings,
                final_note=True,
                note=agent._merge_note(boot_note, summary),
            )
        note = agent._extend_or_finish(plan, state, note)
        agent._save_inv_state(task, route, state)
        return StepResult(
            ok=True,
            value=min(boot_value + total_value, 1.0),
            progress_delta=_TURN_PROGRESS,
            findings=findings,
            note=agent._merge_note(boot_note, note[:300]),
        )
