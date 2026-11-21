"""会话 MCP 连接、工具注册与后台续握；资源清理由基础设施句柄统一负责。"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spiritscout.config.settings import SystemConfig
from spiritscout.core.audit import AuditLogger
from spiritscout.core.exceptions import ConfigurationError
from spiritscout.core.models import Authorization, RunMode
from spiritscout.tools.diagnostics import (
    browser_tool_guidance,
    chrome_tool_guidance,
    compact_mcp_text,
)
from spiritscout.tools.registry import ToolRegistry
from spiritscout.tools.search_policy import is_search_server, is_search_tool
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec


@dataclass
class MCPRuntime:
    cfg: SystemConfig
    authorization: Authorization
    resources: Any
    workspace: Path
    registry: ToolRegistry
    audit: AuditLogger
    question_broker: Any
    mcp_clients: list[Any]
    mcp_startup_tasks: list[asyncio.Task[Any]]
    mcp_tools: dict[str, dict[str, Any]]
    startup_grace: float
    account_base: Any = field(default=None, init=False)
    accounts: Any = field(default=None, init=False)
    granted_clients: dict[str, Any] = field(default_factory=dict, init=False)

    async def start(self) -> None:
        # 外部 MCP 服务器：握手 + 工具清单并入智能体工具目录（失败留痕跳过，
        # 不阻断开工——MCP 是增益能力而非主链依赖；多台服务器并发握手，
        # 慢服务器不拖累开工时序）
        enabled_specs = [
            s
            for s in self.cfg.mcp_servers
            if s.enabled
            and not (
                self.authorization.mode == RunMode.COMPETITION and is_search_server(s)
            )
        ]
        self.account_base = next(
            (
                s
                for s in enabled_specs
                if list(s.args[:2]) == ["-m", "spiritscout.tools.chrome_mcp"]
                and not s.env.get("SPIRITSCOUT_BROWSER_ACCOUNT")
            ),
            None,
        )
        if not self.cfg.gateway.lightweight_local and self.resources is not None:
            from spiritscout.tools.browser_accounts import session_specs

            enabled_specs = session_specs(
                enabled_specs, self.resources, self.workspace.name
            )
        if enabled_specs:
            if (
                self.account_base is not None
                and self.resources is not None
                and not self.cfg.gateway.lightweight_local
            ):
                from spiritscout.resources.browser_accounts import BrowserAccounts
                from spiritscout.tools.browser_account_access import (
                    register_account_access,
                )

                self.accounts = BrowserAccounts(self.resources)
                self.granted_clients = {}

                register_account_access(
                    self.registry,
                    self.mcp_tools,
                    self.account_base,
                    self.accounts,
                    self.question_broker,
                    self._activate_account,
                )

            # 并发握手但只等一个宽限期：快服务器照常开工前并入；慢/卡死的
            # 转入后台续握、就绪一台并入一台（活目录，智能体下回合可见）
            startup = [asyncio.ensure_future(self._start_mcp(s)) for s in enabled_specs]
            # 先登记后等待：宽限期内遭中断/异常时，这些在握任务同样要取消
            # 回收——若等 wait 返回后才登记，收口路径就会漏掉它们
            self.mcp_startup_tasks.extend(startup)
            done, pending = await asyncio.wait(startup, timeout=self.startup_grace)
            for finished in done:
                self._register_mcp(finished.result())
            if pending:
                self.mcp_startup_tasks.append(
                    asyncio.ensure_future(self._finish_mcp_startup(pending))
                )

    async def _start_mcp(self, spec: Any) -> tuple[Any, Any, list[dict[str, Any]], str]:
        from spiritscout.tools.mcp_client import build_client

        client = None
        try:
            from spiritscout.traffic.proxy import tool_environment

            egress_env = await tool_environment(self.workspace)
            if list(spec.args[:2]) == ["-m", "spiritscout.tools.chrome_mcp"]:
                spec = spec.model_copy(
                    update={
                        "env": {
                            **spec.env,
                            "SPIRITSCOUT_CHROME_OUTPUT_DIR": str(
                                self.workspace / "browser-output" / spec.name
                                if spec.env.get("SPIRITSCOUT_BROWSER_ACCOUNT")
                                else self.workspace / "browser-output"
                            ),
                            "SPIRITSCOUT_CHROME_PROFILE_DIR": (
                                spec.env.get("SPIRITSCOUT_CHROME_PROFILE_DIR")
                                or os.environ.get("SPIRITSCOUT_CHROME_PROFILE_DIR")
                                or str(
                                    Path(
                                        self.cfg.workspace.guard_root
                                        or self.cfg.workspace.root
                                    )
                                    / "browser-profile"
                                )
                            ),
                            "SPIRITSCOUT_CHROME_MODE": (
                                (
                                    "session"
                                    if egress_env.get("SPIRITSCOUT_EGRESS_SOCKET")
                                    else "cdp"
                                )
                                if self.authorization.mode == RunMode.SRC
                                else "headless"
                            ),
                        }
                    }
                )
            from spiritscout.tools.mcp_preflight import current_preflight

            pool = current_preflight.get()
            if egress_env.get("SPIRITSCOUT_EGRESS_SOCKET"):
                spec = spec.model_copy(update={"env": {**spec.env, **egress_env}})
            warmed = (
                await pool.take(spec)
                if pool is not None and not egress_env.get("SPIRITSCOUT_EGRESS_SOCKET")
                else None
            )
            client = warmed[0] if warmed is not None else build_client(spec)
            # 启动前登记所有权；gather 尚未完成时取消也能完整回收。
            self.mcp_clients.append(client)
            tools = warmed[1] if warmed is not None else await client.start()
            # 这里只确认 MCP 目录可用；浏览器连接由首次实际调用验证。
            return spec, client, tools, ""
        except Exception as exc:  # noqa: BLE001 —— 不可达：留痕后继续
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            return spec, None, [], str(exc)[:300]

    def _register_mcp(
        self, result: tuple[Any, Any, list[dict[str, Any]], str], *, late: bool = False
    ) -> None:
        """单台服务器的工具注册：开工前与后台续握完成时共用。"""
        spec, client, tools, err = result
        if client is None:
            self.audit.log(
                "system.lifecycle",
                "runtime",
                {
                    "action": "mcp_server_failed",
                    "server": spec.name,
                    "error": err,
                    "late": late,
                },
            )
            return
        shared_browser = (
            spec.name == "chrome"
            or list(spec.args[:2]) == ["-m", "spiritscout.tools.chrome_mcp"]
        ) and spec.env.get("SPIRITSCOUT_CHROME_MODE", "cdp") == "cdp"
        independent_browser_label = (
            "会话独立的无头浏览器"
            if spec.env.get("SPIRITSCOUT_CHROME_MODE") == "headless"
            else "会话独立浏览器"
        )
        browser_gate = None
        if shared_browser:
            from spiritscout.tools.browser_handoff import (
                browser_lock,
                register_handoff,
            )
            from spiritscout.tools.chrome_mcp import cdp_endpoint

            browser_gate = browser_lock(
                f"account:{self.workspace}:{spec.name}"
                if hasattr(client, "account_id")
                else cdp_endpoint(
                    spec.env.get("SPIRITSCOUT_CHROME_CDP_URL")
                    or os.environ.get("SPIRITSCOUT_CHROME_CDP_URL", "")
                )
            )
            register_handoff(
                self.registry,
                self.mcp_tools,
                spec,
                client,
                tools,
                self.question_broker,
                self.audit,
                browser_gate,
            )
        if hasattr(client, "account_id"):
            from spiritscout.tools.browser_account_auth import (
                register_account_auth,
            )
            from spiritscout.tools.browser_handoff import browser_lock

            browser_gate = browser_lock(f"account:{self.workspace}:{spec.name}")
            client.on_cache_error = lambda message, account=client.account_id: (
                self.audit.log(
                    "system.lifecycle",
                    "browser",
                    {
                        "action": "account_cache_failed",
                        "account_id": account,
                        "message": message,
                    },
                )
            )
            register_account_auth(
                self.registry,
                self.mcp_tools,
                spec,
                client,
                self.question_broker,
                browser_gate,
            )
        for t in tools:
            if self.authorization.mode == RunMode.COMPETITION and is_search_tool(
                t["name"]
            ):
                continue
            anonymous_browser = spec.env.get(
                "SPIRITSCOUT_CHROME_ISOLATED"
            ) == "1" and not hasattr(client, "account_id")
            t = {
                **t,
                "description": (t.get("description") or t["name"])
                + browser_tool_guidance(t["name"])
                + (
                    chrome_tool_guidance(t["name"], isolated=anonymous_browser)
                    if shared_browser
                    else ""
                )
                + (
                    " 当前是会话隔离浏览器，默认未登录。通常无需账号，先判断当前步骤是否确实依赖登录身份；"
                    "仅在匿名方式不足且有现场依据时，搜索 account_request 申请对应平台的已存账号，"
                    "等待用户明确同意；没有申请工具或未获同意时继续匿名方式或报告所缺条件。"
                    if anonymous_browser and t["name"] == "browser_navigate"
                    else ""
                )
                + (
                    f" 当前为{independent_browser_label}，不含用户已有登录态；"
                    "并行处理不同题目时使用各自标签，依据当前标签快照操作。"
                    if spec.name == "chrome" and not shared_browser
                    else ""
                ),
            }
            exposed_name = f"mcp__{spec.name}__{t['name']}"
            tool_spec, model_tool, properties = _tool_contract(spec, t, exposed_name)
            callback = self._call_handler(
                spec, client, t, browser_gate, properties, exposed_name
            )
            self.registry.register_builtin(exposed_name, callback, tool_spec)
            self.mcp_tools[exposed_name] = {
                "client": client,
                "server": spec.name,
                "tool": t["name"],
                "timeout": float(spec.timeout),
                "schema": model_tool,
                "risk": tool_spec.risk.value,
                "target_params": list(tool_spec.target_params),
                "egress_status": (
                    "unverified" if spec.transport in {"http", "sse"} else "isolated"
                )
                if spec.env.get("SPIRITSCOUT_EGRESS_SOCKET")
                else "direct",
                "scope_enforcement": spec.scope_enforcement,
            }
        self.audit.log(
            "system.lifecycle",
            "runtime",
            {
                "action": "mcp_server_ready",
                "server": spec.name,
                "transport": spec.transport,
                "late": late,
                "readiness": "tool_catalog",
                "tools": [t["name"] for t in tools],
            },
        )

    async def _activate_account(self, key: str, site: str, version: int) -> list[str]:
        from spiritscout.tools.browser_accounts import account_spec

        # 只由已收到明确用户答复的入口调用；授权不跨运行保存。
        granted_spec = account_spec(
            self.account_base,
            self.accounts,
            key,
            approved_origin=site,
            expected_version=version,
        )
        client = self.granted_clients.get(granted_spec.name)
        if client is not None:
            if client.identity["version"] != version:
                raise ValueError("账号上下文已失效，请重新启动会话")
        else:
            ready = await self._start_mcp(granted_spec)
            if ready[1] is None:
                raise RuntimeError("账号浏览器未能准备就绪")
            self._register_mcp(ready, late=True)
            self.granted_clients[granted_spec.name] = ready[1]
        return [
            name
            for name, entry in self.mcp_tools.items()
            if entry["server"] == granted_spec.name
        ]

    async def _finish_mcp_startup(self, pending) -> None:
        # 后台续握：逐台注册；取消时静默退出（客户端回收在收口）
        for fut in asyncio.as_completed(pending):
            try:
                self._register_mcp(await fut, late=True)
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001 —— 单台异常不拖累其余
                continue

    def _call_handler(
        self,
        spec: Any,
        client: Any,
        t: dict[str, Any],
        browser_gate: Any,
        properties: dict[str, Any],
        exposed_name: str,
    ):
        async def _call_mcp(
            *,
            _client=client,
            _tool=t["name"],
            _timeout=float(spec.timeout),
            _browser_gate=browser_gate,
            _properties=frozenset(properties),
            _remote_egress_unverified=bool(
                spec.env.get("SPIRITSCOUT_EGRESS_SOCKET")
                and spec.transport in {"http", "sse"}
            ),
            _name=exposed_name,
            **kwargs: Any,
        ) -> ToolResult:
            # ToolRegistry 会追加内置上下文；远端只接收 schema 声明的参数。
            arguments = {k: v for k, v in kwargs.items() if k in _properties}
            if _remote_egress_unverified:
                return ToolResult(
                    tool=_name,
                    ok=False,
                    returncode=-3,
                    error="此远程 MCP 的服务端目标出口尚未验证；工具仍保留，本次调用未发出。可使用会话隔离的本地 stdio MCP。",
                )
            scope_token = None
            account_id = str(getattr(_client, "account_id", "") or "")
            if account_id:
                from spiritscout.tools.invocation import (
                    ExecutionScope,
                    current_execution_scope,
                )

                scope_token = current_execution_scope.set(
                    dataclasses.replace(
                        current_execution_scope.get() or ExecutionScope(),
                        identity_id=account_id,
                    )
                )
            try:
                if _browser_gate is not None:
                    async with _browser_gate:
                        result = await _client.call(_tool, arguments, timeout=_timeout)
                else:
                    result = await _client.call(_tool, arguments, timeout=_timeout)
            except Exception as exc:  # noqa: BLE001 —— 归一为工具失败
                return ToolResult(
                    tool=_name,
                    ok=False,
                    returncode=-2,
                    error=f"{type(exc).__name__}: {exc!s}"[:1000],
                )
            finally:
                if scope_token is not None:
                    current_execution_scope.reset(scope_token)
            failed = bool(result.get("is_error"))
            text, diagnostic = await asyncio.to_thread(
                compact_mcp_text,
                _tool,
                str(result.get("text", "")),
                failed=failed,
                output_dir=self.workspace / "mcp-results",
            )
            reason = text or f"MCP 工具 {_tool} 返回 isError=true，未提供文本诊断"
            if (
                diagnostic.get("failure_summary")
                and diagnostic["failure_summary"] not in reason
            ):
                reason = diagnostic["failure_summary"] + "\n" + reason
            return ToolResult(
                tool=_name,
                ok=not failed,
                returncode=1 if failed else 0,
                stdout=text,
                error=reason if failed else "",
                # 服务已给出确定失败结果，交给模型修参；不原样自动重放。
                parsed={"retryable": False, **diagnostic} if failed else diagnostic,
            )

        return _call_mcp


def _tool_contract(
    spec: Any, t: dict[str, Any], exposed_name: str
) -> tuple[ToolSpec, dict[str, Any], dict[str, Any]]:
    from spiritscout.tools.mcp_client import resolve_mcp_risk

    risk = resolve_mcp_risk(spec, t)
    properties = (t.get("inputSchema") or {}).get("properties") or {}
    target_params = (
        ()
        if spec.scope_enforcement == "none"
        else tuple(p for p in spec.target_params if p in properties)
    )
    # 行为声明与测试范围解耦：危险语义仍按每次调用即时审批。
    declaration = spec.risk_by_action or risk == RiskLevel.INTRUSIVE
    action_param = "_safety_action" if declaration else "action"
    model_tool = copy.deepcopy(t)
    if declaration:
        from spiritscout.tools.spec import ActionCategory

        if action_param in properties:
            raise ConfigurationError(f"MCP 参数 {action_param} 与宿主行为声明冲突")
        schema = model_tool.setdefault("inputSchema", {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})[action_param] = {
            "type": "string",
            "enum": [a.value for a in ActionCategory],
            "description": "声明本次实际行为，而非工具能力：普通浏览、点击、授权登录、"
            "只读探测与查询选 analysis；改写业务数据、上传、重启服务等选 "
            "intrusive_write；删除文件、爆破、反弹连接、WebShell、容器逃逸选对应"
            "类别，执行前逐次审批。与原生 action 独立。",
        }
        schema["required"] = list(
            dict.fromkeys([*schema.get("required", []), action_param])
        )
    purpose_param = "_approval_purpose"
    while purpose_param in properties or purpose_param == action_param:
        purpose_param += "_"
    model_tool.setdefault("inputSchema", {}).setdefault("properties", {})[
        purpose_param
    ] = {
        "type": "string",
        "description": "需要用户审批时必填：用自然语言说明针对什么目标、"
        "执行什么操作、为什么需要，以及可能影响。不能只写工具名。此字段只供宿主展示，不传给工具。",
    }
    tool_spec = ToolSpec(
        name=exposed_name,
        kind=ToolKind.MCP,
        internet_search=is_search_server(spec) or is_search_tool(t["name"]),
        description=t.get("description") or t["name"],
        risk=risk,
        timeout_seconds=max(1, int(spec.timeout)),
        allowed_params=set(properties) | ({action_param} if declaration else set()),
        action_required=declaration,
        action_param=action_param,
        approval_purpose_param=purpose_param,
        automatic_replay=not declaration,
        target_params=target_params,
        allowed_agents=tuple(role.value for role in spec.allowed_agents),
    )

    return tool_spec, model_tool, properties
