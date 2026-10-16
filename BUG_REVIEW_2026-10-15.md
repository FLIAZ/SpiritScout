# SpiritScout 项目缺陷审查与修复方案

审查日期：2026-10-15。对象为当前工作区，包括原有未提交改动。本次没有修改业务代码；配置 API 定向复现和 wheel 构建使用临时目录，没有对现有 Web 服务执行写请求。临时复现使用隔离工作区、伪造 HTTP 传输和无害标记，不访问赛题、不调用收费模型、不执行真实危险操作。现有 unittest 按仓库命令执行，部分测试会创建其配置指向的运行期数据。

## 审查结论与证据范围

发现 21 项需要修复的问题，另列 3 项后续验证项。覆盖启动装配、主/子 Agent 调度、审批与范围控制、工具执行、CTF Flag 流转、Web API、前端事件流、模型配置、计量、持久化及构建分发。

这是一轮代码审查和定向复现，不能据此保证不存在其他缺陷。真实云模型、外部 MCP、Redis/NATS、Docker 守护进程和完整实战任务未做端到端验收。

验证结果：

- `.venv/bin/python -m unittest discover tests/`：57 项，53 项通过，1 项失败，3 项报错。
- `.venv/bin/python -m ruff check src/ tests/`：虚拟环境没有 ruff 模块；使用系统 `ruff check src/ tests/` 检查通过。
- 三个前端 JS 文件的 `node --check` 均通过。
- 在临时副本上离线构建 wheel 成功；压缩包中只有 `web/static/index.html`，没有 JS/CSS 和 Jinja 报告模板。
- HTTP、配置、事件流、DAG、审批取消、沙箱取消、SRC 收敛、计量时间、损坏元数据等问题已做定向复现。
- Playwright 检测到本地 8000 端口；有界面 Chromium 因没有 XServer/`DISPLAY` 启动失败。没有把浏览器未完成的检查计作通过。前端事件属性问题使用源码提取、实体解码及 JavaScript 执行做了局部复现，仍需浏览器回归。

优先级说明：P1 为可能越过明确授权边界、使主要功能失效或破坏任务执行正确性；P2 为局部功能、数据准确性或异常处理问题。

## 问题清单与修复方案

### B01 · P1 · 危险操作没有可靠接入即时审批

位置：`tools/builtin.py` 的 `BUILTIN_SPECS`；`ctf/agents.py:1557`；`policy/engine.py:61`；`policy/scope.py:133`。

`sandbox_exec` 被整体标记为 readonly，CTF 的 shell/python 调用只传代码，不携带可供策略判断的行为类别。删除文件等危险行为不会因脚本内容自动进入 ApprovalBroker；`http_probe` 同样把任意 HTTP 方法统一标为 readonly。另外，`allow_sensitive_actions=True` 会直接让敏感操作绕过审批。

证据：在拒绝型审批回调下，设置 `allow_sensitive_actions=True` 后调用 intrusive 工具、动作 `reverse_shell`，仍放行且审批回调调用次数为 0。普通 `sandbox_exec` 也不会触发审批。没有运行真实反弹或删除操作。

修复：让模型在工具决策中给出操作意图、风险和目标，策略层落实既有审批结果；为 shell/python 和有副作用 HTTP 操作接入统一审批入口。无法确认行为性质的通用执行入口应明确请示，不能以 readonly 自动放行。移除启动参数的一揽子免审效果；复用会话中已明确批准的相同动作时，应符合约定的审批粒度。删除动作必须纳入覆盖。不要用后端关键词模板代替模型决策，也不要把所有 shell 永久归为 destructive 而完全禁用。

验收：普通只读分析正常执行；危险动作在执行前出现审批；拒绝时执行器未被调用；`allow_sensitive=true` 不能关闭即时审批。

### B02 · P1 · HTTP 重定向绕过禁打列表

位置：`tools/builtin.py:67` 至 `99`。

Registry 只校验初始 URL；`http_probe` 跟随 Location 时直接请求下一个地址，没有逐跳调用 `ensure_not_denied`。最终的 `is_allowed` 仅做标注，检查发生时请求已经发送。

