# SpiritScout 可执行改造计划

本文件描述完整实施目标，部分基础改造已落地，不能将整份设计视为完成。实际交付、验收和剩余边界见 `OPTIMIZATION_PROGRESS_2026-10-15.md`；下文未在进展文件中确认的模块、表与 API 仍为建议契约。

## 一、目标与约束

第一版目标：任务可以可靠停止与恢复；每个关键结论有证据；两个身份可以完成同一业务流程的对照验证；主 Agent 按模型决策持续规划，直到模型结束或用户停止。

保留 Python/FastAPI/SQLite、现有黑板八层、现有 Agent 类型和原生前端。正式纳入 Redis，承担跨进程通知、Streams 任务分发、共享限速、短期缓存和执行者心跳；SQLite 仍是检查点、调用结果、审批决议和恢复状态的权威来源。先支持同一台机器的 Web/协调器/worker 进程拆分，不立即拆微服务或支持多机器共享 SQLite，也不重写整个 Agent 系统。

必须持续满足：

- 模式、意图、调查方向、派生与收尾由模型决策；后端只校验结构和落实授权。
- 禁打列表是唯一硬目标边界；范围外探索沿用模型询问用户的动态扩围语义。
- 危险操作经过 ApprovalBroker 即时审批；启动参数不能一揽子免审。
- 不设任务固定回合顶，不设 token/金额熔断。
- Flag 候选由模型提交；正则只作格式信息，不恢复自动提取决策。
- 知识库存参考事实、适用条件和坑，不存固定解题步骤序列。
- 前端只改 static，不引入构建工具或外部前端依赖。

## 二、落地后的模块关系

```text
Web SessionManager
    └── SessionRuntime（持有并管理全部会话资源）
        ├── Scheduler：执行模型规划的 DAG，持久化检查点
        ├── InvocationService：登记调用、校验、审批、执行、记录结果
        │   ├── HTTP / Browser / CLI / Sandbox / MCP adapters
        │   └── IdentityManager、ScopeGuard、ApprovalBroker
        ├── EvidenceStore：证据索引 + 文件产物
        ├── Blackboard：资产/假设/漏洞等共享视图
        └── EventOutbox：提交后的事件投递、SSE、JSONL 导出
            └── RedisCoordinator
                ├── Pub/Sub：唤醒订阅者；丢通知可从 SQLite 补读
                ├── Streams：派发任务、回传结果；SQLite 确认后 ACK
                └── 限速、短期缓存、worker 心跳与执行租约
```

不能出现两条可执行路径：Agent 直接调用适配器、浏览器直接发送受管请求、重试路径绕过审批，都属于接入未完成。

Redis 仅协调已由模型规划、已由执行层校验的任务，不决定调查方向、派生时机或收尾。Redis 与 SQLite 之间不假定存在分布式事务：通过 outbox、稳定消息 ID、数据库条件更新和幂等消费处理重复与中断。

## 三、工作拆分：11 个独立变更单

编号是建议的变更单/PR 边界，不表示已创建 PR。每个变更单要求独立可验证，不能为了凑单一次大改后集中测试。

| 编号 | 交付内容 | 前置依赖 | 可见完成标志 |
| --- | --- | --- | --- |
| PR-01 | 配置、API、打包及基础回归修复 | 无 | 干净安装可用，配置不泄露密钥，坏输入可读 |
| PR-02 | 工具执行契约、审批和停止清理 | PR-01 | 被拒调用不执行，停止后无进程残留 |
| PR-03 | 禁打边界及受控网络接入 | PR-02 | HTTP/DNS/浏览器/沙箱按已声明能力落实禁打 |
| PR-04 | 执行存储、事件 outbox、SSE | PR-02 | 调用结果与事件原子记录，断线不丢事件 |
| PR-05 | 调度恢复、复盘和 Flag 协作 | PR-04 | 重启从有效检查点继续，错误 Flag 不宣告成功 |
| PR-06 | 证据库与结论引用 | PR-04 | 漏洞和报告能定位到原始证据 |
| PR-07 | HTTP 身份与流程状态 | PR-03、PR-06 | 两账号 Cookie 和流程状态隔离 |
| PR-08 | 浏览器业务流程工具 | PR-07 | SPA/登录/上传可观测并受统一工具门管理 |
| PR-09 | 假设协作和控制台现场视图 | PR-05、PR-06；身份部分依赖 PR-07 | 可解释分工、交接、阻塞与计划变化 |
| PR-11 | Redis 通知、可靠队列、共享限速与执行协调 | 通知依赖 PR-04；任务分发依赖 PR-05 | 重复消息不重复执行，Redis 中断不丢持久状态 |
| PR-10 | 场景验收与迁移发布 | 各项已完成部分持续接入，最终依赖全部 | 重启、身份、审批、证据、安装全链路通过 |

