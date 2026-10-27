<div align="center">

# 灵枢 · SpiritScout

**面向 SRC、CTF 与智能体竞赛的多智能体安全测试工作台**

在网页下达任务，让 Agent 协作执行，查看过程、证据与产物。

[![Version](https://img.shields.io/badge/version-v0.12.7-2563eb?style=flat-square)](CHANGELOG.md) [![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square)](pyproject.toml) [![CI](https://github.com/FLIAZ/SpiritScout/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/FLIAZ/SpiritScout/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-Apache--2.0-16a34a?style=flat-square)](LICENSE)

[快速开始](#快速开始) · [能力与模式](#能力与模式) · [架构](#架构) · [部署指南](docs/部署与配置.md) · [参与贡献](CONTRIBUTING.md)

</div>

> 用于已获授权的测试目标与赛事。任务范围、审批方式和使用约定见下文及 [安全策略](SECURITY.md)。

## 能力与模式

| 模式 | 适用场景 | 协作与审批 |
| --- | --- | --- |
| **SRC** | 授权资产分析、漏洞复测、渗透报告 | 支持人工补充信息与浏览器接管；危险操作逐次审批 |
| **CTF** | Web、Pwn、逆向、密码、Misc 解题 | 子 Agent 自主推进，支持用户指导；工具操作预授权并留审计 |
| **智能体竞赛** | 平台托管、多题调度与完成回收 | 按用户提供的规则规划，无人值守；不向用户提问，不使用公开网页搜索 |

| 能力 | 可以做什么 |
| --- | --- |
| 编队协作 | 主 Agent 拆分任务，子 Agent 派生路线、共享线索，按角色分配模型 |
| 执行工作台 | 展开任务、路线与推进步骤，查看工具结果及带审计出处的过程拓扑 |
| 项目与产物 | 保存资产和会话；SRC 候选漏洞由「鉴真」独立复测，生成报告或 WriteUp |
| 浏览器与工具 | 接入 MCP、Skill 和 shell/Python 沙箱；SRC 可复用登录态并人工处理验证码 |
| 模型与用量 | 配置供应商、编队与模型，查看费用估算、Token、端点返回的输入缓存用量 |

<details>
<summary>查看历史跑分记录 · tsecbench（2026-10-23）</summary>

原记录使用 DeepSeek v4.1 Flash，保留原图供参考。

<img width="1606" height="838" alt="tsecbench 历史跑分记录" src="https://github.com/user-attachments/assets/f5cea640-aa57-40d2-9d21-919960aadd64" />

</details>

## 快速开始

### Docker · 推荐的完整运行方式

需要 **Linux 容器模式**和 **Docker Compose 2.20+**。Windows 使用 Docker Desktop，
Linux 使用 Docker Engine；macOS/ARM64 保留兼容路径，本次未实测。

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
默认使用镜像源码及独立数据卷；**旧版挂载部署升级前先停服迁移**，不要直接删除数据卷。

<details>
<summary>直接使用 Docker Compose</summary>

复制 `.env.example` 为 `.env`，设置管理员用户名和强密码，再启动：

```bash
docker compose up -d --build --wait
```

源码开发覆盖、管理员恢复、反向代理、数据迁移与备份见 [部署与配置](docs/部署与配置.md)。

</details>

<details>
<summary>原生运行 · Linux / Windows 控制台</summary>

需要 Python 3.11+。原生浏览器工具另需 Node.js 22+ 和 Chromium/Chrome，
也可通过 `SPIRITSCOUT_CHROME_EXECUTABLE` 指定路径。

**Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
sudo apt install bubblewrap   # Debian / Ubuntu；Fedora 使用 sudo dnf install bubblewrap
./launch.sh
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.\launch.ps1
```

访问 <http://127.0.0.1:8000>，按 `Ctrl+C` 停止。Linux 可用 `./launch.sh --port 10010`
修改端口；Windows 将参数传给 `launch.ps1`。监听外部地址前需配置管理员凭据与 HTTPS。

shell/Python 沙箱需要 Linux 与 bubblewrap。Windows/macOS 原生控制台无法提供这一
完整工具链，建议使用 Docker。Windows、WSL、Linux 的虚拟环境不要混用。

</details>

### 配置模型并开始任务

1. 在「模型」页添加供应商地址、API Key 和模型 ID。
2. 在「编队」页为灵枢和各子 Agent 指派模型。
3. 返回首页，选择 SRC、CTF 或智能体竞赛，填写目标、规则与附件，点击「开始执行」。

任务模式由用户选择；项目历史资产不会自动成为本次测试的授权目标。
CTF/智能体模式使用会话独立的无头浏览器，SRC 可连接已有浏览器或启用人工接管窗口。

**模型连通性正常，任务却立即失败？** 连通性测试只要求回复 `ok`，不覆盖完整的流式
工具请求。项目已固定兼容的 LiteLLM/MCP 版本；更新依赖后应重新构建镜像，
不要单独升级 LiteLLM。排查方法见 [模型调用与依赖兼容](docs/部署与配置.md#模型调用与依赖兼容)。

## 架构

![灵枢架构：任务调度、子 Agent 与共用模块](docs/images/architecture.svg)

主 Agent 结合任务和执行结果调整规划，子 Agent 通过黑板与尝试台账共享事实。
模型网关统一处理供应商协议、流式输出与用量；工具层处理权限、按模式审批、沙箱和审计。

步骤、观察、发现和产物保留原始记录出处；拓扑区分实际产出与上下文延续，
不把一次执行步骤当作独立并行 Agent。模块划分见 [架构总览](docs/02-架构总览与项目结构.md)。

## 数据与使用边界

- **数据保存**：原生运行使用 `.spiritscout/`；Docker 使用配置、数据和可选浏览器配置档案卷。
- **凭据管理**：API Key 加密保存在工作区。备份时需要一起保留密钥和数据库；不要公开真实 Cookie、HAR 或审计原文。
- **调用成本**：模型与付费搜索费用由所接入服务收取；仪表盘是估算值，实际计费以供应商账单为准。
- **授权与审批**：测试范围由模型结合用户原话及后续答复维护；SRC 危险操作逐次审批，CTF/智能体预授权并留审计。沙箱不构成目标范围的确定性阻断。
- **缓存与基线**：缓存命中量来自端点返回值；失败、取消和未知用量分别记录。只读基线报告与真实对照方法见 [缓存优化与测试](docs/05-核心算法与技术实现.md#稳定前缀与测试修复0122)。

## 文档与贡献

当前代码版本 **v0.12.7**。版本唯一来源、递增规则和历史版本说明见 [版本管理](CONTRIBUTING.md#版本管理)。

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
<summary>知识库、参考项目、MCP 与技能源</summary>

### 知识库数据来源

- [PayloadsAllTheThings](https://github.com/swisskyrepo/PayloadsAllTheThings)（MIT）——载荷与方法论主体来源，按本项目条目结构重写并补充靶场实测验证
- [NoorQureshi/security-skills](https://github.com/NoorQureshi/security-skills)（MIT）——206 条安全技能（Web/云/AD/移动/逆向/社工），原样接入供智能体检索

### 参考项目

- [carlospolop/HackTricks](https://github.com/carlospolop/HackTricks)（CC BY-NC-SA）——方法论思路参考
- [OWASP WSTG](https://github.com/OWASP/wstg)（CC BY-SA-4.0）——测试标准思路参考
- [GTFOBins](https://github.com/GTFOBins/GTFOBins.github.io)（GPL-3.0）——Unix 二进制利用思路参考
- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)（MIT）——智能体技能目录（1000+ 条）

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

### 技能源

- [obra/superpowers](https://github.com/obra/superpowers)（MIT）——通用工程方法论技能集
- [anthropics/skills](https://github.com/anthropics/skills)——SKILL.md 格式规范参考（无 LICENSE，仅格式参考，内容未使用）
- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)（MIT）——智能体技能目录（1000+ 条）

</details>

### 用户


- [@BProbie](https://github.com/BProbie)：修正文档格式
- [@k1ne](https://github.com/k1ne7)：多平台兼容功能
- [@neko](https://github.com/neko-11451)：对登录页的前端美化

## Star History

<a href="https://www.star-history.com/?repos=fliaz%2Fspiritscout&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=fliaz/spiritscout&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=fliaz/spiritscout&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=fliaz/spiritscout&type=date&legend=top-left" />
 </picture>
</a>
