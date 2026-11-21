"""从业务页面、响应指纹和公开链接收集资产与侦察线索。"""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.blackboard.leads import (
    assess_authenticity,
    compact_profile,
    response_profile,
    upsert_lead,
)
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, Asset, BlackboardLayer, RunMode, Task

# 命中即判定内容泄露的标记（侦察线索置信度依据）
EXPOSURE_MARKERS = {
    "/.git/HEAD": "ref:",
    "/.git/config": "[core]",
    "/.env": "=",
    "/phpinfo.php": "phpinfo()",
    "/.svn/entries": "svn",
}
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".sql", ".bak", ".7z", ".rar")
_SCRIPT_SRC_RE = re.compile(
    r"(?is)<script\b[^>]*\bsrc\s*=\s*([\"'])(.*?)\1")
_PAGE_PATH_RE = re.compile(
    r"(?is)<(?:a\b[^>]*\bhref|form\b[^>]*\baction|iframe\b[^>]*\bsrc)\s*=\s*([\"'])(.*?)\1")
_API_LITERAL_RE = re.compile(r'''["']((?:/api(?:/|\?|$)|/graphql(?:\?|$))[^"'\s<>]*)["']''', re.I)


def _is_wildcard_target(target: str) -> bool:
    parsed = urlparse(target if "://" in target else f"//{target}")
    return "*" in (parsed.hostname or target)


