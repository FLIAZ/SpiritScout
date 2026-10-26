"""Web 漏洞探测：通过基线和变异请求收集候选证据。

模型引用必须能在实际响应中核对；候选写入黑板后交由独立复测角色审核。"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlparse

from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.core.ids import new_id
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    Severity,
    Task,
    Vulnerability,
    VulnStatus,
)
from spiritscout.gateway.context import shared_context

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
        endpoints = self._collect_endpoints(task)
        business = self._business_context()
        routes: list[RoutePlan] = []
        if endpoints and business.get("ready_for_testing"):
            routes.extend((
                RoutePlan(
                    name="sqli_pipeline",
                    strategy="交付可核验结果：每个已观测端点参数的注入判定——"
                             "报错特征/布尔差异/引用核验信号作证据，无证据即排除"
                             "（不取数据，G4）",
                    steps=[{"action": "sqli_probe", "endpoint": ep, "params": ps}
                           for ep, ps in endpoints],
                ),
                RoutePlan(
                    name="xss_pipeline",
                    strategy="交付可核验结果：每个端点参数的反射判定——唯一 canary "
                             "原样反射（附响应片段证据）或排除",
                    steps=[{"action": "xss_probe", "endpoint": ep, "params": ps}
                           for ep, ps in endpoints],
                ),
            ))
            ssrf_steps = [
                {"action": "ssrf_probe", "endpoint": ep, "params": ps}
                for ep, ps in endpoints
                if SSRF_PARAM_HINTS & {p.lower() for p in ps}
            ]
            if ssrf_steps:
                routes.append(RoutePlan(
                    name="ssrf_pipeline",
                    strategy="交付可核验结果：带取址参数端点的出网判定——"
                             "授权内 canary 自证（附响应证据）或排除",
                    steps=ssrf_steps,
                ))
        else:
            routes.append(RoutePlan(
                name="coverage_gap",
                strategy="业务证据覆盖不足时停止主动猜测，并明确需要补充的流程",
                steps=[{"action": "context_gap"}],
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
            "sqli_probe": self._step_sqli,
            "xss_probe": self._step_xss,
            "ssrf_probe": self._step_ssrf,
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
        return StepResult(
            ok=True,
            value=0.1,
            findings=[{"type": "coverage_gap", "gaps": gaps[:10]}],
            note=("未进行接口猜测；业务覆盖不足：" + "；".join(
                str(item) for item in gaps[:5])),
            final_note=True,
        )

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
        return Vulnerability(
            vuln_id=new_id("vuln"), title=title, vuln_type=vuln_type,
            target=task.target, endpoint=endpoint, parameter=parameter,
            severity=severity, status=VulnStatus.CANDIDATE, confidence=confidence,
            evidence=evidence, found_by=self.agent_id,
        )