保留原 PR 编号以便追踪；PR-11 分两个阶段接入，最终纳入 PR-10 验收，不必等浏览器功能完成才开始。

### PR-01：先让当前版本可安装、可配置、可验证

修改：`web/app.py`、`web/modelcfg.py`、`web/vault.py`、`web/sessions.py`、`tools/executor.py`、`gateway/gateway.py`、`web/static/js/console.js`、`pyproject.toml`。

执行项：

1. 新增 `web/schemas.py`：创建会话、审批、模型、供应商使用明确请求模型；审批使用严格布尔类型；未知密钥字段拒绝；错误 YAML 转为 422。
2. 模型/供应商的读写使用验证后的公开字段；规范化后的字段才写 YAML。供应商改名先校验，再迁移 vault，最后应用显式新密钥/清空操作；补失败补偿。
3. 用请求体或稳定 ID 标识模型，避免 slash 模型名进入普通路径参数。旧接口保留兼容入口，前端改用新接口。
4. 删除前端动态内联 onclick，改为 data 属性与事件监听；覆盖 HTML 实体和特殊字符。
5. Docker 已完成 argv 直接进入执行函数，不再二次 format；计量时间与耗时分开记录。
6. meta 原子写回、版本化读取；单条异常记录隔离并报告。不能删除坏记录所对应的附件或报告。
7. 补齐 JS/CSS/Jinja 模板 package-data；修正现有 4 项失败测试的 fixture/断言，不能只跳过或削弱行为检查。

新增测试建议：`test_web_schemas.py`、`test_modelcfg_transactions.py`、`test_package_install.py`；扩展现有 `test_launch_sessions.py`、`test_web_auth.py`。

验收：无网络离线构建 wheel；临时环境安装后静态资源和两类报告模板可用；假密钥标记不出现在 YAML/API；坏 rps/YAML 返回 422；改名换密钥保留新值；`org/model` 可编辑删除。

### PR-02：先统一“执行过什么”，再做恢复

修改：`tools/spec.py`、`tools/registry.py`、`tools/executor.py`、`tools/sandbox.py`、`policy/engine.py`、`policy/approvals.py`、`policy/questions.py`、`ctf/agents.py`。新增 `execution/models.py`、`execution/service.py`。

新增契约：

```text
ToolInvocation
  invocation_id, run_id, task_id, route_id, tool_name
  operation_intent, action_category, target_refs, identity_id
  params_ref, params_digest, declared_effect, replay_policy

InvocationResult
  invocation_id, execution_status, effect_status, domain_result
  evidence_refs, artifact_refs, exit_code, error_code, started_at, finished_at

execution_status = not_started | awaiting_approval | running
                 | succeeded | failed | denied | cancelled | unknown
effect_status    = none | applied | unknown
replay_policy    = safe | idempotent_with_key | never_automatic
```

`succeeded` 仅说明工具层成功；是否验证了假设由 `domain_result` 和模型判断表达，不能等同于发现漏洞。

执行项：

1. 将 registry 作为统一入口，依次完成登记、结构校验、禁打检查、审批、执行和结果记录。适配器只处理实际 I/O。
2. 模型提供行为说明与目标；结构校验不接受模型凭空降低工具固有风险。对无法确认风险的通用脚本执行采取保守审批，不引入简单命令关键词自动放行。
3. 审批关联具体 invocation 和规范化参数摘要；与既有会话动作授权兼容，但不能把批准转成所有目标/任意代码通行证。正文要展示操作目的、目标和预期影响。
4. 审批/提问使用 shield 和等待者管理。持久化的是待办记录与决议，Future 只存在内存，重启后按记录重建。
5. CLI/sandbox 的取消和异常路径杀进程组、等待回收、关闭输出任务与文件；Docker 显式清理容器，MCP 按其取消能力记录确定/未知状态。
6. 保留 `ToolResult.ok` 等旧字段作为兼容映射，逐步迁移调用者，避免一次改断所有 Agent。

