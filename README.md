# 灵枢（SpiritScout）

灵枢是一个多智能体安全测试系统，支持 SRC 漏洞测试、CTF 解题和智能体竞赛。
主 Agent「灵枢」负责规划和调度，专项 Agent 并行执行。后端提供工具、状态存储、
审批和审计，具体测试步骤由模型结合任务与现场信息决定。

<img width="1606" height="838" alt="Benchmark.jpg" src="https://github.com/user-attachments/assets/f5cea640-aa57-40d2-9d21-919960aadd64" />

## 主要功能

- **会话与任务**：按模式启动任务，运行中可发送消息、补充附件或暂停。会话记录
  持久保存，重启后可以查阅和续传。
- **并行执行**：灵枢通过 DAG 分派任务；子 Agent 共享进展、尝试记录和结果。
  角色、模型和工具权限可在编队页查看。
- **执行记录**：会话页显示模型返回的进展、工具调用和结果。工具详情可折叠，
  支持历史分页、审计导出和拓扑查看。
- **子 Agent 对话**：右侧切换执行概况与旁路对话，也可从工作台查看各实例的活动。
- **漏洞复测**：候选交给「鉴真」独立复测，按用户规则判断实际影响；确认结果
  进入报告，其余状态保留审计记录。
- **工具与知识**：支持内置工具、隔离沙箱及 MCP（stdio/http/sse）。MCP 与 Skill
  可手动管理，也可通过资源助手提出变更、确认后执行。密钥配置加密保存。
- **项目归档**：按项目保存会话、资产和发现，供同项目后续会话参考。项目归属
  不扩大授权范围；删除项目保留记录并移至未分类。
- **模型与缓存**：支持角色模型指派、主备切换和流式工具调用。追加式上下文和
  增量事实减少重复输入；按端点返回值统计缓存命中率，缺失指标标为未提供。
- **消耗统计**：仪表盘按北京时间显示费用估算、Token、输入缓存和确认成果，
  支持时间范围与模型、角色、会话分组。费用估算与供应商账单分开。
- **报告与经验**：主 Agent 汇总 SRC 报告或 CTF WriteUp；会话结束后提炼经验，
  可按来源和标签检索、删除。只读秘书可提供带记录引用的建议。
- **审批**：SRC 的危险动作逐次请求批准；CTF 和智能体模式的工具操作自动审批
  并留审计。智能体模式禁止公开网页搜索。

## 架构总览

```
用户 ──任务──▶ 灵枢（主 Agent）
                │ DAG 动态规划 · 复杂度裁定 · 复盘收敛
                ├──▶ 专项智能体集群（并行分支 + 持久对话）
                │      业务体验 │ 斥候 │ 听风 │ 破壁 │ 贯甲 │ 深耕
                │      蛛丝(CTF Web) │ 破壳(Pwn) │ 回溯(逆向) │ 译码(密码) │ 拾遗(杂项)
                ├──▶ 环形台账（已尝试登记/避让 + 督导建议）
                └──▶ 黑板（资产/线索/漏洞/任务态/发现台账）
工具层：http_probe（逐跳范围标注）· tcp_scan · bwrap 沙箱 · 文件分析 · MCP
护栏层：模型上下文测试边界 · 风险审批 · 全局限速 · 审计留痕 · 密钥加密库
```

| 组件 | 职责 |
| --- | --- |
| `agents/master` | 灵枢：元规划、下发决策、复盘收敛、用户直达通道 |
| `agents/hostops` | 听风（端口测绘）、贯甲（服务审计）、深耕（横向） |
| `agents/recon` `webvuln` `business` | SRC 侦察、漏洞探测、业务上下文 |
| `ctf/` | 五方向解题智能体、Flag 管理、WriteUp |
| `blackboard/` | 黑板 8 层（资产/线索/漏洞/任务态/拓扑…）与汇聚引擎 |
| `gateway/` | 多模型网关：按模型指派路由、主备切换、流式工具调用 |
| `policy/` | 范围事实（上下文规则/白名单标注/扩围）、风险审批、提问通道 |
| `tools/` | 内置工具 + MCP 客户端 + bwrap 沙箱 + 注册表权限视图 |
| `verify/` | 漏洞全链路核验（复现/误报/合规，多模型投票） |
| `web/` | FastAPI 控制台：SSE 实时流、拓扑、仪表盘、会话管理 |

