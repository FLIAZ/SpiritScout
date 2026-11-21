"""Web 漏洞探测：通过基线和变异请求收集候选证据。

模型引用必须能在实际响应中核对；候选写入黑板后交由独立复测角色审核。"""

from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.blackboard.hypotheses import resolve as resolve_hypothesis
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    RunMode,
    Severity,
    Task,
    Vulnerability,
    VulnStatus,
)
from spiritscout.gateway.context import shared_context
from spiritscout.traffic import recorder as traffic_recorder

# 当前步的防护拦截信号（按步清零；ContextVar 保证并发路线互不污染）
_STEP_SIGNALS: ContextVar[int] = ContextVar("webvuln_step_signals", default=0)

# SQL 报错特征（初筛信号，覆盖主流 DB 指纹）
SQL_ERROR_PATTERNS = [
    r"SQL syntax.*MySQL", r"Warning.*mysql_", r"MySqlException",
    r"ORA-\d{5}", r"Oracle error", r"PostgreSQL.*ERROR", r"pg_query\(\)",
    r"SQLite3::", r"sqlite3.OperationalError", r"SQLSTATE\[\w+\]",
    r"Microsoft SQL Server", r"ODBC SQL Server Driver", r"Unclosed quotation mark",
    r"Traceback \(most recent call last\)",  # 调试回溯亦属信息泄露信号
]
SQLI_ERROR_PAYLOADS = ["'", "\"", "')", "1' ORDER BY 1-- -"]
SQLI_TRUE_PAYLOAD = "' AND '1'='1"
SQLI_FALSE_PAYLOAD = "' AND '1'='2"
XSS_CANARY = "spirxss"
XSS_PAYLOADS = [
    f"{XSS_CANARY}<svg/onload=1>",
    f'"{XSS_CANARY}><script>1</script>',
    f"'{XSS_CANARY}<img src=x>",
]
# SSRF 参数名启发 + 自校验 canary 路径（仅授权目标内，G4）
SSRF_PARAM_HINTS = {"url", "uri", "link", "callback", "fetch", "target", "dest",
                    "redirect", "path", "src", "source", "feed", "proxy"}
SSRF_CANARY_PATH = "/.well-known/spir-ssrf-canary"
SSRF_CANARY_TOKEN = "SPIR-SSRF-CANARY"
# 布尔差异判定阈值（长度差比例； 可配置化项）
DIFF_LEN_RATIO = 0.05
# LLM 步骤决策边界：单参数 payload 数上限（限请求扇出）与单条长度上限
LLM_PAYLOAD_CAP = 16
LLM_PAYLOAD_MAXLEN = 500

# WAF 拦截响应特征（命中即计 1 个检测信号；9.2 流量特征暴露风险）
WAF_BLOCK_MARKERS = (
    "access denied", "request blocked", "web application firewall",
    "waf", "cloudflare", "安全狗", "云盾", "拦截", " forbidden ",
)


