"""业务体验 Agent：先走真实流程，再把有来源的业务上下文交给安全测试。"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from spiritscout.agents.sub.base import RoutePlan, StepResult, SubAgent
from spiritscout.core.ids import new_id
from spiritscout.core.models import AgentType, Asset, BlackboardLayer, Task

_SKIP_LINK_WORDS = (
    "logout", "log-out", "signout", "delete", "remove", "destroy",
    "checkout", "purchase", "pay", "submit", "confirm", "注销", "退出",
    "删除", "支付", "下单", "提交",
)
_SAFE_HEADERS = ("content-type", "server", "x-powered-by", "location", "allow")


def _redacted_url(raw: str) -> str:
    """保留 URL 结构和查询参数名，移除用户信息与参数值。"""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return raw
    try:
        port = parsed.port
    except ValueError:
        return raw
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    names = [(name[:128], "") for name, _ in parse_qsl(
        parsed.query, keep_blank_values=True) if name]
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/",
                       urlencode(names), ""))


class _SurfaceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.headings: list[str] = []
        self.labels: list[str] = []
        self.links: list[dict[str, str]] = []
        self.forms: list[dict[str, Any]] = []
        self._text_target = ""
        self._text: list[str] = []
        self._link_href = ""
        self._link_text: list[str] = []
        self._form: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag in {"title", "h1", "h2", "h3", "label", "button"}:
            self._text_target = tag
            self._text = []
        elif tag == "a":
            self._link_href = values.get("href", "")
            self._link_text = []
        elif tag == "form":
            self._form = {
                "method": values.get("method", "GET").upper(),
                "action": values.get("action", ""),
                "fields": [],
            }
        elif tag in {"input", "select", "textarea"} and self._form is not None:
            name = values.get("name", "").strip()
            if name:
                self._form["fields"].append({
                    "name": name[:128],
                    "type": values.get("type", tag).lower()[:32],
                })

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text:
            return
        if self._text_target:
            self._text.append(text)
        if self._link_href:
            self._link_text.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._text_target:
            text = " ".join(self._text)[:160]
            if tag == "title":
                self.title = text
            elif tag in {"h1", "h2", "h3"} and text:
                self.headings.append(text)
            elif text:
                self.labels.append(text)
            self._text_target = ""
            self._text = []
        if tag == "a" and self._link_href:
            self.links.append({
                "href": self._link_href[:500],
                "text": " ".join(self._link_text)[:160],
            })
            self._link_href = ""
            self._link_text = []
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


def extract_surfaces(html: str, page_url: str) -> dict[str, Any]:
    """提取页面公开语义与表单结构，不保留字段值、正文或凭据。"""
    parser = _SurfaceParser()
    parser.feed(html[:2_000_000])
    origin = urlsplit(page_url)
    links: list[dict[str, str]] = []
    for item in parser.links:
        absolute = urljoin(page_url, item["href"])
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != origin.netloc:
            continue
        clean = _redacted_url(absolute)
        if not any(existing["url"] == clean for existing in links):
            links.append({"url": clean, "text": item["text"]})
    forms: list[dict[str, Any]] = []
    for form in parser.forms:
        absolute = urljoin(page_url, str(form["action"]) or page_url)
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != origin.netloc:
            continue
        forms.append({
            "action": urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", "")),
            "method": form["method"] if form["method"] in {
                "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"
            } else "GET",
            "fields": form["fields"][:32],
        })
    return {
        "url": _redacted_url(page_url),
        "title": parser.title,
        "headings": parser.headings[:20],
        "labels": parser.labels[:30],
        "links": links[:50],
        "forms": forms[:20],
    }


class BusinessContextAgent(SubAgent):
    agent_type = AgentType.BUSINESS_CONTEXT
    route_namespace = "business_context"
    health_prune_enabled = False
    _MAX_PUBLIC_PAGES = 8

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._observations: dict[str, list[dict[str, Any]]] = {}

    def route_templates(self, task: Task) -> list[RoutePlan]:
        return [RoutePlan(
            name="experience_normal_flow",
            strategy="交付可核验结果：以普通用户视角走访公开流程，产出每条结论都"
                     "能引用证据的业务上下文（角色/对象/流程/信任边界），证据不足"
                     "的如实进 unknowns",
            steps=[
                {"action": "observe_entry"},
                {"action": "walk_public_flow"},
                {"action": "build_context"},
            ],
        )]

    async def execute_step(self, task, route, plan, step) -> StepResult:
        handler = {
            "observe_entry": self._observe_entry,
            "walk_public_flow": self._walk_public_flow,
            "build_context": self._build_context,
        }.get(step["action"])
        if handler is None:
            return StepResult(ok=False, note=f"未知业务体验步骤: {step['action']}")
        return await handler(task)

    async def _fetch_page(self, task: Task, url: str) -> dict[str, Any] | None:
        res = await self.ctx.tools.call(
            "http_probe", {"url": url}, **self.ctx.builtin_ctx)
        if not res.ok:
            return None
        content_type = str((res.parsed.get("headers") or {}).get("content-type", ""))
        if "html" not in content_type.lower() and "<html" not in res.stdout[:1000].lower():
            return None
        surface = extract_surfaces(res.stdout, url)
        surface["status_code"] = res.parsed.get("status_code")
        headers = res.parsed.get("headers") or {}
        surface["headers"] = {
            key: value for key, value in headers.items()
            if str(key).lower() in _SAFE_HEADERS
        }
        return surface

    async def _observe_entry(self, task: Task) -> StepResult:
        url = task.target.rstrip("/") or task.target
        surface = await self._fetch_page(task, url)
        if surface is None:
            self._observations[task.task_id] = []
            return StepResult(ok=False, note="入口页面不可读，业务流程覆盖为空")
        self._observations[task.task_id] = [surface]
        self._store_surface(surface)
        return StepResult(
            ok=True,
            value=0.4,
            findings=[{"type": "business_entry", "value": surface["url"]}],
            note=(f"已体验入口：{surface['title'] or surface['url']}；"
                  f"发现 {len(surface['links'])} 个导航、{len(surface['forms'])} 个表单"),
        )

    async def _walk_public_flow(self, task: Task) -> StepResult:
        observations = self._observations.setdefault(task.task_id, [])
        if not observations:
            return StepResult(ok=True, note="入口无可跟随的公开流程")
        seen = {item["url"] for item in observations}
        queue = [link for item in observations for link in item["links"]]
        while queue and len(observations) < self._MAX_PUBLIC_PAGES:
            link = queue.pop(0)
            url = str(link["url"])
            text = f"{url} {link.get('text', '')}".lower()
            if url in seen or any(word in text for word in _SKIP_LINK_WORDS):
                continue
            seen.add(url)
            surface = await self._fetch_page(task, url)
            if surface is None:
                continue
            observations.append(surface)
            self._store_surface(surface)
            queue.extend(surface["links"])
        return StepResult(
            ok=True,
            value=min(0.2 + len(observations) * 0.08, 0.8),
            findings=[{"type": "business_page", "value": p["url"]}
                      for p in observations],
            note=f"公开流程走访完成：实际访问 {len(observations)} 个页面",
        )

    def _store_surface(self, surface: dict[str, Any]) -> None:
        page = Asset(
            asset_id=new_id("asset"),
            asset_type="app",
            value=str(surface["url"]),
            attrs={
                "kind": "business_page",
                "title": surface["title"],
                "headings": surface["headings"],
                "labels": surface["labels"],
                "status_code": surface["status_code"],
                "headers": surface["headers"],
            },
            source_agent=self.agent_id,
        )
        self.ctx.convergence.merge_asset(page, self.agent_type)
        for form in surface["forms"]:
            endpoint = Asset(
                asset_id=new_id("asset"),
                asset_type="endpoint",
                value=str(form["action"]),
                attrs={
                    "method": form["method"],
                    "params": [f["name"] for f in form["fields"]],
                    "param_locations": {
                        f["name"]: "query" if form["method"] in {"GET", "HEAD"}
                        else "body"
                        for f in form["fields"]
                    },
                    "source": "observed_html_form",
                },
                source_agent=self.agent_id,
            )
            self.ctx.convergence.merge_asset(endpoint, self.agent_type)

    async def _build_context(self, task: Task) -> StepResult:
        observations = self._observations.get(task.task_id, [])
        endpoint_records = [
            record["data"] for record in self.ctx.blackboard.query(
                BlackboardLayer.ASSET, limit=1000)
            if record["data"].get("asset_type") == "endpoint"
        ]
        endpoint_records.sort(key=lambda item: (
            int((item.get("attrs") or {}).get("first_seen_index", 1_000_000)),
            str(item.get("value", "")),
        ))
        evidence: list[dict[str, Any]] = []
        for page in observations:
            evidence.append({
                "id": f"E{len(evidence) + 1}", "kind": "page",
                "url": page["url"], "title": page["title"],
                "headings": page["headings"], "labels": page["labels"],
            })
        for endpoint in endpoint_records[:100]:
            attrs = endpoint.get("attrs") or {}
            source_file = attrs.get("source_file", "")
            evidence.append({
                "id": f"E{len(evidence) + 1}",
                "kind": "har_transaction" if source_file else "observed_form",
                "method": attrs.get("method", "GET"),
                "url": endpoint.get("value", ""),
                "params": attrs.get("params", []),
                "status_code": attrs.get("status_code"),
                "sequence": (attrs.get("first_seen_index") if source_file else None),
                "source": attrs.get("source") or source_file,
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
        form_count = sum(len(page["forms"]) for page in observations)
        coverage = (
            "observed_transaction_flow" if har_count >= 2
            else "public_flow" if len(observations) > 1 or form_count
            else "entry_only" if observations
            else "none"
        )
        gaps: list[str] = list(context.get("unknowns", []))
        if har_count == 0:
            gaps.append("未提供脱敏 HAR，登录后或多角色交易流程尚未被观测")
        if not observations:
            gaps.append("入口页面不可读")
        context.update({
            "kind": "business_context",
            "coverage": coverage,
            "ready_for_testing": bool(endpoint_records),
            "observed_pages": len(observations),
            "observed_endpoints": len(endpoint_records),
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
            note=(f"业务上下文：{coverage}，页面 {len(observations)}，"
                  f"端点 {len(endpoint_records)}，HAR 交易 {har_count}；"
                  f"缺口 {len(context['gaps'])} 项"),
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
