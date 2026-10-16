# 浏览器导航被误判为破坏性工具：修复记录

## 根因

本机已安装的 Playwright MCP 将 browser_navigate 标成 action，并给出 readOnlyHint=false、destructiveHint=true。官方 MCP SDK 的 ToolAnnotations 明确说明这些值是提示，destructiveHint 描述可能修改环境，且仅在 readOnlyHint=false 时有意义。

原装配代码把 destructiveHint=true 直接映射为项目的 RiskLevel.DESTRUCTIVE；PolicyEngine 对该级别永久拒绝，于是打开普通网页也得到“G3 禁 DoS”，无法通过用户审批解决。

## 修改

- 增加统一 resolve_mcp_risk：可能修改环境的提示映射为 intrusive，经过现有即时审批；不再凭远端提示将普通状态变化等同于 DoS。
- readOnlyHint=true 时忽略无意义的 destructiveHint；宿主显式指定的 destructive 风险仍禁止，审批也不能覆盖。
- 增加可信配置 tool_risk_overrides，精确匹配单个远端工具名，不提供通配免审。
- 项目实际配置仅将 browser_navigate 映射为 readonly；浏览器默认风险为 intrusive。点击、表单、脚本等非只读操作仍需即时审批。只读导航若声明敏感 action，也不能绕过审批。
- 导航仍经过原有 ScopeGuard 禁打检查，不扩大目标范围或绕过审批。
- 浏览器 MCP 加 --headless，避免官方 SDK 默认环境不传 DISPLAY/WAYLAND_DISPLAY 导致后台启动失败。
- 示例配置同步更新，保持浏览器接入默认不启用。

## 验证

新增 10 项风险映射/运行时装配回归，以及 1 项可选真实 MCP 集成：

- 使用本机已安装的真实 Playwright MCP，经过完整 runtime → Registry → Policy → MCP → 浏览器链路，成功打开本机随机端口测试页，并读到唯一页面标题。
- 禁打 URL 在发送前拒绝，本机服务没有收到对应请求。
- 点击类调用被拒绝审批时不执行，单次批准后执行；其他调用仍重新审批。
- 导航只读特例不能免除 delete_files 等明确敏感 action 的审批。
- 显式 destructive 工具即使审批回调返回允许，也不会执行。
- 分别使用已安装的 Chromium 和 Google Chrome 验证真实导航；没有访问真实测试目标，没有调用收费模型。
- 全套 155 项：151 项通过、4 项 Redis/Docker 条件集成跳过。真实 MCP 集成本轮已开启。Ruff、原有 Node 回归及 git diff --check 通过。

真实浏览器回归可使用已有本机路径运行，不需要测试脚本在线下载依赖：

```bash
SPIRITSCOUT_TEST_MCP_CLI=/已安装的/playwright-mcp \
SPIRITSCOUT_TEST_BROWSER=/已安装的/chrome \
.venv/bin/python -m unittest discover -s tests -p 'test_mcp*.py'
```

## 生效范围

代码和配置均已修改。已经启动的会话持有旧版 ToolSpec 和 MCP 子进程，修改 YAML 不会热改其中的风险分类，也不会把历史失败记录改成成功。

用户明确授权后已重启后端，按原命令、工作目录和环境加载修复，新进程 PID 为 280182。旧进程停止监听后卡在退出清理阶段，SIGTERM 和 SIGINT 未能使其结束，最终终止旧进程并重新启动。

重启后核对：49 条历史会话保留；sess-2fd70b17 的 2 个检查点保留，状态为 interrupted，未自动恢复测试。首页、仪表盘及该会话的 state、recovery、executions、execution-events 接口全部返回 200。继续旧会话应沿用项目现有的检查点重规划机制，不能从原内存位置无缝续跑；历史失败记录仍保留。