## 部署方式

两种方式并存，共用同一份源码与同一个运行数据目录 `.spiritscout/`；
同一时间只启动一个实例监听同一端口。

### 方式一：本地 Python（默认）

```bash
git clone https://github.com/FLIAZ/SpiritScout.git
cd SpiritScout
python3 -m venv .venv && .venv/bin/pip install -e '.[toolkit]'

./launch.sh                # 前台启动，Ctrl+C 停止（默认 8000）
./launch.sh stop           # 停止控制台（有运行中会话时会拒绝，需 --force）
./launch.sh --host 0.0.0.0 # 其余参数原样透传给程序
./launch.sh --port 10010   # 指定端口
```

沙箱依赖 bubblewrap（`sudo dnf install bubblewrap` 或发行版等价命令）。

### 方式二：Docker（沙箱开箱可用，主机无需 Python/venv）

镜像内已装好 bubblewrap，主机不必安装：

```bash
cp .env.example .env        # 填入管理员用户名与密码
docker compose up -d --build
docker compose logs -f      # 看日志
docker compose down         # 停止
```

- 端口默认 `127.0.0.1:10010`（只发布到回环）；源码以 `./:/app` 挂载，
  改代码立即生效，无需重建镜像；
- 运行数据与本地部署一致，会话/审计/报告都落在 `.spiritscout/`；
- 换国内源重建：
  `docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`；
- 需要 CTF 工具箱（pwntools / pycryptodome / z3 / scapy / binwalk / sqlmap 等）：
  `docker compose build --build-arg INSTALL_TOOLKIT=1`（镜像更大、构建更慢）。

### 启动后配置

打开控制台（本地 Python 默认 `http://127.0.0.1:8000`，Docker 默认
`http://127.0.0.1:10010`）后按顺序配置：

1. **模型**：「模型」页添加供应商（地址 + API Key，密钥加密存本机），
   获取或手填模型 ID，声明思考强度取值与能力；
2. **编队**：「编队」页把模型按 ID 指派给灵枢与各角色（未指派时按模型
   列表顺序路由）；
3. **下达任务**：首页先选择 SRC 渗透测试、CTF 解题或智能渗透赛，
   然后描述目标与授权范围，可随附脱敏 HAR 或题目附件。

MCP 服务器（ida/yakit 等）在 `configs/system.yaml` 配置，开工时握手，
不可达只留痕不阻断。

两种方式监听非回环地址时都必须配置管理员凭据。启动时会把 `.env` 中的
`SPIRITSCOUT_ADMIN_USERNAME` / `SPIRITSCOUT_ADMIN_PASSWORD`（旧版
`SPIRITSCOUT_SERVER_PASSWORD` 仍兼容）导入工作区 `admin.db`，密码只保存
scrypt 加盐哈希。控制台使用与主界面同主题的登录面板；未认证的 API 请求
返回 401，浏览器页面跳到 `/login`。

**忘记用户名或密码**：修改 `.env` 中的两个变量并重启服务即可覆盖恢复；
恢复后可移除变量，数据库中的凭据继续有效。本地回环且数据库与环境变量都
没有管理员时保持免登录（与旧版一致）。远程访问请在反向代理上配置 HTTPS。

## 使用指南

### 浏览器 MCP：自动解题与 SRC 登录态

`chrome` MCP 按用户选择的任务模式装配：智能体/CTF 模式使用会话独立的无头浏览器，
无需启动 CDP 调试端口，也不读取用户已有登录态；SRC 模式连接现有浏览器复用登录态。
工具注册前会验证真实浏览器连接，连接失败会留痕并跳过，不将握手成功误报为浏览器可用。

