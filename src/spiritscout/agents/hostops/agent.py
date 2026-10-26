"""端口测绘、服务审计与横向线索分析。

基于已观测服务执行只读探测；凭据只记录位置和类型。CTF 模式不装配这些角色。"""

from __future__ import annotations

import hashlib
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
# 凭据类证据标记（中英文；中文会话里「密码/口令」是高频表述，
# 2026-10-16 复盘：用户答复「账号xxx,密码xxx」因纯英文正则被漏识别）
_CRED_MARKERS = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|authorization|access[_-]?token"
    r"|密码|口令|凭据)")
# 打码：值紧跟标记（可有 =/:/： 分隔），中英文标签与账号一并覆盖；
# 值在顿号/逗号/分号/空白处截断，避免把后续内容整段吞掉
_SECRET_VALUE = re.compile(
    r"(?i)((?:password|passwd|secret|api[_-]?key|token|密码|口令|账号|用户名|登录名)"
    r"\s*[=:：]?\s*)([^\s,，;；]{2,})")


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
            note=(res.stdout if parsed.get("proxy_virtual") else
                  f"端口测绘 {step['host']}：开放 {len(findings)}"
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

    async def _step_gap(self, task: Task,
                        step: dict[str, Any] | None = None) -> StepResult:
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
            if d.get("clue_type") == "credential_lead":
                continue    # 已登记的线索不再二次登记（防止“线索的线索”滚雪球）
            # user_reply 的 detail 是提问正文（可能含「密码」字样），只扫答复本身
            blob = (str(d.get("value", "")) if d.get("clue_type") == "user_reply"
                    else f"{d.get('value', '')} {d.get('detail', '')}")
            if _CRED_MARKERS.search(blob):
                found.append({"source": f"clue:{d.get('clue_type', '')}",
                              "where": str(d.get("value", ""))[:160]})
        for rec in self.ctx.blackboard.query(BlackboardLayer.VULN, limit=200):
            d = rec["data"]
            blob = f"{d.get('title', '')} {d.get('endpoint', '')}"
            if _CRED_MARKERS.search(str(blob)):
                found.append({"source": f"vuln:{d.get('vuln_type', '')}",
                              "where": str(d.get("endpoint", ""))[:160]})
        for item in found:
            redacted = _SECRET_VALUE.sub(r"\1***", item["where"])
            # 摘要用哈希：既做幂等去重，又不把明文写进 dedup_key
            digest = hashlib.sha1(item["where"].encode("utf-8")).hexdigest()[:12]
            dedup = f"credential_lead|{item['source']}|{digest}"
            if self.ctx.blackboard.find_by_dedup_key(
                    BlackboardLayer.CREDENTIAL, dedup):
                continue    # 同一证据已登记过
            # 凭据层是本职写权（post_exploit 无 RECON_CLUE 写权限）；
            # 旧实现写线索层，识别到即 PolicyViolation 崩步——一直没暴露
            # 只是因为英文标记没命中过
            self.ctx.blackboard.write(
                BlackboardLayer.CREDENTIAL, new_id("cred"),
                {"kind": "credential_lead", "source": item["source"],
                 "where": redacted, "confidence": 0.6, "redacted": True,
                 "task_id": task.task_id, "found_by": self.agent_id},
                agent_type=self.agent_type, updated_by=self.agent_id,
                dedup_key=dedup)
        redacted_all = [_SECRET_VALUE.sub(r"\1***", f["where"]) for f in found]
        return StepResult(
            ok=True, value=min(0.2 * len(found), 0.8),
            findings=[{"type": "credential_lead", "value": r}
                      for r in redacted_all[:10]],
            note=(f"凭据线索 {len(found)} 条（值打码登记："
                  f"{'; '.join(r[:40] for r in redacted_all[:4])}…）"
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
