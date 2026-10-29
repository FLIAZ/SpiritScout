"""浏览器人工接管工具：显示页面、等待用户确认、最小化后保留会话。"""
from __future__ import annotations

import asyncio
import json
import weakref
from typing import Any
from urllib.parse import urlsplit

from spiritscout.tools.invocation import current_execution_scope
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec

_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def browser_lock(endpoint: str) -> asyncio.Lock:
    """同一服务进程连接同一浏览器的路线，人工操作期间串行访问。"""
    endpoint = endpoint.replace("://localhost:", "://127.0.0.1:").rstrip("/")
    locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(endpoint, asyncio.Lock())


async def window_state(client: Any, code_tool: str, state: str) -> None:
    """只调整当前 Chrome 窗口；不会关闭页面、上下文或浏览器进程。"""
    code = ("async (page) => { const c = await page.context().newCDPSession(page); "
            "try { const w = await c.send('Browser.getWindowForTarget'); "
            "await c.send('Browser.setWindowBounds', {windowId:w.windowId, "
            f"bounds:{{windowState:{json.dumps(state)}}}}}); "
            + ("await page.bringToFront(); " if state == "normal" else "")
            + "return {windowState:" + json.dumps(state) + "}; } finally {await c.detach();} }")
    result = await client.call(code_tool, {"code": code}, timeout=10)
    if result.get("is_error"):
        raise ValueError(str(result.get("text") or "调整浏览器窗口失败")[:500])


def register_handoff(registry: Any, catalog: dict, spec: Any, client: Any,
                     tools: list[dict], questions: Any, audit: Any,
                     lock: asyncio.Lock) -> None:
    """复用 MCP 工具目录和角色权限，模型按需选择人工接管。"""
    code_tool = next((t["name"] for t in tools if t["name"] in {
        "browser_run_code", "browser_run_code_unsafe"}), "")
    if not code_tool or questions is None or not getattr(questions, "interactive", True):
        return
    name = f"mcp__{spec.name}__browser_login"
    description = (
        "打开可见 Chrome 供用户手动登录、扫码或完成验证码；无本机 Chrome 时自动启动专用进程。"
        "工具显示页面并通过会话提问等待用户确认，用户答复后自动最小化窗口，保留浏览器进程与登录态。"
        "调用期间不要并行控制浏览器。不需要再单独 ask_user；返回后重新读取页面验证登录结果，"
        "用户完成操作不等于已确认登录成功。无桌面的远程部署需先提供 src-browser 操作页。")
    schema = {"name": "browser_login", "description": description,
              "inputSchema": {"type": "object", "properties": {
                  "url": {"type": "string", "description": "需要用户操作的 HTTP(S) 页面"},
                  "question": {"type": "string", "description": "告诉用户需要完成的具体操作"}},
                  "required": ["url", "question"]}}

    async def login(url: str, question: str, **_: Any) -> ToolResult:
        if urlsplit(url).scheme not in {"http", "https"} or not question.strip():
            return ToolResult(tool=name, ok=False, error="需要 HTTP(S) 页面及非空操作说明")
        scope = current_execution_scope.get()
        actor = scope.agent_id if scope else "browser"
        async with lock:
            shown = False
            answer: dict = {}
            error = ""
            try:
                result = await client.call("browser_navigate", {"url": url}, timeout=30)
                if result.get("is_error"):
                    raise ValueError(str(result.get("text") or "打开页面失败")[:500])
                await window_state(client, code_tool, "normal")
                shown = True
                audit.log("browser.handoff", actor, {"state": "waiting", "server": spec.name})
                answer = await questions.ask(
                    actor, question.strip() + "\n请在已打开的 Chrome 窗口操作，完成后在这里确认；窗口随后自动最小化。",
                    ["已完成操作", "取消本次操作"])
            except Exception as exc:  # noqa: BLE001 —— 回灌实际失败，避免假报完成
                error = str(exc)[:600]
            finally:
                if shown:
                    try:
                        await asyncio.shield(window_state(client, code_tool, "minimized"))
                        audit.log("browser.handoff", actor, {"state": "minimized", "server": spec.name})
                    except Exception as exc:  # noqa: BLE001 —— 登录输入保留，明确报告窗口处理失败
                        error = (error + "；" if error else "") + "窗口未能最小化：" + str(exc)[:300]
            cancelled = bool(answer.get("cancelled")) or answer.get("answer") == "取消本次操作"
            payload = {"answer": answer.get("answer", ""), "cancelled": cancelled,
                       "login_verified": False, "window_minimized": shown and not error,
                       "browser_preserved": True,
                       "next": "重新读取页面确认实际登录状态，不要将用户答复当作登录成功证据"}
            return ToolResult(tool=name, ok=not error and not cancelled,
                              stdout=json.dumps(payload, ensure_ascii=False), error=error,
                              parsed={**payload, "retryable": False})

    registry.register_builtin(name, login, ToolSpec(
        name=name, kind=ToolKind.MCP, description=description, risk=RiskLevel.READONLY,
        allowed_params={"url", "question"}, automatic_replay=False,
        allowed_agents=tuple(role.value for role in spec.allowed_agents)))
    catalog[name] = {"client": client, "server": spec.name, "tool": "browser_login",
                     "schema": schema, "timeout": float(spec.timeout)}
