# 优化实施进展（2026-10-15）

第一、二批改造及补充审计修复已写入工作区。未自动提交 Git；仅在确认线上空闲、无待答问题后正常重载服务，46 个历史会话完整保留。完整路线见 `IMPLEMENTATION_PLAN_2026-10-15.md`；本文件区分实际完成的能力与后续工作。

## 已落地

| 能力 | 实际行为 |
| --- | --- |
| 统一调用记录 | `ToolRegistry.call` 为每次调用生成 invocation_id，复制参数并计算摘要，写入会话 bb.db；ToolResult 保留原字段，追加执行状态与效果状态。 |
| 单次审批 | 审批绑定调用 ID 与参数摘要。不同调用不再复用同类动作许可；旧 ScopeGuard 批准集合也不能绕过调用门。相同调用的共享 Future 仍支持单个等待者取消。 |
| 持久化结果与裁决 | 调用状态、加密结果与状态事件共用黑板连接/事务；审批决议持久化后才继续执行。取消或异常可能留下未知效果，有副作用工具不自动重试。 |
| 结果保密 | 原始结果用 Fernet 加密。会话 execution.key 权限 0600，并已加入 gitignore。已有密文而密钥缺失时拒绝生成替代密钥；备份必须一起保存密钥。参数摘要用于绑定审批；完整参数单独加密保存，供内部核对使用，列表 API 不返回参数密文或明文。沙箱遮蔽密钥文件，并隔离 PID，避免通过宿主 /proc 路径绕过遮蔽。 |
| 黑板可靠通知 | 黑板数据和 bb_event_outbox 同一次 SQLite 提交；提交后发布，失败保留，启动/重连后补送。保持原有进程内事件 payload 兼容，Redis 只传引用。 |
| 进程内背压 | 队列满时等待容量，不再静默丢消息并误标 outbox 已送达；相同事件重复发布不会触发优先级队列比较异常。 |
| Redis 通知 | 受管连接池；启动前/启动后的订阅都有效；启动等待订阅确认；断线由客户端重连；停止时回收监听任务与连接。部署和会话两层命名空间隔离。 |
| Redis 共享限速 | Redis TIME + Lua 原子许可，在同一部署所有会话间限制内置 HTTP、DNS、TCP 探测的合计速率；原会话速率继续生效。断线不绕过限速。 |
| 分页查询 | GET `/api/sessions/{id}/executions` 和 `/execution-events`，支持 after/limit。仅返回元数据；后者使用 execution-v1 游标，与现有 JSONL SSE 行号分离。旧会话查询不创建数据库/密钥。 |
| 旧库保护 | 旧黑板首次增表前使用 SQLite backup API 生成 bb.db.pre-runtime-v1.bak，包含已提交 WAL 数据；不覆盖已有备份。 |

关键代码：`tools/registry.py`、`tools/invocation.py`、`tools/journal.py`、`policy/engine.py`、`policy/approvals.py`、`blackboard/store.py`、`bus/redis_bus.py`、`bus/inprocess.py`、`runtime.py`、`web/app.py`。

## 第二批：检查点与中断核对