证据：授权 `allowed.test`、禁打 `denied.test`，模拟前者 302 到后者。结果 `ok=True`，记录到两次请求，第二次到达禁打域名。

修复：在每次 `client.request` 之前检查当前 URL；将禁打异常归一为工具拒绝结果。保留“白名单外允许探索并标注”的现有语义，不能把白名单恢复成硬围栏。

验收：首跳、第二跳和多跳命中禁打时，都不得向被禁目标发出请求；正常跨站跳转仍保留范围说明和认证头清理行为。

### B03 · P1 · 禁打 CIDR 不生效

位置：`policy/scope.py:145`。

禁打规则只做 URL 前缀和主机 fnmatch，没有把 CIDR 解析成地址网络。`10.0.0.0/8` 无法匹配 `10.1.2.3`。

证据：`ScopeGuard(Scope(allowed_domains=['allowed.test'], denied=['10.0.0.0/8'])).ensure_not_denied('10.1.2.3')` 未抛异常。

修复：区分 URL、主机通配与 IPv4/IPv6 CIDR，对 IP 执行网络成员判断；DNS 解析后的连接地址也要检查，否则以域名访问禁打 IP 仍可能绕过。要处理多 A/AAAA 地址及解析到实际连接之间的变化。

验收：IPv4/IPv6 网络、单 IP、主机通配和 URL 路径边界分别覆盖；不相关目标不能误拒。

### B04 · P1 · 模型 API 可把密钥字段原样写进 YAML 并回传

位置：`web/modelcfg.py:176`；`web/app.py` 的模型新增与列表端点。

`upsert_entry` 只删除 `api_key`，保留 `api_key_value` 等任意未知字段；Pydantic 校验后写回的仍是原始字典。供应商路径也应一起审计未知字段透传。

证据：POST `/api/models`，传入合法 model/provider_ref 及 `api_key_value: harmless-marker`，返回 201；该标记同时出现在配置文件及 GET `/api/models` 响应中。

修复：输入使用明确模型并拒绝未知字段，或把验证后的规范字段重新序列化；模型端点禁止接收任何密钥字段。供应商密钥仅经 vault 写入，读 API 应显式选择公开字段，不直接返回原始 YAML。对已有污染配置给出可控清理方案，避免再次在日志中打印秘密。

验收：模型接口收到密钥字段返回 422；供应商接口只保留加密密文；API 和 YAML 都找不到明文标记。

### B05 · P1 · 内联 onclick 可被 HTML 实体突破

位置：`web/static/js/console.js:77`、`109`。

`jsArg` 转义了单引号和 `<`，但没有处理 `&`。值进入 HTML 属性后，`&quot;` 会被浏览器解码成双引号，改变原本 JSON 字符串的 JavaScript 语义。模型名可来自供应商模型列表，供应商名称也是可变数据。

局部证据：模型名为 `&quot;);globalThis.auditMarker=1;//` 时，属性解码后成为 `modelTest("");globalThis.auditMarker=1;//")`，JavaScript 执行使无害内存标记变为 1。真实浏览器点击回归尚未完成。

修复：移除动态内联事件处理器，使用 `data-*` 与 `addEventListener`/事件委托；属性值按 HTML 上下文编码。不要继续叠加手写 JavaScript 引号替换。

验收：引号、`&quot;`、`&#34;`、`&#39;`、换行及 Unicode 模型名只作为数据；编辑、测试、删除按钮不执行数据中携带的代码。

### B06 · P1 · 停止会话后沙箱子进程仍运行

位置：`tools/sandbox.py:226`；同类清理缺口见 `tools/executor.py:86`。

执行器只在超时路径杀进程，没有在 `CancelledError` 路径清理。在停止会话时，上层任务被取消，宿主应用仍存活，`--die-with-parent` 不会替代取消清理。

证据：在隔离临时工作区运行 `sleep 1; printf audit > orphan-marker`，0.2 秒后取消 `sandbox_exec`；随后文件仍被创建，证明取消返回后命令继续执行。

