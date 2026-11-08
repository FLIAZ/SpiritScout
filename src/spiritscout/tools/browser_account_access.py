"""按需申请已存账号；平台匹配只用于发现，明确答复才授予本次运行的访问。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from spiritscout.resources.browser_accounts import BrowserAccounts, origin
from spiritscout.tools.invocation import current_execution_scope
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec


def register_account_access(registry: Any, catalog: dict, spec: Any,
                            accounts: BrowserAccounts, questions: Any,
                            activate: Callable[[str, str, int], Awaitable[list[str]]]) -> None:
    if questions is None or not questions.interactive:
        return
    name = f'mcp__{spec.name}__account_request'
    description = (
        '默认使用未登录浏览器，不主动枚举账号。只有当前步骤确实依赖登录身份且匿名方式不能满足时，'
        '才说明必要性和现场依据，按平台 URL 精确查找已存账号并请求用户确认。'
        '发现登录页、收到 401/403 或平台存有账号本身不代表需要账号。'
        '多个匹配账号时仅返回名称和身份，选定相关 account_id 后再次调用才能申请；'
        '用户明确选择同意前不加载登录态。拒绝、取消或无答复时不使用、不自动换账号反复申请。'
        '批准仅限当前运行和指定站点，不扩大测试范围，也不代表已登录。密码和 Cookie 不返回模型。')
    properties = {
        'url': {'type': 'string', 'description': '当前需要身份的 HTTP(S) 平台地址'},
        'reason': {'type': 'string', 'description': '为什么当前步骤必须使用此身份，匿名方式为何不足'},
        'evidence': {'type': 'string', 'description': '支持这一判断的当前页面或任务要求，不包含凭据'},
        'account_id': {'type': 'string', 'description': '多个匹配账号时，选择相关身份的账号标识'},
    }
    lock = asyncio.Lock()

    def result(status: str, *, error: str = '', **data: Any) -> ToolResult:
        payload = {'status': status, 'retryable': False, 'login_verified': False, **data}
        return ToolResult(tool=name, ok=not error, error=error,
                          stdout=json.dumps(payload, ensure_ascii=False), parsed=payload)

    async def request(url: str, reason: str, evidence: str, account_id: str = '', **_: Any) -> ToolResult:
        if not questions.interactive:
            return result('unavailable', error='当前会话不能询问用户，不能申请已存账号')
        try:
            site = origin(url)
            if (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 600
                    or not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 600
                    or not isinstance(account_id, str)):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return result('invalid', error='请提供有效平台地址、必要性和现场依据；说明各限 600 字')
        async with lock:
            # 不在注册或会话启动时读取账号；这里只回传匹配平台的公开身份。
            matches = [{k: item[k] for k in ('id', 'name', 'role', 'version')}
                       for item in accounts.list() if site in item['origins']
                       and (not account_id or item['id'] == account_id)]
            if not matches:
                return result('unavailable', origin=site, next='没有匹配账号，继续匿名方式或报告所缺条件')
            if len(matches) > 1:
                return result('choose_account', origin=site, accounts=matches[:20],
                              truncated=len(matches) > 20,
                              next='按当前所需身份选择 account_id 后申请，不要逐个尝试账号')
            account = matches[0]
            scope = current_execution_scope.get()
            actor = scope.agent_id if scope else 'browser'
            answer = await questions.ask(actor,
                f"是否允许本次运行在 {site} 使用已存账号「{account['name']}」（{account['id']}）？\n"
                f"身份：{account['role'] or '未设置'}\n必要性：{reason.strip()}\n依据：{evidence.strip()}\n"
                '仅加载该站点的登录态；不会公开密码或 Cookie，不增加测试目标。停止或重启后需重新申请。'
                '请点击明确的确认选项；输入文字不授予账号权限。',
                ['不使用账号', '仅本次使用此账号'])
            if (answer.get('choices') != [1] or answer.get('answer') != '仅本次使用此账号'
                    or answer.get('cancelled') or answer.get('autonomous')):
                return result('declined', origin=site,
                              next='未获账号授权，继续匿名方式或报告阻塞；不要自动更换账号再次申请')
            try:
                # 用户等待期间可能修改或撤销账号；绑定版本，不能替换为后来保存的身份。
                row = accounts.row(account['id'])
                if row['version'] != account['version'] or site not in row['data']['origins']:
                    return result('changed', error='账号在等待期间已变更，本次确认已失效')
                tools = await activate(account['id'], site, account['version'])
            except (KeyError, ValueError):
                return result('changed', error='账号已变更或不可用，本次未加载')
            except Exception:  # noqa: BLE001 —— 浏览器错误可能含登录态，不回显原文。
                return result('failed', error='账号浏览器未能准备就绪；未确认登录成功，请检查连接')
            return result('approved', origin=site, account_id=account['id'], tools=tools,
                          next='仅在批准的站点使用本次账号浏览器，核对实际身份后再继续')

    registry.register_builtin(name, request, ToolSpec(name=name, kind=ToolKind.MCP,
        description=description, risk=RiskLevel.READONLY, automatic_replay=False,
        allowed_params=set(properties), allowed_agents=tuple(role.value for role in spec.allowed_agents)))
    catalog[name] = {'server': spec.name, 'tool': 'account_request',
                     'schema': {'name': 'account_request', 'description': description,
                                'inputSchema': {'type': 'object', 'properties': properties,
                                                'required': ['url', 'reason', 'evidence']}}}