class ReconAgent(SubAgent):
    agent_type = AgentType.RECON
    route_namespace = "recon"
    # 入口→完整源码→真实路径/脚本是单条证据链，通用早期健康分会把它误剪。
    health_prune_enabled = False

    # ------------------------------------------------------------ 路线模板

    def route_templates(self, task: Task) -> list[RoutePlan]:
        if _is_wildcard_target(task.target):
            return [RoutePlan(
                name="wildcard_asset_handoff",
                strategy="任务契约要求资产发现；交由探微执行 CT、通配基线与范围内候选核验，"
                         "不以根域页面、robots 或路径猜测替代子域清单",
                steps=[{"action": "asset_discovery_required"}],
            )]
        contract = task.payload.get("investigation_contract")
        if (isinstance(contract, dict) and contract
                or getattr(getattr(getattr(self, "ctx", None), "policy", None), "mode", None) == RunMode.SRC):
            return [RoutePlan(
                name="contract_investigation",
                strategy="执行任务书中的具体最小实验，按实际响应和契约判据交付；不以通用页面基线替代",
                steps=[{"action": "investigate"}],
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
                    {"action": "source_path_followup"},
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
            "investigate": self._step_investigate,
            "homepage": self._step_homepage,
            "dns": self._step_dns,
            "fingerprint": self._step_fingerprint,
            "path_probe": self._step_path_probe,
            "robots_discover": self._step_robots_discover,
            "port_probe": self._step_port_probe,
            "llm_path_sweep": self._step_llm_path_sweep,
            "source_path_followup": self._step_source_path_followup,
            "asset_discovery_required": self._step_asset_discovery_required,
        }.get(action)
        if handler is None:
            return StepResult(ok=False, note=f"未知步骤动作: {action}")
        return await handler(task, step)

    def _observe_investigation_tool(self, task: Task, name: str, arguments: dict, result: Any) -> dict:
        """归档模型实际取得的资产与脚本证据，不追加探测或继承 IP 授权。"""
        if not result.ok:
            return {}
        refs = []
        if name == "dns_resolve":
            if result.parsed.get("proxy_virtual"):
                return {}
            host = str(arguments.get("host") or "")
            for ip in result.parsed.get("ips", []):
                record_id, _ = self.ctx.convergence.merge_asset(Asset(
                    asset_id=new_id("asset"), asset_type="ip", value=ip,
                    attrs={"resolved_from": host, "sources": ["dns_resolution"], "scope_inherited": False},
                    source_agent=self.agent_id), self.agent_type)
                refs.append(f"asset:{record_id}")
        elif name == "http_probe":
            url = str(result.parsed.get("final_url") or arguments.get("url") or "")
            record_id, _ = self.ctx.convergence.merge_asset(Asset(
                asset_id=new_id("asset"), asset_type="url", value=url,
                fingerprint=result.parsed.get("fingerprint", {}),
                attrs={"method": str(arguments.get("method") or "GET").upper(),
                       "status_code": result.parsed.get("status_code"), "sources": ["target_response"],
                       "body_ref": result.parsed.get("body_ref"), "body_sha256": result.parsed.get("body_sha256")},
                source_agent=self.agent_id), self.agent_type)
            refs.append(f"asset:{record_id}")
            if str(result.parsed.get("content_type") or "").lower() == "text/html":
                self._publish_script_assets(task, url, result, source=self._response_text(result))
        return {"evidence_refs": refs} if refs else {}

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
        source = self._response_text(res)
        script_findings = self._publish_script_assets(task, url, res, source=source)
        page_paths = self._observed_page_paths(url, source)
        profile = response_profile(source, parsed)
        return StepResult(
            ok=True, value=0.3 if is_new else 0.1,
            findings=[{"type": "asset", "value": url, "title": parsed.get("title")},
                      *script_findings],
            note=(f"首页 {parsed.get('status_code')} title={parsed.get('title')!r}；"
                  f"提取可分析脚本 {len(script_findings)} 个、源码真实路径 {len(page_paths)} 个"),
            cache_writes={f"fingerprint|{url}": {
                "fingerprint": parsed.get("fingerprint", {}),
                "title": parsed.get("title", ""),
                "headers": self._safe_headers(parsed.get("headers", {})),
                "response_profile": profile,
                "page_paths": page_paths,
            }},
        )

    def _response_text(self, result: Any) -> str:
        """优先读 http_probe 已保存的完整正文，不再次请求目标。"""
        body_ref = str(result.parsed.get("body_ref") or "")
        workspace = Path(str(self.ctx.builtin_ctx.get("workspace_root") or ""))
        if re.fullmatch(r"[a-f0-9]{32}", body_ref) and workspace:
            root = (workspace / "http-responses").resolve()
            body = (root / body_ref / "body.txt").resolve()
            if body.parent.parent == root and body.is_file() and not body.is_symlink():
                try:
                    return body.read_text(encoding="utf-8")
                except OSError:
                    pass
        return str(result.stdout or "")

    @staticmethod
    def _observed_page_paths(source_page: str, source: str) -> list[dict[str, str]]:
        """只提取 HTML/内联配置中真实出现的站内入口，不按技术栈猜路径。"""
        origin = urlsplit(source_page)
        observed: dict[str, dict[str, str]] = {}
        for match in _PAGE_PATH_RE.finditer(source):
            raw = match.group(2).strip()
            absolute = urlsplit(urljoin(source_page, raw))
            if (absolute.scheme, absolute.hostname, absolute.port) != (
                    origin.scheme, origin.hostname, origin.port):
                continue
            path = urlunsplit(("", "", absolute.path or "/", absolute.query, ""))
            if path != "/" and ".." not in path:
                observed.setdefault(path, {"path": path[:500], "source": "html_navigation"})
        for match in _API_LITERAL_RE.finditer(source):
            path = match.group(1).strip()
            if ".." not in path:
                observed.setdefault(path, {"path": path[:500], "source": "inline_api_literal"})
        return list(observed.values())[:80]

    def _publish_script_assets(self, task: Task, source_page: str, result: Any, *,
                               source: str | None = None) -> list[dict[str, Any]]:
        """把页面明确引用的脚本交给静态分析；跨域只继承精确 URL 的只读许可。"""
        guard = self.ctx.policy.guard
        evidence_ref = str(result.invocation_id or result.parsed.get("source_invocation_id") or "")
        body_ref = str(result.parsed.get("body_ref") or "")
        findings: list[dict[str, Any]] = []
        for _, raw in _SCRIPT_SRC_RE.findall(source if source is not None else result.stdout or ""):
            absolute = urljoin(source_page, raw.strip())
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            in_scope = guard.is_allowed(absolute)
            decision = "in_scope" if in_scope else "derived_readonly"
            if not in_scope:
                absolute = guard.approve_derived_resource(absolute, source_page)
            finding = {
                "type": "script", "absolute_url": absolute,
                "source_page": source_page, "host": parsed.hostname,
                "scope_decision": decision, "evidence_ref": evidence_ref,
                "body_ref": body_ref,
            }
            findings.append(finding)
            self.ctx.convergence.merge_asset(Asset(
                asset_id=new_id("asset"), asset_type="script", value=absolute,
                attrs={
                    "source": "recon_html", "source_page": source_page,
                    "scope_decision": decision, "evidence_ref": evidence_ref,
                    "body_ref": body_ref, "sources": ["target_response"],
                    "verification_status": "observed",
                    "readonly": True,
                },
                source_agent=self.agent_id,
            ), self.agent_type)
        return findings

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
                attrs={"resolved_from": host, "sources": ["dns_resolution"],
                       "scope_inherited": False},
                source_agent=self.agent_id), self.agent_type)
            findings.append({"type": "ip", "value": ip, "resolved_from": host})
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

    # ------------------------------------------------------------ 页面源码路径接力

    LLM_PATH_CAP = 12          # 兼容旧调用名；实际只筛选页面已观测路径

    async def _step_llm_path_sweep(self, task: Task, step: dict) -> StepResult:
        """兼容旧路线名：不再让模型凭技术栈创造路径。"""
        return await self._step_source_path_followup(task, step)

    async def _step_source_path_followup(self, task: Task, step: dict) -> StepResult:
        """仅验证首页源码真实出现的站内导航/API 路径。"""
        base = task.target.rstrip("/")
        fp = self.cache.get(self.route_namespace, f"fingerprint|{base}") or {}
        paths = [str(item.get("path") or "") for item in fp.get("page_paths", [])
                 if isinstance(item, dict)]
        paths = [path for path in dict.fromkeys(paths)
                 if path.startswith("/") and path not in {"/", "/robots.txt"}
                 and ".." not in path][: self.LLM_PATH_CAP]
        paths = paths[: self.LLM_PATH_CAP]
        if not paths:
            return StepResult(ok=True, value=0.0,
                              note="页面源码没有可接力的真实站内路径；未做字典猜测")
        hits = 0
        notes = []
        for path in paths:
            sub = await self._step_path_probe(task, {"path": path, "origin": "page_source"})
            notes.append(sub.note)
            hits += sum(1 for f in sub.findings if f.get("confirmed"))
        return StepResult(ok=True, value=min(0.3 + 0.4 * hits, 1.0),
                          findings=[{"type": "source_path_followup", "paths": paths,
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
        note = ("资产发现契约不能由页面路径侦察代替；本路线未发送 HTTP、DNS 或端口请求，"
                "等待探微完成 CT、通配基线与具体 hostname 核验后再继续侦察。")
        self._write_clue(
            task, clue_type="asset_discovery_gap", value=task.target,
            confidence=0.95,
            detail={"reason": "asset_discovery_requires_scanner",
                    "next": "等待资产发现交付已核验的具体 hostname"},
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