源码自动解题环境需安装 Chromium 或 Google Chrome；可通过
`SPIRITSCOUT_CHROME_EXECUTABLE` 指定浏览器可执行文件。Docker 镜像包含 Chromium。
以下 CDP 配置与人工接管说明仅适用于 SRC 模式。

#### SRC 可选：随 Docker 部署可操作浏览器

项目包含 Chromium、虚拟桌面与 noVNC，按需启用 `src-browser` profile：

```bash
docker compose --profile src-browser up -d --build --wait
```

准备好 `.env` 中的控制台密码后执行上述命令，等待浏览器健康检查通过再下发 SRC
任务。控制台地址为 `http://127.0.0.1:10010`，浏览器操作页为
`http://127.0.0.1:10011`，两者用户名均为 `spiritscout`，密码一致。
收到登录、验证码或滑块提问时，在操作页完成后回会话回复「已完成」。

默认 `docker compose up -d --build` 只启动主服务，智能体/CTF 的无头浏览器在主服务内
独立运行。共享浏览器不作为主服务的启动依赖；其故障不阻止主服务启动。
启用 profile 后，SRC 默认通过 Compose 内网的 `http://browser:9223` 连接浏览器。
CDP/VNC 不发布到宿主机，两个网页端口只绑定本机回环；远程使用时可通过 SSH 转发
`10010`、`10011`，或配置支持 WebSocket 的 HTTPS 反向代理。

SRC 浏览器的持久登录态保存到 Docker 卷 `browser-profile`，重启和重建容器后保留；
`docker compose --profile src-browser down -v` 会删除该卷。不同 SRC 会话共享这个
浏览器，避免同时操作同一页面；智能体/CTF 的隔离浏览器不读取这个卷。
内置浏览器为标准 Chromium，不含商业指纹管理能力。首次构建需要联网下载依赖。

Fedora 等启用 SELinux 的主机使用项目挂载的 `:z` 选项设置容器共享标签；浏览器日志
直接输出到标准错误，不通过 `/dev/stderr` 的 proc 路径创建日志文件。无需关闭 SELinux
或导入宽泛的放行策略。该挂载会重标记当前项目目录，源码与运行数据仍保存在宿主机。

#### SRC 可选：已有 Chrome / 指纹浏览器

已接入 `chrome-mcp-kit` 的 CDP 链路：`chrome` MCP 通过 Playwright 连接 Windows
现有浏览器，用户在那个浏览器窗口里完成登录、验证码或滑块，Agent 收到答复后继续。
复用原有浏览器的配置档案与登录态；不需要在灵枢页面中另造浏览器窗口。

- `configs/system.yaml` 已启用 `chrome`，原来的无头 `playwright` 改为可选关闭。
  默认授权给业务体验 Agent 与 CTF Web Agent，其他支持动态 MCP 的角色可在
  `allowed_agents` 中显式加入；危险操作继续逐次审批。
- 先确保原有 kit 的 Windows 浏览器及 `9223 → 9222` 转发器正在运行。无需重新执行
  `artex-add-chrome-mcp.py`，也无需修改 ARTEX 或其他 Agent 的配置。
- 使用外部浏览器时，在 `.env` 中设置
  `SPIRITSCOUT_CHROME_CDP_URL=http://实际可达IP:9223`，无需启用 `src-browser` profile。
  Docker 会自动解析 `host.docker.internal` 为 IP；源码部署在 WSL 中使用自己的默认
  网关，其余环境使用本机回环。不能把 WSL 网关直接当成 Docker 网关。
- Docker 镜像内置 Node.js 22、Chromium 和固定版本的 MCP，无需在运行时下载浏览器。源码部署
  需安装 Node.js 22+；默认通过 npx 安装固定版本，已有 MCP CLI 时可设置
  `SPIRITSCOUT_CHROME_MCP_CLI` 为其 `cli.js` 路径。
