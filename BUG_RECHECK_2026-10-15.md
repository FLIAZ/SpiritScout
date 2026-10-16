# SpiritScout 原缺陷修复复查

**后续修复已落地。** 下文保留复查当时的问题和证据，当前修复范围与验证结果见文末“本轮修复结果”。

复查日期：2026-10-15。基于当前工作区，覆盖本轮修复及原有未提交修改。本次不修改业务代码，不覆盖历史审查报告；新增本报告。定向复现使用临时配置、无害标记、假执行函数和本机测试服务，没有向真实目标发出测试请求或调用收费模型。

## 结论

原 21 项问题中，**15 项原复现路径已通过，6 项仍只修复了一部分**。另确认 3 项补充问题，其中缺失工具异常和供应商名规范化问题与本轮修改直接相关。此前 3 组待验证风险也重新检查了当前修复。

“原复现通过”不等于该模块全部边界验收完成。例如 B14 的常规改名换密钥已经通过，但额外发现了名称空格导致密钥错配，单列为 N03。

验证：

- `.venv/bin/python -m unittest discover tests/`：57 项全部通过；上轮为 1 失败、3 报错。
- 系统 `ruff check src/ tests/`：通过。
- 三个前端 JS 的 `node --check`：通过。
- `git diff --check`：通过。
- 主复查脚本执行了 35 个定向案例，另执行 DNS 实际连接、wheel 隔离加载、计量、冲突写入、MCP 取消与协议映射复现。
- wheel 离线构建成功；从解包目录隔离加载后，3 个 JS、2 个 CSS 返回 200，两份 Jinja 报告模板可加载。
- 本次未进行真实浏览器点击、真实云模型、真实 Docker 守护进程或外部 MCP 服务端到端验收。Docker 参数和 Responses 映射采用捕获实际适配层入参的方法验证。

## 原 21 项逐项结果

| 编号 | 状态 | 本轮证据或剩余范围 |
| --- | --- | --- |
| B01 危险动作审批 | 部分修复 | 启动参数免审已堵；CTF 要求 action，但未知类别和直接 Registry 调用仍可免审 |
| B02 重定向禁打 | 原复现通过 | 允许域名 302 到禁打域名只发送第一跳，返回 -3 |
| B03 禁打 CIDR | 部分修复 | IPv4/IPv6 字面量命中已拦；DNS 检查与实际连接二次解析仍可绕过 |
| B04 模型字段泄密 | 原复现通过 | 模型 api_key_value 返回 422；标记未写入 YAML、未出现在读 API |
| B05 内联事件实体注入 | 原路径已从代码移除 | 模型/供应商动态按钮改用 data-* 与事件委托；浏览器点击回归未做 |
| B06 取消后进程残留 | 原复现通过 | 取消沙箱后不再产生延迟标记文件；CLI/Docker 取消清理已有代码路径 |
| B07 SRC 提前收敛 | 原复现通过 | 已完成 DAG 仍调用模型复盘，模型要求继续时继续 |
| B08 固定回合顶 | 原复现通过 | 实际 MasterAgent.run + 分段图受控运行 30 轮，30 个视图/派发计数一致 |
| B09 纯附件 CTF | 原启动门通过 | local_analysis=True，scope 非空，ScopeGuard 启动门通过；未测真实附件解题 |
| B10 SSE 游标 | 原复现通过 | 分批中文事件 ID 为 1、2，从 2 重连读取 3 |
| B11 DAG 依赖 | 部分修复 | 前向依赖正确；父节点失败重建后，既有子节点仍指向旧父节点 |
| B12 共享 Future 取消 | 原复现通过 | 取消创建者后另一等待者仍能收到审批/回答；两种 Broker 均通过 |
| B13 slash 模型 ID | 原复现通过 | org/audit 的编码路径编辑、删除均返回 200 |
| B14 改名换密钥 | 原复现通过 | old 改 new 并换密钥，实际 new 密钥为新输入；额外名称规范化见 N03 |
| B15 Docker 二次格式化 | 原复现通过 | 捕获执行 argv，JSON 大括号保持原样，不再抛 KeyError |
| B16 wheel 资源 | 原复现通过 | wheel 含 JS/CSS/Jinja，隔离加载静态资源和模板通过 |
| B17 计量 | 部分修复 | 调用时间已改为墙钟、增加恢复；dashboard 仍不读取运行中 usage.json |
| B18 字符串审批 false | 原复现通过 | 字符串 false 请求在严格布尔验证处返回 422 |
| B19 坏输入 500 | 部分修复 | 坏 rps/YAML 返回 422；scope、upload_ids 等自由字段仍可触发 500 |
| B20 坏 meta 阻止启动 | 部分修复 | 缺 session_id 被隔离，原子写已加入；其他错误字段仍阻止整个注册表启动 |
| B21 错误 Flag 误收工 | 原路径已修复 | False 应答写 rejected，共享收尾只筛 accepted；错误候选仍可见但不作为成功信号 |