验收：拒绝后适配器调用次数为 0；`allow_sensitive=true` 不能免审；一个等待者取消不影响其他等待者；停止后临时命令不能再写标记文件；非零退出码、超时和结果未知可区分。

### PR-03：把网络边界落实到真实连接

修改：`policy/scope.py`、`tools/builtin.py`、`tools/sandbox.py`、`tools/mcp_client.py`、`runtime.py`。新增 `execution/network.py`。

分两个交付层：

1. **受管 HTTP/DNS 层**：逐跳检查 URL；CIDR 支持 IPv4/IPv6；DNS 所有候选连接地址检查；连接时绑定已检查地址，保留正确 Host/TLS 服务名；默认端口、IDNA、尾点、编码路径统一规范化。HTTP 重试、重定向、WebSocket 建连均使用此层。
2. **通用沙箱层**：Linux 网络命名空间中禁止直接出网，只能经连接代理/网络 broker。HTTP(S)、原始 TCP 和 DNS 使用不同受管能力；不能只配置 HTTP_PROXY 就宣称所有 socket 已受控。检查 IPv6、UDP、WebRTC、QUIC 等旁路；限制对宿主控制面的访问。

边界能力必须真实披露：HTTPS CONNECT 和原始 TCP 无法检查加密后的 URL 路径。当禁打规则包含路径时，应使用可检查完整请求的受管 HTTP/浏览器能力；不支持的通道明确不可用，不能宣称路径规则已覆盖。MCP 若在另一台机器执行网络请求，需远端执行器配合；仅本机校验参数不等于控制远端真实连接。

范围外目标仍按既有动态扩围语义处理。这里不引入“只能连白名单”的新政策。

验收：使用本地测试服务覆盖 302 多跳、DNS 多地址、IP 禁打、IPv6、域名解析变化；确认被禁服务收到的连接数为 0。通用沙箱需同时测 Python socket、curl、子进程和直连 IP，不能只测 http_probe。

发布门：未完成沙箱/远程 MCP 的真实出口控制前，在能力页准确标注限制，不把 PR-03 标为全通道完成。

### PR-04：同一个 SQLite 内持久化调用、检查点和事件

新增：`execution/store.py`、`execution/migrations.py`、`execution/outbox.py`。修改 `blackboard/store.py`、`web/events.py`、`runtime.py`。

在会话现有 `bb.db` 中增表，不另建一套相互无法原子提交的会话数据库：

| 表 | 核心字段与约束 |
| --- | --- |
| `execution_runs` | run_id、session_id、status、created_at、finished_at |
| `tool_invocations` | invocation_id 主键、run/task/route、status、params_digest、结果引用、租约/心跳 |
| `approval_records` | approval_id、invocation_id、pending/allowed/denied/expired、决定者、时间 |
| `runtime_checkpoints` | run_id、sequence、schema_version、DAG/state JSON、last_event_id；run+sequence 唯一 |
| `runtime_events` | event_id INTEGER PRIMARY KEY、run_id、kind、actor、公开 payload、created_at |
| `event_outbox` | event_id 唯一、投递状态、重试信息 |
| `schema_migrations` | version、applied_at |

要求：调用结果、对应任务状态、检查点和事件通过同一连接/UnitOfWork 原子提交。仅使用同一数据库文件但分成多个连接分别 commit，不能提供这个保证。

提交后再通过 outbox 投递总线；Redis 模式下由 PR-11 发布通知或任务消息，通知丢失可从表重放。跨进程审批先提交 approval_records，再发送唤醒通知；执行者补读数据库决议，不能把 Redis 消息本身视为用户批准。JSONL 保留为兼容导出/审计投影，避免 SQLite 和 JSONL 都是权威状态。秘密参数存加密引用，事件 payload 只留脱敏数据。

SSE 新事件直接使用数据库 event_id，按 `id > last` 分页读取；旧 JSONL 会话在一次事务中导入，保持原文件不动。新流标识与旧行号游标需版本化，避免旧客户端拿行号当新主键。

验收：模拟提交前/后崩溃，数据库无“结果成功但任务仍未执行”的分裂状态；重复 outbox 投递能幂等消费；分批写入中文、半行旧日志与多次重连不丢事件。