修复：在取消与异常路径杀进程组、等待回收、取消并回收输出读取任务、关闭日志句柄，然后重新抛出取消。LocalExecutor 应创建并管理进程组；DockerExecutor 取消时显式停止命名容器。

验收：停止/关停/超时后均无子进程或容器残留，取消后不会出现新的产物；普通结束仍保留输出。

### B07 · P1 · SRC 节点执行完就硬收敛，跳过模型复盘

位置：`agents/master/agent.py:760` 至 `788`。

代码先在 DAG 完成、流水线清空时设置 `stop_reason`，随后只有 `not stop_reason` 才调用 `_llm_review`。因此后面声称 SRC 应交给模型复盘的注释并未落实，零漏洞也可能直接结束。

证据：构造已完成 DAG、无 Flag、空流水线，令 `_llm_review` 明确要求继续；实际返回“DAG 全部完成且核验流水线清空”，模型复盘调用次数为 0。

修复：把“当前节点跑完”作为态势输入；模型决定任务是否达成、是否补节点及下一步方向。确定性数据为空不应替代目标完成判断。

验收：同样的已完成 DAG 可按模型判断继续或结束；模型不可用要有明确可见的失败/等待语义。

### B08 · P1 · 主循环仍保留固定回合顶

位置：`config/settings.py` 的 `iteration_max_rounds=20`；`runtime.py:438`；`agents/master/agent.py:760`、`152`。

虽然子 Agent 调查环移除了回合顶，主 Agent 仍在 `next_round >= max_rounds` 时强制结束。LangGraph 调用也未处理长期循环的框架递归限制。

证据：代码直接输出“轮次上限”；用持续返回继续的节点运行同一图结构，最终出现 `GraphRecursionError`。当前环境实际框架阈值较大，不能按旧版“默认 25 步”估算。

修复：移除业务固定轮次终止条件及其有效配置传递。采用可持续分段执行/保存状态的调度方式，消除框架递归上限对业务收敛的影响；不要仅把数字改大或改成另一个预算熔断。

验收：模型连续选择继续时可越过原 20 轮；用户停止和模型主动结束仍有效。

### B09 · P1 · 纯附件 CTF 无法进入执行

位置：`launch.py` 的附件题装配；`web/sessions.py:943`；`policy/scope.py:34`。

只有附件的赛题会生成 challenge，但网络授权三项均为空；Web 层将其认定为缺少目标并转成对话，直接 runtime 入口则被 ScopeGuard 拒绝。

证据：`prepare_launch(LaunchRequest(mission='分析附件中的密码题', has_uploads=True))` 得到 CTF challenge，同时 `scope.is_empty()` 为 True。创建流程只挂载附件路径，没有补充离线授权表达。

修复：在授权数据中表达“已授权分析这些本地附件/工作区”；将本地文件任务与网络测试的启动要求区分。不要塞入虚假的域名、伪 URL 或扩大网络范围来绕过空范围校验。

验收：无 URL 的密码/逆向/杂项附件可直接分析；未提供目标也未提供附件的网络任务仍应询问用户。

### B10 · P2 · SSE 游标跳号导致断线续传漏事件

位置：`web/events.py:73`。

`data.split('\n')` 在完整换行结尾产生空字符串；循环把它计入 cursor，但它不是文件中的下一行，后续事件 ID 高于真实行号。重连却以真实行数跳过，造成漏读。

证据：先写第一行再追加第二行，得到事件 ID 1、3；从 ID 3 重连会直接读到 done。若断线时真实第三行已写入，它也会被跳过。

修复：只对实际完整行增加游标；半行以字节缓冲处理，断点定位与输出 ID 使用同一计量方式。避免把文本流 tell 的 cookie 当作一般字节偏移进行算术运算。

验收：分批追加、中文、尾部半行、多次重连后，每条真实记录正好消费一次。

### B11 · P1 · DAG 前向依赖丢失，失败重建仍可能绑定旧节点

位置：`agents/master/dag.py:36`。