## 尚未修完整的六项

### R01 / B01 · P1 · action 非空不等于行为已被正确分类

位置：`src/spiritscout/ctf/agents.py:1628`、`src/spiritscout/tools/registry.py:74`、`src/spiritscout/policy/engine.py:80`。

目前 shell/python 的 action 只检查非空，schema 也是普通 string，随后策略只认固定敏感类别。模型输出不在分类集合中的值会被当成普通 readonly 调用。Registry 本身也没有落实“通用执行入口必须声明”的要求。

复现（只使用返回成功的假执行器，不执行危险脚本）：

- CTF shell 的 action=`unclassified_action`：执行器调用 1 次，审批调用 0 次，结果 ok=True。
- 直接 `registry.call('sandbox_exec', {'code': 'printf harmless'})`：不传 action 也执行成功。
- 已修好的子项：allow_sensitive_actions=True 下 reverse_shell 仍会询问；拒绝后不执行。

修复：将 action 定义为统一的强类型类别，CTF、Registry、MCP 等入口共用；缺失/未知类别不能自动归为只读，应回灌模型补充或请求用户确认。语义判断仍由模型做，但结构与已知风险要求由统一门落实。不要只在某个 Agent 包装函数里校验，也不要恢复关键词扫描自动批准。

### R02 / B03 · P1 · DNS 二次解析仍能连接到禁打地址

位置：`src/spiritscout/tools/builtin.py:28`、`103`。

`_check_hop_scope` 先 getaddrinfo 检查，`client.send` 随后让底层连接器重新解析。连接并没有绑定到已验证地址。解析失败时前置检查还会直接返回，让请求继续自行解析。

本机复现：禁打 `127.0.0.0/8`；对自定义域名第一次解析返回允许的 203.0.113.1，第二次返回本机测试服务地址。实际输出为：`ok=True`、解析 2 次、被禁服务收到请求 1 次。没有连接真实外部地址。

修复：把地址检查放进实际连接阶段，使用已验证地址建立连接并保留正确 Host/TLS server name；重定向每跳仍校验。不得只在请求前解析一次再让底层独立解析。补 IPv6、多地址和代理路径验证。

### R03 / B11 · P1 · 重建父节点没有修复已有子节点的依赖

位置：`src/spiritscout/agents/master/dag.py:51`、`82`。

两遍建图已解决前向引用，但依赖更新只处理本次新建节点。既有 CHILD 没有失败，因此被 skip；其 PARENT 失败后重建，CHILD 仍保留旧 node_id。

复现：建立 CHILD→PARENT，旧 PARENT 标 failed，重新创建 PARENT 并标 done。结果 CHILD 仍依赖旧父节点，`ready_nodes()` 为空。

影响：下游长期 pending；复盘又可能因 DAG 未完成否决模型停止，形成无效重规划。