### PR-05：可恢复调度与正确收敛

修改：`agents/master/agent.py`、`agents/master/dag.py`、`agents/sub/base.py`、`modes/assembler.py`、`ctf/agents.py`、`ctf/flags.py`、`web/sessions.py`、`runtime.py`。新增 `execution/recovery.py`。

执行项：

1. DAG 节点先创建、再解析依赖，使用稳定 node_id 引用；未解析依赖和环返回模型可理解的规划错误，不静默删除依赖。
2. 为任务保存计划版本、路线步骤、已完成步骤、下一动作和证据引用。模型请求前保存输入引用，响应后保存决策，工具执行后保存结果。
3. 使用可持续的调度循环：规划→派发→聚合→模型复盘。复用现有节点方法时明确 additive state 的合并规则，不能用普通 dict.update 丢掉 LangGraph reducer 语义。
4. 移除 `iteration_max_rounds` 的执行效果及主循环固定轮次终止；不以增大 recursion_limit 作为最终方案。让每轮持久化后可继续调度，循环定期让出事件循环。
5. 当前 DAG 完成只是复盘输入；目标是否达成由模型判断。工具/模型服务不可用显示明确阻塞原因，不伪装成“没有新发现”。
6. Web 的“暂停后继续”“用户停止后的继续对话”“崩溃恢复”分开处理，但复用同一个状态恢复服务。旧会话无检查点时保留原成果，由模型基于证据重新规划，不宣称精确恢复。
7. Flag 共享缓存改为带 candidate/pending/rejected/accepted 状态的记录；拒绝结果回灌所有相关路线，不能触发成功收工。
8. 纯附件任务保存本地附件授权与证据引用，允许离线分析，不补虚假网络目标。

恢复规则：

| 中断时状态 | 恢复动作 |
| --- | --- |
| 未启动 | 根据原调用继续调度 |
| 等待审批 | 校验参数与授权未变后重建待办；变化则旧审批失效并生成新待办 |
| 已有持久结果 | 复用结果推进状态，不再次执行 |
| 执行中、进程归属可确认 | 先检查/收敛原执行，再决定是否重新调度 |
| 执行中、结果无法确认 | 标记 unknown；模型提出核实方案或请示用户，禁止自动重放副作用 |

不承诺网络操作 exactly-once；通过调用 ID、幂等键、结果核实与 unknown 状态控制重复风险。

验收：工具返回后、保存前/后分别杀掉子测试进程并恢复；已完成操作不重复；未知写操作不自动重试；连续规划超过原 20 轮；错误 Flag 不结束其他路线；附件题可进入执行。

### PR-06：建立证据库，逐步迁移旧 evidence 字典

新增：`evidence/models.py`、`evidence/store.py`、`evidence/redact.py`；修改 `core/models.py`、各工具适配器、`blackboard/convergence.py`、`verify/pipeline.py`、`agents/report/agent.py`。

新增表：

```text
evidence_records:
  evidence_id, invocation_id, kind, identity_id, captured_at
  sha256, byte_size, media_type, public_summary, artifact_path
  sensitivity, completeness, redaction_version

claims:
  claim_id, task_id, kind(observation/hypothesis/conclusion)
  statement, status, created_by

claim_evidence:
  claim_id, evidence_id, relation(supports/refutes/context)
```

产物保存于会话 `artifacts/<evidence_id>/`，先写临时文件再原子替换，再登记索引。摘要与原件分别处理：模型和普通 API 得到脱敏视图；需要保留的敏感原件加密存储、显式受控读取。哈希用于完整性检查，不等于证据真实性或漏洞成立证明。

所有读取按 evidence_id 查索引，不允许客户端传任意磁盘路径。大响应流式采集并标注是否完整；截断的证据不能伪称原件完整。临时文件和未入库孤立产物只做受控回收，不删除用户原始附件。

新增 API：`GET /api/sessions/{id}/evidence`（游标分页）、`GET .../evidence/{evidence_id}`、`GET .../evidence/{evidence_id}/content`。沿用现有认证并校验证据确属该会话；不新增无需授权的附件静态目录。

兼容期：Vulnerability 增加 evidence_refs，保留旧 evidence 字段只读适配；新工具只产引用；旧数据按历史导入证据标记，不能补造捕获时间和网络请求。

