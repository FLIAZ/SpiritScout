# 灵枢（SpiritScout）

多智能体安全测试系统：主 Agent「灵枢」集中决策，专项智能体集群并行执行，
覆盖 SRC 漏洞挖掘、CTF 解题与渗透赛三类场景。设计原则是 **LLM 优先**——
模型负责全部判断与取舍，后端只做感知、安全护栏与结果流向。

## 核心特性

- **子 Agent 实时工作台**：会话页按实例查看模型明确返回的分析与输出、工具反馈、
  失败原因和结论。SSE 支持历史回放，角色与并行路线分别聚合；端点未提供的分析
  不补写，非流式端点在调用返回后展示正文。工作台可折叠，工具反馈可展开。

- **资源管理工作台**：主页和侧栏提供 MCP、Skill 与资产收集入口。MCP/Skill
  各有独立、持久化的资源助手对话；模型先提出可查看的变更，确认后执行。
  支持 npm MCP 安装（固定版本、禁止安装脚本）、已有命令/HTTP/SSE 接入，
  以及文本或 HTTPS 文档形式的参考 Skill 导入；命令与密钥配置加密保存。
- **跨会话资产记忆**：独立 SQLite 目录按资产身份聚合指纹、端点、服务和历史发现
  摘要，保留观测时间、来源与变化。新会话按目标主机读取摘要，也可按需查询。
  历史不代表本次有效，不继承授权，不共享凭据、Cookie 和请求正文。

- **独立危害复测**：发现漏洞候选即交给「鉴真」，依据用户发送的任务与危害规则独立复测。
  只有实际复现、对照证据和规则引用齐全的确认结果才进入收录池；误报、规则拒收和证据不足
  分别保留审计状态，不进入报告或共享资产的漏洞发现目录。编队页可单独指派复测模型，
  复杂场景可给 `verifier` 接入专用 MCP。危险操作继续逐次审批，复测不设固定回合上限。
- **只读秘书**：在编队页为「秘书」单独指派模型。灵枢按需要在规划或复盘时咨询；
  建议必须引用实际记录版本，灵枢逐条说明采纳情况。秘书没有工具和审批权限，
  咨询失败不会中断原决策。`secretary.enabled: false` 可关闭。
- **缓存计量与事实历史**：规划与结构化执行按任务、路线、阶段保持追加式上下文，
  首次发送事实快照，后续仅追加 JSON Patch；契约变化或超过上下文软窗口时从最新现场重建。
  不复用旧推理结果，不靠填充提示词提高命中率。会话按角色展示输入、真实缓存读取和输出消耗；未返回
  的指标显示未提供。档案支持查看启用版本留存之后的历史快照；历史不作为当前事实。

- **模式化装配**：SRC 编队（业务体验 → 侦察 → 端口测绘 → 主机审计 → Web
  探测 → 横向 → 全链路核验 → 报告）与 CTF 编队（Web/Pwn/逆向/密码/杂项
  五方向专项）按任务模式自动切换；渗透赛按赛制动态调度。
- **灵枢元决策**：任务图（DAG）动态规划与重规划；按复杂度裁定并发分支
  预算（1~8）与思考强度；轮末复盘决定换向或收敛。
- **并行分支**：同一任务发散多条调查路线同时开工（如「业务体验」与
  「服务技术面」互补视角），共享现场互相接力，任一分支夺旗其余即收尾。
- **意图路由**：每条输入由模型判读——普通对话与规则说明只回复不派工；
  执行规则全程遵守并随任务下发到每个智能体；明确的任务才开工。
- **动态分支派生**：智能体在任务进行中可自行派生分支执行者（spawn）
  推进子问题——为什么派、派几个、各自负责什么由模型思考决定，结论
  回传后继续主线。
- **持久解题对话**：每条路线是一条原生工具调用对话——`http_request`
  （任意 HTTP 方法）、隔离沙箱 `shell`/`python`（pwntools/pycryptodome/
  z3/scapy 等开箱即用）、附件分析、经验库检索、共享笔记。