修复：明确重建替代关系并更新受影响的既有边；或使依赖指向稳定逻辑 ID，执行代次单独管理。更新须整体校验、原子应用；失败旧节点不能继续作为唯一可满足依赖。

### R04 / B17 · P2 · 运行中用量在列表和 dashboard 不一致

位置：`src/spiritscout/web/app.py:474`、`484`；`src/spiritscout/web/sessions.py` 的 `brief`。

时间戳已正确，BudgetTracker 也新增了恢复。但列表从 usage.json 读取实时计量，dashboard 仍只读 summary.run.model_usage。

复现：运行中会话 usage.json 记 123 token，summary 尚为空；`/api/sessions` 显示 123，`/api/dashboard.total_tokens` 为 0。

修复：抽出统一用量读取接口，列表、state、dashboard、终态摘要采用同一来源优先级；日汇总也使用实时 call_series。补运行中、终态、续传和文件损坏回退的交叉断言。

### R05 / B19 · P2 · 创建请求仍保留未验证的自由字段

位置：`src/spiritscout/web/app.py:74`、`79`、`576`。

SessionCreatePayload 只校验 rps 和两个布尔，extra=allow 使后续会执行 list()/字符串操作的字段仍可为任意类型。

复现：

```json
{"mode":"src","target":"https://audit.test","scope":9}
```

```json
{"upload_ids":123}
```

两者均返回 500。坏 rps、坏 YAML 的原复现则已返回 422。

修复：完整声明 mission/target/scope/upload_ids/YAML/no_llm 等实际消费字段；验证列表元素类型，未知字段有明确策略。使所有结构错误在任何建目录、搬附件或写配置前返回 422。

### R06 / B20 · P2 · meta 类型收敛和恢复不在完整故障隔离范围内

位置：`src/spiritscout/web/sessions.py:230`、`438`、`460`。

缺关键 ID 的记录已隔离；但 `_coerce_meta` 在 try 之外直接迭代 opinions/dialogue_history，PauseGate.restore 也在 try 之外。status 未规范化，后续放入集合判断会因不可哈希类型失败。

复现：给合法 session_id 的 meta 分别设置以下值，构造 SessionManager：

| 字段 | 值 | 结果 |
| --- | --- | --- |
| opinions | 3 | TypeError，启动失败 |
| dialogue_history | 7 | TypeError，启动失败 |
| excluded_seconds | "oops" | ValueError，启动失败 |
| status | [] | TypeError，启动失败 |

修复：完整验证记录 schema，或把解析、归一化、Session 构造、PauseGate 恢复和状态分类包在逐记录隔离中；数值处理非有限值，列表与状态严格验证。坏记录只隔离，不影响其他会话。

## 三项补充发现

### N01 · P1 · 有副作用的调用仍按 readonly 工具规格自动重试

位置：`src/spiritscout/tools/registry.py:88`；`src/spiritscout/tools/builtin.py` 的 BUILTIN_SPECS。

HTTP 和 sandbox 的 spec 仍是 readonly；重试次数只依据 spec.risk，不看这次 action。现在模型能声明 intrusive_write 并进入审批，但执行失败后仍可能自动再执行一次。

复现：只用假函数模拟 POST + intrusive_write，第一次返回“响应丢失”，第二次成功；结果执行次数 2，审批 1 次。

这不是据 diff 能确认的新引入问题，而是新增动作声明后的重试语义仍未接通。真实后端可能已经完成第一次写入，重试造成重复操作。

修复：根据本次有效行为和可重放属性决定重试；有副作用且结果未知的调用默认不自动重放，回灌模型核实或请示用户；明确幂等键可另行处理。

### N02 · P2 · 缺失 CLI 工具从结构化失败退化为异常

位置：`src/spiritscout/tools/executor.py:78`、`87`、`206`。