验收：任意报告结论能打开支撑证据；跨会话 ID 和路径穿越被拒；日志/摘要中无测试 Cookie 标记；大输出不会无界占内存。

### PR-07：HTTP 身份与流程上下文

新增：`identities/models.py`、`identities/manager.py`、`tools/http_session.py`；修改 `runtime.py`、`tools/builtin.py`、`agents/business/agent.py`、`agents/webvuln/agent.py`、`traffic/har.py`。

每个 session 的 IdentityManager 维护匿名、账号 A、账号 B 等身份；identity_id 与显示名/角色提示独立。每身份持有独立 HTTP 客户端与 CookieJar；共享连接池与状态要有明确隔离，不能通过全局 Cookie 存储复用。

请求增加 identity_id、flow_id、step_id。Cookie、令牌和 CSRF 值通过 secret_ref/执行层变量引用；模型可见变量名、来源和适用范围，不需要读到明文。HAR 继续仅提取脱敏结构，不自动把 HAR 中的历史凭据转成可用身份。

提供身份注册/删除和人工接管登录接口；登录动作经统一执行与证据入口。进程恢复时只恢复经同意持久化的加密状态；失效状态明确提示重新登录。

验收：A 创建的对象由 B 发出独立请求核验；两者 Cookie 不串；跨站重定向不泄露认证；CSRF 值更新后下一步骤读取新值；账号过期不会被解释为漏洞结论。

### PR-08：浏览器只提供动作能力，不写死业务流程

新增：`tools/browser.py`、`tools/browser_types.py`；在 Python 项目增加可选 browser 依赖组，版本与浏览器二进制安装在实施时验证并锁定。修改 `runtime.py`、`agents/profiles.py`、`agents/business/agent.py`、前端 static。

首版工具：navigate、inspect、click、fill、select、upload、screenshot、observe_requests。以页面可访问语义和稳定元素引用操作；页面文字和 DOM 当作不可信目标数据，不提升为系统规则。不首先开放任意浏览器 JavaScript 执行。

每身份一个 BrowserContext，多个标签页显式归属；请求采集写证据库。浏览器和 HTTP 身份状态的同步必须保留域、路径、Secure、过期等属性；HttpOnly Cookie 不暴露给模型。localStorage/CSRF 不是 CookieJar 能自动同步的内容，必须分别表达。需要严格一致的 Cookie 会话时优先使用同一浏览器上下文关联的请求通道，并验证实际行为。

用户在控制台看到当前页面截图、动作目的和接管入口；身份验证成功由页面/请求证据确认，不只靠模型说“登录完成”。点击可能产生副作用，仍经过 PR-02 审批，不能假设浏览器点击天然只读。

验收：本地 SPA 的异步按钮、登录、表单、上传、下载和多标签页正常；账号上下文隔离；页面诱导文字不能修改禁打与审批政策；新窗口、子资源、WebSocket、Service Worker 等网络通道纳入 PR-03 的边界验证。

### PR-09：模型按假设协作，前端展示真实状态

修改：`core/models.py`、`agents/master/agent.py`、`agents/sub/base.py`、`agents/ring.py`、`agents/profiles.py`、`web/app.py`、`web/static/js/views.js`、`web/static/css/views.css`。

Task 增加 question、hypothesis_refs、evidence_refs、expected_deliverable、identity_refs、plan_version。结果增加 supports/refutes/inconclusive/blocked 等结构化结论、证据引用、阻塞原因与后续建议。

主 Agent 的计划输出包含新增/修改/取消节点和理由；后端仅验证节点引用、依赖环、角色工具权限及结构。具体分工与派生时机仍由模型决定。新模型输出无法解析时可见地重试/回灌错误，不偷偷执行预设调查路线。

attempt_key 不再只依据 URL 和截断参数；加入身份、方法、规范化请求体摘要、测试目的和假设标识。仅把“已做过”作为模型上下文，不能因相似就硬跳过必要的独立复核。

前端首版增加五块：当前调查问题、证据列表、计划变更、阻塞/审批、用量与运行状态。不要只新增一个复杂拓扑图。HTTP 阶段先显示请求证据，浏览器能力就绪后再增加截图，不展示尚未接入的按钮。

