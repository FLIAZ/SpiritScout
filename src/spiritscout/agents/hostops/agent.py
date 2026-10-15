"""主机与横向三专项（SRC / 渗透赛）：听风测绘 → 贯甲审计 → 深耕横向。

路线是**交付目标**：strategy 声明要产出的可核验结果（证据 → 结论），
步骤是探测单元与合规通道（禁打门 / RPS / 审计边界不变）。
- 听风（port_scan）：TCP 连接级端口测绘 + 服务横幅/HTTP 指纹 → 资产层；
- 贯甲（host_vuln）：对已观测服务做管理面/调试面暴露核验——有内容
  证据才算命中，仅可达不构成漏洞；
- 深耕（post_exploit）：凭据线索汇总（值打码，只记位置与类型）+
  授权范围内相邻主机测绘；不做任何破坏性动作。
均为只读探测；CTF 模式不装配本模块（硬约束 C7）。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, Asset, BlackboardLayer, RunMode, Task

# 常见 Web 服务端口（横幅不说话时的协议猜测依据）
_WEB_PORTS = {80, 443, 8080, 8000, 8081, 8443, 8888, 3000, 5000, 7001, 9090, 6443}
# 管理面/调试面核验路径（内容证据判定，非字典爆破）
_ADMIN_PATHS = (
    ("/console", "console"), ("/actuator", "actuator"), ("/swagger", "swagger"),
    ("/api-docs", "api-docs"), ("/phpinfo.php", "phpinfo()"),
    ("/manager/html", "tomcat-manager"), ("/debug", "debug"),
    ("/.env", "="),
)
# 凭据类证据标记（深耕：值打码登记）
_CRED_MARKERS = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|authorization|access[_-]?token)")
_SECRET_VALUE = re.compile(
    r"(?i)((?:password|passwd|secret|api[_-]?key|token)\s*[=:]\s*)(\S{4,})")


class PortScanAgent(SubAgent):
    """听风（TA0007）：授权主机上的 TCP 存活面与服务指纹测绘。"""

    agent_type = AgentType.PORT_SCAN
    route_namespace = "port_scan"

    def route_templates(self, task: Task) -> list[RoutePlan]:
        host = urlparse(task.target).hostname or task.target
        return [RoutePlan(
            name="tcp_service_sweep",
            strategy="交付可核验结果：授权主机存活的 TCP 服务清单"
                     "（开放端口 + 服务横幅 / HTTP 指纹证据）",
            steps=[{"action": "tcp_sweep", "host": host},
                   {"action": "fingerprint_services", "host": host}],
        )]

    async def execute_step(self, task, route, plan, step) -> StepResult:
        handler = {"tcp_sweep": self._step_tcp_sweep,
                   "fingerprint_services": self._step_fingerprint}.get(step["action"])
        if handler is None:
            return StepResult(ok=False, note=f"未知步骤动作: {step['action']}")
        return await handler(task, step)

    async def _step_tcp_sweep(self, task: Task, step: dict) -> StepResult:
        res = await self.ctx.tools.call(
            "tcp_scan", {"host": step["host"]}, **self.ctx.builtin_ctx)
        if not res.ok:
            return StepResult(ok=False, note=f"端口测绘失败: {res.error}")
        parsed = res.parsed
        if parsed.get("scope_note"):
            self._write_clue(task, "scope_note", parsed["scope_note"], 0.5)
        findings: list[dict[str, Any]] = []
        for item in parsed.get("open", []):
            port, banner = item["port"], str(item.get("banner") or "")
            value = f"{step['host']}:{port}"
            self.ctx.convergence.merge_asset(Asset(
                asset_id=new_id("asset"), asset_type="service", value=value,
                port=port,
                service="tcp", attrs={"banner": banner}, source_agent=self.agent_id,
            ), self.agent_type)
            self.cache.put(self.route_namespace, f"service|{value}",
                           {"host": step["host"], "port": port, "banner": banner})
            findings.append({"type": "service", "value": value})
        return StepResult(
            ok=True, value=min(0.15 * len(findings), 0.9),
            findings=findings,
            note=(f"端口测绘 {step['host']}：开放 {len(findings)}"
                  f"/{parsed.get('scanned', 0)}（{', '.join(f['value'] for f in findings[:6])}"
                  f"{'…' if len(findings) > 6 else ''}）"))

    async def _step_fingerprint(self, task: Task, step: dict) -> StepResult:
        services: list[tuple[str, int, str]] = []
        for rec in self.ctx.blackboard.query(BlackboardLayer.ASSET, limit=300):
            data = rec["data"]
            if data.get("asset_type") != "service":
                continue
            value = str(data.get("value") or "")
            if ":" not in value:
                continue
            host, port = value.rsplit(":", 1)
            try:
                port = int(port)
            except ValueError:
                continue
            services.append((host, port,
                             str((data.get("attrs") or {}).get("banner", ""))))
        notes: list[str] = []
        for host, port, banner in services[:14]:
            if port not in _WEB_PORTS and "HTTP" not in banner:
                notes.append(f"{host}:{port} 非 HTTP（{banner or '无横幅'}）")
                continue
            scheme = "https" if port in (443, 8443) else "http"
            url = f"{scheme}://{host}:{port}/"
            res = await self.ctx.tools.call(
                "http_probe", {"url": url}, **self.ctx.builtin_ctx)
            if not res.ok:
                notes.append(f"{url} 不可达")
                continue
            title = res.parsed.get("title", "")
            fp = res.parsed.get("fingerprint", {})
            self.ctx.convergence.merge_asset(Asset(
                asset_id=new_id("asset"), asset_type="service", value=f"{host}:{port}",
                port=port, service=scheme, fingerprint=fp,
                attrs={"title": title}, source_agent=self.agent_id,
            ), self.agent_type)
            notes.append(f"{url} → {res.parsed.get('status_code')} "
                         f"title={title!r} server={fp.get('server', '')}")
        return StepResult(
            ok=True, value=min(0.1 * len(notes), 0.6),
            note="服务指纹：" + "；".join(notes)[:400] if notes else "无服务可指纹")

    def _write_clue(self, task: Task, clue_type: str, value: str,
                    confidence: float) -> None:
        self.ctx.blackboard.write(
            BlackboardLayer.RECON_CLUE, new_id("clue"),
            {"clue_type": clue_type, "value": value, "confidence": confidence,
             "task_id": task.task_id, "found_by": self.agent_id},
            agent_type=self.agent_type, updated_by=self.agent_id,
            dedup_key=f"clue|{clue_type}|{value}",
        )


class HostVulnAgent(SubAgent):
    """贯甲：对已观测服务做管理面/调试面暴露核验与指纹弱项登记。"""

    agent_type = AgentType.HOST_VULN
    route_namespace = "host_vuln"

    def _services(self, task: Task) -> list[str]:
        urls: list[str] = []
        for rec in self.ctx.blackboard.query(BlackboardLayer.ASSET, limit=500):
            data = rec["data"]
            if data.get("asset_type") != "service":
                continue
            value = str(data.get("value") or "")
            if data.get("service") in ("http", "https") and value:
                scheme = str(data.get("service") or "http")
                urls.append(f"{scheme}://{value.split(':', 1)[0]}"
                            f":{data.get('port') or 80}/" if ":" in value else value)
        entry = task.target.rstrip("/")
        if entry.startswith(("http://", "https://")) and entry not in urls:
            urls.append(entry + "/")
        return urls[:16]

    def route_templates(self, task: Task) -> list[RoutePlan]:
        services = self._services(task)
        if not services:
            return [RoutePlan(
                name="coverage_gap",
                strategy="交付可核验结果：等待听风/斥候的服务清单——无已观测服务，不猜测",
                steps=[{"action": "coverage_gap"}],
            )]
        return [RoutePlan(
            name="service_exposure",
            strategy="交付可核验结果：每个已观测服务的管理面/调试面暴露核验——"
                     "有内容证据才算命中，无证据即排除",
            steps=[{"action": "audit_service", "url": s} for s in services],
        )]

    async def execute_step(self, task, route, plan, step) -> StepResult:
        handler = {"audit_service": self._step_audit,
                   "coverage_gap": self._step_gap}.get(step["action"])
        if handler is None:
            return StepResult(ok=False, note=f"未知步骤动作: {step['action']}")
        return await handler(task, step)

    async def _step_audit(self, task: Task, step: dict) -> StepResult:
        base = str(step["url"]).rstrip("/")
        hits: list[str] = []
        clues: list[str] = []
        for path, marker in _ADMIN_PATHS:
            res = await self.ctx.tools.call(
                "http_probe", {"url": base + path}, **self.ctx.builtin_ctx)
            if not res.ok or res.parsed.get("status_code") != 200:
                continue
            body = res.stdout or ""
            evidence = marker.lower() in body.lower() or path == "/.env" and "=" in body
            if evidence:
                hits.append(base + path)
                self.ctx.convergence.merge_vulnerability(self._new_vuln(
                    task, base + path,
                    severity="medium",
                    evidence=[{"method": "admin_surface", "url": base + path,
                               "marker": marker, "content_length": len(body)}],
                ), self.agent_type)
            else:
                clues.append(base + path)
        server = ""
        probe = await self.ctx.tools.call(
            "http_probe", {"url": base + "/"}, **self.ctx.builtin_ctx)
        if probe.ok:
            server = str((probe.parsed.get("fingerprint") or {}).get("server", ""))
        if server:
            self.ctx.blackboard.write(
                BlackboardLayer.RECON_CLUE, new_id("clue"),
                {"clue_type": "tech_stack", "value": server, "confidence": 0.7,
                 "detail": {"source": "server_header", "url": base},
                 "task_id": task.task_id, "found_by": self.agent_id},
                agent_type=self.agent_type, updated_by=self.agent_id,
                dedup_key=f"clue|tech_stack|{server}|{base}")
        return StepResult(
            ok=True, value=min(0.4 * len(hits), 0.9),
            findings=[{"type": "admin_surface", "value": h} for h in hits],
            note=(f"{base} 管理面暴露 {len(hits)} 处（{'; '.join(hits)[:160]}）"
                  if hits else f"{base} 无管理面暴露证据"
                  + (f"；可达无证据 {len(clues)} 条留线索" if clues else "")))

    async def _step_gap(self, task: Task) -> StepResult:
        return StepResult(ok=True, value=0.1, final_note=True,
                          note="无已观测服务：等待测绘/侦察路线产出后再审计")

    def _new_vuln(self, task: Task, endpoint: str, *, severity: str,
                  evidence: list[dict[str, Any]]):
        from spiritscout.core.models import Severity, Vulnerability, VulnStatus

        return Vulnerability(
            vuln_id=new_id("vuln"),
            title=f"管理面/调试面暴露（{urlparse(endpoint).path}）",
            vuln_type="info_disclosure", target=task.target,
            endpoint=urlparse(endpoint).path, parameter="",
            severity=Severity(severity), status=VulnStatus.CANDIDATE,
            confidence=0.75, evidence=evidence, found_by=self.agent_id,
        )


class PostExploitAgent(SubAgent):
    """深耕：凭据线索汇总（值打码）与授权范围内相邻主机测绘。"""

    agent_type = AgentType.POST_EXPLOIT
    route_namespace = "post_exploit"

    def route_templates(self, task: Task) -> list[RoutePlan]:
        routes = [RoutePlan(
            name="credential_survey",
            strategy="交付可核验结果：凭据类线索台账（只记位置与类型，值打码）",
            steps=[{"action": "credential_survey"}],
        )]
        peers = self._peer_hosts(task)
        if peers:
            routes.append(RoutePlan(
                name="lateral_scope",
                strategy="交付可核验结果：授权范围内相邻主机的存活服务清单"
                         "（与已测主机相互印证）",
                steps=[{"action": "lateral_probe", "host": h} for h in peers],
            ))
        return routes

    def _peer_hosts(self, task: Task) -> list[str]:
        """授权范围内的相邻主机（显式域名白名单，排除当前目标；只读测绘）。"""
        scope = self.ctx.policy.guard.scope
        target_host = urlparse(task.target).hostname or ""
        peers = [d for d in scope.allowed_domains
                 if d and not str(d).startswith("*.") and d != target_host][:8]
        return peers if self.ctx.policy.mode != RunMode.CTF else []

    async def execute_step(self, task, route, plan, step) -> StepResult:
        handler = {"credential_survey": self._step_credential,
                   "lateral_probe": self._step_lateral}.get(step["action"])
        if handler is None:
            return StepResult(ok=False, note=f"未知步骤动作: {step['action']}")
        return await handler(task, step)

    async def _step_credential(self, task: Task,
                               step: dict[str, Any] | None = None) -> StepResult:
        """凭据类证据台账：值一律打码——位置与类型才进黑板。"""
        found: list[dict[str, str]] = []
        for rec in self.ctx.blackboard.query(BlackboardLayer.RECON_CLUE, limit=200):
            d = rec["data"]
            blob = f"{d.get('value', '')} {d.get('detail', '')}"
            if _CRED_MARKERS.search(str(blob)):
                found.append({"source": f"clue:{d.get('clue_type', '')}",
                              "where": str(d.get("value", ""))[:160]})
        for rec in self.ctx.blackboard.query(BlackboardLayer.VULN, limit=200):
            d = rec["data"]
            blob = f"{d.get('title', '')} {d.get('endpoint', '')}"
            if _CRED_MARKERS.search(str(blob)):
                found.append({"source": f"vuln:{d.get('vuln_type', '')}",
                              "where": str(d.get("endpoint", ""))[:160]})
        for item in found:
            self.ctx.blackboard.write(
                BlackboardLayer.RECON_CLUE, new_id("clue"),
                {"clue_type": "credential_lead",
                 "value": _SECRET_VALUE.sub(r"\1***", item["where"]),
                 "confidence": 0.6,
                 "detail": {"source": item["source"], "redacted": True},
                 "task_id": task.task_id, "found_by": self.agent_id},
                agent_type=self.agent_type, updated_by=self.agent_id,
                dedup_key=f"clue|credential_lead|{item['source']}|{item['where']}")
        return StepResult(
            ok=True, value=min(0.2 * len(found), 0.8),
            findings=[{"type": "credential_lead", "value": f["where"]}
                      for f in found[:10]],
            note=(f"凭据线索 {len(found)} 条（值打码登记："
                  f"{'; '.join(f['where'][:40] for f in found[:4])}…）"
                  if found else "黑板中暂无凭据类证据"))

    async def _step_lateral(self, task: Task, step: dict) -> StepResult:
        host = str(step["host"])
        res = await self.ctx.tools.call(
            "tcp_scan", {"host": host}, **self.ctx.builtin_ctx)
        if not res.ok:
            return StepResult(ok=False, note=f"{host} 测绘失败: {res.error}")
        open_ports = res.parsed.get("open", [])
        for item in open_ports:
            self.ctx.convergence.merge_asset(Asset(
                asset_id=new_id("asset"), asset_type="service",
                value=f"{host}:{item['port']}", port=item["port"], service="tcp",
                attrs={"banner": item.get("banner", "")}, source_agent=self.agent_id,
            ), self.agent_type)
        return StepResult(
            ok=True, value=min(0.15 * len(open_ports), 0.8),
            findings=[{"type": "lateral_service",
                       "value": f"{host}:{p['port']}"} for p in open_ports],
            note=(f"{host} 开放 {len(open_ports)}"
                  f"/{res.parsed.get('scanned', 0)} 端口"
                  + (f"（{', '.join(str(p['port']) for p in open_ports[:8])}）"
                     if open_ports else "")))