本轮提取 run_argv 时移除了旧 LocalExecutor.run 的 FileNotFoundError 归一化，新的启动点也未捕获。

复现：执行路径 `/nonexistent/spiritscout-audit`，现在直接抛 FileNotFoundError；之前返回 ok=False、returncode=127。Registry 无法按原结果契约重试/降级，外层 Agent 可能只记成普通步骤异常。

修复：在启动边界恢复 OSError/FileNotFoundError 的结构化结果，保留 CancelledError 的取消传播；补工具不存在、无权限、工作目录不可用及正常非零退出的契约测试。

### N03 · P2 · 供应商名称规范化后 vault 仍使用原始名称

位置：`src/spiritscout/web/modelcfg.py:43`；`src/spiritscout/web/app.py:760`、`769`。

canonical_provider 通过 ProviderEntry 去掉名称首尾空格；API 写 vault 使用的仍是原始 payload 名称。于是 YAML 与密钥库主键不一致。

复现：POST 合法供应商，name=`" old "`、api_key_value 为无害标记，返回 201；配置名称是 `old`，`vault.has_key('old')=False`，`vault.has_key(' old ')=True`。模型装载和页面密钥状态按规范名查找，会显示没有密钥。

修复：配置保存、重名检查、模型引用、vault 改名和写入都使用同一个规范化结果；失败时不能留下半套配置。新增与更新都覆盖首尾空格案例。

## 上次三组待验证风险的进展

1. **Responses 工具映射：适配层定向通过。** 捕获 LiteLLM 调用参数得到 user、function_call、function_call_output，两个 call_id 一致，工具列表为扁平结构。真实服务端、多轮与流式体验仍未验收。
2. **输出限幅：部分修复。** file_analyze 已改为限量读取，CLI 与 HTTP 已采用流式路径。但沙箱只在一次调用开始前判断文件大小并轮转，运行中 sink.write 没有限额。缩小轮转阈值为 1024 字节后，单次输出 4096 字节，日志仍增长到 4096；默认阈值同样不能约束单次长输出。应在写入过程中轮转或分块归档，并标明证据完整性；这属于单工具资源管理，不应引入调查预算熔断。
3. **生命周期与冲突写入：部分修复。** 通用 teardown 和 experience.close 已加入；终态冲突也不再被覆盖。但有两个确定剩余点：
   - MCP 启动客户端在 gather 全部完成后才加入清理列表。用一个立即成功、另一个挂起的假客户端，在初始化中取消：成功客户端 start=1、close=0，挂起客户端 close=0。应在创建/启动时即时登记所有权，并在失败/取消路径关闭。
   - VerifyPipeline._save 对中间态冲突仍只更新 expected_version 后写旧对象。使用真实临时 SQLite：并发方在 SCREENED 记录加入证据，再让旧对象保存为 REPRODUCED，新增证据消失。应合并最新证据并重新判断状态迁移，或终止旧迁移让调用方重读重算，不能仅绕过版本冲突。

## 建议下一轮修复顺序

1. R01、R02、N01：审批入口、实际连接边界和副作用重试。
2. R03：重建依赖，避免任务卡死或空转。
3. R06、N02、N03、R05：启动、执行器、凭据引用和输入契约。
4. R04 与三组待验证风险的剩余项：统一计量、运行中日志、MCP 生命周期和并发证据合并。

把本轮复现固化为回归测试，再运行全套检查。现有 57 项全绿说明既有测试通过，不能代替这些新边界案例的验收。本次未自动改动你的修复，避免审查与修复混在同一轮。

## 本轮修复结果

在用户随后授权修复后，已完成以下代码修改，保留原有工作区修改，未自动提交或重启现有服务：