- `--caps storage` 提供 `mcp__chrome__browser_cookie_list` 等 Cookie 工具，能读取
  HttpOnly Cookie；不要用 `document.cookie` 代替完整登录态读取。手动完成验证后，
  在会话提问处回复 Agent，由它重新检查页面与 Cookie。

该接入使用的是共享的浏览器配置档案，登录态会随原浏览器保存；它不提供会话之间的
自动隔离。同一浏览器应由一个任务操作，避免与其他 Agent 同时控制同一页面。
指纹配置由原浏览器提供，灵枢不改写浏览器指纹、代理或已有配置。

#### 外部浏览器的启动与人工接管

1. 启动已有 kit 的 Windows 浏览器和 CDP 转发器，保留浏览器窗口供用户操作。
2. 在项目根目录执行 `docker compose up -d --build spiritscout`，打开
   `http://127.0.0.1:10010`。源码部署按上文启动，另需准备 Node.js 22+。
3. 在 SRC 模式下向业务体验 Agent 下达浏览器任务，例如：
   「打开已授权站点，遇到登录或验证码时提问等我操作，完成后重新读取登录态再继续。」
4. 收到 Agent 提问后，在 Windows 浏览器中完成登录、验证码或滑块，再在会话中
   回复「已完成」。Agent 会重新检查页面和 Cookie，确认成功后继续；浏览器中的变化
   在下一次工具读取时可见，不会自动推送 Cookie。

截图等浏览器产物写入当前会话的 `workspace/browser-output/`。Cookie 属于登录凭据，
只在当前任务需要时读取，避免复制到项目记忆、报告或聊天分享中。

#### 连接排查与升级说明

- 若 SRC Chrome MCP 不可达，先在实际运行灵枢的环境中访问
  `http://实际可达IP:9223/json/version`，确认返回浏览器版本与
  `webSocketDebuggerUrl`；Windows 本机连通不代表 WSL 或 Docker 内也能连通。
  修正转发器或 `SPIRITSCOUT_CHROME_CDP_URL` 后重新启动服务。
- 源码首次使用需通过 npx 下载固定的 `@playwright/mcp@0.0.83`；离线环境可预装
  并用 `SPIRITSCOUT_CHROME_MCP_CLI` 指向运行环境内可访问的 `cli.js`。
- 升级已有部署时需重建 Docker 镜像以加入 Node.js、Chromium 与 MCP；SRC 内置
  可操作浏览器用 `docker compose --profile src-browser up -d --build --wait` 构建启动。
  使用自定义配置的用户
  请参考 `configs/system.example.yaml` 添加并启用 `chrome`；原无头 `playwright`
  可按需保留，但它不共享这个 Windows 浏览器的登录态。

### 会话操作

- **下达任务**：用户先选择任务类型，再描述目标与意图；后端直接按选择装配
  编队，不调用模型识别模式。智能体模式不限定赛制，原始规则交模型理解与规划。
  续聊保留意图理解，模式保持用户选择；旧自动会话兼容。
- **会话协作**：底部输入坞随时发消息、传附件——消息直达灵枢，回执即时
  可见，要点转达执行中的智能体；规则与约定先于任务下发也井然有序；
  终态会话发消息即带现场续传。
- **智能体模式全自动**：用于无人值守竞赛，所有工具操作预授权并审计留痕，
  不请求批准、不向用户提问。独立题目按列表顺序推进，卡题留档后先跳过；
  执行期间后台补充待发队列，通关释放容器后立即补位，平台允许时持续三题并行。
- **CTF 自动审批**：普通 CTF 的所有工具操作自动批准，逐次记录调用与批准结果，
  不弹出命令审批。任务信息缺失或授权范围不清时仍可向用户提问。
- **SRC 的提问与审批**：遇到范围外目标或路线分叉时会发起提问（问题与
  选项由智能体撰写），在输入区作答；勾选目标即授权扩围；高风险动作
  触发时即时确认。
- **拓扑图**：灵枢 → 任务 → 路线分支 → 发现台账，全部实时；支持平移、
  缩放、复位。