class WebVulnAgent(SubAgent):
    agent_type = AgentType.WEB_VULN
    route_namespace = "webvuln"

    # ------------------------------------------------------------ 路线模板

    def route_templates(self, task: Task) -> list[RoutePlan]:
        if getattr(getattr(self.ctx, "policy", None), "mode", None) == RunMode.SRC:
            return [RoutePlan(
                name="contract_hypothesis_test",
                strategy="根据任务与现场自主搜集、提出假设并验证，保留真实请求与可追溯证据",
                steps=[{"action": "hypothesis_investigate"}],
            )]
        endpoints = self._collect_endpoints(task)
        business = self._business_context()
        routes: list[RoutePlan] = []
        contract = task.payload.get("investigation_contract")
        contract = contract if isinstance(contract, dict) else {}
        resolved = resolve_hypothesis(
            self.ctx.blackboard, str(contract.get("hypothesis_ref") or ""))
        hypothesis = resolved[1] if resolved else {}
        test_type = str(hypothesis.get("test_type") or "").strip().lower()
        subject = str(hypothesis.get("subject") or "").strip()
        if subject:
            focused = [(endpoint, params) for endpoint, params in endpoints
                       if endpoint == subject or endpoint.rstrip("/") == subject.rstrip("/")]
            endpoints = focused or endpoints
        fixtures = self._boundary_fixtures(subject) if test_type in {
            "idor", "authorization", "business_logic", "access_control"} else []
        if not resolved:
            routes.append(RoutePlan(
                name="hypothesis_required",
                strategy="攻击任务必须引用当前会话可证伪漏洞假设，不执行全接口例行扫描",
                steps=[{"action": "context_gap", "reason": "missing_hypothesis"}],
            ))
        elif (str(contract.get("next_minimal_experiment") or "").strip()
              and test_type not in {"sqli", "xss", "ssrf"} and not fixtures):
            routes.append(RoutePlan(
                name="contract_hypothesis_test",
                strategy="按已引用假设执行具体最小对照；缺合法基线、身份或授权时报告阻塞，不退化为页面走访",
                steps=[{"action": "hypothesis_investigate"}],
            ))
        elif test_type in {"idor", "authorization", "business_logic", "access_control"}:
            routes.append(RoutePlan(
                name="business_boundary_hypothesis",
                strategy="权限假设只比较同一资源的双身份真实只读流量，不重放认证请求或枚举对象",
                steps=([{"action": "boundary_compare", "fixture": fixture,
                         "subject": subject, "test_type": test_type}
                        for fixture in fixtures]
                       or [{"action": "context_gap", "reason": "missing_boundary_fixture",
                            "test_type": test_type, "subject": subject}]),
            ))
        elif not endpoints or not business.get("ready_for_testing"):
            routes.append(RoutePlan(
                name="coverage_gap",
                strategy="当前假设缺少真实接口、输入或业务流程证据，停止主动猜测",
                steps=[{"action": "context_gap", "reason": "missing_surface",
                        "test_type": test_type}],
            ))
        elif test_type in {"sqli", "sql_injection", "sql-injection"}:
            routes.append(RoutePlan(
                name="sqli_hypothesis_test",
                strategy="只验证当前 SQL 注入假设：控制组、变异组和反证条件成对执行",
                steps=[{"action": "sqli_probe", "endpoint": ep, "params": ps}
                       for ep, ps in endpoints],
            ))
        elif test_type in {"xss", "reflected_xss", "reflected-xss"}:
            routes.append(RoutePlan(
                name="xss_hypothesis_test",
                strategy="只验证当前反射型 XSS 假设，唯一 canary 原样反射才形成支持信号",
                steps=[{"action": "xss_probe", "endpoint": ep, "params": ps}
                       for ep, ps in endpoints],
            ))
        elif test_type == "ssrf":
            steps = [{"action": "ssrf_probe", "endpoint": ep, "params": ps}
                     for ep, ps in endpoints
                     if SSRF_PARAM_HINTS & {p.lower() for p in ps}]
            routes.append(RoutePlan(
                name="ssrf_hypothesis_test",
                strategy="只验证当前 SSRF 假设，使用授权目标内 canary，不请求第三方地址",
                steps=steps or [{"action": "context_gap", "reason": "missing_ssrf_parameter",
                                 "test_type": test_type}],
            ))
        else:
            routes.append(RoutePlan(
                name="unsupported_hypothesis",
                strategy="保留当前假设并明确能力缺口，不退化成 SQLi/XSS 全量扫",
                steps=[{"action": "context_gap", "reason": "unsupported_hypothesis",
                        "test_type": test_type}],
            ))
        clues = self.ctx.blackboard.query(BlackboardLayer.RECON_CLUE, limit=200)
        hit_clues = [c for c in clues
                     if c["data"].get("clue_type") == "sensitive_path"
                     and c["data"].get("detail", {}).get("exposure_confirmed")]
        if hit_clues:
            # 弹性路线：侦察线索跟进（敏感路径 → 信息泄露候选）
            routes.append(RoutePlan(
                name="clue_followup",
                strategy="交付可核验结果：敏感路径暴露的确认或排除"
                         "（内容证据判定，仅可达不构成泄露）",
                steps=[{"action": "clue_verify", "clue": c["data"]} for c in hit_clues],
            ))
        return routes

    # ------------------------------------------------------------ 单步执行

    async def execute_step(self, task, route, plan, step) -> StepResult:
        action = step["action"]
        handler = {
            "hypothesis_investigate": self._step_contract_hypothesis,
            "sqli_probe": self._step_sqli,
            "xss_probe": self._step_xss,
            "ssrf_probe": self._step_ssrf,
            "boundary_compare": self._step_boundary_compare,
            "clue_verify": self._step_clue_verify,
            "context_gap": self._step_context_gap,
        }.get(action)
        if handler is None:
            return StepResult(ok=False, note=f"未知步骤动作: {action}")
        # 拦截信号按步累计（ContextVar：gather 并发路线各持上下文副本，
        # 实例级属性会被并行路线互相清零/污染，风险分随之失真）
        _STEP_SIGNALS.set(0)
        result = await handler(task, step)
        # 防护拦截信号喂 6.5 风险分（被 WAF/限流拦截＝暴露风险上升，9.2）
        result.detection_signals += _STEP_SIGNALS.get()
        return result

    # ------------------------------------------------------------ LLM 决策环

    async def _step_contract_hypothesis(self, task: Task, step: dict) -> StepResult:
        result = await self._step_investigate(task, step)
        decision = getattr(self, "_completion_assessments", {}).get(task.task_id, {})
        if decision.get("findings"):
            return result
        # 兼容旧会话的双身份 signals；新调查直接交付任意漏洞类型的 findings。
        signals = decision.get("signals")
        controls_valid = decision.get("baseline_valid") is True and decision.get("control_valid") is True
        legacy = []
        for signal in signals[:8] if isinstance(signals, list) and controls_valid else []:
            if not isinstance(signal, dict):
                continue
            quote = str(signal.get("evidence_quote") or "")[:200]
            baseline_ref, variant_ref = signal.get("baseline_ref"), signal.get("variant_ref")
            if not quote or not baseline_ref or baseline_ref == variant_ref:
                continue
            legacy.append({"vuln_type": "access_control", "title": "疑似未授权读取受保护内容",
                           "endpoint": task.target, "severity": "medium", "evidence": [
                               {"body_ref": ref, "quote": quote} for ref in (baseline_ref, variant_ref)]})
        if signals is not None:
            from spiritscout.agents.sub.findings import publish_findings

            decision["findings"] = legacy
            result.findings = await asyncio.to_thread(
                publish_findings, self.ctx, task, decision, actor=self.agent_id,
                agent_type=self.agent_type,
                observations=getattr(self, "_task_observations", {}).get(task.task_id, []))
            result.note = str(decision.get("reason") or result.note)
        return result

    async def _llm_decide_payloads(
        self, task: Task, *, vuln_class: str, endpoint: str, param: str,
        baseline_body: str, candidates: list[str], require_token: str = "",
    ) -> list[str] | None:
        """步骤决策：LLM 在骨架候选上裁剪/扩展 payload；None = 回退骨架。

        产出校验（确定性）：字符串、长度上限、必含探测 token（XSS canary）、
        数量封顶——LLM 决定「打什么」，规则决定「打多少、怎么判」。
        """
        data = await self._llm_chat_json("payload_planner", {
            "action": "decide_payloads",
            "vuln_class": vuln_class, "endpoint": endpoint, "param": param,
            "baseline_digest": baseline_body[:500],
            "candidate_payloads": candidates,
            "business_context": self._compact_business_context(),
            "max_payloads": LLM_PAYLOAD_CAP,
            "required_probe_token": require_token,
            "constraints": (
                "目的：为该参数产出可核验的判定结论，payload 是最小对照实验，"
                f"不是攻击流程。返回 JSON：{{\"payloads\": [{{\"payload\": str, \"purpose\": str}}], "
                f"\"rationale\": str}}。可在骨架候选上裁剪/修改/新增；每条 ≤{LLM_PAYLOAD_MAXLEN} 字符"
                + "；required_probe_token 非空时，每条必须原样包含该字段的探测标记"
                + "；不构造取数/拖库/破坏性载荷（G4）。"),
        }, reasoning_effort=self.task_effort(task), task=task)
        if data is None:
            return None
        out: list[str] = []
        for item in data.get("payloads", []):
            p = item.get("payload") if isinstance(item, dict) else item
            if not isinstance(p, str) or not p or len(p) > LLM_PAYLOAD_MAXLEN:
                continue
            if require_token and require_token not in p:
                continue
            out.append(p)
        out = out[:LLM_PAYLOAD_CAP]
        if not out:
            return None
        assert self.ctx.audit is not None
        self.ctx.audit.log("llm.step_decision", self.agent_id, {
            "vuln_class": vuln_class, "endpoint": endpoint, "param": param,
            "decided": len(out), "skeleton": len(candidates),
            "rationale": str(data.get("rationale", ""))[:200]})
        return out

    async def _llm_analyze_signals(
        self, task: Task, *, vuln_class: str, endpoint: str,
        observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """步骤分析：LLM 从响应摘要中提信号；证据引用逐字核验后生效（防幻觉）。

        引用核验：signal.evidence_quote 必须逐字出现在对应 (param, payload)
        的真实响应体中——LLM 只能指向它「看得到」的证据，核验不过即丢弃。
        """
        if not observations:
            return []
        data = await self._llm_chat_json("response_analyst", {
            "action": "analyze_responses",
            "vuln_class": vuln_class, "endpoint": endpoint,
            "observations": [{k: v for k, v in o.items() if k != "_body"}
                             for o in observations],
            "constraints": (
                "返回 JSON：{\"signals\": [{\"param\": str, \"payload\": str, "
                "\"signal_type\": str, \"confidence\": 0~1, \"rationale\": str, "
                "\"evidence_quote\": str}]}。evidence_quote 必须逐字摘自对应响应 "
                "digest，不得杜撰；无把握不输出信号。"),
        }, reasoning_effort=self.task_effort(task), task=task)
        if data is None:
            return []
        bodies = {(o["param"], o["payload"]): o["_body"] for o in observations}
        verified: list[dict[str, Any]] = []
        for sig in data.get("signals", []):
            if not isinstance(sig, dict):
                continue
            quote = str(sig.get("evidence_quote") or "")
            body = bodies.get((sig.get("param"), sig.get("payload")), "")
            if not quote or quote not in body:
                continue                      # 引用核验失败：幻觉信号丢弃
            try:
                confidence = min(float(sig.get("confidence", 0.5)), 0.7)
            except (TypeError, ValueError):
                confidence = 0.5
            verified.append({
                "param": sig.get("param"), "payload": sig.get("payload"),
                "signal_type": str(sig.get("signal_type", "llm_observed")),
                "confidence": confidence,
                "rationale": str(sig.get("rationale", ""))[:200],
                "evidence_quote": quote[:200],
            })
        if verified:
            assert self.ctx.audit is not None
            self.ctx.audit.log("llm.step_analysis", self.agent_id, {
                "vuln_class": vuln_class, "endpoint": endpoint,
                "signals_verified": len(verified)})
        return verified

    # ------------------------------------------------------------ SQLi

    async def _step_sqli(self, task: Task, step: dict) -> StepResult:
        endpoint = step["endpoint"]
        base_url = self._resolve_endpoint(task, endpoint)
        findings: list[dict[str, Any]] = []
        notes = []
        for param in step["params"]:
            # 1) 基线响应
            baseline = await self._probe(base_url, {param: "1"})
            if baseline is None:
                continue
            # 2) LLM 决策 payload（云端主路径；骨架候选供裁剪；不可用回退骨架）
            payloads = await self._llm_decide_payloads(
                task, vuln_class="sqli", endpoint=endpoint, param=param,
                baseline_body=baseline.stdout, candidates=SQLI_ERROR_PAYLOADS)
            if payloads is None:
                payloads = SQLI_ERROR_PAYLOADS
            # 3) 确定性执行（同一 http_probe 通道）+ 规则匹配（安全网）
            observations: list[dict[str, Any]] = []
            error_hit = None
            for payload in payloads:
                res = await self._probe(base_url, {param: payload})
                if res is None:
                    break
                observations.append({
                    "param": param, "payload": payload,
                    "status": res.parsed.get("status_code"),
                    "len": len(res.stdout), "digest": res.stdout[:300],
                    "_body": res.stdout,
                })
                if not error_hit:
                    error_hit = self._match_sql_error(res.stdout)
                    if error_hit:
                        self.cache.put(self.route_namespace,
                                       f"sqli_err|{endpoint}|{param}",
                                       {"payload": payload, "pattern": error_hit})
            # 4) 布尔差异二次信号（独立于报错特征的规则判定方式）
            diff_hit = await self._boolean_diff(base_url, param, baseline)
            # 5) LLM 分析（引用核验过的信号才作数，补充规则漏判）
            llm_signals = await self._llm_analyze_signals(
                task, vuln_class="sqli", endpoint=endpoint,
                observations=observations)
            if error_hit or diff_hit or llm_signals:
                if error_hit and diff_hit:
                    confidence = 0.85
                elif error_hit or diff_hit:
                    confidence = 0.6
                else:
                    confidence = max(s["confidence"] for s in llm_signals)
                vuln = self._new_vuln(
                    task, vuln_type="sqli", title=f"SQL 注入（{endpoint} 参数 {param}）",
                    endpoint=endpoint, parameter=param,
                    severity=Severity.HIGH, confidence=confidence,
                    evidence=self._evidence(base_url, param, error_hit, diff_hit, baseline)
                    + [{"method": "llm_analysis", "url": base_url, **s}
                       for s in llm_signals],
                )
                _, is_new, _ = self.ctx.convergence.merge_vulnerability(vuln, self.agent_type)
                if is_new:
                    findings.append({"type": "sqli", "endpoint": endpoint, "param": param})
                notes.append(
                    f"SQLi 信号 {endpoint}?{param} err={bool(error_hit)} "
                    f"diff={bool(diff_hit)} llm={len(llm_signals)}")
        return StepResult(ok=True, value=0.5 * len(findings), findings=findings,
                          note="; ".join(notes) or f"{endpoint} 无 SQLi 信号")

    async def _boolean_diff(self, url: str, param: str, baseline) -> dict[str, Any] | None:
        """布尔差异：恒真/恒假响应应显著分化（长度 + 关键文本双因子）。"""
        cached = self.cache.get(self.route_namespace, f"sqli_diff|{url}|{param}")
        if cached is not None:
            return cached or None
        true_res = await self._probe(url, {param: "1" + SQLI_TRUE_PAYLOAD})
        false_res = await self._probe(url, {param: "1" + SQLI_FALSE_PAYLOAD})
        if true_res is None or false_res is None:
            return None
        base_len = max(len(baseline.stdout), 1)
        t_len, f_len = len(true_res.stdout), len(false_res.stdout)
        # 恒真≈基线 且 恒假显著偏离（页面结构变化），视为差异信号
        true_close = abs(t_len - base_len) / base_len < DIFF_LEN_RATIO * 4
        false_far = abs(f_len - t_len) / max(t_len, 1) > DIFF_LEN_RATIO
        hit = {"true_len": t_len, "false_len": f_len, "base_len": base_len} \
            if (true_close and false_far) else {}
        self.cache.put(self.route_namespace, f"sqli_diff|{url}|{param}", hit)
        return hit or None

    @staticmethod
    def _match_sql_error(body: str) -> str | None:
        for pattern in SQL_ERROR_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                return pattern
        return None

    # ------------------------------------------------------------ XSS

    async def _step_xss(self, task: Task, step: dict) -> StepResult:
        endpoint = step["endpoint"]
        base_url = self._resolve_endpoint(task, endpoint)
        findings: list[dict[str, Any]] = []
        for param in step["params"]:
            # LLM 决策上下文感知 payload（必含 canary；不可用回退骨架）
            payloads = await self._llm_decide_payloads(
                task, vuln_class="xss", endpoint=endpoint, param=param,
                baseline_body="", candidates=XSS_PAYLOADS,
                require_token=XSS_CANARY)
            if payloads is None:
                payloads = XSS_PAYLOADS
            for payload in payloads:
                res = await self._probe(base_url, {param: payload})
                if res is None:
                    continue
                body = res.stdout
                if XSS_CANARY not in body:
                    continue  # 无反射
                # 未编码反射判定：尖括号原样出现（canary 标签结构存活）
                raw_reflected = f"{XSS_CANARY}<" in body or f'"{XSS_CANARY}>' in body \
                    or f"'{XSS_CANARY}<" in body
                encoded = f"{XSS_CANARY}&lt;" in body
                if raw_reflected and not encoded:
                    vuln = self._new_vuln(
                        task, vuln_type="xss",
                        title=f"反射型 XSS（{endpoint} 参数 {param}）",
                        endpoint=endpoint, parameter=param,
                        severity=Severity.MEDIUM, confidence=0.7,
                        evidence=[{"method": "canary_reflect", "url": base_url,
                                   "param": param, "payload": payload,
                                   "reflection": self._evidence_snippet(
                                       body, XSS_CANARY),
                                   "encoded": False}],
                    )
                    _, is_new, _ = self.ctx.convergence.merge_vulnerability(vuln, self.agent_type)
                    if is_new:
                        findings.append({"type": "xss", "endpoint": endpoint, "param": param})
                    break  # 该参数已确认反射，不再重复打
        return StepResult(ok=True, value=0.4 * len(findings), findings=findings,
                          note=f"{endpoint} XSS 反射 {len(findings)} 处" if findings
                          else f"{endpoint} 无未编码反射")

    # ------------------------------------------------------------ SSRF

    async def _step_ssrf(self, task: Task, step: dict) -> StepResult:
        endpoint = step["endpoint"]
        base_url = self._resolve_endpoint(task, endpoint)
        canary_url = task.target.rstrip("/") + SSRF_CANARY_PATH
        findings: list[dict[str, Any]] = []
        for param in step["params"]:
            res = await self._probe(base_url, {param: canary_url})
            if res is None:
                continue
            if SSRF_CANARY_TOKEN in res.stdout:
                vuln = self._new_vuln(
                    task, vuln_type="ssrf", title=f"SSRF（{endpoint} 参数 {param}）",
                    endpoint=endpoint, parameter=param,
                    severity=Severity.MEDIUM, confidence=0.75,
                    evidence=[{"method": "in_scope_canary", "url": base_url, "param": param,
                               "canary_url": canary_url,
                               "response": self._evidence_snippet(
                                   res.stdout, SSRF_CANARY_TOKEN)}],
                )
                _, is_new, _ = self.ctx.convergence.merge_vulnerability(vuln, self.agent_type)
                if is_new:
                    findings.append({"type": "ssrf", "endpoint": endpoint, "param": param})
        return StepResult(ok=True, value=0.4 * len(findings), findings=findings,
                          note=f"{endpoint} SSRF canary {'命中' if findings else '未命中'}")

    # ------------------------------------------------------------ 侦察线索跟进

    async def _step_boundary_compare(self, task: Task, step: dict) -> StepResult:
        """离线比较已采集的双身份响应；仅强一致的越权读取信号进入候选池。"""
        fixture = step.get("fixture") if isinstance(step.get("fixture"), dict) else {}
        records = fixture.get("records") if isinstance(fixture.get("records"), list) else []
        if len(records) != 2:
            return await self._step_context_gap(task, {
                "reason": "missing_boundary_fixture", "subject": step.get("subject", "")})
        first, second = records
        statuses = [int(item.get("status") or 0) for item in records]
        record_ids = [int(item.get("record_id") or 0) for item in records]
        evidence_ref = f"traffic:{record_ids[0]}" if all(record_ids) else ""
        denied = any(status in {401, 403} for status in statuses)
        same_body = bool(first.get("response_digest") and
                         first.get("response_digest") == second.get("response_digest"))
        meaningful = min(int(item.get("response_bytes") or 0) for item in records) > 0
        both_success = all(200 <= status < 300 for status in statuses)
        if denied:
            return StepResult(
                ok=True, value=0.2,
                note=(f"双身份真实流量显示边界生效：记录 {record_ids} 状态 {statuses}；"
                      "未形成越权支持信号"),
                final_note=True,
            )
        if not (both_success and same_body and meaningful):
            return StepResult(
                ok=True, value=0.1,
                blockers=[{
                    "type": "boundary_fixture_inconclusive",
                    "description": "双身份真实响应不足以确定是否读取了同一受保护对象",
                    "prerequisite": "补充可确认对象归属的合法交易，或经账号浏览器通道执行单变量对照",
                    "evidence_refs": [f"traffic:{record_id}" for record_id in record_ids if record_id],
                }],
                note=f"双身份离线对照不确定：记录 {record_ids} 状态 {statuses}",
                final_note=True,
            )
        evidence = [{
            "method": "recorded_identity_boundary_compare",
            "url": fixture.get("url", ""),
            "http_method": fixture.get("method", "GET"),
            "record_ids": record_ids,
            "statuses": statuses,
            "response_bytes": [int(item.get("response_bytes") or 0) for item in records],
            "response_digest": first.get("response_digest", ""),
            "json_shape": first.get("json_shape"),
            "note": "两个不同采集身份访问同一只读资源并得到相同完整响应；需独立复核对象归属与共享权限",
        }]
        vuln = self._new_vuln(
            task, vuln_type="access_control",
            title=f"疑似跨身份读取（{fixture.get('path') or step.get('subject') or '/'}）",
            endpoint=str(fixture.get("url") or step.get("subject") or ""),
            parameter="", severity=Severity.MEDIUM, confidence=0.6,
            evidence=evidence,
        )
        _, is_new, _ = self.ctx.convergence.merge_vulnerability(vuln, self.agent_type)
        findings = ([{"type": "access_control_signal", "endpoint": vuln.endpoint,
                      "evidence_ref": evidence_ref, "record_ids": record_ids}]
                    if is_new else [])
        return StepResult(
            ok=True, value=0.5 if is_new else 0.1, findings=findings,
            note=(f"双身份同资源响应一致，已形成待独立复核的权限候选：记录 {record_ids}"
                  if is_new else "相同权限候选已存在，未重复写入"),
            final_note=True,
        )

    # 内容证据之外的「存在即泄露」归档/备份后缀（200 + 非错误内容即曝光）
    _ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".sql", ".bak",
                         ".old", ".swp", ".7z", ".rar", ".env", ".log")

    async def _step_clue_verify(self, task: Task, step: dict) -> StepResult:
        clue = step["clue"]
        url = clue.get("value", "")
        res = await self.ctx.tools.call("http_probe", {"url": url}, **self.ctx.builtin_ctx)
        if not res.ok or res.parsed.get("status_code") != 200:
            return StepResult(ok=True, value=0.0, note=f"线索复核不可达: {url}")
        marker = clue.get("detail", {}).get("marker")
        if marker and marker not in res.stdout:
            return StepResult(ok=True, value=0.0, note=f"线索复核标记消失: {url}")
        path = urlparse(url).path
        # 升级漏洞需要泄露证据：内容 marker 命中（.git/.env/phpinfo 等）或
        # 归档/备份文件被直接供出；仅「可达」不构成漏洞（robots.txt、
        # swagger、一般 Disallow 路径——可达是常态，不是泄露）
        is_archive = path.lower().endswith(self._ARCHIVE_SUFFIXES)
        if not marker and not is_archive:
            return StepResult(ok=True, value=0.1,
                              note=f"线索复核可达但无泄露证据，仅留线索: {url}")
        vuln = self._new_vuln(
            task, vuln_type="info_disclosure",
            title=f"敏感信息泄露（{urlparse(url).path}）",
            endpoint=urlparse(url).path, parameter="",
            severity=Severity.MEDIUM if marker else Severity.LOW, confidence=0.8,
            evidence=[{"method": "http_recheck", "url": url,
                       "marker": marker, "content_length": len(res.stdout)}],
        )
        _, is_new, _ = self.ctx.convergence.merge_vulnerability(vuln, self.agent_type)
        return StepResult(ok=True, value=0.4 if is_new else 0.1,
                          findings=[{"type": "info_disclosure", "url": url}] if is_new else [],
                          note=f"线索复核确认泄露: {url}")

    # ------------------------------------------------------------ 内部

    async def _step_context_gap(self, task: Task, step: dict) -> StepResult:
        context = self._business_context()
        gaps = context.get("gaps") or ["未观测到带参数的 GET/HEAD 业务端点"]
        reason = str(step.get("reason") or "missing_surface")
        if reason == "missing_hypothesis":
            description = "任务没有绑定可证伪漏洞假设，已拒绝对所有接口例行扫描"
            prerequisite = "由源码、真实交易或业务边界证据形成 hypothesis_ref"
        elif reason == "missing_boundary_fixture":
            description = "权限/业务逻辑假设缺少合法的身份、对象或状态对照样本"
            prerequisite = "提供已授权测试账号和已观测对象样本，或录制两组合法请求；不枚举未知对象"
        elif reason == "missing_ssrf_parameter":
            description = "当前 SSRF 假设没有真实取址参数，未发送替代猜测请求"
            prerequisite = "从源码或真实交易确认 URL/回调/导入参数"
        elif reason == "unsupported_hypothesis":
            description = "当前漏洞假设尚无对应的确定性最小实验能力"
            prerequisite = "补充可执行请求样本、预期信号与反证信号"
        else:
            description = "业务与接口证据不足，未执行漏洞变异实验"
            prerequisite = "补充真实页面流程、脚本端点或带参数接口证据"
        return StepResult(
            ok=True,
            value=0.1,
            blockers=[{
                "type": reason,
                "description": description,
                "prerequisite": prerequisite,
                "alternative_capabilities": ["js_reverse", "business_context", "recon"],
            }],
            note=(description + "；当前缺口：" + "；".join(
                str(item) for item in gaps[:5])),
            final_note=True,
        )

    def _contract_incomplete_reason(self, task: Task, summary: dict[str, Any]) -> str:
        contract = task.payload.get("investigation_contract")
        contract = contract if isinstance(contract, dict) else {}
        experiment = str(contract.get("next_minimal_experiment") or "").strip()
        if not experiment:
            return ""
        feedback = summary.get("execution_feedback") or {}
        evidence = (summary.get("handoff") or {}).get("evidence_refs") or []
        if int(feedback.get("tool_calls") or 0) == 0 and not evidence:
            return ("调查契约要求执行最小实验，但本任务没有真实工具调用或本轮证据引用；"
                    "应标记为未执行/阻塞，并改派具备入口与能力的角色")
        return ""

    def _business_context(self) -> dict[str, Any]:
        return (self.ctx.blackboard.read(
            BlackboardLayer.TOPOLOGY, "business-context") or {})

    def _compact_business_context(self) -> dict[str, Any]:
        return shared_context(self.ctx.blackboard)

    @staticmethod
    def _evidence_snippet(body: str, marker: str, radius: int = 100) -> str:
        """只保留命中标记附近的最小证据，避免把整段业务响应写入黑板。"""
        index = body.find(marker)
        if index < 0:
            return marker
        start = max(0, index - radius)
        end = min(len(body), index + len(marker) + radius)
        return body[start:end]

    async def _probe(self, url: str, params: dict[str, str]):
        res = await self.ctx.tools.call(
            "http_probe", {"url": url, "params": params}, **self.ctx.builtin_ctx)
        if res.ok and self._looks_blocked(res):
            _STEP_SIGNALS.set(_STEP_SIGNALS.get() + 1)
        return res if res.ok else None

    @staticmethod
    def _looks_blocked(res) -> bool:
        """防护拦截判定：HTTP 403/429 或响应体含 WAF 拦截特征（6.5 暴露风险）。"""
        if res.parsed.get("status_code") in (403, 429):
            return True
        body = (res.stdout or "")[:2000].lower()
        return any(marker in body for marker in WAF_BLOCK_MARKERS)

    def _collect_endpoints(self, task: Task) -> list[tuple[str, list[str]]]:
        """只消费 HAR、真实 HTML 表单等已经观测到的端点。"""
        endpoints: list[tuple[str, list[str]]] = []
        seen: set[tuple[str, str]] = set()
        for rec in self.ctx.blackboard.query(BlackboardLayer.ASSET, limit=500):
            data = rec["data"]
            if data.get("asset_type") != "endpoint":
                continue
            attrs = data.get("attrs", {})
            # 当前确定性 payload 管线使用查询参数；非 GET 请求先保留在黑板，
            # 等请求重放器消费，避免把真实 POST 错测成 GET。
            if str(attrs.get("method") or "GET").upper() not in {"GET", "HEAD"}:
                continue
            value = str(data.get("value") or "")
            key = (str(attrs.get("method") or "GET").upper(), value)
            if value and key not in seen:
                seen.add(key)
                query_params = [p for p in attrs.get("params", [])
                                if attrs.get("param_locations", {}).get(p, "query") == "query"]
                if query_params:
                    # HAR 资产保留完整 URL，避免授权目标已带路径时
                    # 把 /app/search 误拼为 /app/app/search。
                    endpoints.append((value, query_params))
        return endpoints

    def _boundary_fixtures(self, subject: str) -> list[dict[str, Any]]:
        workspace = str((getattr(self.ctx, "builtin_ctx", None) or {}).get(
            "workspace_root") or "")
        if not workspace or not subject:
            return []
        try:
            return traffic_recorder.boundary_fixtures(Path(workspace), subject)
        except (OSError, ValueError):
            return []

    @staticmethod
    def _resolve_endpoint(task: Task, endpoint: str) -> str:
        if urlparse(endpoint).scheme in {"http", "https"}:
            return endpoint
        return task.target.rstrip("/") + "/" + endpoint.lstrip("/")

    @staticmethod
    def _evidence(url: str, param: str, error_hit: str | None,
                  diff_hit: dict | None, baseline) -> list[dict[str, Any]]:
        ev: list[dict[str, Any]] = []
        if error_hit:
            ev.append({"method": "error_pattern", "url": url, "param": param,
                       "pattern": error_hit})
        if diff_hit:
            ev.append({"method": "boolean_diff", "url": url, "param": param, **diff_hit})
        return ev

    def _new_vuln(self, task: Task, *, vuln_type: str, title: str, endpoint: str,
                  parameter: str, severity: Severity, confidence: float,
                  evidence: list[dict[str, Any]]) -> Vulnerability:
        contract = task.payload.get("investigation_contract")
        contract = contract if isinstance(contract, dict) else {}
        hypothesis_ref = str(contract.get("hypothesis_ref") or "")[:200]
        return Vulnerability(
            vuln_id=new_id("vuln"), title=title, vuln_type=vuln_type,
            target=task.target, endpoint=endpoint, parameter=parameter,
            severity=severity, status=VulnStatus.CANDIDATE, confidence=confidence,
            evidence=evidence, found_by=self.agent_id,
            hypothesis_ref=hypothesis_ref,
            hypothesis_refs=[hypothesis_ref] if hypothesis_ref else [],
        )