单遍入图只查当前已存在的战术。模型先输出 CHILD 再输出 PARENT 时，CHILD 的依赖被静默丢弃；多个同战术节点出现时，还总选第一个依赖，可能继续绑到失败的旧节点。

证据：导入 `[CHILD depends_on PARENT, PARENT]` 后，两者的 depends_on 都为空，并且同时 ready。

修复：分两遍创建节点和解析依赖；未知依赖明确返回规划错误；定义重建节点的依赖选择与旧边重绑定规则；校验环、自依赖和节点标识冲突。

验收：输出顺序不影响执行顺序；失败后重排可解锁下游；循环依赖不会静默死等。

### B12 · P2 · 一个审批/提问等待者取消，会取消所有共用等待者

位置：`policy/approvals.py:38`、`51`；`policy/questions.py:50`、`69`。

多个调用直接 await 同一个 Future，任意 await 者取消会把共享 Future 一并取消；创建者的 finally 随后删除待办，其他分支失去审批/答复。

证据：两协程等待相同 action，取消第二个；第一个也进入 cancelled，待办列表变为空。

修复：共享裁决 Future 用 `asyncio.shield` 隔离单个消费者取消；增加等待者引用管理，待办生命周期由 Broker 管理，避免创建者退出就移除仍有人等待的问题。整体停止时仍必须统一唤醒/清理。

验收：取消一个分支后，其他分支仍可收到用户裁决；会话停止不会遗留待办。

### B13 · P2 · 带斜杠的模型 ID 无法编辑或删除

位置：`web/app.py:805`、`815`；前端 `console.js:227`、`245`。

前端 encodeURIComponent 后，ASGI 路由仍会解码 `%2F` 成路径分隔符；普通 `{model}` 参数不匹配 `org/model`，而这类 ID 可以正常新增。

证据：PUT `/api/models/org%2Fmodel` 返回路由级 404。

修复：使用独立稳定 ID 或请求体/query 传模型名；也可采用适当的 path converter，并检查静态路由优先级及兼容性。供应商名称如允许 `/`，也需统一解决。

验收：`org/model`、带空格/中文/百分号的合法名称均可 CRUD，且不会影响 `/api/models/test`。

### B14 · P2 · 供应商改名并换密钥时新密钥被覆盖

位置：`web/app.py:732` 至 `738`；`web/vault.py` 的 `rename_key`。

先把新密钥存入新名称，再执行 `UPDATE OR REPLACE` 把旧名称密钥迁入新名称，导致刚输入的新值被覆盖。配置校验失败前先写 vault，也可能产生不对应的密钥副作用。

证据：先新增 old/旧标记，再 PUT 改为 new/新标记，两个请求均成功，但 `vault.get_key('new')` 不是新标记。

修复：先完整校验；明确“仅改名”“改名并设新密钥”“改名并清空”的顺序和语义。先迁移后按显式输入覆写，或将这些动作封装为单个 vault 事务；跨 YAML/vault 失败需要补偿。

验收：改名和换密钥的各组合结果符合输入；失败请求不改变已有可用密钥。

### B15 · P2 · Docker 命令发生二次 format

位置：`tools/executor.py` 的 `DockerExecutor.run` 与 `LocalExecutor.run`。

DockerExecutor 已替换参数，再把完整 argv 包成 ToolSpec 交给 LocalExecutor；后者对每个 argv 再执行 `.format(**{})`。用户参数含 JSON 大括号时会被当成新模板字段。

证据：value 为 `{"a":1}`，二次格式化抛 `KeyError: '"a"'`，尚未调用 Docker。

修复：将“模板展开”和“已完成 argv 执行”拆成两个明确接口；每个参数只格式化一次。不要使用全局替换花括号的方式破坏用户脚本。

验收：JSON、Python 字典、shell 花括号、引号和换行参数保持原值。

### B16 · P1 · wheel 缺前端和报告模板

位置：`pyproject.toml:53`。

package-data 仅声明 `web/static/*.html`。源码目录运行看似正常，干净安装 wheel 后前端 JS/CSS 不存在，报告生成也找不到 Jinja 模板。

