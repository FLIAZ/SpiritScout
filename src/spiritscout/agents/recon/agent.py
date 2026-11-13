"""从业务页面、响应指纹和公开链接收集资产与侦察线索。"""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
from typing import Any
from urllib.parse import urlparse

from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.blackboard.leads import (
    assess_authenticity,
    compact_profile,
    response_profile,
    upsert_lead,
)
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, Asset, BlackboardLayer, Task

# 命中即判定内容泄露的标记（侦察线索置信度依据）
EXPOSURE_MARKERS = {
    "/.git/HEAD": "ref:",
    "/.git/config": "[core]",
    "/.env": "=",
    "/phpinfo.php": "phpinfo()",
    "/.svn/entries": "svn",
}
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".sql", ".bak", ".7z", ".rar")


def _is_wildcard_target(target: str) -> bool:
    parsed = urlparse(target if "://" in target else f"//{target}")
    return "*" in (parsed.hostname or target)


class ReconAgent(SubAgent):
    agent_type = AgentType.RECON
    route_namespace = "recon"

    # ------------------------------------------------------------ 路线模板

    def route_templates(self, task: Task) -> list[RoutePlan]:
        if _is_wildcard_target(task.target):
            return [RoutePlan(
                name="wildcard_asset_handoff",
                strategy="通配符只声明资产范围，不能作为具体主机发送 HTTP、DNS 或端口请求",
                steps=[{"action": "asset_discovery_required"}],
            )]
        base = task.target.rstrip("/")
        routes = [
            RoutePlan(
                name="observed_application_surface",
                strategy="交付可核验结果：入口与技术栈的实证画像（指纹/标题/服务头"
                         "证据），以及信息泄露面的证据化核验结论——有内容证据才算命中",
                steps=[
                    {"action": "homepage", "url": base},
                    {"action": "fingerprint"},
                    {"action": "robots_discover"},
                    {"action": "llm_path_sweep"},
                ],
            ),
        ]
        host_wide = self._host_wide_scope(task.target)
        if host_wide:
            routes.append(RoutePlan(
                name="dns_identity",
                strategy="交付可核验结果：已授权主机的身份记录（IP 解析证据），供资产归并",
                steps=[{"action": "dns"}],
            ))
        return routes

    # ------------------------------------------------------------ 单步执行

    async def execute_step(self, task, route, plan, step) -> StepResult:
        action = step["action"]
        handler = {
            "homepage": self._step_homepage,
            "dns": self._step_dns,
            "fingerprint": self._step_fingerprint,
            "path_probe": self._step_path_probe,
            "robots_discover": self._step_robots_discover,
            "port_probe": self._step_port_probe,
            "llm_path_sweep": self._step_llm_path_sweep,
            "asset_discovery_required": self._step_asset_discovery_required,
        }.get(action)
        if handler is None:
            return StepResult(ok=False, note=f"未知步骤动作: {action}")
        return await handler(task, step)

    async def _step_homepage(self, task: Task, step: dict) -> StepResult:
        url = step["url"]
        if _is_wildcard_target(url):
            return self._wildcard_gap(task)
        res = await self.ctx.tools.call(
            "http_probe", {"url": url}, **self.ctx.builtin_ctx)
        if not res.ok:
            return StepResult(ok=False, note=f"首页抓取失败: {res.error}")
        parsed = res.parsed
        asset = Asset(
            asset_id=new_id("asset"), asset_type="url", value=url,
            port=urlparse(url).port, fingerprint=parsed.get("fingerprint", {}),
            attrs={"title": parsed.get("title", ""),
                   "status_code": parsed.get("status_code"),
                   "sources": ["target_response"],
                   "headers": self._safe_headers(parsed.get("headers", {}))},
            source_agent=self.agent_id,
        )
        _, is_new = self.ctx.convergence.merge_asset(asset, self.agent_type)
        profile = response_profile(res.stdout, parsed)
        return StepResult(
            ok=True, value=0.3 if is_new else 0.1,
            findings=[{"type": "asset", "value": url, "title": parsed.get("title")}],
            note=f"首页 {parsed.get('status_code')} title={parsed.get('title')!r}",
            cache_writes={f"fingerprint|{url}": {
                "fingerprint": parsed.get("fingerprint", {}),
                "title": parsed.get("title", ""),
                "headers": self._safe_headers(parsed.get("headers", {})),
                "response_profile": profile,
            }},
        )

    async def _step_dns(self, task: Task, step: dict) -> StepResult:
        host = urlparse(task.target).hostname or task.target
        if _is_wildcard_target(host):
            return self._wildcard_gap(task)
        res = await self.ctx.tools.call("dns_resolve", {"host": host}, **self.ctx.builtin_ctx)
        if not res.ok:
            return StepResult(ok=False, note=f"DNS 解析失败: {res.error}")
        # 代理虚拟地址（TUN fake-ip）不是目标资产：标注留痕即可，不入册
        if res.parsed.get("proxy_virtual"):
            return StepResult(ok=True, value=0.1, findings=[],
                              note=f"DNS {host} → {res.parsed.get('ips')}"
                                   f"（代理虚拟地址，未入册）")
        findings = []
        for ip in res.parsed.get("ips", []):
            self.ctx.convergence.merge_asset(Asset(
                asset_id=new_id("asset"), asset_type="ip", value=ip,
                source_agent=self.agent_id), self.agent_type)
            findings.append({"type": "ip", "value": ip})
        return StepResult(ok=True, value=0.2, findings=findings,
                          note=f"DNS {host} → {res.parsed.get('ips')}")

    async def _step_fingerprint(self, task: Task, step: dict) -> StepResult:
        """基于路线缓存的指纹归因（路线间共享缓存消费示例）。"""
        url = task.target.rstrip("/")
        fp = self.cache.get(self.route_namespace, f"fingerprint|{url}")
        if not fp:
            return StepResult(ok=False, note="无缓存指纹（首页步骤未成功）")
        techs = []
        server = fp.get("fingerprint", {}).get("server", "")
        if server:
            techs.append(server)
        powered = fp.get("fingerprint", {}).get("x_powered_by")
        if powered:
            techs.append(powered)
        evidence = fp.get("fingerprint", {}).get("technologies", [])
        techs.extend(item["name"] for item in evidence if isinstance(item, dict) and item.get("name"))
        techs = list(dict.fromkeys(techs))
        if techs:
            self._write_clue(task, clue_type="tech_stack", value=", ".join(techs),
                             confidence=0.8, detail={"source": "response_observations", "evidence": evidence})
        return StepResult(ok=True, value=0.2 if techs else 0.0,
                          note=f"技术栈: {', '.join(techs)}" if techs else "技术栈：当前响应未提供可确认指纹")

    async def _step_path_probe(self, task: Task, step: dict) -> StepResult:
        base = task.target.rstrip("/")
        path = step["path"]
        url = base + path
        res = await self.ctx.tools.call("http_probe", {"url": url}, **self.ctx.builtin_ctx)
        if not res.ok:
            return StepResult(ok=True, value=0.0, note=f"{path} 不可达（不计失败）")
        status = res.parsed.get("status_code", 0)
        origin = str(step.get("origin") or "model_generated_test")
        profile = response_profile(res.stdout, res.parsed)
        if status != 200:
            assessment = assess_authenticity(
                profile=profile, baseline=None, origin=origin, marker_confirmed=False)
            upsert_lead(
                self.ctx.blackboard, subject=url, origins=[origin], status=assessment["lead_status"],
                authenticity=assessment, observation=compact_profile(profile),
                unknowns=[] if assessment["lead_status"] in {"refuted", "blocked"}
                else ["该候选是否对应独立真实入口"], replace_unknowns=True,
                actor=self.agent_id, agent_type=self.agent_type,
            )
            return StepResult(ok=True, value=0.0, note=f"{path} → {status}")
        baseline = await self._response_baseline(
            task, include_missing=origin == "model_generated_test")
        assessment = assess_authenticity(
            profile=profile, baseline=baseline, origin=origin,
            marker_confirmed=False,
        )
        marker = EXPOSURE_MARKERS.get(path)
        marker_hit = bool(marker and marker in res.stdout)
        content_type = str((res.parsed.get("headers") or {}).get("content-type", "")).lower()
        archive_hit = path.lower().endswith(ARCHIVE_SUFFIXES) and "text/html" not in content_type
        confirmed = marker_hit or archive_hit
        if confirmed:
            assessment = assess_authenticity(
                profile=profile, baseline=baseline, origin=origin,
                marker_confirmed=True,
            )
        # 确认命中给足价值分：保证命中路线不被健康度剪枝误杀（阈值调优）
        value = 0.8 if confirmed else 0.2
        soft_200 = assessment["verdict"] == "soft_200_suspected"
        if soft_200:
            value = 0.0
        clue_ref = ""
        # 软 200 仍是待消歧候选，不同时写成普通 observed_path，避免下游把
        # “真实性存疑”和“已观察到有效路径”当成两条相互矛盾的事实。
        if confirmed or assessment["lead_status"] == "actionable":
            clue_ref = self._write_clue(
                task, clue_type="sensitive_path" if confirmed else "observed_path", value=url,
                confidence=0.9 if confirmed else 0.6,
                detail={"status_code": status, "marker": marker,
                        "marker_hit": marker_hit, "archive_hit": archive_hit,
                        "exposure_confirmed": confirmed},
            )
        upsert_lead(
            self.ctx.blackboard, subject=url, origins=[origin],
            source_refs=[clue_ref] if clue_ref else [],
            evidence_state="observed", status=assessment["lead_status"],
            authenticity=assessment, observation=compact_profile(profile),
            unknowns=[] if assessment["lead_status"] == "actionable"
            else ["该候选是否对应独立真实入口"], replace_unknowns=True,
            next_minimal_experiment=("比较不存在路径、Content-Type、正文摘要、重定向与候选类型标记"
                                     if assessment["lead_status"] == "needs_disambiguation" else ""),
            actor=self.agent_id, agent_type=self.agent_type,
        )
        return StepResult(ok=True, value=value,
                          findings=[{"type": "sensitive_path" if confirmed else "observed_path",
                                     "value": url,
                                     "confirmed": confirmed,
                                     "authenticity": assessment["verdict"]}],
                          note=(f"{'泄露证据命中' if confirmed else '疑似软 200' if soft_200 else '路径可达'} "
                                f"{path} → 200（{assessment['reason']}）"))

    def _contract_incomplete_reason(self, task: Task, summary: dict[str, Any]) -> str:
        """子域清单型任务没有产出任何子域时不能以根域探测冒充完成。"""
        contract = task.payload.get("investigation_contract")
        if not isinstance(contract, dict):
            return ""
        expected = str(contract.get("expected_information") or "")
        if "子域" not in expected:
            return ""
        base_host = (urlparse(task.target).hostname or task.target).lower().lstrip("*.")
        domains = {
            str(row["data"].get("value") or "").lower().rstrip(".")
            for row in self.ctx.blackboard.query(BlackboardLayer.ASSET, limit=2000)
            if row["data"].get("asset_type") == "domain"
        }
        discovered = sorted(value for value in domains
                            if value != base_host and value.endswith("." + base_host))
        if discovered:
            return ""
        return "调查契约要求交付子域清单，但本任务未发现或验证任何子域；根域与端口基线不能替代该交付"

    async def _step_robots_discover(self, task: Task, step: dict) -> StepResult:
        """robots.txt 路径发现：解析 Disallow/Allow 条目接力探测。

        robots.txt 本身可达不构成漏洞（不产生泄露线索）；其声明的隐藏
        路径才是侦察价值所在——逐条接力 path_probe，命中与否由内容证据
        决定（marker/归档后缀），无证据仅留线索供后续路线参考。
        """
        import re as _re

        base = task.target.rstrip("/")
        res = await self.ctx.tools.call(
            "http_probe", {"url": base + "/robots.txt"}, **self.ctx.builtin_ctx)
        if not res.ok or res.parsed.get("status_code", 0) != 200:
            return StepResult(ok=True, value=0.0, note="robots.txt 不可达（不计失败）")
        entries: list[str] = []
        for line in res.stdout.splitlines():
            m = _re.match(r"(?i)^\s*(?:disallow|allow)\s*:\s*(\S+)", line)
            if not m:
                continue
            path = m.group(1).strip()
            # 确定性过滤：站内相对路径、无通配/穿越、非根、去重、封顶
            if (path.startswith("/") and path != "/" and "*" not in path
                    and ".." not in path and path not in entries):
                entries.append(path[:200])
            if len(entries) >= 10:
                break
        if not entries:
            return StepResult(ok=True, value=0.1, note="robots.txt 无可用 Disallow 条目")
        self._write_clue(
            task, clue_type="robots_entry", value=",".join(entries),
            confidence=0.6, detail={"source": "robots.txt", "count": len(entries)})
        hits: list[str] = []
        for path in entries:
            sub = await self._step_path_probe(task, {"path": path, "origin": "robots.txt"})
            hits.append(sub.note)
        return StepResult(
            ok=True, value=0.3,
            findings=[{"type": "robots_entry", "value": e} for e in entries],
            note=f"robots.txt 披露 {len(entries)} 条路径，已接力探测；" + "; ".join(hits)[:200])

    # ------------------------------------------------------------ LLM 路径建议

    LLM_PATH_CAP = 12          # 仅按已观测指纹提出少量可解释假设

    async def _step_llm_path_sweep(self, task: Task, step: dict) -> StepResult:
        """收尾步：LLM 按已观测指纹/标题建议追加敏感路径（云端主路径）。

        观察来自路线间共享缓存与黑板（首页指纹、技术栈线索）；建议路径经
        确定性校验（站内相对路径、无穿越、去重、封顶）后走同一 http_probe
        通道（范围标注/限速不动）。LLM 不可用 → 空步（字典即骨架兜底）。
        """
        base = task.target.rstrip("/")
        fp = self.cache.get(self.route_namespace, f"fingerprint|{base}") or {}
        observed = {
            "fingerprint": fp.get("fingerprint", {}), "title": fp.get("title", ""),
            "tech_clues": [rec["data"].get("value")
                           for rec in self.ctx.blackboard.query(
                               BlackboardLayer.RECON_CLUE, limit=100)
                           if rec["data"].get("clue_type") == "tech_stack"][:5],
            "already_probed": ["/robots.txt"],
        }
        data = await self._llm_chat_json("path_planner", {
            "action": "suggest_paths", "target": task.target, **observed,
            "max_paths": self.LLM_PATH_CAP,
            "constraints": (
                "返回 JSON：{\"paths\": [str], \"rationale\": str}。仅站内相对路径"
                "（以 / 开头），不要重复已探测清单；针对观测到的技术栈给出"
                "少量高价值信息泄露面（配置/公开文档/调试端点）；每个建议都必须"
                "能由给出的技术栈或页面标题解释，不猜测业务接口。"),
        }, reasoning_effort=self.task_effort(task))
        if data is None:
            return StepResult(ok=True, value=0.0,
                              note="无可信的指纹扩展建议，未做字典猜测")
        paths: list[str] = []
        for p in data.get("paths", []):
            if not isinstance(p, str):
                continue
            p = p.strip()
            # 确定性校验：站内相对路径、禁目录穿越、去重（骨架 + 本轮）
            if (not p.startswith("/")) or "://" in p or ".." in p \
                    or p == "/robots.txt" or p in paths:
                continue
            paths.append(p[:200])
        paths = paths[: self.LLM_PATH_CAP]
        if not paths:
            return StepResult(ok=True, value=0.0, note="LLM 无新增路径建议")
        assert self.ctx.audit is not None
        self.ctx.audit.log("llm.step_decision", self.agent_id, {
            "action": "suggest_paths", "decided": len(paths),
            "rationale": str(data.get("rationale", ""))[:200]})
        hits = 0
        notes = []
        for path in paths:
            sub = await self._step_path_probe(task, {"path": path, "origin": "model_generated_test"})
            notes.append(sub.note)
            hits += sum(1 for f in sub.findings if f.get("confirmed"))
        return StepResult(ok=True, value=min(0.3 + 0.4 * hits, 1.0),
                          findings=[{"type": "llm_path_sweep", "paths": paths,
                                     "confirmed": hits}],
                          note="; ".join(notes)[:300])

    async def _step_port_probe(self, task: Task, step: dict) -> StepResult:
        parsed_target = urlparse(task.target)
        host = parsed_target.hostname or task.target
        if _is_wildcard_target(host):
            return self._wildcard_gap(task)
        port = step["port"]
        if parsed_target.port == port:
            return StepResult(ok=True, value=0.0, note=f"端口 {port} 与目标同端口，跳过")
        scheme = "https" if port in (443, 8443) else "http"
        url = f"{scheme}://{host}:{port}/"
        res = await self.ctx.tools.call("http_probe", {"url": url, "timeout": 5.0},
                                        **self.ctx.builtin_ctx)
        if not res.ok:
            return StepResult(ok=True, value=0.0, note=f"端口 {port} 无 Web 服务")
        asset = Asset(
            asset_id=new_id("asset"), asset_type="service", value=f"{host}:{port}",
            port=port, service=scheme, fingerprint=res.parsed.get("fingerprint", {}),
            attrs={"title": res.parsed.get("title", ""), "sources": ["target_response"]},
            source_agent=self.agent_id,
        )
        _, is_new = self.ctx.convergence.merge_asset(asset, self.agent_type)
        return StepResult(ok=True, value=0.3 if is_new else 0.1,
                          findings=[{"type": "service", "value": f"{host}:{port}"}],
                          note=f"端口 {port} Web 服务 {res.parsed.get('status_code')}")

    # ------------------------------------------------------------ 内部

    async def _step_asset_discovery_required(
            self, task: Task, step: dict | None = None) -> StepResult:
        return self._wildcard_gap(task)

    def _wildcard_gap(self, task: Task) -> StepResult:
        note = ("通配符不能作为具体主机主动探测；本路线未发送 HTTP、DNS 或端口请求，"
                "等待探微交付具体 hostname 后再继续侦察。")
        self._write_clue(
            task, clue_type="asset_discovery_gap", value=task.target,
            confidence=0.95,
            detail={"reason": "wildcard_requires_concrete_host",
                    "next": "等待资产发现交付具体 hostname"},
        )
        return StepResult(ok=True, value=0.05, final_note=True, note=note)

    def _host_wide_scope(self, target: str) -> bool:
        host = urlparse(target).hostname or target
        scope = self.ctx.policy.guard.scope
        for domain in scope.allowed_domains:
            pattern = domain.lower().strip()
            if pattern.startswith("*."):
                pattern = "*" + pattern[1:]
            if fnmatch.fnmatchcase(host.lower(), pattern):
                return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(address in ipaddress.ip_network(cidr, strict=False)
                   for cidr in scope.allowed_cidrs)

    @staticmethod
    def _safe_headers(headers: dict[str, Any]) -> dict[str, Any]:
        allowed = {"content-type", "server", "x-powered-by", "location", "allow"}
        return {key: value for key, value in headers.items()
                if str(key).lower() in allowed}

    async def _response_baseline(
            self, task: Task, *, include_missing: bool = False) -> list[dict[str, Any]]:
        base = task.target.rstrip("/")
        homepage = self.cache.get(self.route_namespace, f"fingerprint|{base}") or {}
        profiles: list[dict[str, Any]] = []
        profile = homepage.get("response_profile")
        if isinstance(profile, dict):
            profiles.append(profile)

        if not include_missing:
            return profiles
        missing_key = f"authenticity-missing|{base}"
        missing = self.cache.get(self.route_namespace, missing_key)
        if not isinstance(missing, dict):
            suffix = hashlib.sha256(base.encode()).hexdigest()[:12]
            res = await self.ctx.tools.call(
                "http_probe", {"url": f"{base}/.spiritscout-missing-{suffix}"},
                **self.ctx.builtin_ctx)
            if res.ok:
                missing = response_profile(res.stdout, res.parsed)
                self.cache.put(self.route_namespace, missing_key, missing)
        if isinstance(missing, dict):
            profiles.append(missing)
        return profiles

    def _write_clue(self, task: Task, *, clue_type: str, value: str,
                    confidence: float, detail: dict[str, Any]) -> str:
        record_id = new_id("clue")
        dedup_key = f"clue|{clue_type}|{value}"
        self.ctx.blackboard.write(
            BlackboardLayer.RECON_CLUE, record_id,
            {"clue_type": clue_type, "value": value, "confidence": confidence,
             "detail": detail, "task_id": task.task_id, "found_by": self.agent_id},
            agent_type=self.agent_type, updated_by=self.agent_id,
            dedup_key=dedup_key,
        )
        saved = self.ctx.blackboard.find_by_dedup_key(BlackboardLayer.RECON_CLUE, dedup_key)
        return (f"recon_clue:{saved['record_id']}@{saved['version']}" if saved
                else f"recon_clue:{record_id}")