- **报告**：CTF 由主 Agent 汇总 WriteUp，SRC 产出经全链路核验的渗透报告，
  均可从会话页下载。

## 授权边界与安全模型

- **测试边界由模型上下文维护**：用户原话、项目规则与后续答复完整传给
  各 Agent；后端不编译硬性禁打列表。旧 `denied` 字段仅作为排除声明兼容。
  用户消息和询问答复在接收时即原样落盘，跨会话恢复、派单与上下文压缩保留；
  不按关键词分类或生成禁打规则。这不提供确定性阻断保证：模型必须自行遵守
  规则；SRC、普通 CTF 边界不清时先询问用户，智能体模式依据原始任务与赛方规则自主决策。
- **授权白名单是初始范围**：范围外目标不会被拦截——智能体向你提问，
  你确认后目标写入会话批准集；批准答复原文留在共享上下文中供恢复后理解。
- **敏感动作默认禁用**（反弹 shell/WebShell/容器逃逸/爆破），触发时刻
  即时审批，决策不可代答、不设超时；智能体模式的工具操作采用全自动预授权。
- **沙箱隔离**：整机只读挂载、主目录与宿主私有安装层遮蔽、工作目录
  会话内持久、完整输出落盘可查。
- **密钥不落明文**：API Key 经 Fernet 加密存本机密钥库。
- **全程审计**：每个决策、工具调用、Flag 提交、用户消息均有审计事件，
  可从会话页导出。

仅限已获得书面授权的目标与赛事。

## 配置

| 文件 | 用途 |
| --- | --- |
| `configs/system.yaml` | 主配置：工作区、执行器、MCP 服务器、网关、总线、黑板 |
| `configs/models.yaml` | 供应商与模型注册表（也可在控制台「模型」页维护） |
| `configs/agent_models.yaml` | 角色到模型的指派（「编队」页维护） |
| `configs/system.example.yaml` | 带注释的配置示例 |
| `Dockerfile` | Docker 部署：镜像构建（Python 3.11 + bubblewrap 沙箱） |
| `docker-compose.yml` | Docker 部署：端口发布、管理员凭据注入、源码与数据挂载 |
| `.env` / `.env.example` | 管理员用户名/密码与构建参数（启动时导入 `admin.db`；`.env` 不入库） |

共享资源默认保存于 `<workspace>/resources/`，可用 `resources.root` 指定持久目录。
`resources.freshness_hours` 控制“近期观测”提示（默认 24 小时），不会把历史标记为本次已验证。
`resources.npm_registry` 默认为 npm 官方仓库，可指向组织内仓库。MCP 配置变更对
新启动的会话生效，运行中的连接不会被替换；Skill 在下一次参考检索时读取当前版本。

模型默认使用 `prompt_caching: provider_default`，不附加缓存参数。确认端点支持
Anthropic Messages 显式缓存后，可在模型编辑页选择 `anthropic_ephemeral`。
可按实际美元报价配置 `cost_cached_input_per_mtok` 与 `cost_cache_write_per_mtok`；
未配置缓存单价时费用仍按普通输入单价估算。缓存命中收益需以端点实际返回用量评估。

## 目录结构

```
src/spiritscout/
├── agents/          # 灵枢（master）+ 各专项智能体 + 角色档案（profiles）
│   ├── hostops/     # 听风 / 贯甲 / 深耕（SRC 主机与横向专项）
│   └── master/      # DAG 元决策状态机
├── ctf/             # CTF 五方向智能体、Flag 管理、WriteUp
├── blackboard/      # 黑板分层存储与汇聚引擎
├── gateway/         # 多模型网关与注册表
├── policy/          # 范围声明与标注、风险审批、用户提问
├── tools/           # 内置工具、MCP 客户端、bwrap 沙箱、注册表
├── knowledge/       # 经验知识库（红队/SRC 检索 + 平台规则）
├── modes/           # 模式装配器（SRC/CTF/渗透赛）
├── competition/     # 渗透赛赛制引擎
├── traffic/         # HAR 导入（脱敏）
└── web/             # FastAPI 控制台与前端
```

