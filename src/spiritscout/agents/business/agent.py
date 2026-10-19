"""业务体验 Agent：真实浏览器走访业务流程，观测接口交易，产出带证据的业务上下文。

模型持有工具（playwright 浏览器 / http_probe / record_endpoint）自主走访——
渲染后的页面、网络面板里的 XHR/fetch 交易都算证据；后端只做证据记账
（导航页与登记的接口入黑板）与终局建模（business_analyst 归纳角色/对象/
流程），不写死走访步骤。静态 HTML 解析管线已拆除：对 JS 渲染站点它天然
失明（主页 0 导航 0 表单的教训），真实浏览器是唯一对得起「体验」二字的手。
"""

from __future__ import annotations

import json
import re
from typing import Any

from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.core.audit import tool_preview
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, Asset, BlackboardLayer, Task


class BusinessContextAgent(SubAgent):
    agent_type = AgentType.BUSINESS_CONTEXT
    route_namespace = "business_context"
    health_prune_enabled = False
    _TOOL_RESULT_CAP = 6000
    _TOOL_TIMEOUT = 180

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 每个任务的走访状态：消息历史 / 是否收尾 / 后端记账的导航页
        self._explore: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ 路由

    def route_templates(self, task: Task) -> list[RoutePlan]:
        return [RoutePlan(
            name="experience_normal_flow",
            strategy="以真实用户视角走访业务，产出每条结论都有证据的业务上下文"
                     "（角色/对象/流程/信任边界）；证据不足的如实进 unknowns",
            steps=[{"action": "explore"}],
        )]

    async def execute_step(self, task, route, plan, step) -> StepResult:
        handler = {"explore": self._explore_turn}.get(step["action"])
        if handler is None:
            return StepResult(ok=False, note=f"未知业务体验步骤: {step['action']}")
        return await handler(task, route, plan)

    # ------------------------------------------------------------ 工具面

    def _tool_schemas(self) -> list[dict[str, Any]]:
        """模型可用工具清单：record_endpoint + http_probe + 会话装配的浏览器 MCP。"""
        schemas: list[dict[str, Any]] = [
            {"type": "function", "function": {
                "name": "record_endpoint",
                "description": (
                    "登记一条实际观测到的接口交易（来自浏览器网络面板/页面行为），"
                    "作为业务上下文的证据入黑板。只登记真实发生的请求，不要推测。"),
                "parameters": {"type": "object", "properties": {
                    "method": {"type": "string", "description": "HTTP 方法，如 GET/POST"},
                    "url": {"type": "string", "description": "接口地址"},
                    "params": {"type": "array",
                               "items": {"type": "string"},
                               "description": "参数名列表（不要参数值）"},
                    "status_code": {"type": "integer"},
                    "note": {"type": "string", "description": "该交易的业务语义，一句话"},
                }, "required": ["url"]}}},
            {"type": "function", "function": {
                "name": "http_probe",
                "description": "HTTP 只读探测（结构化解析状态码/标题/响应头/指纹；"
                               "走授权与限速门），适合裸接口与响应头层面的确认",
                "parameters": {"type": "object", "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "params": {"type": "object"},
                    "headers": {"type": "object"},
                }, "required": ["url"]}}},
        ]
        schemas.append({"type":"function","function":{"name":"asset_memory_lookup",
            "description":"读取同一主机的历史资产，旧结果须按本次任务复核，不携带授权",
            "parameters":{"type":"object","properties":{"host":{"type":"string"},"query":{"type":"string"},"offset":{"type":"integer","minimum":0}},"required":["host"]}}})
        for name, meta in sorted((self.ctx.mcp_tools or {}).items()):
            schema = meta.get("schema")
            if not isinstance(schema, dict):
                continue
            schemas.append({"type": "function", "function": {
                "name": name,
                "description": str(schema.get("description") or meta.get("tool") or name),
                "parameters": schema.get("inputSchema") or {"type": "object", "properties": {}},
            }})
        return schemas

    def _explore_brief(self, task: Task) -> str:
        """走访任务书：目标、任务意图与交付契约（怎么走由模型决定）。"""
        payload: dict[str, Any] = {
            "target": task.target,
            "交付要求": (
                "以真实用户视角走访该目标的业务流程。渲染后的页面、网络请求"
                "（XHR/fetch）都是证据；把实际观测到的接口交易用 record_endpoint "
                "登记（方法/URL/参数名/业务语义，不要参数值）。结论只写有证据"
                "支撑的。证据收集完成后直接输出结论文本（不再调用工具），"
                "系统会据此归纳业务上下文。"),
        }
        intent = {"mission": str(task.payload.get("mission", ""))[:600],
                  "node": str(task.payload.get("node_title", ""))[:120]}
        rules = str(task.payload.get("rules", "")).strip()
        if rules:
            intent["rules"] = rules[:600]
        cleaned = {k: v for k, v in intent.items() if v}
        if cleaned:
            payload["task_intent"] = cleaned
        steering = (self.ctx.ring.steering_for(self.agent_type.value)
                    if self.ctx.ring is not None else None)
        if steering:
            payload["灵枢督导"] = {"hint": steering["hint"],
                                  "rationale": steering.get("rationale", "")}
        return json.dumps(payload, ensure_ascii=False)

    # ------------------------------------------------------------ 走访回合

    async def _explore_turn(self, task: Task, route, plan) -> StepResult:
        """一回合模型工具环：模型调工具 → 后端派发回灌；模型不再调工具即收尾建模。

        不设固定回合顶：走访何时结束由模型判断（与解题环同语义），
        终局产出业务上下文。"""
        st = self._explore.setdefault(task.task_id, {
            "messages": [], "concluded": False, "pages": [], "turn": 0})
        if st["concluded"]:
            return StepResult(ok=True, value=0.9, note="走访已完成并建模")
        if not st["messages"]:
            st["messages"] = [
                {"role": "system",
                 "content": self._system_prompt() + "\nROLE: business_explorer"},
                {"role": "user", "content": self._explore_brief(task)},
            ]
        else:
            steering = (self.ctx.ring.steering_for(self.agent_type.value)
                        if self.ctx.ring is not None else None)
            if steering and steering.get("ts") != st.get("steer_seen"):
                st["steer_seen"] = steering.get("ts")
                st["messages"].append({"role": "user", "content": json.dumps(
                    {"灵枢督导": {"hint": steering["hint"],
                                  "rationale": steering.get("rationale", "")}},
                    ensure_ascii=False)})
        from spiritscout.gateway.context import compact_history

        compact_history(st["messages"])
        st["turn"] += 1
        if st["turn"] > 1:
            self.ctx.audit.log("agent.turn", self.agent_id, {"turn": st["turn"], "task_id": task.task_id})
        try:
            resp = await self.ctx.gateway.chat(
                st["messages"], task_type="execution",
                timeout_seconds=self._TOOL_TIMEOUT,
                tools=self._tool_schemas(), tool_choice="auto",
                reasoning_effort=self.task_effort(task))
        except Exception as exc:  # noqa: BLE001 —— 决策不可用如实报给路线
            self.ctx.audit.log("llm.step_fallback", self.agent_id, {
                "role": "business_explorer", "error": str(exc)[:200]})
            return StepResult(ok=False, note=f"走访决策不可用: {exc}")

        # assistant 消息（含工具调用）回灌，保持多轮协议完整
        if resp.tool_calls and str(resp.content or "").strip():
            self.ctx.audit.log("agent.note", self.agent_id, {"text": str(resp.content)[:2000]})
        st["messages"].append({
            "role": "assistant",
            "content": resp.content or "",
            "tool_calls": [{"id": c.get("id") or "", "type": "function",
                            "function": {"name": c.get("name") or "",
                                         "arguments": str(c.get("raw") or "{}")}}
                           for c in resp.tool_calls],
        })
        if not resp.tool_calls:
            st["concluded"] = True
            # 终局：带着走访所得归纳业务上下文（证据都在黑板里）
            ctx_result = await self._build_context(task)
            conclusion = (resp.content or "").strip()[:1200]
            if conclusion:
                ctx_result.note = f"{conclusion}\n{ctx_result.note}"
            return ctx_result

        # 浏览器是有状态会话，逐个派发避免页面竞态
        for call in resp.tool_calls:
            text = await self._run_tool_call(task, call)
            st["messages"].append({
                "role": "tool",
                "tool_call_id": call.get("id") or "call_0",
                "content": text[: self._TOOL_RESULT_CAP],
            })
        plan.steps.append({"action": "explore"})   # 续回合：终局由模型给出
        return StepResult(ok=True, value=min(0.1 * st["turn"], 0.6),
                          note=f"走访第 {st['turn']} 回合：{len(resp.tool_calls)} 次工具调用")

    async def _run_tool_call(self, task: Task, call: dict[str, Any]) -> str:
        """派发一次模型工具调用：record_endpoint 记账，其余一律过工具门。"""
        name = str(call.get("name") or "")
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        self.ctx.audit.log("agent.tool_call", self.agent_id, {
            "calls": [{"id": call.get("id") or "", "name": name, "arguments": args,
                       "preview": tool_preview(args)}]})
        result_meta: dict[str, Any] = {"tool": name}
        try:
            if call.get("parse_error"):
                ok, text = False, f"工具参数解析失败: {call['parse_error']}。请修正 JSON 后重新调用。"
            elif name == "record_endpoint":
                ok, text = await self._tool_record_endpoint(task, args)
            else:
                res = await self.ctx.tools.call(name, args, **self.ctx.builtin_ctx)
                ok = res.ok
                text = (f"工具执行失败: {res.failure_reason()}"
                        if not res.ok else (res.stdout or "（无输出）"))
                result_meta.update(invocation_id=res.invocation_id, returncode=res.returncode,
                                   attempts=res.attempts)
                result_meta.update({key: res.parsed[key] for key in ("failure_kind", "failure_summary")
                                    if key in res.parsed and not res.ok})
                if res.ok and name.endswith("browser_navigate"):
                    self._track_page(task, str((args or {}).get("url") or ""))
        except Exception as exc:  # noqa: BLE001 —— 工具异常转结果回灌自纠
            ok, text = False, f"工具调用异常: {type(exc).__name__}: {exc}"
        self.ctx.audit.log("agent.tool_result", self.agent_id, {
            "id": call.get("id") or "", "ok": ok, "note": text[:300], **result_meta})
        return text

    def _track_page(self, task: Task, url: str) -> None:
        """导航页记账：URL 脱敏后作为页面证据入黑板（dedup 由黑板处理）。"""
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return
        st = self._explore.setdefault(task.task_id, {
            "messages": [], "concluded": False, "pages": [], "turn": 0})
        clean = url
        if any(p["url"] == clean for p in st["pages"]):
            return
        st["pages"].append({"url": clean})
        self.ctx.convergence.merge_asset(Asset(
            asset_id=new_id("asset"), asset_type="app", value=clean,
            attrs={"kind": "business_page", "source": "model_navigated"},
            source_agent=self.agent_id), self.agent_type)

    async def _tool_record_endpoint(self, task: Task, args: dict[str, Any]) -> tuple[bool, str]:
        """模型登记的接口交易：URL 原文入黑板，作为端点证据。"""
        url = str((args or {}).get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return False, "登记失败：url 必须是 http(s) 地址"
        status = args.get("status_code")
        endpoint = Asset(
            asset_id=new_id("asset"), asset_type="endpoint",
            value=url,
            attrs={
                "method": str(args.get("method") or "GET").upper()[:10],
                "params": [str(p)[:128] for p in (args.get("params") or [])][:32],
                "status_code": int(status) if isinstance(status, (int, float)) else None,
                "note": str(args.get("note") or "")[:300],
                "source": "model_observed",
            },
            source_agent=self.agent_id,
        )
        self.ctx.convergence.merge_asset(endpoint, self.agent_type)
        return True, "已登记"

    # ------------------------------------------------------------ 上下文建模

    async def _build_context(self, task: Task) -> StepResult:
        # 页面证据从黑板回读：导航记账在走访时已入册，任务续传/重启后
        # 内存走访态丢失也不丢证据
        pages: list[dict[str, Any]] = [
            {"url": rec["data"].get("value", ""), "title": "", "record_id": rec["record_id"]}
            for rec in self.ctx.blackboard.query(BlackboardLayer.ASSET, limit=500)
            if rec["data"].get("asset_type") == "app"
            and (rec["data"].get("attrs") or {}).get("kind") == "business_page"
        ]
        endpoint_records = [
            {**record["data"], "_record_id": record["record_id"]} for record in self.ctx.blackboard.query(
                BlackboardLayer.ASSET, limit=1000)
            if record["data"].get("asset_type") == "endpoint"
        ]
        endpoint_records.sort(key=lambda item: (
            int((item.get("attrs") or {}).get("first_seen_index", 1_000_000)),
            str(item.get("value", "")),
        ))
        evidence: list[dict[str, Any]] = []
        for page in pages:
            evidence.append({
                "id": f"E{len(evidence) + 1}", "kind": "page",
                "record_id": page["record_id"],
                "url": page["url"], "title": page.get("title", ""),
            })
        recorded = 0
        for endpoint in endpoint_records[:100]:
            attrs = endpoint.get("attrs") or {}
            source_file = attrs.get("source_file", "")
            source = attrs.get("source") or source_file
            if source == "model_observed":
                recorded += 1
            evidence.append({
                "id": f"E{len(evidence) + 1}",
                "record_id": endpoint["_record_id"],
                "kind": ("har_transaction" if source_file
                         else "observed_transaction" if source == "model_observed"
                         else "observed_form"),
                "method": attrs.get("method", "GET"),
                "url": endpoint.get("value", ""),
                "params": attrs.get("params", []),
                "status_code": attrs.get("status_code"),
                "note": attrs.get("note", ""),
                "sequence": (attrs.get("first_seen_index") if source_file else None),
                "source": source,
            })
        valid_ids = {item["id"] for item in evidence}
        model_context = await self._llm_chat_json("business_analyst", {
            "evidence": evidence,
            "constraints": (
                "返回 JSON：{roles:[{name,evidence_refs}], objects:[{name,evidence_refs}], "
                "workflows:[{name,steps,evidence_refs}], trust_boundaries:[{description,"
                "evidence_refs}], facts:[{statement,evidence_refs}], hypotheses:[{statement,"
                "evidence_refs}], unknowns:[str]}。每条结论必须引用 evidence 中的 id；"
                "证据不能支持时放到 unknowns，不得猜账号角色、权限或隐藏接口。"
            ),
        }, max_user_chars=12000, reasoning_effort=self.task_effort(task), task=task)
        context = self._validated_context(model_context or {}, valid_ids)
        har_count = sum(
            1 for endpoint in endpoint_records
            if (endpoint.get("attrs") or {}).get("source_file")
        )
        coverage = (
            "observed_transaction_flow" if har_count + recorded >= 2
            else "public_flow" if pages or recorded or har_count
            else "entry_only" if pages
            else "none"
        )
        gaps: list[str] = list(context.get("unknowns", []))
        if har_count + recorded == 0:
            gaps.append("未观测到登录后或多角色交易的接口记录，相关流程尚未被走访")
        if not pages:
            gaps.append("入口页面不可达或未走访")
        context.update({
            "kind": "business_context",
            "coverage": coverage,
            "ready_for_testing": bool(endpoint_records),
            "observed_pages": len(pages),
            "observed_endpoints": len(endpoint_records),
            "recorded_transactions": recorded,
            "har_transactions": har_count,
            "gaps": list(dict.fromkeys(str(g)[:300] for g in gaps if g)),
            "evidence": evidence,
            "target": task.target,
        })
        self.ctx.blackboard.write(
            BlackboardLayer.TOPOLOGY,
            "business-context",
            context,
            agent_type=self.agent_type,
            updated_by=self.agent_id,
            dedup_key="business|context",
        )
        return StepResult(
            ok=True,
            value=0.9 if context["ready_for_testing"] else 0.3,
            findings=[{"type": "business_context", "coverage": coverage}],
            note=(f"业务上下文：{coverage}，页面 {len(pages)}，"
                  f"端点 {len(endpoint_records)}（含登记交易 {recorded}、"
                  f"请求记录 {har_count}）；缺口 {len(context['gaps'])} 项"),
            final_note=True,
        )

    @staticmethod
    def _validated_context(raw: dict[str, Any], valid_ids: set[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ("roles", "objects", "workflows", "trust_boundaries", "facts", "hypotheses"):
            clean: list[dict[str, Any]] = []
            for item in raw.get(key) or []:
                if not isinstance(item, dict):
                    continue
                refs = [ref for ref in item.get("evidence_refs", []) if ref in valid_ids]
                if not refs:
                    continue
                sanitized = {k: v for k, v in item.items() if k != "evidence_refs"}
                sanitized["evidence_refs"] = refs
                clean.append(sanitized)
            out[key] = clean[:30]
        out["unknowns"] = [
            re.sub(r"\s+", " ", str(item)).strip()[:300]
            for item in (raw.get("unknowns") or [])
            if str(item).strip()
        ][:30]
        return out