状态接口按游标/版本增量更新；事件触发合并刷新，避免每条流式增量全量读黑板。任务、证据与历史事件分页，避免固定 limit 截断后悄悄丢失历史。

Redis 模式增加运行状态：连接状态、待派发/待确认消息、最老等待时间、worker 存活、租约冲突、限速等待。展示面隐藏 Redis 凭据；队列等待与模型调查无进展不能混为一谈。

验收：每个任务能说明“正在验证什么”；用户能打开证据、看到改计划的原因、区分等模型/等工具/等用户；同 URL 不同身份测试不被去重；前端 node --check 通过，无构建步骤。

### PR-11：Redis 协调与可靠任务分发

当前仓库已有 `bus/redis_bus.py` 的 Pub/Sub 实现、`BusConfig.redis_url` 和可选 redis 依赖，但它们不等于可靠任务队列。保留该通知适配器并补齐生命周期，再单独实现 Streams 调度，不把 EventBus 的三种后端描述为完全相同的交付语义。

修改：`bus/redis_bus.py`、`bus/__init__.py`、`config/settings.py`、`runtime.py`、`web/sessions.py`、`gateway/gateway.py`、`tools/builtin.py`、`agents/ring.py`、`__main__.py`、`configs/system.example.yaml`。新增 `coordination/redis_client.py`、`coordination/streams.py`、`coordination/limits.py`、`coordination/leases.py`、`execution/worker.py`、部署示例与运维说明。

职责划分：

| 组件 | 保存内容 | 数据丢失后的处理 |
| --- | --- | --- |
| SQLite | 任务状态、调用/决议、检查点、事件、outbox、执行代次 | 从一致性备份恢复；不能靠 Redis 补造权威结果 |
| Redis Pub/Sub | 事件/审批已变更的唤醒通知 | 按 SQLite 游标补读，不据此判断是否执行过 |
| Redis Streams | 任务与结果信封、待确认消息 | 根据持久任务和调用状态核对重建；未知副作用不能重放 |
| Redis 缓存/心跳/限速 | 可重建摘要、短期活跃信息、共享速率状态 | 缓存可回源；租约和限速不能直接清空后无约束执行 |
| 文件系统 | 附件、日志、截图、完整证据与报告 | 按证据索引校验并备份；不把大文件塞入 Redis |

**阶段 A：通知、连接管理与共享限速，PR-04 后开始。**

1. 每进程使用受管理的连接池，启动做连通性/能力检查，关停关闭连接并回收后台任务。重连后恢复订阅；start 前登记的订阅也要生效；取消订阅时按引用计数管理远端模式订阅。
2. 键/频道统一命名 `ss:<deployment_id>:session:<session_id>:...`；跨会话共享的供应商限速单独置于 deployment 级。消息携带 session_id、run_id、schema_version、稳定 event/invocation ID。禁止不同会话共用默认频道后仅靠 topic 区分。
3. Pub/Sub 只传唤醒信息；SSE 仍从 SQLite 按会话 event_id 补读。Redis Stream ID 与 SQLite event_id 是不同游标，不可互换。给控制台保留周期性补读，避免静默丢通知。
4. 模型 RPM 按实际供应商账号/凭据引用跨会话共享；目标 RPS 按规范化目标与授权策略定义 key。用 Redis 端时间和原子操作实现限速，等待只延后调用，不终止调查或降低模型决策权限。
5. RingLedger 可缓存脱敏摘要，但按会话和版本隔离；必要结论仍先落 SQLite。去重仍区分身份、请求体和测试目的，缓存不命中不触发额外副作用。

**阶段 B：Streams 与 worker，PR-05 后开始。**

