"""全链路核验流水线（设计书 4.3.2，硬约束 C5 的核心落地）。

候选 → 初筛 → 独立复现（≥2 种方式交叉）→ 误报排查 → SRC 合规判定
     → CVSS 定级 → PoC 自动生成与实测 → 收录/淘汰

裁决原则：状态迁移全部由规则引擎完成（模型是建议者、规则是裁决者）；
每次迁移写回黑板漏洞层（乐观锁版本控制，冲突自动重试）。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from spiritscout.core.audit import AuditLogger, NullAuditLogger
from spiritscout.core.exceptions import VersionConflict
from spiritscout.core.models import (
    AgentType,
    BlackboardLayer,
    RunMode,
    Severity,
    Vulnerability,
    VulnStatus,
)
from spiritscout.policy.engine import PolicyEngine
from spiritscout.tools.registry import ToolRegistry
from spiritscout.verify.cvss import cvss31_base_score, severity_from_score
from spiritscout.verify.poc_templates import (
    REPRO_METHODS,
    build_poc_request,
    render_poc_text,
)
from spiritscout.verify.rules import BaselineRules

# SQL 报错特征（与探测侧同源的最小副本； 统一迁入知识库特征表）
_SQL_ERROR_RE = re.compile(
    r"SQL syntax|mysql_|ORA-\d{5}|PostgreSQL.*ERROR|SQLite3?::|SQLSTATE\[|"
    r"Microsoft SQL Server|ODBC SQL Server|Unclosed quotation mark", re.IGNORECASE)

ReproFn = Callable[[Vulnerability], Awaitable[tuple[bool, dict[str, Any]]]]


class VerifyPipeline:
    """核验流水线执行器（以 MASTER 权限写漏洞层：系统级裁决者角色）。"""

    WRITER = AgentType.MASTER

    def __init__(
        self,
        blackboard,
        tools: ToolRegistry,
        policy: PolicyEngine,
        *,
        rules: BaselineRules | None = None,
        audit: AuditLogger | None = None,
        builtin_ctx: dict[str, Any] | None = None,
        knowledge=None,
        collab=None,
    ):
        self.bb = blackboard
        self.tools = tools
        self.policy = policy
        self.rules = rules or BaselineRules()
        self.audit = audit or NullAuditLogger()
        self.builtin_ctx = builtin_ctx or {}
        # 知识库接入（误报特征库/平台规则/PoC 模板/P0~P4 分级 verify_method 联动）
        self.knowledge = knowledge
        # 多模型投票（4.9.3-2 高影响判定复核；None=不启用，规则引擎独立裁决）
        self.collab = collab
        self._repro_fns: dict[str, ReproFn] = {
            "error_replay": self._repro_error_replay,
            "boolean_replay": self._repro_boolean_replay,
            "reflect_replay": self._repro_reflect_replay,
            "reflect_alt_payload": self._repro_reflect_alt,
            "canary_replay": self._repro_canary,
            "canary_variant": self._repro_canary_variant,
            "http_recheck": self._repro_http_recheck,
            "http_recheck_alt_ua": self._repro_http_recheck_alt_ua,
        }

    # ------------------------------------------------------------ 入口

    async def run(self) -> dict[str, Any]:
        """处理漏洞层全部候选，返回核验统计。"""
        stats: dict[str, Any] = {"processed": 0, "accepted": 0,
                                 "rejected": 0, "by_status": {}}
        for rec in self.bb.query(BlackboardLayer.VULN, limit=10000):
            vuln = Vulnerability.model_validate(rec["data"])
            if vuln.status != VulnStatus.CANDIDATE:
                continue
            final = await self.process(rec["record_id"], rec["version"])
            stats["processed"] += 1
            stats["by_status"][final.status.value] = \
                stats["by_status"].get(final.status.value, 0) + 1
            if final.status == VulnStatus.ACCEPTED:
                stats["accepted"] += 1
            else:
                stats["rejected"] += 1
        self.audit.log("verify.round", "verify_pipeline", stats)
        return stats

    async def process(self, record_id: str, version: int) -> Vulnerability:
        """单漏洞全链路核验（状态机迁移逐阶段落黑板）。"""
        rec = self.bb.read_versioned(BlackboardLayer.VULN, record_id)
        assert rec is not None
        vuln = Vulnerability.model_validate(rec[0])
        version = rec[1]

        def save(expected: int) -> int:
            return self._save(record_id, vuln, expected)

        # 1) 初筛（叠加知识库 P4 判定——附录C-2.2「系统应直接判无效不入库/不提交」）
        verdict = self.rules.screen(vuln)
        if not verdict.passed:
            vuln.status = VulnStatus.REJECTED_SCREEN
            vuln.false_positive_reason = verdict.reason
            self._transition(vuln, "screen_reject", verdict.reason)
            save(version)
            return vuln
        kb_reject = self._kb_reject_reason(vuln)
        if kb_reject:
            vuln.status = VulnStatus.REJECTED_SCREEN
            vuln.false_positive_reason = kb_reject
            self._transition(vuln, "screen_reject", kb_reject)
            save(version)
            return vuln
        vuln.status = VulnStatus.SCREENED
        self._transition(vuln, "screen_pass", "初筛通过")
        version = save(version)

        # 2) 独立复现（≥2 种方式交叉）
        methods = REPRO_METHODS.get(vuln.vuln_type, [])
        successes = 0
        for method in methods[: max(self.rules.min_reproductions, 1)]:
            fn = self._repro_fns.get(method)
            if fn is None:
                continue
            try:
                ok, record = await fn(vuln)
            except Exception as exc:  # noqa: BLE001 —— 复现异常按失败计
                ok, record = False, {"method": method, "error": str(exc)[:200]}
            record.setdefault("method", method)
            record["ok"] = ok
            vuln.reproductions.append(record)
            successes += int(ok)
        if successes < self.rules.min_reproductions:
            vuln.status = VulnStatus.REJECTED_UNREPRO
            vuln.false_positive_reason = (
                f"交叉复现失败：{successes}/{self.rules.min_reproductions} 种方式成功")
            self._transition(vuln, "reproduce_fail", vuln.false_positive_reason)
            save(version)
            return vuln
        vuln.status = VulnStatus.REPRODUCED
        vuln.confidence = min(vuln.confidence + 0.15, 1.0)
        self._transition(vuln, "reproduce_pass",
                         f"{successes} 种方式交叉复现成功")
        version = save(version)

        # 3) 误报排查
        fp_reason, fp_notes = await self._fp_check(vuln)
        if fp_reason:
            vuln.status = VulnStatus.REJECTED_FP
            vuln.false_positive_reason = fp_reason
            self._transition(vuln, "fp_reject", fp_reason)
            save(version)
            return vuln
        vuln.status = VulnStatus.FP_CHECKED
        self._transition(vuln, "fp_pass", "误报排查通过")
        version = save(version)

        # 4) SRC 合规判定（叠加知识库 P0~P4 分级裁决与 verify_method 联动）
        decision, reason = self._compliance_check(vuln)
        vuln.compliance = {"platform": self.policy.platform_rule.platform or "generic",
                           "mode": self.policy.mode.value,
                           "decision": decision, "reason": reason}
        if fp_notes:
            vuln.compliance["kb_fp_notes"] = fp_notes
        self._attach_kb_context(vuln)
        if not decision:
            vuln.status = VulnStatus.REJECTED_COMPLIANCE
            self._transition(vuln, "compliance_reject", reason)
            save(version)
            return vuln
        vuln.status = VulnStatus.COMPLIANCE_CHECKED
        self._transition(vuln, "compliance_pass", reason)
        version = save(version)

        # 5) CVSS 定级（规则引擎裁决严重度，探测侧建议值仅参考）
        vector = self.rules.cvss_vectors.get(vuln.vuln_type)
        if vector:
            vuln.cvss_vector = vector
            vuln.cvss_score = cvss31_base_score(vector)
            vuln.severity = severity_from_score(vuln.cvss_score)

        # 6) PoC 自动生成与实测（优先知识库 PoC 模板，内置模板兜底）
        poc_req, poc_template_id = self._build_poc(vuln)
        if poc_template_id:
            vuln.compliance["kb_poc_template"] = poc_template_id
        if poc_req is not None:
            vuln.poc_code = render_poc_text(vuln, poc_req)
            vuln.poc_verified = await self._run_poc(poc_req)
        vuln.exploitable = vuln.poc_verified  # MVP：实测通过即可利用
        if not vuln.poc_verified:
            vuln.status = VulnStatus.REJECTED_UNREPRO
            vuln.false_positive_reason = "PoC 实测未通过（证据门控：无实证不收录）"
            self._transition(vuln, "poc_fail", vuln.false_positive_reason)
            save(version)
            return vuln
        self._transition(vuln, "poc_pass", "PoC 实测通过")
        version = save(version)

        # 7) 收录裁决（中危及以上且可利用 → 收录池；低危仅记录）
        verdict = self.rules.accept_to_pool(vuln)
        vuln.status = VulnStatus.ACCEPTED if verdict.passed else VulnStatus.REJECTED_LOW
        if not verdict.passed:
            vuln.false_positive_reason = verdict.reason
        self._transition(vuln, "final",
                         f"{'收录' if verdict.passed else '不收录'}: {verdict.reason or '达标'}")
        version = save(version)

        # 8) 高影响判定多模型投票（4.9.3-2 / 6.10）：高危/严重收录前
        # ≥2 个不同模型独立复核，一致才通过；分歧降级人工复核（
        #    模型是建议者——投票不改裁决状态，仅标记 review_required 并留痕，
        #    终审权仍在规则引擎/人工，4.9.5）。
        if (vuln.status == VulnStatus.ACCEPTED and self.collab is not None
                and vuln.severity in (Severity.HIGH, Severity.CRITICAL)):
            await self._collab_vote(vuln)
            save(version)
        return vuln

    async def _collab_vote(self, vuln: Vulnerability) -> None:
        """收录/高危定级投票：结果写入 compliance（分歧 → review_required）。"""
        assert self.collab is not None
        subject = (
            f"漏洞「{vuln.title}」（类型 {vuln.vuln_type}，定级 {vuln.severity.value}）"
            f"是否满足收录条件（真实可利用、证据充分、非误报）？")
        context = {
            "target": vuln.target, "endpoint": vuln.endpoint,
            "evidence_methods": [e.get("method") for e in vuln.evidence],
            "poc_verified": vuln.poc_verified,
            "kb_priority": vuln.compliance.get("kb_priority", ""),
        }
        try:
            result = await self.collab.vote(subject, context=context)
        except Exception as exc:  # noqa: BLE001 —— 投票故障不阻断已裁决链路
            self.audit.log("verify.vote_error", "verify_pipeline",
                           {"vuln_id": vuln.vuln_id, "error": str(exc)[:200]})
            return
        vuln.compliance["collab_vote"] = {
            "decision": result.decision, "agreed": result.agreed,
            "models": result.models_used,
            "verdicts": [v.verdict for v in result.votes],
        }
        if not result.agreed or result.decision != "pass":
            vuln.compliance["review_required"] = True
        self._transition(vuln, "collab_vote",
                         f"投票 {result.decision}（一致={result.agreed}）"
                         + ("，降级人工复核" if vuln.compliance.get("review_required") else ""))

    # ------------------------------------------------------------ 复现方式

    async def _repro_error_replay(self, vuln: Vulnerability) -> tuple[bool, dict]:
        ev = next((e for e in vuln.evidence if e.get("method") == "error_pattern"), {})
        res = await self._fetch(ev.get("url", ""), {vuln.parameter: "'"})
        body = res.stdout if res else ""
        hit = bool(body and _SQL_ERROR_RE.search(body))
        return hit, {"url": ev.get("url"), "payload": "'",
                     "pattern_hit": hit, "status": res.parsed.get("status_code") if res else None}

    async def _repro_boolean_replay(self, vuln: Vulnerability) -> tuple[bool, dict]:
        ev = next((e for e in vuln.evidence if e.get("method") in
                   ("boolean_diff", "error_pattern")), {})
        url = ev.get("url", "")
        t = await self._fetch(url, {vuln.parameter: "1' AND '1'='1"})
        f = await self._fetch(url, {vuln.parameter: "1' AND '1'='2"})
        if not t or not f:
            return False, {"url": url, "reason": "请求失败"}
        # 方式二（与报错特征独立）：恒真/恒假响应长度分化 或 恒假触发报错
        len_diff = abs(len(t.stdout) - len(f.stdout)) / max(len(t.stdout), 1) > 0.05
        err_on_false = bool(_SQL_ERROR_RE.search(f.stdout))
        return len_diff or err_on_false, {
            "url": url, "true_len": len(t.stdout), "false_len": len(f.stdout),
            "len_diff": len_diff, "err_on_false": err_on_false}

    async def _repro_reflect_replay(self, vuln: Vulnerability) -> tuple[bool, dict]:
        ev = vuln.evidence[0] if vuln.evidence else {}
        res = await self._fetch(ev.get("url", ""),
                                {vuln.parameter: "spirvfy<svg/onload=2>"})
        body = res.stdout if res else ""
        hit = "spirvfy<" in body
        return hit, {"url": ev.get("url"), "canary": "spirvfy", "raw_reflected": hit}

    async def _repro_reflect_alt(self, vuln: Vulnerability) -> tuple[bool, dict]:
        ev = vuln.evidence[0] if vuln.evidence else {}
        res = await self._fetch(ev.get("url", ""),
                                {vuln.parameter: 'spirvfy2"><b>3</b>'})
        body = res.stdout if res else ""
        hit = 'spirvfy2"><b>3</b>' in body
        return hit, {"url": ev.get("url"), "canary": "spirvfy2", "raw_reflected": hit}

    async def _repro_canary(self, vuln: Vulnerability) -> tuple[bool, dict]:
        ev = vuln.evidence[0] if vuln.evidence else {}
        res = await self._fetch(ev.get("url", ""),
                                {vuln.parameter: ev.get("canary_url", "")})
        hit = bool(res and "SPIR-SSRF-CANARY" in res.stdout)
        return hit, {"url": ev.get("url"), "canary_url": ev.get("canary_url"), "token_hit": hit}

    async def _repro_canary_variant(self, vuln: Vulnerability) -> tuple[bool, dict]:
        ev = vuln.evidence[0] if vuln.evidence else {}
        variant = (ev.get("canary_url") or "") + "?v=repro"
        res = await self._fetch(ev.get("url", ""), {vuln.parameter: variant})
        hit = bool(res and "SPIR-SSRF-CANARY" in res.stdout)
        return hit, {"url": ev.get("url"), "canary_url": variant, "token_hit": hit}

    async def _repro_http_recheck(self, vuln: Vulnerability) -> tuple[bool, dict]:
        ev = vuln.evidence[0] if vuln.evidence else {}
        res = await self._fetch(ev.get("url", ""), {})
        marker = ev.get("marker")
        hit = bool(res and res.parsed.get("status_code") == 200
                   and (not marker or marker in res.stdout))
        return hit, {"url": ev.get("url"), "marker": marker, "hit": hit}

    async def _repro_http_recheck_alt_ua(self, vuln: Vulnerability) -> tuple[bool, dict]:
        ev = vuln.evidence[0] if vuln.evidence else {}
        res = await self._fetch(ev.get("url", ""), {},
                                headers={"User-Agent": "SpiritScout-Verifier/0.1"})
        marker = ev.get("marker")
        hit = bool(res and res.parsed.get("status_code") == 200
                   and (not marker or marker in res.stdout))
        return hit, {"url": ev.get("url"), "ua": "alt", "hit": hit}

    # ------------------------------------------------------------ 误报/合规/PoC

    async def _fp_check(self, vuln: Vulnerability) -> tuple[str | None, list[str]]:
        """误报排查。返回 (拒绝原因|None, 降级提示列表)。

        命名代码检查（fp-* code 行）为内置逻辑；知识库 pattern 行叠加执行：
        action=reject → 直接判误报；action=downgrade → 记入合规附注供定级参考。
        """
        notes: list[str] = []
        if vuln.vuln_type == "xss":
            body = " ".join(str(e.get("reflection", "")) for e in vuln.evidence)
            if "&lt;" in body and "<svg" not in body and "<img" not in body:
                return ("误报：反射内容经 HTML 实体编码，不可执行"
                        "（fp-encoded-reflect）"), notes
        if vuln.vuln_type == "sqli":
            # 静态错误页排查：不带载荷的基线响应若同样命中报错特征 → 误报
            ev = next((e for e in vuln.evidence if e.get("url")), {})
            baseline = await self._fetch(ev.get("url", ""), {vuln.parameter: "1"})
            if baseline and _SQL_ERROR_RE.search(baseline.stdout):
                # 基线有回溯属框架调试模式，仍需载荷触发差异才算有效
                repro_ok = any(r.get("ok") for r in vuln.reproductions
                               if r.get("method") == "boolean_replay")
                if not repro_ok:
                    return ("误报：基线响应即含报错特征且无布尔差异"
                            "（fp-static-error-page）"), notes
        if vuln.vuln_type == "ssrf":
            body = " ".join(str(e.get("response", "")) for e in vuln.evidence)
            if "SPIR-SSRF-CANARY" not in body:
                return ("误报：canary 仅为参数回显而非实际请求结果"
                        "（fp-canary-no-reflect）"), notes
        # 误报特征库 pattern 行（附录C-2.2 机器可判子集）
        if self.knowledge is not None:
            blob = f"{vuln.title} {vuln.target} {vuln.evidence}"
            for sig in self.knowledge.fp_signatures(vuln_type=vuln.vuln_type):
                if sig["check_kind"] != "pattern":
                    continue
                if re.search(sig["check_ref"], blob):
                    if sig["action"] == "reject":
                        return (f"误报特征库命中 {sig['fp_id']}：{sig['desc']}"), notes
                    notes.append(f"{sig['fp_id']}: {sig['desc']}")
        return None, notes

    def _compliance_check(self, vuln: Vulnerability) -> tuple[bool, str]:
        """SRC 合规判定：模式能力边界 + 数据最小化核查（G4）。"""
        if self.policy.mode == RunMode.SRC and not self.policy.platform_rule.auto_test_allowed:
            return False, f"平台 {self.policy.platform_rule.platform} 禁止自动化测试"
        # 数据最小化：证据链不得包含拖库/批量数据导出特征
        blob = str(vuln.evidence) + str(vuln.reproductions)
        if re.search(r"UNION SELECT|information_schema|load_file|into outfile", blob, re.IGNORECASE):
            return False, "证据链含数据导出特征，违反数据最小化原则（G4）"
        return True, "自动化测试许可范围内，证据链符合数据最小化（G4）"

    # ------------------------------------------------------------ 知识库联动

    def _attach_kb_context(self, vuln: Vulnerability) -> None:
        """把知识库 P0~P4 分级与 verify_method 指导写入合规记录（3.5 核验联动）。

        分级只作裁决参考与附注（P4 除外，见 _kb_reject_reason）；
        verify_method 来自 PoC 模板库 + 挖洞模板，供报告与人工复核使用。
        """
        if self.knowledge is None:
            return
        pri = self.knowledge.priority_for(vuln.vuln_type)
        if pri:
            vuln.compliance["kb_priority"] = pri["priority"]
            vuln.compliance["kb_weight"] = pri["weight"]
            if pri.get("reject_if"):
                vuln.compliance["kb_reject_if"] = pri["reject_if"]
        methods = [t["verify_method"] for t in
                   self.knowledge.poc_templates(vuln_type=vuln.vuln_type)
                   if t.get("verify_method")]
        hunts = self.knowledge.hunt_templates(vuln_type=vuln.vuln_type)
        methods.extend(t["compliance_redline"] for t in hunts
                       if t.get("compliance_redline"))
        if methods:
            vuln.compliance["kb_verify_method"] = "；".join(methods)

    def _kb_reject_reason(self, vuln: Vulnerability) -> str | None:
        """附录C-5.2 自动判定：weight=0（P4 默认拒收）→ 规则引擎直接判无效。"""
        if self.knowledge is None:
            return None
        pri = self.knowledge.priority_for(vuln.vuln_type)
        if pri and pri["weight"] == 0:
            return (f"知识库判定 {vuln.vuln_type} 为 P4 默认拒收类型"
                    f"（{pri['reject_if'] or pri['representative_scene']}），不提交")
        return None

    def _build_poc(self, vuln: Vulnerability) -> tuple[dict[str, Any] | None, str | None]:
        """PoC 请求构造：知识库 PoC 模板优先，内置模板兜底。返回 (请求规格, 模板id)。"""
        if self.knowledge is not None:
            templates = self.knowledge.poc_templates(vuln_type=vuln.vuln_type)
            if templates:
                req = self._poc_from_template(vuln, templates[0]["request_spec"])
                return req, templates[0]["template_id"]
        return build_poc_request(vuln), None

    def _poc_from_template(self, vuln: Vulnerability,
                           spec: dict[str, Any]) -> dict[str, Any] | None:
        """按模板语义构造请求（params_from：payload_or_default/evidence.payload/
        evidence.canary_url/none；marker_from：evidence.marker）。"""
        ev = vuln.evidence[0] if vuln.evidence else {}
        url = ev.get("url") or (vuln.target.rstrip("/") + vuln.endpoint)
        params: dict[str, str] = {}
        src = spec.get("params_from")
        if src in ("payload_or_default", "evidence.payload"):
            payload = next(
                (e.get("payload") for e in vuln.evidence if e.get("payload")), None)
            if payload is None:
                if src == "evidence.payload":
                    return None
                payload = spec.get("default_payload", "'")
            params = {vuln.parameter: payload}
        elif src == "evidence.canary_url":
            canary = ev.get("canary_url")
            if not canary:
                return None
            params = {vuln.parameter: canary}
        req: dict[str, Any] = {"method": spec.get("method", "GET"),
                               "url": url, "params": params}
        expect = spec.get("expect", "status_200")
        if spec.get("marker_from") == "evidence.marker":
            marker = ev.get("marker")
            expect = "substring" if marker else "status_200"
            req["substring"] = marker or ""
        req["expect"] = expect
        for key in ("substring", "canary"):
            if key in spec:
                req[key] = spec[key]
        return req

    async def _run_poc(self, req: dict[str, Any]) -> bool:
        """PoC 实测（经 http_probe，全程授权校验）。"""
        res = await self._fetch(req["url"], req.get("params", {}))
        if res is None:
            return False
        expect = req.get("expect")
        if expect == "sql_error":
            return bool(_SQL_ERROR_RE.search(res.stdout))
        if expect == "raw_reflection":
            canary = req.get("canary", "spirxss")
            return f"{canary}<" in res.stdout
        if expect == "substring":
            return req.get("substring", "") in res.stdout
        if expect == "status_200":
            return res.parsed.get("status_code") == 200
        return False

    # ------------------------------------------------------------ 内部

    async def _fetch(self, url: str, params: dict[str, str], headers=None):
        if not url:
            return None
        res = await self.tools.call(
            "http_probe", {"url": url, "params": params, "headers": headers or {}},
            **self.builtin_ctx)
        return res if res.ok else None

    def _save(self, record_id: str, vuln: Vulnerability, expected_version: int) -> int:
        """乐观锁写回（冲突重读重试，最多 3 次）。"""
        for _ in range(3):
            try:
                self.bb.write(
                    BlackboardLayer.VULN, record_id, vuln.model_dump(mode="json"),
                    agent_type=self.WRITER, updated_by="verify_pipeline",
                    dedup_key=vuln.dedup_key(), expected_version=expected_version,
                )
                return expected_version + 1
            except VersionConflict:
                rec = self.bb.read_versioned(BlackboardLayer.VULN, record_id)
                assert rec is not None
                expected_version = rec[1]
        raise VersionConflict(f"漏洞 {record_id} 状态迁移多次冲突")

    def _transition(self, vuln: Vulnerability, stage: str, detail: str) -> None:
        self.audit.log("verify.transition", "verify_pipeline", {
            "vuln_id": vuln.vuln_id, "type": vuln.vuln_type,
            "stage": stage, "status": vuln.status.value, "detail": detail})