- **上下文治理**：每轮保留稳定系统约束以支持前缀复用；已读取地址的**记录回放**（重取
  不发请求）；沙箱完整输出落盘、上下文只进头尾摘要；对话超预算自动压缩
  （锚点不丢、折叠内容可恢复）。
- **用户直达灵枢**：会话中途的消息 1 秒内直达灵枢，即时回复并转达执行中
  的智能体；时间线、拓扑图同步可见。
- **人在环上**：范围外目标由智能体向你提问、你确认后扩围（禁打列表是
  唯一硬边界）；高风险动作触发时刻即时审批；Flag 由智能体判定并提交，
  平台应答回灌。
- **拓扑全景**：任务、路线分支、发现台账（笔记/入口/Flag）、推理摘要、
  督导连线、提问与审批全部作为图元素实时呈现。
- **仪表盘**：跨会话的 token 消耗、估算费用、轮次与 Flag 态势总览。
- **WriteUp 由灵枢亲笔**：会话收尾后由主 Agent 汇总全场脉络撰写解题报告，
  取代模板拼装。
- **MCP 扩展**：官方 SDK 接入 ida/yakit 等服务器（stdio/http/sse），
  工具按角色权限收窄后并入智能体目录。

## 架构总览

```
用户 ──任务──▶ 灵枢（主 Agent）
                │ DAG 动态规划 · 复杂度裁定 · 复盘收敛
                ├──▶ 专项智能体集群（并行分支 + 持久对话）
                │      业务体验 │ 斥候 │ 听风 │ 破壁 │ 贯甲 │ 深耕
                │      蛛丝(CTF Web) │ 破壳(Pwn) │ 回溯(逆向) │ 译码(密码) │ 拾遗(杂项)
                ├──▶ 环形台账（已尝试登记/避让 + 督导建议）
                └──▶ 黑板（资产/线索/漏洞/任务态/发现台账）
工具层：http_probe（逐跳授权）· tcp_scan · bwrap 沙箱 · 文件分析 · MCP
护栏层：禁打硬边界 · 风险审批 · 全局限速 · 审计留痕 · 密钥加密库
```

| 组件 | 职责 |
| --- | --- |
| `agents/master` | 灵枢：元规划、下发决策、复盘收敛、用户直达通道 |
| `agents/hostops` | 听风（端口测绘）、贯甲（服务审计）、深耕（横向） |
| `agents/recon` `webvuln` `business` | SRC 侦察、漏洞探测、业务上下文 |
| `ctf/` | 五方向解题智能体、Flag 管理、WriteUp |
| `blackboard/` | 黑板 8 层（资产/线索/漏洞/任务态/拓扑…）与汇聚引擎 |
| `gateway/` | 多模型网关：按模型指派路由、主备切换、流式工具调用 |
| `policy/` | 范围守卫（禁打/白名单/扩围）、风险审批、提问通道 |
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
cp .env.example .env        # 填入控制台管理密码
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
3. **下达任务**：首页描述目标与授权范围（Flag 格式可自动识别），可随附
   脱敏 HAR 或题目附件。

MCP 服务器（ida/yakit 等）在 `configs/system.yaml` 配置，开工时握手，
不可达只留痕不阻断。

两种方式监听非回环地址时都必须设置 `SPIRITSCOUT_SERVER_PASSWORD`
（Basic Auth，用户名 `spiritscout`），并在前端配置 HTTPS 反向代理；
Docker 方式由 compose 从 `.env` 注入该变量。

## 使用指南

- **下达任务**：一段自然语言描述目标与意图；系统自动判定模式（SRC/CTF/
  渗透赛）并装配编队，开工前校验模型链路。
- **会话协作**：底部输入坞随时发消息、传附件——消息直达灵枢，回执即时
  可见，要点转达执行中的智能体；规则与约定先于任务下发也井然有序；
  终态会话发消息即带现场续传。