1. 单机进程边界先定为：一个协调器负责会话持久状态、审批和调度；worker 执行已领取任务，通过本机受控接口核对授权、领取执行许可并回报结果。HTTP/浏览器有状态资源归属特定 worker，任务携带能力要求和 affinity，不能随机迁移活跃身份。
2. 派发顺序：SQLite 事务写任务及 outbox → dispatcher 发布 Streams → worker 消费并请求领取 → 协调器用数据库条件更新授予 execution_generation → worker 经统一 InvocationService 执行 → 协调器提交结果、证据引用、检查点和事件 → ACK。
3. 任务/结果消息仅包含引用和脱敏摘要，不携带 Cookie、密钥、审批凭据或大段脚本明文。领取前必须核实任务仍有效、计划版本和参数摘要未变。等待审批的任务先持久化等待状态，释放工作槽，获批后通过 outbox 唤醒，不靠占住 worker 等待 Future。
4. Streams 使用消费组和 pending 恢复；先核对数据库执行状态再决定接管。outbox 发布后进程崩溃可能再次发布，因此以 invocation_id 和执行代次去重，不能以 Redis 消息 ID 作为业务幂等键。
5. 租约记录 owner/token/TTL，续租和释放只允许匹配 token 的持有者操作；数据库执行代次单调递增，旧代次结果不能覆盖新状态。TTL 到期只能触发核实，不能单凭锁过期再次执行。旧执行者仍可能对外产生副作用，必须先停止/核实，无法确定则进入 unknown。
6. 交付语义明确为允许重复投递、按持久状态幂等处理；不承诺网络副作用 exactly-once。失败、拒绝、取消、未知分别处理，不能统一 ACK 后重试。
7. 消息体非法等传输故障可隔离到待处理队列；保留原始引用和原因。基础设施重试策略不成为调查次数上限，也不能把隔离状态伪装成目标完成。
8. Streams 保留策略以已入库消费位置与恢复需求为依据；不能用随意 MAXLEN 裁掉未处理任务。任务队列和可淘汰缓存分离实例，或提供等效隔离并验证；仅不同逻辑 DB 不能隔离实例级内存淘汰。可靠队列实例采用明确的持久化与 noeviction 策略，内存不足时公开报错并保留 SQLite outbox。

建议配置契约（实施时加入配置模型与 example，不是当前已支持的字段）：

```yaml
coordination:
  backend: redis                  # inprocess 保留单进程开发路径
  redis_url_env: SPIRITSCOUT_REDIS_URL
  namespace: spiritscout-dev      # 由部署标识派生，不能多部署共用
  worker_concurrency: 4
  lease_ttl_seconds: 60
  heartbeat_seconds: 10
  connect_timeout_seconds: 5
```

worker_concurrency 是执行并发配置；租约和连接超时是存活/故障检测参数，均不作为模型任务回合顶。部署凭据走环境变量/秘密引用；不得在 API 状态、日志或异常中输出带密码的 redis URL。

故障与降级：

- 显式选择 Redis 但启动不可达时，展示可读错误；不能静默切到 inprocess 形成两个调度域。
- 运行中失联时，已获得有效执行许可的在途动作收尾并先可靠记录；结果上报失败要本地暂存、后续去重回传，不能丢结果或自动再执行。协调器暂停新派发，等待重连或用户明确切换。
- 审批服务/共享限速无法核实时，等待协调恢复；不得自动批准或绕开全局限速。用户停止请求先持久化，worker 通过控制通道与执行检查点落实停止，不能只广播一次 Pub/Sub。
- Redis 重启或 Streams 丢失后，协调器对 SQLite 未结束调用、结果暂存和 outbox 做核对。Pub/Sub 通知、缓存、心跳可重建；有副作用且结果未知的调用仍按 PR-05 处理。
- 切回 inprocess 必须显式停派发、确认原 worker 已停止或隔离、核对未决结果，并记录调度代次变化；不在线无缝双跑。

部署范围：首版只支持单机共享工作区与本机 SQLite 协调器。多机器 worker 需要受认证的远端控制协议、可访问的证据存储和明确的执行回报机制；多协调器高可用还需独立验证数据库与领导权设计，不因加入 Redis 就宣称已完成分布式架构。

验收：新增 `test_redis_notifications.py`、`test_redis_streams.py`、`test_redis_limits.py`、`test_worker_recovery.py`，在临时真实 Redis 和独立 worker 进程上执行。覆盖订阅恢复、跨会话隔离、重复投递、ACK 前崩溃、过期租约、旧代次结果、Redis 重启/不可达、队列内存压力、全局限速、多 worker 停止和拒绝审批。测试实例与正式 Redis 隔离，不能清空用户实例。要求重复消息不导致重复副作用、旧结果不覆盖新状态、断线恢复后证据与事件可补齐，inprocess 模式也保持通过。

### PR-10：建立场景验收和安全迁移

新增 `tests/scenarios/`：本地、可重置、仅绑定回环的测试应用和进程测试 harness。可控模型端点负责返回规划/工具调用，不能用它替代真实模型质量验收。