证据：在不带仓库元数据的临时源码副本上离线构建 wheel，资源清单仅有 `spiritscout/web/static/index.html`。

修复：显式打包 `web/static/js/*.js`、`web/static/css/*.css`、`agents/report/templates/*.j2`，并审计其他运行期非 Python 资源。验证 sdist→wheel 和直接 wheel 两条路径。

验收：干净环境安装产物后，三个 JS、两个 CSS 均返回 200，SRC 报告和 CTF writeup 能成功渲染。

### B17 · P2 · 用单调时钟作为日期时间记录用量

位置：`gateway/gateway.py:238`；`web/app.py` 的 dashboard 日汇总。

started 来自 `time.perf_counter()`，却被写进 call_log 的 ts；前端和日聚合把它当 Unix 时间戳处理。

证据：同一次调用的记录时间约为 13972，而墙钟为 1792044114，按 Unix 日期解析会落到 1970 年。

修复：耗时仍用 perf_counter，日期记录独立调用 time.time；明确 call_series 的时区和日期归属。另需统一运行中 dashboard 读取 usage.json 与终态 summary 的统计口径，续传计量应避免覆盖历史累计。

验收：调用曲线与日汇总落在实际日期；跨日和续传数据不回到 1970 年，也不丢失前轮用量。

### B18 · P1 · 审批字符串 false 被当成允许

位置：`web/app.py:441`；类似 bool 强制转换见会话参数解析。

`bool(payload.get('allow', False))` 会把字符串 `"false"`、非空数组等都解释为 True。审批接口声明接受 true/false，却没有严格校验。

证据：该代码路径直接应用 Python bool 转换，`bool('false') is True`。普通网页目前传布尔值，风险主要出现在其他调用端和异常客户端输入。

修复：审批请求采用 `StrictBool` 的 Pydantic 模型；不合法值返回 422，不能更改待办状态。其他安全相关布尔字段使用相同约束。

验收：仅 JSON true 表示允许；false 表示拒绝；字符串、数字和对象全部拒绝解析。

### B19 · P2 · 非法输入返回 500

位置：`web/app.py` 的 `create_session`；`launch.py` 的 YAML 解析函数。

接口以普通 dict 接收输入，`float(rps)`、YAML 语法错误和列表项转 dict 的错误没有统一映射为请求校验错误。

证据：rps=`not-a-number` 的创建请求返回 500；`challenges_yaml='challenges: ['` 同样返回 500。

修复：将请求形状、字段类型、数值范围交给输入模型；YAML 错误包装成领域校验错误，并核验顶层和每一项结构。对非有限浮点、负速率、字符串 scope 等增加校验。

验收：坏请求稳定返回 422 及可读原因，不创建半成品会话、不污染配置。

### B20 · P2 · 单条坏会话元数据可阻止控制台启动

位置：`web/sessions.py:379`、`415`。

`_scan` 仅捕获读取和 JSON 解码失败，合法 JSON 但缺字段时抛 KeyError；错误字段类型也可能在恢复阶段崩溃。meta.json 直接覆写，崩溃时可能截断；无法读取的元数据又被静默跳过，使历史会话消失。

证据：在临时 sessions/sess-corrupt/meta.json 写入 `{}`，构造 SessionManager 即抛 `KeyError: 'session_id'`。

修复：为持久化记录定义 schema/版本迁移；单条坏记录隔离并提供可见恢复提示；写入采用同目录临时文件加原子替换，按可靠性需求保留最近有效副本。不要自动删除用户的会话产物。

验收：缺字段、错误类型、截断和旧版本记录均不影响其他会话及控制台启动；有效产物可恢复。

### B21 · P1 · 已被赛方拒绝的 Flag 仍导致同伴收工

位置：`ctf/agents.py:1194`、`906`。

提交前就调用 `_record_flag`，共享缓存只有 value/by/round，没有裁决状态；赛方返回 False 后不撤销。其他路线看到 fresh Flag 就立即 concluded，将候选等同于成功。