## 开发与验证

```bash
.venv/bin/python -m pip install -e '.[dev]'  # 固定版本的开发检查工具
LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m unittest discover tests/
.venv/bin/python -m ruff check src/ tests/     # 静态检查
```

测试遵循「mock 不等于真实体验」原则：单测覆盖确定性机制（权限、上下文保真、
状态机），行为质量以真实任务在浏览器中实测为准。CI 使用轻量 test 依赖，
覆盖 Python 3.11/3.14 和 Node 22；沙箱中的外部工具仍需按部署环境另行验收。
前端修改同时执行 `node --check`，并运行 `tests/test_*.js` 回归。

用量逐笔写入会话目录的 `usage.db`（SQLite WAL 事务），首次打开时导入旧
`usage.json`，保留原文件；备份运行中的数据库请使用 SQLite 备份接口，
或停服后复制。旧版程序无法读取新增的数据库计量，回退前需另行导出。
读取结果按数据库/WAL 文件版本缓存，单文件仅保留最新快照。
资产库启动导入按历史记录游标增量执行；`POST /api/assets/sync` 可全量重扫历史会话。
会话列表与状态由单一调度器兜底刷新，隐藏页面降频，SSE 继续提供即时事件。

## 法律免责声明 & 使用合规须知

### 1. 工具定位

本项目（以下简称「本工具」）为网络安全学术研究、授权安全测试、企业内部风险自查用途的开源技术工具。

本工具所有功能仅面向拥有合法授权的测试场景，旨在提升网络安全防御能力、助力漏洞合规治理、推进安全技术学术研究。

### 2. 严格授权使用原则

所有使用者必须提前获得目标系统的书面合法授权。

严禁在无授权、越权、超范围情况下，对以下对象进行扫描、探测、渗透、数据获取、流量分析、自动化测试：

- 互联网公网任意未授权系统
- 金融、能源、政务、运营商、交通等关键信息基础设施
- 第三方企业、个人、公有云任意业务系统
- 任何法律法规禁止测试的信息系统

未授权测试即属于违法行为，使用者自行承担全部法律责任。

### 3. 禁止滥用条款

所有使用者承诺：

1. 不得利用本工具实施非法入侵、非法控制、数据窃取、流量攻击、漏洞滥用等违法犯罪行为；
2. 不得将本工具用于黑产、牟利、恶意破坏、未授权批量扫描、网络骚扰等场景；
3. 不得绕过、破解本工具内置的安全限制、黑白名单风控机制用于非法用途；
4. 不得用于危害国家网络安全、扰乱公共秩序、侵犯他人合法权益的活动。

### 4. 责任边界（核心免责）

1. 项目开源作者、维护团队、贡献者：仅提供公开的技术代码与学术实现，不参与、不知情、不协助任何第三方的非法使用行为，不对第三方滥用产生的任何后果承担刑事、行政、民事连带责任。
2. 本工具技术中立，合法合规用途明确，任何使用者的违法操作均为独立个人行为，与项目作者无关。

### 5. 强制合规义务

使用者使用本工具即默认表示：

- 已充分知晓《中华人民共和国网络安全法》
- 已充分知晓《中华人民共和国刑法》第285条、第286条相关规定
- 已具备合法网络安全测试资质或授权资质
- 自愿承担所有使用风险及法律后果

### 6. 违规处置机制

项目团队保留以下权利：

- 随时关停仓库、终止分发、更新风控限制
- 举报、记录、留存恶意滥用行为证据
- 拒绝为非法使用者提供任何技术答疑、指导、更新、协助

### 7. 最终声明

本工具严禁非法滥用。任何违法使用行为，由行为人独立承担全部法律责任，与项目开发、维护、贡献团队无关。

## 致谢

### 知识库数据来源