- **提问与审批**：智能体遇到范围外目标或路线分叉时会发起提问（问题与
  选项由智能体撰写），在拓扑图内作答；勾选目标即授权扩围；高风险动作
  触发时即时确认。
- **拓扑图**：灵枢 → 任务 → 路线分支 → 发现台账，全部实时；支持平移、
  缩放、复位。
- **报告**：CTF 产出灵枢亲笔 WriteUp，SRC 产出经全链路核验的渗透报告，
  均可从会话页下载。

## 授权边界与安全模型

- **禁打列表是唯一硬边界**：显式配置的拒绝目标，任何工具调用命中即拒。
- **授权白名单是初始范围**：范围外目标不会被拦截——智能体向你提问，
  你确认后目标写入会话批准集（不落盘，随会话消亡）。
- **敏感动作默认禁用**（反弹 shell/WebShell/容器逃逸/爆破），触发时刻
  即时审批，决策不可代答、不设超时。
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
| `docker-compose.yml` | Docker 部署：端口发布、密码注入、源码与数据挂载 |
| `.env` / `.env.example` | 本地管理密码与构建参数（`.env` 不入库，模板见 `.env.example`） |

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
├── policy/          # 范围守卫、风险审批、用户提问
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

测试遵循「mock 不等于真实体验」原则：单测覆盖确定性机制（权限、边界、
状态机），行为质量以真实任务在浏览器中实测为准。CI 使用轻量 test 依赖，
覆盖 Python 3.11/3.14 和 Node 22；沙箱中的外部工具仍需按部署环境另行验收。
前端修改同时执行 `node --check`，并运行 `tests/test_*.js` 回归。

用量逐笔写入会话目录的 `usage.db`（SQLite WAL 事务），首次打开时导入旧
`usage.json`，保留原文件；备份运行中的数据库请使用 SQLite 备份接口，
或停服后复制。旧版程序无法读取新增的数据库计量，回退前需另行导出。
读取结果按数据库/WAL 文件版本缓存，单文件仅保留最新快照。
资产库启动导入按历史记录游标增量执行；资产页的手动同步保留全量重扫。
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
| [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp) | 自定义 | ✅ 已启用 | 逆向：反编译 / 交叉引用 / 函数与类型检索（无头 idalib） |
| [yakit](https://github.com/yaklang/yakit) | 自定义 | ✅ 已启用 | Web 渗透：编解码 / HTTP 流量 / 模糊测试 / 指纹 / 端口 / 反序列化 |
| [playwright-mcp](https://github.com/microsoft/playwright-mcp) | Apache-2.0 | ✅ 已启用 | 浏览器自动化：JS 渲染 DOM / 点击 / 表单 / 截图 |
| pentester-mcp | 自定义 | 按需 | 一站式渗透工具箱：nmap / nuclei / sqlmap / ffuf / impacket / hydra |
| burp-mcp | 自定义 | 按需 | Web 漏洞扫描与模糊测试（PortSwigger 官方扩展） |
| shodan-mcp | MIT | 按需 | 资产情报（出网至第三方服务） |
| virustotal-mcp | MIT | 按需 | 文件 / 域名情报（VirusTotal 官方托管端点） |

### 技能源

- [obra/superpowers](https://github.com/obra/superpowers)（MIT）——通用工程方法论技能集
- [anthropics/skills](https://github.com/anthropics/skills)——SKILL.md 格式规范参考（无 LICENSE，仅格式参考，内容未使用）
- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)（MIT）——智能体技能目录（1000+ 条）

### 用户




自定义 MCP 可用 `allowed_agents` 指定额外执行角色，例如 `[business_context, ctf_web]`（当前支持业务体验和 CTF 各执行角色）；空列表沿用原有工具范围。该配置不会授予灵枢、秘书或报告角色执行权限，也不会绕过目标范围检查与危险操作审批。
