# 最新会话「工具执行失败：未知错误」定位与修复

## 排查对象与证据

会话 sess-70a90310，已停止。读取 audit.jsonl 和执行台账的加密结果（只读解密，不改日志或密钥）后，确认 7 次已结束工具失败：

| 工具 | 失败调用数 | 实际诊断 | 每次实际尝试 |
| --- | ---: | --- | ---: |
| browser_navigate | 5 | Invalid arguments；url 必须是字符串，实际为 undefined | 2 |
| browser_tabs | 1 | action 必须是 list/new/close/select | 1 |
| browser_evaluate | 1 | function 必须是字符串，实际为 undefined | 1 |

7 次调用的参数快照全是空对象 `{}`。导航失败发生在 2026-10-16 17:08:36 至 17:09:49（北京时间）这一段。共发生 12 次远端失败尝试；重复调用没有补充参数，不能修复此错误。

## 根因

1. 业务 Agent 的 `_tool_schemas()` 把 MCP 工具整份描述当成模型的 `parameters`。整份描述包含 name、description、inputSchema、annotations，真正的参数定义在 inputSchema 下。模型收到的顶层没有 url/function/action 的 properties 和 required。同时 description 从字符串 tool 上用 getattr 取值，导致描述也为空。
2. 远端明确返回 `isError=true`，其具体诊断在文本正文。runtime 把正文放进 ToolResult.stdout，却没有填 error；业务 Agent 失败分支只读 error，因而把已有的错误统一替换成“未知错误”。模型和前端都看不到该如何修参。
3. Registry 对 readonly 导航默认自动重试一次，重复相同空参数；这是确定性参数错误，重复请求没有价值。
4. 业务 Agent 原来不检查导航是否成功就登记页面。当前这 7 次失败因参数空、URL 空而没有登记页面，但有 URL 的失败调用存在污染证据的风险，已加成功条件。

根因在工具契约与结果传递链路。本次没有修改风险分类、授权范围或即时审批规则。

## 修复

- 模型参数定义取 `schema.inputSchema`，描述取 `schema.description`；保留原参数类型、必填项、枚举和嵌套结构。
- runtime 为 MCP 的 isError 返回填充具体 error，同时保留原 stdout；无正文时也提供工具名和协议失败状态。
- ToolResult.failure_reason() 支持 error → stderr → stdout → 超时/退出码回退，业务 Agent 使用统一诊断。
- MCP 服务明确返回失败时标记不可自动原样重试/降级；模型仍可根据诊断修改参数，发起下一次新调用。
- 非法 JSON 参数直接反馈，不派发远端工具；异常保留异常类型。
- 只有成功导航才记录页面证据。
- 后续 agent.tool_result 审计补充 tool、invocation_id、returncode、attempts，便于关联模型工具调用和加密执行结果；参数依旧不以明文进入审计。

## 验证

新增 7 项回归：模型参数 schema 与描述正确、原生 action 枚举保留、错误正文回灌、失败导航不记证据、成功导航去重、非法 JSON 不执行、错误回退、MCP 明确失败不重试及无正文诊断等断言。

扩展原有真实 MCP 集成，进入生产 BusinessContextAgent 工具环，而不是只调用 Registry：

1. 检查传给模型的真实浏览器参数定义包含 type、properties、required.url，且不再嵌套整份 MCP 描述。
2. 故意缺少 url：真实 Playwright MCP 返回参数错误，回灌内容包含 url 与 Invalid arguments，实际尝试为 1，没有新增页面证据。
3. 下一轮修正参数：真实浏览器打开本机随机端口 /business，返回唯一页面标题，黑板新增 1 条页面证据。
4. 禁打 URL 仍在请求前拒绝，测试审批通道未被绕过。

模型决策部分使用脚本桩，MCP SDK、服务进程、Registry、Policy、BusinessContextAgent 与 Chromium 均真实运行；没有调用收费模型或真实测试目标。

全套 172 项：168 项通过、4 项 Redis/Docker 条件集成跳过。Ruff、原有 observer/dashboard Node 回归和 git diff --check 通过。

## 生效与历史记录

确认无活动会话后已优雅重载后端，新进程 PID 348654。50 个历史会话、最新会话的 2 个检查点和 2 条未决记录均保留；仪表盘、会话状态和档案接口返回 200。没有自动恢复任务或重放未决调用。

原历史失败审计保持不变，未重写成成功或改动旧调用参数；修复作用于后续新运行或检查点重规划。
