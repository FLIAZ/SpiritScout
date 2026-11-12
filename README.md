<div align="center">

# 灵枢 · SpiritScout

**面向 SRC、CTF、AWDP 与智能体竞赛的多智能体安全测试工作台**

在网页下达任务，让 Agent 协作执行，查看过程、证据与产物。

**简体中文 · [English](README_EN.md)**

[![Version](https://img.shields.io/badge/version-v0.21.2-2563eb?style=flat-square)](CHANGELOG.md) [![Python](https://img.shields.io/badge/Python-3.11--3.14-3776ab?style=flat-square)](pyproject.toml) [![CI](https://github.com/FLIAZ/SpiritScout/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/FLIAZ/SpiritScout/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-Apache--2.0-16a34a?style=flat-square)](LICENSE)

[界面预览](#界面预览) · [快速开始](#快速开始) · [能力与模式](#能力与模式) · [架构](#架构) · [部署指南](docs/部署与配置.md) · [参与贡献](CONTRIBUTING.md)

</div>

> 用于已获授权的测试目标与赛事。任务范围、审批方式和使用约定见下文及 [安全策略](SECURITY.md)。

## 界面预览

实际界面截图，使用本地演示数据。点击图片查看大图。

**任务入口**

[![任务入口](docs/images/screenshots/task-entry.png)](docs/images/screenshots/task-entry.png)

<table>
<tr>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/dashboard.png"><img src="docs/images/screenshots/dashboard.png" alt="仪表盘" width="100%"></a><br>
<b>仪表盘</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/session-workbench.png"><img src="docs/images/screenshots/session-workbench.png" alt="会话协作" width="100%"></a><br>
<b>会话协作</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/exploration-graph.png"><img src="docs/images/screenshots/exploration-graph.png" alt="探索链路" width="100%"></a><br>
<b>探索链路</b>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/agent-team.png"><img src="docs/images/screenshots/agent-team.png" alt="编队管理" width="100%"></a><br>
<b>编队管理</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/model-settings.png"><img src="docs/images/screenshots/model-settings.png" alt="模型配置" width="100%"></a><br>
<b>模型配置</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/mcp-tools.png"><img src="docs/images/screenshots/mcp-tools.png" alt="MCP 工具" width="100%"></a><br>
<b>MCP 工具</b>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/skills.png"><img src="docs/images/screenshots/skills.png" alt="技能管理" width="100%"></a><br>
<b>技能管理</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/projects.png"><img src="docs/images/screenshots/projects.png" alt="项目归属" width="100%"></a><br>
<b>项目归属</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/experience.png"><img src="docs/images/screenshots/experience.png" alt="经验沉淀" width="100%"></a><br>
<b>经验沉淀</b>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/assets.png"><img src="docs/images/screenshots/assets.png" alt="资产视图" width="100%"></a><br>
<b>资产视图</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/vulnerabilities.png"><img src="docs/images/screenshots/vulnerabilities.png" alt="漏洞复核" width="100%"></a><br>
<b>漏洞复核</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/ctf-writeup.png"><img src="docs/images/screenshots/ctf-writeup.png" alt="CTF 报告" width="100%"></a><br>
<b>CTF 报告</b>
</td>
</tr>
<tr>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/background-tasks.png"><img src="docs/images/screenshots/background-tasks.png" alt="后台任务" width="100%"></a><br>
<b>后台任务</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/deliverables.png"><img src="docs/images/screenshots/deliverables.png" alt="档案与文件" width="100%"></a><br>
<b>档案与文件</b>
</td>
<td width="33%" align="center" valign="top">
<a href="docs/images/screenshots/proxy-pool.png"><img src="docs/images/screenshots/proxy-pool.png" alt="代理池" width="100%"></a><br>
<b>代理池</b>
</td>
</tr>
</table>

## 能力与模式

控制台支持简体中文和 English，可在侧栏底部或登录页切换，浏览器会记住选择。
界面使用本地语言包，不调用在线翻译服务；会话内容、用户配置、技能说明与证据原文保持原样。

| 模式 | 适用场景 | 协作与审批 |
| --- | --- | --- |
| **SRC** | 授权资产分析、漏洞复测、渗透报告 | 支持人工补充信息与浏览器接管；危险操作逐次审批 |
| **CTF** | Web、Pwn、逆向、密码、Misc 解题 | 常规部署由子 Agent 自主推进，轻量部署由单执行者推进；工具操作预授权并留审计 |
| **AWDP 修补** | 本地源码审计、最小修复与补丁交付 | 轻量控制台提供专用入口；保留正常功能，按原始目录结构校验修补提交件 |
| **智能体竞赛** | 平台托管、多题调度与完成回收 | 按用户提供的规则规划，无人值守；不向用户提问，不使用公开网页搜索 |

| 能力 | 可以做什么 |
| --- | --- |
| 编队协作 | 主 Agent 拆分任务，子 Agent 派生路线、共享线索，按角色分配模型 |
| 执行工作台 | 展开任务、路线与推进步骤，查看工具结果及事实／依据黑板图 |
| 项目与产物 | 保存资产和会话；SRC 候选漏洞由「鉴真」独立复测，生成报告或 WriteUp |
| 浏览器与工具 | 接入 MCP、Skill 和 shell/Python 沙箱；SRC 与 CTF 执行角色支持本地离线 CyberChef 配方与完整字节结果复用；MCP 与 Skill 以卡片分组管理、组合筛选和启停；SRC 可复用登录态并人工处理验证码 |
| CVE 情报库 | 官方 CVE List 全量本地收录、增量更新与离线检索，按需补充 NVD、KEV、EPSS、OSV，并关联本地 Nuclei 模板；情报与现场复测证据分开。[建库与维护](docs/部署与配置.md#cve-情报库) |
| 模型与用量 | 配置供应商、编队与模型，查看费用估算、Token、端点返回的输入缓存用量 |

Skill 管理采用卡片目录，支持启用分组、类型／来源／日期组合筛选、绿色开关和删除；点击卡片浏览技能目录、渲染并直接编辑 Markdown，支持草稿保护和版本校验保存。支持直接多选导入 ZIP、tar.gz 等压缩包，RAR / 7z 通过系统 libarchive 读取；参见[管理与导入说明](docs/部署与配置.md#skill-目录与文件浏览)。
主 Agent 与子 Agent 可自主发现技能、完整读取 SKILL.md，再按需查阅子目录资料；会话展示实际技能读取，避免重复注入已读正文。

### Ollama 本地小模型优化

**面向低显存、断外网赛事的修补工作流**。灵枢为 Ollama 本地小模型提供独立的 CTF / AWDP 轻量模式：模型在本机分析题目、执行工具并修改源码，通过 `submit_fix` 交付可下载的修补提交件，无需云端模型 API。

- **单执行者持续推进**：一条持久工具循环处理当前目标，不启动主控规划、督导或专项派遣；Ollama 原生工具调用与共享推理槽减少额外调用和显存争用。
- **针对小模型的上下文处理**：按完整工具往返整理长历史，保留授权、规则与当前证据，原文可分页读回；重复失败和相同观察进入证据复盘，帮助模型重新选择动作。
- **面向赛事交付**：保留被修改文件的原始相对路径，校验 tar.gz 格式、文件清单和实际字节变化，拒绝目录穿越、链接及跨题路径。归档校验不替代功能与漏洞回归，也不代表比赛平台已接收。
- **独立部署**：轻量配置、模型指派和会话目录与常规部署分开。默认以 `qwen3.5:9b`、16GB 内存 / 8GB 显存作为适配目标；实际速度与修复效果取决于题目、模型和设备。

先准备项目依赖、运行中的 Ollama、已下载模型与所需工具，再启动：

```bash
./launch.sh start --offline
```

Ollama 与 llama.cpp 都使用这一个入口，共用轻量工作区与会话记录；后端在模型配置中选择。

默认控制台为 `http://127.0.0.1:8001`，选择 **AWDP 修补** 后上传题目源码并说明修补要求。赛前准备、参数和提交格式见 [Ollama 轻量配置](docs/Ollama轻量配置.md)；工程取舍与同类工作核对见 [创新点与技术思想](docs/06-创新点与技术思想.md)。

<details>
<summary>历史跑分记录 · tsecbench（2026-10-23）</summary>

原记录使用 DeepSeek v4.1 Flash，供参考

<img width="1606" height="838" alt="tsecbench 历史跑分记录" src="https://github.com/user-attachments/assets/f5cea640-aa57-40d2-9d21-919960aadd64" />

</details>

## 快速开始

**Windows 用户推荐在 WSL2 内源码部署，Linux 用户推荐直接源码部署。** 项目日常开发采用源码运行，便于检查配置、补装工具和排查问题。已有 Docker 环境或希望统一容器依赖时，也可以选择 Docker。

### 使用 Agent 部署项目

在准备安装的电脑上打开能读写文件、执行终端命令的编程 Agent，将下面内容发给它。
已有仓库时，让 Agent 在该目录工作；选择 WSL2 后，让 Agent 在 WSL 的 Linux 环境中执行安装。

```text
请帮我部署灵枢（SpiritScout），项目仓库：https://github.com/FLIAZ/SpiritScout.git
先阅读仓库根目录的 AGENTS.md 和 AGENTINSTALL.md；没有本地仓库时先获取这两份指南。
检查操作系统、CPU 架构、依赖、端口和已有配置，再确认安装方式；我已经选定的方式直接采用。
Windows 优先推荐 WSL2 内源码部署，Linux 优先推荐源码部署，Docker 和 Linux 安装包作为可选方式。
选择 WSL2 后，在 /home 下存放项目、虚拟环境和数据；已有仓库先检查位置，不覆盖配置或混用 Windows 的 .venv。
没有仓库时，克隆到选定运行环境中的安装目录。
确认我要常规多 Agent 部署还是本地模型轻量部署，以及是否需要远程访问和 SRC 可操作浏览器。
按我选择的方式安装，保留已有配置和数据，遇到报错按指南排查。
完成后给出访问地址、启动和停止命令、配置与数据位置，并说明哪些检查已通过、哪些还没验证。
```

环境检查、安装步骤和常见报错处理见 [Agent 安装指南](AGENTINSTALL.md)。
模型 API Key 在部署后的本机控制台填写。

### Linux / WSL2 源码部署 · 推荐

需要 Python 3.11–3.14、Git 和 bubblewrap。以下命令用于首次安装；已有仓库和虚拟环境时先检查并复用，升级前保留配置与数据。

**Windows 先准备 WSL2**

在 PowerShell 查看已安装的发行版，确认使用的发行版 `VERSION` 为 `2`：

```powershell
wsl --list --verbose
```

没有发行版时，先用 `wsl --list --online` 查看可安装名称，再在管理员 PowerShell 安装，例如 `wsl --install -d Ubuntu-24.04`。按提示重启、创建 Linux 用户，进入该发行版的终端。已有 WSL1 的转换和常见问题见 [Agent 安装指南](AGENTINSTALL.md#4-源码安装推荐)；WSL 安装要求见 [微软说明](https://learn.microsoft.com/en-us/windows/wsl/install)。Linux 用户直接继续下面步骤。

**以下命令在 Linux / WSL2 终端执行**

Debian / Ubuntu 安装基础依赖，并确认系统 Python 为 3.11+：

```bash
sudo apt update
sudo apt install git python3 python3-venv bubblewrap
/usr/bin/python3 --version
```

Fedora 使用 `sudo dnf install git python3 python3-pip bubblewrap`。系统 Python 版本不足时先安装合适版本，再创建虚拟环境。

项目、`.venv` 和 `.spiritscout/` 放在 Linux 文件系统中，例如 `~/projects/SpiritScout`。WSL2 下避免放在 `/mnt/c`、`/mnt/d`，不要复用 Windows 创建的虚拟环境。[微软文件存储建议](https://learn.microsoft.com/en-us/windows/wsl/filesystems)

```bash
mkdir -p ~/projects
cd ~/projects
git clone https://github.com/FLIAZ/SpiritScout.git
cd SpiritScout
./install.sh
./launch.sh
```

`install.sh` 自动创建或复用 `.venv`，安装并检查核心依赖，补齐缺失配置；已有配置和数据保留。

访问 <http://127.0.0.1:8000>，WSL2 用户可从 Windows 浏览器打开。按 `Ctrl+C` 停止，换端口用 `./launch.sh --port 8100`。需要登录时，先运行 `python3 deploy.py init` 创建管理员凭据；该命令不启动 Docker。远程访问另行配置管理员凭据和 HTTPS。

浏览器工具另需 Node.js 22+ 和 Chromium/Chrome；CyberChef 需要 Node.js 24–26。CTF 工具箱、MCP、沙箱验证及 WSL 访问 Windows 模型服务的方法见 [Agent 安装指南](AGENTINSTALL.md)。控制台能打开后，还需验证实际使用的 shell/Python、模型和浏览器工具。

<details>
<summary>Windows 原生运行：仅需控制台和 MCP 时使用</summary>

Windows 原生需要 Python 3.11–3.14，不提供本项目的 Linux shell/Python 沙箱。需要完整工具执行时优先使用上面的 WSL2 部署。

在仓库根目录的 PowerShell 中执行：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
if (-not (Test-Path configs/system.yaml)) { Copy-Item configs/system.example.yaml configs/system.yaml }
if (-not (Test-Path configs/models.yaml)) { Set-Content -Path configs/models.yaml -Value "providers: []`nmodels: []" -Encoding utf8 }
.\launch.ps1
```

访问 <http://127.0.0.1:8000>，按 `Ctrl+C` 停止，参数传给 `launch.ps1`。Windows 与 WSL 必须使用各自的 `.venv`。

</details>

也可使用 `uv sync` 按 `uv.lock` 建立环境，然后运行 `./launch.sh`；Windows 原生使用 `uv sync` 和 `launch.ps1`。构建独立可执行文件见 [免安装可执行文件](docs/部署与配置.md#免安装可执行文件)。

### Docker 部署 · 可选

适合已有 Docker 环境、希望统一依赖或运行多个独立实例的用户。需要 **Linux 容器模式**和 **Docker Compose 2.20+**。Windows 使用 Docker Desktop，Linux 使用 Docker Engine；macOS/ARM64 尚未完成完整实测。

```bash
git clone https://github.com/FLIAZ/SpiritScout.git
cd SpiritScout
python deploy.py up
```

Windows 可将 `python` 换成 `py -3`，Linux/macOS 可换成 `python3`。
首次运行会创建 `.env`、生成随机管理员密码，构建镜像并等待服务健康。

| 入口 | 地址与说明 |
| --- | --- |
| **控制台** | <http://127.0.0.1:10010>；用户名和密码见项目 `.env` |
| **SRC 浏览器** | 执行 `python deploy.py up --browser` 后访问 <http://127.0.0.1:10011>；凭据与控制台一致 |

```bash
python deploy.py status   # 查看状态
python deploy.py logs     # 跟踪日志
python deploy.py down     # 停服，保留数据卷
```

镜像包含 Chromium 和 bubblewrap。CTF 工具箱按需在 `.env` 设置 `INSTALL_TOOLKIT=1` 后重建。
默认使用镜像源码及独立数据卷；旧版挂载部署升级前先停服迁移，不要直接删除数据卷。


### Docker Compose部署
<details>
 <summary> 点击展开 </summary>


复制 `.env.example` 为 `.env`，设置管理员用户名和强密码，再启动：

```bash
docker compose up -d --build --wait
```

源码开发覆盖、管理员恢复、反向代理、数据迁移与备份见 [部署与配置](docs/部署与配置.md)。
</details>



### 配置模型并开始任务

1. 在「模型」页添加供应商地址、API Key 和模型 ID。
2. 在「编队」页为灵枢和各子 Agent 指派模型，修改后自动保存；也可选择一个模型，一键应用到所有可指派角色。新指派对新任务生效。
3. 返回首页，选择 SRC、CTF 或智能体竞赛，填写目标、规则与附件，点击「开始执行」。

侧栏底部或会话详情的「清理存储」可查看占用、精简数据库及清理下载缓存，保留会话与成果；查看范围并确认后执行。

输入框右侧的圆形按钮可暂停或继续当前任务；输入消息时切换为发送。暂停保留原任务进度，“结束任务”位于更多操作菜单。

在输入框输入 `/` 可筛选 Agent 命令，↑↓ 选择、Tab 或 Enter 补全，再按 Enter 执行。主任务会话支持 `/compact` 手动整理上下文、`/status` 查看任务与压缩结果、`/pause` 暂停、`/resume` 继续、`/model` 选模、`/files` 查看产物、`/report` 查看已有报告、`/help` 帮助。`/compact` 在各上下文下一次模型调用前处理一次，不额外调用模型；保留用户原话、当前授权、最近四轮执行回复及完整历史，短历史可能无需缩小。暂停或结束的任务不会因此自动启动。命令不接受附件或额外参数；以 `//` 开头可发送以 `/` 开头的原文。首页可用 `/help` 和 `/model`，其他命令需先打开任务会话。

发送按钮左侧的模型按钮用于本会话选模，默认“自动”跟随编队。选择按供应商与模型区分并随会话保存；后续调用生效，不中断正在返回的请求，也不覆盖子 Agent 的显式指派。选择“自动”可撤销会话覆盖。

会话的「经验」页可查看产出和提炼结果，停止后可补提炼一次；「经验提炼」页可按字符相似度找候选，预览并确认整理，保留原件、质量状态和来源，支持撤销。字符相似不是语义相同，超长合并稿会被拒绝而不是截断。

「用量」页按供应商和模型汇总共享及会话账本，未定价和不完整用量单独说明；「通知」页可开启已确认解题结果与待答提醒。浏览器须保持打开，系统通知不可用时退化为页面提示，关闭浏览器后不会推送。

任务模式由用户选择；项目历史资产不会自动成为本次测试的授权目标。
CTF/智能体模式使用会话独立的无头浏览器，SRC 可连接已有浏览器或启用人工接管窗口。

**模型连通性正常，任务却立即失败？** 连通性测试只要求回复 `ok`，不覆盖完整的流式
工具请求。项目已固定兼容的 LiteLLM/MCP 版本；更新依赖后应重新构建镜像，
不要单独升级 LiteLLM。排查方法见 [模型调用与依赖兼容](docs/部署与配置.md#模型调用与依赖兼容)。

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

当前代码版本 **v0.21.2**。版本唯一来源、递增规则和历史版本说明见 [版本管理](CONTRIBUTING.md#版本管理)。

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
