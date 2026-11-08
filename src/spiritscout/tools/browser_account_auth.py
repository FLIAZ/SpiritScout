"""受控凭据填写与二次验证；模型只提供页面定位，永远不接收密码或验证码。"""
from __future__ import annotations

import json
from typing import Any

from spiritscout.resources.browser_accounts import MFA_METHODS
from spiritscout.tools.invocation import current_execution_scope
from spiritscout.tools.spec import RiskLevel, ToolKind, ToolResult, ToolSpec


async def fill(client: Any, values: list[tuple[str, str]], submit: str = '') -> None:
    row = client.accounts.row(client.account_id)
    if row['version'] != client.identity['version']:
        raise ValueError('账号已更新，请重新启动使用此账号的会话')
    sites = client.identity['origins']
    client.protect([value for _, value in values])
    # 校验的是凭据的目的站点，不是渗透测试范围。JSON 编码阻止定位器逃逸为代码。
    code = ('async (page) => { const origins = ' + json.dumps(sites)
            + ', fields = ' + json.dumps(values) + ', submit = ' + json.dumps(submit) + ';' + '''
      const check = async (loc) => {
        if (!origins.includes(new URL(page.url()).origin)) throw new Error('Account site mismatch');
        if (loc && !origins.includes(await loc.evaluate(el => el.ownerDocument.location.origin)))
          throw new Error('Account frame mismatch');
        if (loc && !await loc.evaluate((el, allowed) => !el.form || allowed.includes(new URL(
          el.hasAttribute('formaction') ? el.formAction : el.form.action || el.ownerDocument.URL).origin), origins))
          throw new Error('Account form destination mismatch');
      };
      await check();
      for (const [selector, value] of fields) {
        const loc = await page.locator(selector).elementHandle();
        if (!loc) throw new Error('Account field missing');
        // 绑定已经核对的文档节点，跳转后不会重新解析到另一个站点的同名字段。
        try { await check(loc); await loc.fill(value); } finally { await loc.dispose(); }
      }
      if (submit) {
        const loc = await page.locator(submit).elementHandle();
        if (!loc) throw new Error('Account submit missing');
        try { await check(loc); await loc.click(); } finally { await loc.dispose(); }
      }
      return {filled: fields.length, submitted: Boolean(submit)};
    }''')
    try:
        result = await client.call(client.code_tool, {'code': code}, timeout=30)
        if result.get('is_error'):
            raise ValueError('fill failed')
    except Exception:
        # 原始错误可能包含传给 Playwright 的凭据，不能拼接到工具结果。
        raise ValueError('填写未完成：请检查当前网站、字段和账号版本；没有确认登录成功') from None


def register_account_auth(registry: Any, catalog: dict, spec: Any, client: Any,
                          questions: Any, lock: Any) -> None:
    if not hasattr(client, 'account_id'):
        return

    def result(name: str, *, error: str = '', **data: Any) -> ToolResult:
        data = {'account_id': client.account_id, 'login_verified': False, 'retryable': False, **data}
        return ToolResult(tool=name, ok=not error, error=error,
                          stdout=json.dumps(data, ensure_ascii=False), parsed=data)

    login_name = f'mcp__{spec.name}__account_login'
    verify_name = f'mcp__{spec.name}__account_verify'

    async def login(username_selector: str, password_selector: str,
                    submit_selector: str = '', **_: Any) -> ToolResult:
        async with lock:
            try:
                data = client.accounts.row(client.account_id)['data']
                if not data.get('username') or not data.get('password'):
                    return result(login_name, error='未保存账号密码，请在浏览器账号管理页设置或使用人工登录')
                await fill(client, [(username_selector, data['username']), (password_selector, data['password'])], submit_selector)
                return result(login_name, submitted=bool(submit_selector),
                              mfa_method=data.get('mfa_method', 'none'),
                              next='读取当前页面核对实际登录身份；出现二次验证时使用 account_verify')
            except Exception:
                return result(login_name, error='登录填写失败，请核对当前网站、字段定位及账号设置；不要盲目重试')

    async def verify(code_selector: str, submit_selector: str = '',
                     request_selector: str = '', **_: Any) -> ToolResult:
        if questions is None or not questions.interactive:
            return result(verify_name, error='当前会话不能请求二次验证')
        async with lock:
            try:
                data = client.accounts.row(client.account_id)['data']
                method = data.get('mfa_method', 'none')
                if method == 'none':
                    return result(verify_name, error='请先在账号管理页选择二次验证方式')
                if method == 'authenticator' and request_selector:
                    return result(verify_name, error='验证器动态码由用户的 App 生成，不发送验证码')
                # 先检查凭据目的站点，短信/邮箱按页面按钮请求，不擅自寻找发送接口。
                await fill(client, [], request_selector)
                scope = current_execution_scope.get()
                actor = scope.agent_id if scope else 'browser'
                hint = client.identity.get('mfa_destination_hint', '')
                answer = await questions.ask(actor,
                    f"账号「{data['name']}」（{client.account_id}）需要{MFA_METHODS[method]}{('（' + hint + '）') if hint else ''}。"
                    '请填写本次验证码；仅用于当前浏览器，不保存到对话或经验。', ['取消验证'], secret=True)
                code = answer.get('answer', '')
                if not code or code == '取消验证' or answer.get('cancelled'):
                    return result(verify_name, error='本次验证已取消，登录未确认')
                await fill(client, [(code_selector, code)], submit_selector)
                return result(verify_name, submitted=bool(submit_selector),
                              next='验证材料已填写，请读取页面确认是否成功；未成功时保留失败原因')
            except Exception:
                return result(verify_name, error='二次验证未完成，请检查页面状态；不会自动重发验证码')

    for name, fn, description, required, optional in (
        (login_name, login, '使用此账号加密保存的用户名和密码填写当前登录页；先用页面快照定位字段，不索取或输出密码。',
         ['username_selector', 'password_selector'], ['submit_selector']),
        (verify_name, verify, '为此账号请求二次验证。短信/邮箱可指定页面发送按钮；验证器由用户提供动态码。工具等待用户并直接填写，模型不接收验证码。',
         ['code_selector'], ['submit_selector', 'request_selector']),
    ):
        description = f"账号「{client.identity['name']}」（{client.account_id}）：" + description
        properties = {key: {'type': 'string', 'maxLength': 500} for key in required + optional}
        registry.register_builtin(name, fn, ToolSpec(name=name, kind=ToolKind.MCP,
            description=description, risk=RiskLevel.READONLY, automatic_replay=False,
            allowed_params=set(properties), allowed_agents=tuple(role.value for role in spec.allowed_agents)))
        catalog[name] = {'client': client, 'server': spec.name, 'tool': name.rsplit('__', 1)[-1],
                        'schema': {'name': name.rsplit('__', 1)[-1], 'description': description,
                                   'inputSchema': {'type': 'object', 'properties': properties, 'required': required}},
                        'timeout': float(spec.timeout)}