- [PayloadsAllTheThings](https://github.com/swisskyrepo/PayloadsAllTheThings)（MIT）——载荷与方法论主体来源，按本项目条目结构重写并补充靶场实测验证
- [NoorQureshi/security-skills](https://github.com/NoorQureshi/security-skills)（MIT）——206 条安全技能（Web/云/AD/移动/逆向/社工），原样接入供智能体检索

### 参考的开源 Agent 项目

- [carlospolop/HackTricks](https://github.com/carlospolop/HackTricks)（CC BY-NC-SA）——方法论思路参考，内容未直接使用
- [OWASP WSTG](https://github.com/OWASP/wstg)（CC BY-SA-4.0）——测试标准思路参考，内容未直接使用
- [GTFOBins](https://github.com/GTFOBins/GTFOBins.github.io)（GPL-3.0）——Unix 二进制利用思路参考，内容未直接使用
- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)（MIT）——智能体技能目录（1000+ 条）

### 项目使用的 MCP 服务器

以下 MCP 服务器在 `configs/system.yaml` 中已配置或提供示例，智能体开工时自动握手并按角色权限收窄后并入工具目录：

| MCP | 许可 | 状态 | 用途 |
| --- | --- | --- | --- |
| [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp) | 自定义 | 已启用 | 逆向：反编译 / 交叉引用 / 函数与类型检索（无头 idalib） |
| [yakit](https://github.com/yaklang/yakit) | 自定义 | 已启用 | Web 渗透：编解码 / HTTP 流量 / 模糊测试 / 指纹 / 端口 / 反序列化 |
| [playwright-mcp](https://github.com/microsoft/playwright-mcp) | Apache-2.0 | 已启用 | 浏览器自动化：JS 渲染 DOM / 点击 / 表单 / 截图 |
| pentester-mcp | 自定义 | 按需 | 渗透工具集：nmap / nuclei / sqlmap / ffuf / impacket / hydra |
| burp-mcp | 自定义 | 按需 | Web 漏洞扫描与模糊测试（PortSwigger 官方扩展） |
| shodan-mcp | MIT | 按需 | 资产情报（出网至第三方服务） |
| virustotal-mcp | MIT | 按需 | 文件 / 域名情报（VirusTotal 官方托管端点） |

### 技能源

- [obra/superpowers](https://github.com/obra/superpowers)（MIT）——通用工程方法论技能集
- [anthropics/skills](https://github.com/anthropics/skills)——SKILL.md 格式规范参考（无 LICENSE，仅格式参考，内容未使用）
- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)（MIT）——智能体技能目录（1000+ 条）

### 用户




自定义 MCP 可用 `allowed_agents` 指定额外执行角色，例如 `[business_context, ctf_web]`（当前支持业务体验和 CTF 各执行角色）；空列表沿用原有工具范围。该配置不会授予灵枢、秘书或报告角色执行权限，也不会扩大任务授权范围，工具审批仍按会话模式执行。

### 搜索与费用偏好

SRC 和普通 CTF 的 `web_search` 优先复用有效缓存，再使用 Exa 官方免密搜索；免费服务不可用时，通过 curl 获取公开搜索引擎结果。模型也可设置 `source: public` 直接使用公开引擎，并在关键词中组合精确短语、`site:` 和版本/错误特征。公开结果的时效与适用版本仍须核对。

付费 Exa API 仅用于特别困难且明显阻塞进度的问题。模型须基于实际证据自行判断，显式给出 `paid: true` 和 `blocker`，说明已尝试的方法、免费资料为何不足以及关键缺口如何影响下一步。免费额度耗尽、限流或普通查询失败不会自动触发付费请求；有效的旧付费结果可以免费复用。MCP 管理页保存的密钥只供这一受控路径使用。智能体模式禁止所有上述网页搜索路径。

搜索输出先在本地去除 HTML 脚本/样式/导航噪声、重复段落和追踪参数，再合并重复 URL，返回固定字段的紧凑 JSON（每条摘要最多 480 字符，整份最多 4,800 字符）。缓存命中逐字复用结果正文；旧版已付费缓存就地清理并保留原检索时间，格式升级不触发重新检索。缩短结果降低输入量，实际模型缓存命中率仍以端点返回用量为准。