- `ExecutionRunLock` 在工作区之外保存排他锁，进程退出时由内核释放；同一工作区第二个运行会被拒绝。初始化异常和关闭路径均释放锁，正式服务的模式入口区分 completed / failed / cancelled。
- 持锁开始新运行后，旧 running 运行标记 interrupted；旧执行中的调用变为 unknown，未开始/等待审批的调用取消，不复用旧审批，不启动补执行。持久化结果保留。该锁只协调遵循本协议的本机进程，切换版本前仍须停止旧版进程；释放锁也不代表所有外部副作用已经结束。
- 调用记录关联 task_id / route_id / agent_id，identity_id 暂未绑定身份管理器。异步 ContextVar 隔离并行路线，CTF 派生子路线覆盖父路线归属。参数单独使用 Fernet 加密，按调用 ID 内部读取。
- 主图四个节点前后保存加密状态，与图的列表 reducer 合并语义一致；路线保存步骤前后快照与计划/结果，CTF 保存解题回合对话。检查点与 checkpoint.saved 事件共用一次事务。检查点尚未与工具结果、黑板任务写入统一事务。
- 开始新任务时读取上次主图检查点，提供目标、轮次、节点状态摘要与未决调用给模型，供模型结合本次用户意图重新规划；不直接把旧 DAG/对话当作执行指令。
- `/api/sessions/{id}/recovery` 和原 state 响应展示运行、最新检查点元数据及未核对计数，不解密输出。已停止会话的旧 running 调用也显示待核对，查看页面不会修改它们的状态。
- 会话页显示待核对提示；审批按钮与说明改成“仅本次调用”。真实可见 Chromium 已验收页面提示、拒绝审批、刷新保留提示、接口无参数/检查点明文，并检查页面异常及 5xx。
- 路线并发改用 TaskGroup；一路异常时取消并等待其余路线，防止任务失败后仍遗留执行者。
- 台账升级到 v2，旧库先使用 SQLite backup API 生成 `bb.db.pre-execution-v2.bak`。v1 结果保持可读；高于支持版本的台账或检查点拒绝自动处理。历史读取仍兼容 v1，不为查看而升级。

这批不自动重建模型未完成的 tool_calls，也没有任务执行的 exactly-once 保证。暂未增加“已核实”人工裁决接口，待核对记录会保留，不能通过关闭提示将未知效果伪装为成功。

## Redis 启用方式

本轮已安装项目虚拟环境的 redis 可选依赖，并用独立临时 Redis 7 容器验收。正式配置保留原 inprocess 默认值，不把测试 Redis 作为正式依赖。

部署时安装依赖：

```bash
.venv/bin/python -m pip install -e '.[redis]'
```

在实际 system.yaml 中配置：

```yaml
bus:
  backend: redis
  redis_url_env: SPIRITSCOUT_REDIS_URL
  namespace: spiritscout-prod
  connect_timeout_seconds: 5
  shared_request_rps: 10
```

使用环境变量 SPIRITSCOUT_REDIS_URL 提供部署地址和凭据。不同部署使用不同 namespace，同一部署各会话使用一致的 shared_request_rps。URL 未提供时兼容既有 redis_url 配置。显式选择 Redis 后连接失败会拒绝启动，不会静默切换后端；统计与错误信息不输出认证 URL。

共享限速当前是部署级合计限制，不是精细的目标/供应商配额。沙箱脚本、外部 CLI/MCP 自行发出的流量尚不受它控制。Pub/Sub 仍是通知通道，不是可靠任务队列；通知丢失时应重读 SQLite，不能据通知次数执行副作用。

## 验收

第二批完整验收 **114 项 Python 测试全部通过，无跳过**，包括真实 Redis/Docker/bwrap 场景；ruff、前端语法检查和仪表盘 Node 回归通过。对应测试为 `tests/test_execution_journal.py`、`tests/test_redis_notifications.py` 与 `tests/test_runtime_persistence.py`。执行：

```bash
# 常规回归；未提供隔离实例时 Redis 集成场景明确跳过
.venv/bin/python -m unittest discover tests/
ruff check src/ tests/
node tests/test_dashboard.js
for f in src/spiritscout/web/static/js/*.js; do node --check "$f"; done

# 实际执行的完整验收：TEST_REDIS_URL 须指向隔离测试实例
SPIRITSCOUT_TEST_DOCKER=1 \
SPIRITSCOUT_TEST_REDIS_URL=redis://127.0.0.1:32771/0 \
.venv/bin/python -m unittest discover tests/
```

Docker 场景只启动/重启/删除测试自己创建的随机命名 Redis 容器；外部实例测试使用随机命名空间，不执行清库。实际覆盖本地 CLI、真实 bwrap 密钥遮蔽与 PID 隔离、进程在副作用之后直接退出、取消、两次审批、并发参数隔离、加密结果重读、API 分页与隔离、事务回滚、WAL 备份、进程内背压、Redis 订阅/命名空间/多连接限速、真实 Redis 停服与重启补送。

