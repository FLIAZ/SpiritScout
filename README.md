<div align="center">

# 灵枢 · SpiritScout

**面向授权安全测试与安全竞赛的多智能体工作台**

资产收集、任务执行、漏洞复核与证据整理，在统一的 Web 控制台中完成。

**简体中文 · [English](README_EN.md)**

[![Version](https://img.shields.io/badge/version-v0.22.0-2563eb?style=flat-square)](CHANGELOG.md) [![Python](https://img.shields.io/badge/Python-3.11--3.14-3776ab?style=flat-square)](pyproject.toml) [![CI](https://github.com/FLIAZ/SpiritScout/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/FLIAZ/SpiritScout/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-Apache--2.0-16a34a?style=flat-square)](LICENSE)

[快速开始](#快速开始) · [核心能力](#核心能力) · [使用场景](#使用场景) · [界面预览](#界面预览) · [使用文档](docs/控制台使用.md) · [更新日志](CHANGELOG.md)

</div>

SpiritScout 面向安全研究人员、授权测试团队和安全竞赛参与者，支持 SRC、CTF、智能体竞赛与本地 AWDP 修补工作流。用户通过浏览器创建和管理任务，主 Agent 结合目标与执行结果调整规划，专项 Agent 协作执行，并将发现、验证依据和产物保存在会话中。

项目支持接入不同模型供应商、MCP 服务、Skill 与本地工具。执行过程中可以查看进展、补充要求、管理后台作业，并在任务结束后回看证据与报告。常规部署提供多智能体协作，本地轻量部署提供面向 CTF 和 AWDP 的单执行者路径。

> 仅用于明确授权的测试目标与赛事。测试范围、审批方式和使用约定见下文及 [安全策略](SECURITY.md)。

## 核心能力

### 任务规划与多智能体协作

主 Agent 根据任务目标、用户规则和已有证据安排工作，专项 Agent 分别承担资产发现、业务分析、漏洞测试、逆向分析与复核等职责。任务可以并行推进，执行结果与未解决事项回传主控，后续计划随现场变化调整。

在 SRC 和普通 CTF 会话中，用户可以继续向主 Agent 补充信息，也可以与可用子 Agent 开展独立旁路对话。旁路不要求绑定来源任务，消息与草稿独立保存，不中断父会话正在执行的工作。

### 项目资产收集与目标管理

按项目组织资产、测试会话与发现记录。资产收集支持独立选择模型和主动请求出口，查看实时命令输出、验证状态与用量，并按批次筛选和导出 CSV。

收集完成后可勾选本次测试目标，直接带入 SRC 会话。创建任务时可选择边缘资产优先、核心资产优先或均衡推进；模型结合现场证据调整测试顺序。历史资产和第三方情报作为参考，不自动扩大本次授权范围。

### 执行观察与后台工作

控制台展示任务进展、探索链路、工具调用和返回结果。会话阅读区提供已加载记录搜索、分类目录和长历史浏览，刷新与新增输出保留阅读位置。

耗时命令可以在当前会话后台执行。后台任务页集中展示归属、状态、原始命令与 stdout/stderr 存档，支持搜索、分页回读、下载和单项停止。流量页用于浏览已保存的请求响应及会话交互，离线 PCAP/PCAPNG 通过附件查看器分析。

### 验证证据与交付产物

发现、工具观测、候选漏洞和独立复测结果分别记录。SRC 候选漏洞由独立角色复核，正式报告关联实际验证依据；专项任务报告保留交接摘要、证据引用及未完成事项。

附件、脚本、工具原文与报告在会话中统一查看，可按已有版本预览和下载。CTF 支持题目资料与多答案任务；AWDP 支持源码审计、修改校验和按原始目录结构打包修补提交件。

### 模型、工具与知识扩展

可按角色配置模型，并为会话选择模型覆盖。通过 MCP 和 Skill 扩展浏览器操作、信息检索、源码与二进制分析等能力；本地 shell/Python 工具通过沙箱执行，实际可用能力取决于部署环境与已安装工具。

CVE 情报库支持本地检索与增量维护，可关联 Nuclei 模板，并按配置补充 NVD、KEV、EPSS、OSV 等来源。情报用于辅助判断，不能替代目标现场的验证证据。

### 用量与运行状态

仪表盘按时间范围展示 Token、调用、费用估算、端点返回的输入缓存用量及确认成果，支持按模型、角色和会话查看分组。未定价或未提供的计量显示为未知，不按零消耗处理。

系统资源面板提供主机与应用进程树的 CPU、内存观察。控制台支持简体中文与 English、浅色与深色主题以及动效偏好；项目名称、会话内容和证据原文保持原样。

## 使用场景

| 模式 | 主要用途 | 执行与协作方式 |
| --- | --- | --- |
| **SRC** | 已授权资产的安全评估、漏洞验证与渗透报告整理 | 多智能体协作，支持人工补充信息与浏览器接管；危险操作逐次审批 |
| **CTF** | Web、Pwn、逆向、密码、Misc 等题目分析与答案验证 | 常规部署由专项 Agent 推进，轻量部署由单执行者推进；可上传附件、补充题型提示并处理多答案任务 |
| **智能体竞赛** | 平台托管的多题调度、实例管理与成果提交 | 根据赛方资料和规则自主规划，无人值守；不向用户提问，不使用公开网页搜索 |
| **AWDP 修补** | 本地源码审计、漏洞修复与补丁交付 | 轻量控制台提供专用入口，检查源码修改、修补文件清单和提交件结构 |

### 常规部署与本地轻量部署

**常规部署**面向多智能体协作，可连接已配置的云端或兼容模型端点，按角色分配能力，适用于 SRC、CTF 和智能体竞赛。

**本地轻量部署**支持 Ollama 与 llama.cpp，使用独立配置、模型指派和会话目录，提供 CTF 与 AWDP 模式。当前目标由一条持续工具循环推进，按需读取工具契约和历史证据，减少多角色调度对本地资源的占用。

轻量模式不要求云端模型 API。离线运行仍需提前准备模型权重、依赖、知识资料与实际使用的工具；上下文容量、速度及任务效果取决于模型、设备与题目。初始化、参数和交付要求见 [本地轻量配置](docs/Ollama轻量配置.md) 与 [AWDP 源码审计与补丁](docs/AWDP源码审计与补丁.md)。

## 界面预览

以下为当前版本的真实界面截图，使用演示数据。点击图片可查看原图。

**任务入口**

[![任务入口](docs/images/screenshots/task-entry.png)](docs/images/screenshots/task-entry.png)

**会话工作台**

[![会话工作台](docs/images/screenshots/session-workbench.png)](docs/images/screenshots/session-workbench.png)

<details>
<summary>展开查看所有界面截图</summary>

<table>
<tr>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/task-entry.png"><img src="docs/images/screenshots/task-entry.png" alt="任务入口" width="100%"></a><br><b>任务入口</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/session-workbench.png"><img src="docs/images/screenshots/session-workbench.png" alt="会话工作台" width="100%"></a><br><b>会话工作台</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/asset-collection.png"><img src="docs/images/screenshots/asset-collection.png" alt="资产收集" width="100%"></a><br><b>资产收集</b></td>
</tr>
<tr>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/asset-intelligence.png"><img src="docs/images/screenshots/asset-intelligence.png" alt="资产情报" width="100%"></a><br><b>资产情报</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/system-resources.png"><img src="docs/images/screenshots/system-resources.png" alt="系统资源" width="100%"></a><br><b>系统资源</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/dashboard.png"><img src="docs/images/screenshots/dashboard.png" alt="仪表盘" width="100%"></a><br><b>仪表盘</b></td>
</tr>
<tr>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/exploration-graph.png"><img src="docs/images/screenshots/exploration-graph.png" alt="探索链路" width="100%"></a><br><b>探索链路</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/agent-team.png"><img src="docs/images/screenshots/agent-team.png" alt="编队管理" width="100%"></a><br><b>编队管理</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/model-settings.png"><img src="docs/images/screenshots/model-settings.png" alt="模型配置" width="100%"></a><br><b>模型配置</b></td>
</tr>
<tr>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/mcp-tools.png"><img src="docs/images/screenshots/mcp-tools.png" alt="MCP 工具" width="100%"></a><br><b>MCP 工具</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/skills.png"><img src="docs/images/screenshots/skills.png" alt="技能管理" width="100%"></a><br><b>技能管理</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/projects.png"><img src="docs/images/screenshots/projects.png" alt="项目归属" width="100%"></a><br><b>项目归属</b></td>
</tr>
<tr>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/experience.png"><img src="docs/images/screenshots/experience.png" alt="经验沉淀" width="100%"></a><br><b>经验沉淀</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/assets.png"><img src="docs/images/screenshots/assets.png" alt="资产视图" width="100%"></a><br><b>资产视图</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/vulnerabilities.png"><img src="docs/images/screenshots/vulnerabilities.png" alt="漏洞复核" width="100%"></a><br><b>漏洞复核</b></td>
</tr>
<tr>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/ctf-writeup.png"><img src="docs/images/screenshots/ctf-writeup.png" alt="CTF 报告" width="100%"></a><br><b>CTF 报告</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/background-tasks.png"><img src="docs/images/screenshots/background-tasks.png" alt="后台任务" width="100%"></a><br><b>后台任务</b></td>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/deliverables.png"><img src="docs/images/screenshots/deliverables.png" alt="档案与文件" width="100%"></a><br><b>档案与文件</b></td>
</tr>
<tr>
<td width="33%" align="center" valign="top"><a href="docs/images/screenshots/proxy-pool.png"><img src="docs/images/screenshots/proxy-pool.png" alt="代理池" width="100%"></a><br><b>代理池</b></td>
</tr>
</table>

</details>

## 快速开始

### 1. 选择部署方式

| 方式 | 适用情况 | 安装入口 |
| --- | --- | --- |
| Linux / WSL2 源码部署 | 需要检查配置、补充工具和进行日常开发 | [安装指南](AGENTINSTALL.md#4-源码安装推荐) |
| Docker 部署 | 已有容器环境，希望统一服务依赖与数据卷 | [部署与配置](docs/部署与配置.md#docker-构建) |
| Linux 安装包或独立程序 | 希望使用预构建产物运行控制台 | [Linux 打包与发行](docs/Linux打包与发行.md) |
| 本地轻量部署 | 使用 Ollama / llama.cpp 执行 CTF 与 AWDP | [本地轻量配置](docs/Ollama轻量配置.md) |

Windows 完整工具执行推荐使用 WSL2。Windows 原生控制台、可选浏览器、远程访问和数据迁移的具体范围见安装与部署指南。需要编程 Agent 协助安装时，可让其先阅读仓库中的 `AGENTS.md` 与 `AGENTINSTALL.md`，再按所选运行环境执行。

### 2. 安装并启动控制台

以下为 Linux / WSL2 的首次源码安装路径。先准备 Python 3.11–3.14、Git、bubblewrap 和系统基础依赖；已有仓库或工作区时，请按升级说明复用配置与数据。

```bash
git clone https://github.com/FLIAZ/SpiritScout.git
cd SpiritScout
./install.sh
./launch.sh
```

访问 **http://127.0.0.1:8000**。`install.sh` 创建或复用虚拟环境并准备核心依赖；浏览器工具、CTF 工具箱等按实际需要配置。首次安装与工具可用性检查见 [Agent 安装指南](AGENTINSTALL.md)。

已有 Docker 环境也可使用部署助手：

```bash
git clone https://github.com/FLIAZ/SpiritScout.git
cd SpiritScout
python deploy.py up
```

Docker 控制台默认位于 **http://127.0.0.1:10010**，管理员凭据保存在本机 `.env`。可选浏览器、数据卷、镜像更新和停服方式见 [部署与配置](docs/部署与配置.md)。

### 3. 配置模型与工具

1. 在「模型」页添加供应商地址、API Key 和模型 ID，检查所需模型端点是否可用。
2. 在「编队」页为主 Agent 与专项角色分配模型，也可将一个模型应用到所有可指派角色。
3. 根据任务需要，在 MCP 和 Skill 页面配置工具与参考资料。需要浏览器、命令行或专业分析工具时，同时核对服务端运行环境。

本地轻量模式在准备好模型服务与工具后，通过 `./launch.sh start --offline` 启动，默认访问 **http://127.0.0.1:8001**。Ollama 与 llama.cpp 共用该入口，后端在轻量模型配置中选择。

### 4. 创建第一个任务

- **SRC**：填写已授权目标、规则与预期结果，或从项目资产库勾选测试目标；选择优先策略与本次会话出口。
- **CTF**：在题目资料卡填写名称、地址、描述、提示和题型，上传附件；任务输入框可补充已有思路与额外要求。
- **智能体竞赛**：提供平台入口、赛方规则与任务资料，由系统结合真实平台信息安排执行。
- **AWDP**：在轻量模式下上传源码或服务附件，说明修补目标与验证要求。

任务模式由用户明确选择。提交前确认目标范围与资料，在会话中查看执行进展；信息缺失、工具不可用或执行失败时，依据实际提示补充或调整。

### 5. 跟踪进展与查看结果

会话输入区支持发送补充消息、暂停和继续任务；SRC 与普通 CTF 还可展开「实况」与子 Agent 独立对话。文件与报告、资产与情报、后台任务和流量分别通过会话功能入口访问。

需要查看当前会话状态或常用操作时，在输入框输入 `/` 打开命令菜单；查看完整功能、报告入口和用量解释，请阅读 [控制台使用](docs/控制台使用.md)。

模型连通性测试只验证基础应答，完整工具执行还依赖模型协议、沙箱与外部工具配置。出现“连通但任务失败”等问题时，参见 [模型调用与依赖兼容](docs/部署与配置.md#模型调用与依赖兼容)。

## 架构

![灵枢架构：任务调度、子 Agent 与共用模块](docs/images/architecture.svg)

主 Agent 结合任务和执行结果调整规划，子 Agent 通过黑板与尝试台账共享事实。
模型网关统一处理供应商协议、流式输出与用量；工具层处理权限、按模式审批、沙箱和审计。

拓扑直接展示黑板中的产出事实、依据与用户规则，只按明确引用连线；执行步骤、
工具回执与“输出产物”行为留在会话历史，文件路径附在事实依据中。用户规则原文
保存在黑板，模型接收版本引用并通过 `blackboard_read` 按需分段读取；执行角色
通过 `blackboard_write` 共享带依据的事实。来源由运行时关联 Agent、推进步骤和工具执行台账，
证据状态区分待核实、直接观察、有依据记录、独立复测、冲突与过期。精确相同的无状态只读
调用可共享在途结果或短时复用；身份、实例及写入状态变化隔离缓存，独立复测始终重新执行。模块划分见 [架构总览](docs/02-架构总览与项目结构.md)。

## 数据与使用边界

- **数据保存**：原生运行使用 `.spiritscout/`；Docker 使用配置、数据和可选浏览器配置档案卷。
- **凭据管理**：API Key 加密保存在工作区。备份时需要一起保留密钥和数据库；不要公开真实 Cookie、HAR 或审计原文。
- **调用成本**：模型与付费搜索费用由所接入服务收取；仪表盘是估算值，实际计费以供应商账单为准。
- **授权与审批**：测试范围由模型结合用户原话及后续答复维护；SRC 危险操作逐次审批，CTF/智能体预授权并留审计。沙箱不构成目标范围的确定性阻断。
- **缓存与基线**：缓存命中量来自端点返回值；失败、取消和未知用量分别记录。只读基线报告与真实对照方法见 [缓存优化与测试](docs/05-核心算法与技术实现.md#稳定前缀与测试修复0122)。

## 文档与贡献

智能体模式默认保留未完成题的运行现场；暂缓派工、取消在途工作与释放实例分别由模型明确决策，不按固定耗时强制切题。

当前代码版本 **v0.22.0**。版本唯一来源、递增规则和历史版本说明见 [版本管理](CONTRIBUTING.md#版本管理)。

贡献时按 [Agent 工作约定](AGENTS.md) 执行：同期功能共用一个版本，调整版本号和推送远端须先取得用户明确同意。

| 我想了解 | 阅读入口 |
| --- | --- |
| 安装、模型、浏览器、迁移与备份 | [部署与配置](docs/部署与配置.md) |
| 架构、执行流程和代码位置 | [项目文档索引](docs/README.md) |
| 修复问题或提交新功能 | [贡献指南](CONTRIBUTING.md) · [Agent 工作约定](AGENTS.md) |
| 版本变化与兼容修复 | [更新日志](CHANGELOG.md) |
| 私密报告项目自身漏洞 | [安全策略](SECURITY.md) |

## 许可与使用约定

项目代码采用 [Apache License 2.0](LICENSE)。引用的知识库、Skill 和外部工具遵循各自的许可证。

<details>
<summary>法律免责声明与使用合规须知</summary>

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

</details>

## 致谢

<details>
<summary>知识库、参考项目、MCP 、SKILL源及用户</summary>

### 知识库数据来源

- [PayloadsAllTheThings](https://github.com/swisskyrepo/PayloadsAllTheThings)（MIT）
- [NoorQureshi/security-skills](https://github.com/NoorQureshi/security-skills)（MIT）

### 参考项目

- [carlospolop/HackTricks](https://github.com/carlospolop/HackTricks)（CC BY-NC-SA）
- [OWASP WSTG](https://github.com/OWASP/wstg)（CC BY-SA-4.0）
- [GTFOBins](https://github.com/GTFOBins/GTFOBins.github.io)（GPL-3.0）
- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)（MIT）
- ARTEX

### 项目使用的 MCP 服务器

项目配置和示例涉及以下 MCP 服务器，是否启用以本机配置为准：

| MCP | 许可 | 用途 |
| --- | --- | --- |
| [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp) | 自定义 | 逆向：反编译 / 交叉引用 / 函数与类型检索（无头 idalib） |
| [yakit](https://github.com/yaklang/yakit) | 自定义 | Web 渗透：编解码 / HTTP 流量 / 模糊测试 / 指纹 / 端口 / 反序列化 |
| [playwright-mcp](https://github.com/microsoft/playwright-mcp) | Apache-2.0 | 浏览器自动化：JS 渲染 DOM / 点击 / 表单 / 截图 |
| pentester-mcp | 自定义 | 渗透工具集：nmap / nuclei / sqlmap / ffuf / impacket / hydra |
| burp-mcp | 自定义 | Web 漏洞扫描与模糊测试（PortSwigger 官方扩展） |
| shodan-mcp | MIT | 资产情报（出网至第三方服务） |
| virustotal-mcp | MIT | 文件 / 域名情报（VirusTotal 官方托管端点） |

### SKILL源

- [obra/superpowers](https://github.com/obra/superpowers)
- [anthropics/skills](https://github.com/anthropics/skills)
- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)



### 用户


- [@BProbie](https://github.com/BProbie)：修正文档规范
- [@k1ne](https://github.com/k1ne7)：多平台适配兼容优化
- [@neko](https://github.com/neko-11451)：登录页的前端美化

</details>

## 联系
- atroot.fliaz@gmail.com

## Star History

<a href="https://www.star-history.com/?repos=fliaz%2Fspiritscout&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=fliaz/spiritscout&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=fliaz/spiritscout&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=fliaz/spiritscout&type=date&legend=top-left" />
 </picture>
</a>