| 场景 | 必须断言 |
| --- | --- |
| 只读 HTTP 与正常重定向 | 结果与证据完整，范围说明正确 |
| 重定向/DNS 到禁打目标 | 被禁服务连接计数为 0 |
| 审批拒绝与并发取消 | 动作未执行，其他等待者可继续 |
| 长进程中停止/关停 | 无子进程残留，后续无写入 |
| 工具成功后进程崩溃 | 恢复复用持久结果，不重复副作用 |
| 写请求超时、结果未知 | 不自动重放，提供核实/请示路径 |
| 两账号与 CSRF | 身份隔离、流程状态更新正确 |
| 纯附件 CTF | 无网络假目标也可完成本地分析 |
| 错误 Flag 与正确 Flag | 拒绝不宣告成功，接受通知关联正确题目 |
| 长期规划 | 超过旧 20 轮仍由模型继续/结束 |
| SSE 重连与分页 | 新旧事件顺序正确，无遗漏、无重复呈现 |
| Redis 重启/通知丢失 | SQLite 状态保留，事件补读，未决调用按状态核对 |
| Streams 重复投递/ACK 前崩溃 | 同一调用不因重复消息再次产生副作用 |
| 多 worker 与租约过期 | 旧执行代次不能覆盖新状态，未知副作用不自动重放 |
| Redis 限速与跨会话隔离 | 多进程总速率符合配置，任务/审批不会串会话 |
| wheel 干净安装 | 静态资源、模板与可选能力提示正常 |

真实模型验收单独执行一组授权的本地任务，记录成功率、错误结论、重复操作、首个有效证据耗时、恢复成功率和身份串用次数。token/金额仅记录，不作为执行熔断条件。

迁移要求：

1. 迁移前生成可恢复备份；SQLite 运行中备份使用一致性方式，不直接只复制主文件而忽略 WAL。
2. schema_migrations 顺序升级，原有八层记录和报告保留；旧 meta/JSONL 只做兼容读取/导入。
3. 旧会话无检查点时标注“从历史证据重新规划”；不要把旧 running 记录直接当作仍有真实执行者。
4. 新旧客户端 API 通过版本或兼容响应过渡；事件游标不得混用。
5. 代码回滚仅保证在兼容 schema 下读取；已经产生新格式运行数据时，先恢复备份或使用适配器，不能假定降级无损。备份后的新成果须另外保全。
6. Redis 启用前分配独立部署命名空间，确认协调器/worker 版本和持久化配置一致；切换后依据 SQLite 核对未决任务、outbox 和 Streams pending。旧通知模式与新可靠任务分发分别迁移，不能仅更改 bus.backend 就声称可靠队列已启用。

## 四、第一批可以直接开始的工作

先实施 PR-01 与 PR-02，不同时重写调度器。它们解决当前可复现问题，并为后续持久化提供稳定工具契约。

第一批交付清单：

- [x] 新增严格请求 schema，修复审批布尔转换与坏输入 500。
- [x] 修复模型字段泄露、供应商密钥更新顺序、slash 名称和前端事件绑定。
- [x] 补齐 wheel 资源与 Docker 参数格式化。
- [ ] 完整 invocation/result 契约（已落地调用 ID、摘要、执行/效果状态、审批绑定、加密参数/结果与 task/route/agent 归属；identity 实际绑定、证据引用及安全参数预览待补齐）。
- [x] 审批绑定调用，通用执行入口不能以 readonly 免审。
- [x] 修复 CLI/sandbox/Docker 取消清理和共享 Future 取消传播。
- [x] 运行 unittest、ruff、node 语法检查及对应定向复现。

第一批结束的验收证据应包含：测试输出、wheel 资源清单、被拒调用未执行记录、取消后无进程/无延迟写入记录，以及临时配置中的密钥不泄漏检查。提交说明列出已解决的 B 编号及仍存在的边界，不能只写“完成架构优化”。

Redis 接入安排：完成 PR-04 后实施 PR-11 阶段 A，先用真实 Redis 验证通知、隔离和共享限速；完成 PR-05 后实施阶段 B，分离 worker 并验证 Streams 恢复。无需等待所有 UI/浏览器功能完成，也不在第一批直接把现有 Pub/Sub 当任务队列使用。