环境 `.venv/bin/python -m ruff` 仍不可用，本轮使用系统 `ruff`。这组测试不代表完成真实模型、浏览器身份流或 worker 分布式恢复验收。

## 后续批次

1. task_id/route_id/agent_id 与加密参数已落地；identity_id 为预留字段，仍需身份管理器、安全参数预览与证据索引，当前不是 PR-02 完整契约。
2. 运行生命周期和加密 DAG/路线检查点已落地，历史 DAG 摘要与未知调用会进入模型重规划上下文；后续仍需逐工具调用对话的恢复协议、结果核实闭环、追问/暂停/崩溃恢复的明确入口。当前模式是 checkpoint_replan / history_replan，不能宣称从任意指令位置精确续跑；旧 running 记录也不能证明进程仍存活。
3. 统一调用结果、任务进度、检查点与通知的 UnitOfWork。当前原子性分别覆盖“调用状态+结果+调用事件”和“黑板记录+黑板通知”，尚未跨这两组提交实现统一事务。
4. 完成检查点后再接 Redis Streams、worker、租约与执行代次。不能用当前 Pub/Sub 代替可靠派发，也没有 exactly-once 承诺。
5. 继续实现沙箱实际网络出口约束、身份隔离、浏览器、假设/证据 UI，以及真实模型场景验收。

本轮没有新增固定回合数或 token/金额熔断，也没有修改模型的规划与 Flag 判定权限。

## 提问交互修复

问题根因：提问/审批容器只存在于拓扑画布内；画布 pointerdown 无条件 preventDefault 并捕获指针，拦截卡片交互；每三秒轮询与拓扑重绘使用 innerHTML 重建卡片，导致焦点、答复和勾选丢失。另外，前端使用 addTargets，后端消费 add_targets，导致扩围选择未送达。

现已将提问/审批移至所有会话选项卡共用的独立区域，并按 question_id 保留问题 DOM；轮询、画布重绘和选项卡切换均保留输入。支持选项点击、自由输入、Enter 发送和 Shift+Enter 换行，中文输入法确认不误发；请求期间防重复提交，失败保留草稿并可重试。状态请求有版本校验，迟到快照不会覆盖新会话或复活已回答问题。扩围仅提交用户勾选的 add_targets。

回答接口增加严格 schema 与选项范围验证，空答复、错误类型、越界选项返回 422，不关闭问题或扩大范围；不同会话不能代答，重复回答返回 409。对应新增 tests/test_question_answers.py 的五项回归全部通过。

119 项 Python 测试运行成功，其中 4 项 Redis 集成测试因本轮未配置隔离实例而跳过；系统 ruff、全部前端 node --check 和仪表盘 Node 回归通过。可见 Chromium 在隔离测试服务实测：会话页选项及扩围、拓扑页文字输入、跨三秒轮询与画布重绘保持焦点/草稿、切换选项卡、输入法确认、迟到响应、失败重试、手机宽度自由作答，均通过。

提问修复当时正式服务仍有等待回答的问题，因此当时只直接提供更新的静态资产，没有重启。后续补充审计修复结束时已确认服务空闲，正常重载后新增后端输入校验也已生效。

## 补充审计修复

`BUG_AUDIT_2026-10-15.md` 中的 8 项复现路径已修复：控制面文件从沙箱挂载中隔离、模型停止决策生效、HTTP 解压内存有界、审计参数与历史导出脱敏、技术失败不再标记成功、普通 CLI 后台进程回收、数据库执行排他及密钥归属、并发 Flag 终判幂等。

执行数据库 schema 为 v3；旧库升级前保留在线备份，原有检查点 schema v1 和执行事件游标 execution-v1 保持兼容。新增 19 项回归，全套 138 项中 134 项通过、4 项条件集成跳过；Ruff、JS 检查与可见浏览器提问回归通过。原始审计证据和修复后对照分别保存在 audits/2026-10-15/evidence.json 与 evidence-after.json。
