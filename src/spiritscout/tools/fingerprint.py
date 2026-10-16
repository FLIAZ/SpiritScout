"""HTTP 技术线索：只记录实际响应头、标签和资源路径，不猜测服务端技术。"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit


class _TechnologyTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.evidence: list[dict[str, str]] = []

    def add(self, name: str, source: str, evidence: str) -> None:
        item = {"name": name[:160], "source": source, "evidence": evidence[:240]}
        if item not in self.evidence:
            self.evidence.append(item)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag == "meta" and str(data.get("name") or "").lower() == "generator" and data.get("content"):
            self.add(str(data["content"]), "meta_generator", "meta[name=generator]")
        if tag == "script" and data.get("id") in {"__NEXT_DATA__", "__NUXT_DATA__"}:
            self.add("Next.js" if data["id"] == "__NEXT_DATA__" else "Nuxt", "script_id", str(data["id"]))
        if tag not in {"script", "link"}:
            return
        resource = str(data.get("src") if tag == "script" else data.get("href") or "")
        try:
            path = urlsplit(resource).path.lower()
        except ValueError:
            return
        for marker, name in (("/_next/static/", "Next.js"), ("/_nuxt/", "Nuxt"),
                             ("/wp-content/", "WordPress"), ("/wp-includes/", "WordPress"),
                             ("/vue.global", "Vue"), ("/react-dom", "React")):
            if marker in path:
                self.add(name, "resource_path", marker)


def web_fingerprint(headers: dict[str, Any], body: str) -> dict[str, Any]:
    headers = {key.lower(): value for key, value in headers.items()}
    result: dict[str, Any] = {}
    for field, header in (("server", "server"), ("x_powered_by", "x-powered-by")):
        if headers.get(header):
            result[field] = headers[header]
    content_type = str(headers.get("content-type") or "").lower()
    if ("text/html" in content_type or "application/xhtml+xml" in content_type
            or body.lstrip().lower().startswith(("<!doctype html", "<html"))):
        parser = _TechnologyTags()
        parser.feed(body)
        if parser.evidence:
            result["technologies"] = parser.evidence
    return result