证据：提交函数明确返回 `SubmitResult(False, 'incorrect')` 后，同轮另一个方向的 `_peer_flags` 仍得到 `flag{wrong}`。调查环的 fresh 分支会据此收尾。

修复：共享记录区分候选、待判、已拒绝和已接受；拒绝反馈回灌模型，继续探索是否收尾仍由模型判断。自动成功通知应基于明确接受结果；没有提交接口时，应明确呈现“候选待人工判定”。不恢复后端正则决定 Flag。

验收：错误 Flag 不造成其他路线成功收敛；正确 Flag 才触发成功通知；无接口状态不会冒充 accepted。

## 三项需要继续验证的代码风险

这些项不计入上述 21 项定向确认问题，不应直接当作真实外部服务验收结论。

1. **Responses 工具协议转换**：`gateway/backends.py:367` 把历史消息只映射成 role/content，丢失 assistant.tool_calls、tool_call_id；同时直接转发 Chat Completions 形状的 tools。需用本地假端点检查最终请求，并验证所用 LiteLLM 版本的转换行为；修复方向是显式生成 function_call/function_call_output 与 Responses 工具形状，再做多轮契约回归。
2. **输出限幅在读取之后**：`file_analyze` 使用 `read_bytes()[:limit]`，HTTP 使用完整 `resp.text`，LocalExecutor 使用完整 communicate 后再截断；沙箱完整日志落盘也无大小控制。需用受控大文件/流测内存与磁盘增长。修复应针对单次工具输入输出做流式限幅或轮转，不增加任务回合/预算熔断。
3. **基建初始化与冲突重试**：`_build_infra` 失败前创建的总线/数据库/MCP 资源未统一纳入退出栈，`_Infra.close` 未关闭 experience；`VerifyPipeline._save` 在冲突时仅重读版本而不合并新数据，最终构造 VersionConflict 的参数也不完整。需注入初始化失败和并发写，核实资源泄漏与覆盖后再修；推荐 AsyncExitStack、结构化清理和基于最新状态重新裁决。

## 现有测试失败如何处理

不能把本轮 4 项测试失败都当成 4 个产品运行缺陷：

| 测试 | 直接原因 | 修复方式 |
| --- | --- | --- |
| `test_pre_runtime_error_cannot_leave_session_running` | 当前 invalid 模式已回退，随后因空模型注册表失败；测试仍断言 ValueError | 保留“异常必落终态”断言，使用明确故障注入覆盖导入/运行前异常；不要为迎合测试撤回合法模式处理 |
| `test_deferred_mode_replan_continues_to_next_round` | SimpleNamespace 测试上下文缺 pause_gate | 使用真实 MasterContext 或完整 fixture，保留继续重规划语义的断言 |
| `test_intent_from_task_package_reaches_decision_context` | 模型消息追加提问说明后不再是纯 JSON | 按实际消息契约断言任务意图；如决定规范为结构化载荷，则同时调整生产代码和契约 |
| `test_no_intent_without_task` | 同上 | 同上，并验证无任务时不会凭空注入任务意图 |

测试还出现未关闭 SQLite 的 ResourceWarning，需要区分 fixture 清理与真实基建生命周期问题。

## 建议实施顺序

1. **授权与停止闭环**：B01–B06、B18。先堵住禁打和审批绕过，保证“停止”确实停止；同步修复密钥明文和事件属性注入。
2. **任务执行正确性**：B07–B12、B21。让复盘、离线题、DAG 依赖、共享等待与 Flag 状态形成一致语义，不加入固定轮次/预算终止。
3. **可安装与配置可用性**：B13–B16、B19–B20。完成 wheel 干净安装、配置事务、特殊模型名和坏数据恢复。
4. **计量与协议回归**：B17 及三项待验证风险；补真实浏览器、假端点协议和按需外部集成验收。

回归门槛：现有 unittest 全绿；ruff 和 node 语法检查通过；新增测试围绕拒绝前不执行、取消无残留、重连不丢事件、模型可继续规划和错误 Flag 不宣告成功这些行为；最后在可用浏览器环境做真实任务流程验证。Mock 可以验证契约，不能替代真实模型与实战体验。