| 问题 | 当前实现 |
| --- | --- |
| R01：未知/缺失 action 免审 | 统一 ActionCategory；Registry 验证通用执行与副作用 HTTP 声明，未知/缺失类别不执行；CTF schema 共用枚举，敏感动作仍即时审批。MCP 的业务 action 参数保留，已知敏感类别继续进入审批 |
| R02：DNS 检查与连接分离 | 新增 ScopedHTTPTransport，实际连接使用已检查的 IP，保留逻辑域名的 Host、SNI 和证书校验；逻辑 origin 隔离连接池；关闭环境代理旁路，解析失败不再放行；IPv4 映射的 IPv6 同受 IPv4 禁打约束 |
| R03：重建后的旧依赖 | 在 DAG 副本完成校验，原子应用重建与待执行下游依赖重绑定；成环时回滚；历史失败不使已成功重建节点反复创建 |
| R04：实时统计不一致 | Session.model_usage 统一读取并验证快照，列表/state/dashboard 共用；前端仪表盘读取聚合 API，正确显示每日和模型统计，迟到响应不覆盖新页面 |
| R05：自由字段 500 | 创建请求完整声明实际使用字段并拒绝未知字段，类型错误在搬附件、建目录前返回 422 |
| R06：异常 meta 启动崩溃 | 规范化列表、状态、有限数值、嵌套 run 汇总；将归一化和暂停门恢复纳入逐条故障隔离 |
| N01：副作用自动重放 | 按本次行为和 HTTP 方法决定可重放性；通用沙箱和写请求失败不自动重试，也不自动调用 fallback |
| N02：缺失工具异常 | 启动 OSError 归一化为 ToolResult，缺失工具返回 127，其他启动错误返回 126；不吞取消异常 |
| N03：规范化名称与 vault 错配 | 新增/更新供应商时先取规范化结果，配置、改名和密钥写入使用同一名称 |
| 沙箱日志单次输出过大 | 写入过程中分块轮转，两代各自有界；旧超大日志只读尾窗；结果与模型观察明确说明轮转和上代文件 |
| MCP 初始化取消遗漏清理 | 客户端启动前登记所有权，初始化失败/取消关闭已创建客户端；关闭超时取消后台任务后等待回收 |
| 核验冲突覆盖证据 | 版本冲突即放弃旧迁移，下一轮基于最新记录重新核验；终态记录不再降回中间态 |

新增回归文件：

- `tests/test_repair_execution.py`：行为校验、审批拒绝、重试/fallback、真实 HTTP 地址绑定、真实 HTTPS 证书验证、MCP 参数兼容、DAG 更新及回滚。
- `tests/test_repair_web.py`：错误请求不创建会话、供应商名称和密钥、实时统计与损坏快照回退、异常元数据恢复。
- `tests/test_repair_lifecycle.py`：MCP 初始化取消、SQLite 并发证据、终态保护、单次大日志与旧日志轮转。
- `tests/test_dashboard.js`：使用实际渲染函数验证实时图表和请求乱序，不依赖第三方前端包。

验证结果：

- `.venv/bin/python -m unittest discover tests/`：**82 项全部通过，较修复前新增 25 项**。
- `ruff check src/ tests/`：通过，使用系统 ruff。
- `node tests/test_dashboard.js`：通过。
- 三个前端 JS 的 `node --check` 与 `git diff --check`：通过。
- 本机真实 HTTP/HTTPS 服务验证：二次 DNS 解析不会改变连接地址，Host 和证书验证仍使用原域名；多地址禁打与 IPv4-mapped IPv6 拒绝路径通过。
- 真实 bwrap 执行验证：缩小日志阈值后单次输出仍保持两代各不超限，取消后不产生延迟标记文件。

边界说明：本轮修的是复查残留，不实施 Redis 或完整可恢复架构。受管 http_probe 现在直接连接经校验地址，不再隐式使用环境代理；沙箱任意脚本的网络出口控制仍属于实施方案中的独立工作。未调用收费模型、未操作真实目标，也未做外部 MCP/Docker 守护进程及真实浏览器端到端验收。
