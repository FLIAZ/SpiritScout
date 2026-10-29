<div align="center">

# SpiritScout · 灵枢

**A multi-agent security testing workbench for vulnerability research, CTFs, and agent competitions**

Assign tasks in your browser, let agents collaborate, and inspect their progress, evidence, and artifacts.

**[简体中文](README.md) · English**

[![Version](https://img.shields.io/badge/version-v0.17.0-2563eb?style=flat-square)](CHANGELOG.md) [![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square)](pyproject.toml) [![CI](https://github.com/FLIAZ/SpiritScout/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/FLIAZ/SpiritScout/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-Apache--2.0-16a34a?style=flat-square)](LICENSE)

[Quick start](#quick-start) · [Capabilities and modes](#capabilities-and-modes) · [Architecture](#architecture) · [Deployment guide (中文)](docs/部署与配置.md) · [Contributing (中文)](CONTRIBUTING.md)

</div>

> For authorized targets and competitions. See the scope, approval, and usage notes below and the [security policy (中文)](SECURITY.md).

## Capabilities and modes

| Mode | Use cases | Collaboration and approval |
| --- | --- | --- |
| **SRC** | Authorized asset analysis, vulnerability reproduction, and penetration testing reports | Supports user clarification and manual browser takeover; dangerous operations require approval each time |
| **CTF** | Web, Pwn, reverse engineering, cryptography, and Misc challenges | Sub-agents work autonomously with optional user guidance; tool operations are pre-authorized and audited |
| **Agent competition** | Platform-hosted competitions, multi-challenge scheduling, and collection of completed results | Plans from user-provided rules and runs unattended; does not ask the user questions or search the public web |

SRC refers to security response center / bug bounty workflows.

| Capability | What it provides |
| --- | --- |
| Agent teams | A lead agent decomposes tasks; sub-agents branch into different approaches, share findings, and use models assigned by role |
| Execution workbench | Expand tasks, approaches, and execution steps; inspect tool results and a process graph linked to audit records |
| Projects and artifacts | Persist assets and sessions; independently reproduce SRC vulnerability candidates with the verifier agent, Jianzhen, and generate reports or write-ups |
| Browser and tools | Connect MCP servers, Skills, and shell/Python sandboxes; manage MCP and Skill cards with status groups, combined filters, and switches; SRC can reuse authenticated browser sessions and let users handle CAPTCHAs |
| Models and usage | Configure providers, teams, and models; inspect estimated costs, tokens, and input cache usage reported by endpoints |

Skill management uses cards with separate enabled/disabled groups, combined category/source/date filters, green switches, and deletion. Open a card to browse its folders, preview and edit rendered Markdown, with draft protection and version-checked saves. Multiple ZIP and TAR archives can be imported directly; RAR / 7z requires system libarchive. See [management and import details (中文)](docs/部署与配置.md#skill-目录与文件浏览).
Main and child agents can discover Skills, load SKILL.md in full, and read supporting files on demand. Conversation traces show actual reads, while retained content is reused without duplicate injection.

----

Historical benchmark · tsecbench (2026-10-23)

The original run used DeepSeek v4.1 Flash. Included for reference.

<img width="1606" height="838" alt="Historical tsecbench benchmark results" src="https://github.com/user-attachments/assets/f5cea640-aa57-40d2-9d21-919960aadd64" />

## Quick start

### Docker · Recommended

Requires **Linux container mode** and **Docker Compose 2.20+**. Use Docker Desktop on Windows or Docker Engine on Linux. macOS/ARM64 compatibility is not yet supported.

```bash
git clone https://github.com/FLIAZ/SpiritScout.git
cd SpiritScout
python deploy.py up
```

On Windows, you can replace `python` with `py -3`; on Linux/macOS, use `python3` where needed.
The first run creates `.env`, generates a random administrator password, builds the image, and waits for the service to become healthy.

| Entry point | Address and notes |
| --- | --- |
| **Console** | <http://127.0.0.1:10010>; find the username and password in the project's `.env` |
| **SRC browser** | Run `python deploy.py up --browser`, then visit <http://127.0.0.1:10011>; uses the same credentials as the console |

```bash
python deploy.py status   # Show service status
python deploy.py logs     # Follow logs
python deploy.py down     # Stop services and retain data volumes
```

The image includes Chromium and bubblewrap. To include the optional CTF toolkit, set `INSTALL_TOOLKIT=1` in `.env` and rebuild.
By default, deployment uses the source code in the image and separate data volumes. When upgrading a legacy bind-mount deployment, stop services and migrate first; do not delete data volumes directly.

### Docker Compose deployment

<details>
<summary>Expand instructions</summary>

Copy `.env.example` to `.env`, set an administrator username and a strong password, then start:

```bash
docker compose up -d --build --wait
```

For source development overrides, administrator recovery, reverse proxies, data migration, and backups, see [Deployment and configuration (中文)](docs/部署与配置.md).

</details>

### Run locally on Linux / Windows

<details>
<summary>Expand instructions</summary>

Requires Python 3.11+. Native browser tools additionally require Node.js 22+ and Chromium/Chrome.
You can set the browser executable path with `SPIRITSCOUT_CHROME_EXECUTABLE`.

**Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
sudo apt install bubblewrap   # Debian / Ubuntu; use sudo dnf install bubblewrap on Fedora
./launch.sh
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.\launch.ps1
```

Visit <http://127.0.0.1:8000> and press `Ctrl+C` to stop. On Linux, use `./launch.sh --port 10010` to change the port; on Windows, pass arguments to `launch.ps1`. Configure administrator credentials and HTTPS before listening on an external address.

The shell/Python sandbox requires Linux and bubblewrap. A native Windows/macOS console cannot provide this complete toolchain; Docker is recommended. Do not share virtual environments between Windows, WSL, and Linux.

</details>

### Configure models and start a task

1. On the Models page (模型), add the provider URL, API key, and model ID.
2. On the Teams page (编队), assign models to Lingshu and each sub-agent. Changes save automatically; you can also apply one model to all configurable roles. New assignments take effect for new tasks.
3. Return to the home page, select SRC, CTF, or agent competition, enter the target and rules, attach any files, and click Start (开始执行).

Use **Clean storage (清理存储)** in the sidebar or session details to inspect sessions sorted by disk usage, compact the database, and remove download caches. Cleanup runs only after confirmation and preserves conversations, results, and the latest recovery chain. Cold archive and restore are not included.

The user selects the task mode. Assets from a project's history are not automatically authorized targets for the current task.
CTF and agent competition modes use a separate headless browser for each session. SRC can connect to an existing browser or enable a manual takeover window.

**Does the model connectivity check pass, but tasks fail immediately?** The connectivity check only asks for an `ok` reply; it does not exercise a complete streaming tool request. The project pins compatible LiteLLM/MCP versions. Rebuild the image after updating dependencies, and do not upgrade LiteLLM alone. See [Model calls and dependency compatibility (中文)](docs/部署与配置.md#模型调用与依赖兼容).

## Architecture

![SpiritScout architecture: task scheduling, sub-agents, and shared modules (Chinese labels)](docs/images/architecture.svg)

The lead agent adjusts its plan based on the task and execution results. Sub-agents share facts through a blackboard and an attempt ledger.
The model gateway handles provider protocols, streaming, and usage accounting. The tool layer handles permissions, mode-specific approvals, sandboxing, and auditing.

The topology shows persistent blackboard facts, evidence, and user-rule references, with edges only for explicit citations. Execution steps and tool receipts remain in session history; file paths are evidence attached to facts rather than standalone artifact nodes. Rules stay on the blackboard and are read on demand through versioned, paginated `blackboard_read` calls. Execution agents share evidence-backed facts through `blackboard_write`. Runtime provenance links agents, stages, and tool executions; evidence states distinguish unresolved sources, observations, supported records, independent verification, conflicts, and expired dependencies. Identical stateless read calls can share in-flight results or short-lived cache entries, isolated by identity and environment. Independent verification always executes again. See the [architecture overview (中文)](docs/02-架构总览与项目结构.md) for the module layout.

## Data and usage boundaries

- **Storage:** Native deployments use `.spiritscout/`; Docker uses configuration and data volumes, plus an optional browser profile volume.
- **Credentials:** API keys are encrypted in the workspace. Back up both the encryption key and database. Do not publish real cookies, HAR files, or raw audit records.
- **Costs:** Connected providers charge for model calls and paid searches. Dashboard costs are estimates; provider invoices are authoritative.
- **Scope and approval:** The model maintains the testing scope from the user's instructions and subsequent replies. Dangerous SRC operations require approval each time; CTF and agent competition tool operations are pre-authorized and audited. The sandbox does not deterministically enforce target scope.
- **Caching and baselines:** Cache hits come from endpoint-reported usage. Failed, cancelled, and unknown usage are recorded separately. See [Cache optimization and testing (中文)](docs/05-核心算法与技术实现.md#稳定前缀与测试修复0122) for read-only baseline reports and real comparison methods.

## Documentation and contributing

Agent competition mode preserves unfinished instances across investigation stages. Deferring dispatch, cancelling running work, and releasing an instance are explicit model decisions; elapsed time alone does not force a switch.

Current code version: **v0.17.0**. See [Version management (中文)](CONTRIBUTING.md#版本管理) for the single source of truth, increment rules, and historical version notes.

Follow the [Agent working rules](AGENTS.md): keep changes in the same batch under one version, and obtain explicit user approval before changing the version or pushing to a remote.
`ctfy-docker/` and `tsecbench-docker/` are local validation projects for the maintainer, are excluded from the repository, and are not required for contributors.

This README is available in English and [Simplified Chinese](README.md). The detailed documentation, contribution guide, changelog, and security policy linked below are currently in Chinese. The architecture diagram also retains its original Chinese labels.

| Topic | Start here |
| --- | --- |
| Installation, models, browsers, migration, and backups | [Deployment and configuration](docs/部署与配置.md) |
| Architecture, execution flow, and code locations | [Documentation index](docs/README.md) |
| Bug fixes and new features | [Contribution guide](CONTRIBUTING.md) · [Agent working conventions](AGENTS.md) |
| Version changes and compatibility fixes | [Changelog](CHANGELOG.md) |
| Privately reporting vulnerabilities in SpiritScout itself | [Security policy](SECURITY.md) |

## License and terms of use

The project code is licensed under the [Apache License 2.0](LICENSE). Referenced knowledge bases, Skills, and external tools retain their respective licenses.

<details>
<summary>Legal disclaimer and usage requirements</summary>

### 1. Purpose

This project (the “tool”) is an open-source technical tool for cybersecurity research, authorized security testing, and internal organizational risk assessment.

All features are intended for legally authorized testing to improve defenses, support responsible vulnerability management, and advance security research.

### 2. Authorization requirements

Users must obtain lawful written authorization for the target system before testing.

Scanning, probing, penetration testing, data collection, traffic analysis, and automated testing without authorization or beyond its scope are prohibited for:

- Any unauthorized public internet system
- Critical information infrastructure, including finance, energy, government, telecommunications, and transportation
- Any business system belonging to a third-party organization, individual, or public cloud
- Any information system for which testing is prohibited by law or regulation

Unauthorized testing is unlawful, and users bear full legal responsibility for their actions.

### 3. Prohibited misuse

Users agree:

1. Not to use the tool for illegal intrusion, unauthorized control, data theft, traffic attacks, vulnerability abuse, or other criminal activity.
2. Not to use it for illicit commercial operations, malicious destruction, unauthorized mass scanning, or online harassment.
3. Not to bypass or compromise built-in safety restrictions or allowlist/blocklist risk controls for illegal purposes.
4. Not to endanger national cybersecurity, disrupt public order, or infringe the lawful rights of others.

### 4. Responsibility and disclaimer

1. The authors, maintainers, and contributors provide publicly available technical code and research implementations only. They do not participate in, knowingly support, or assist third-party illegal use and disclaim joint criminal, administrative, or civil liability for consequences of misuse.
2. The tool is technically neutral and intended for lawful use. Any unlawful operation is the user's independent action and is unrelated to the authors.

### 5. Compliance obligations

By using the tool, users acknowledge that they:

- Understand the Cybersecurity Law of the People's Republic of China
- Understand the relevant provisions of Articles 285 and 286 of the Criminal Law of the People's Republic of China
- Have the lawful qualifications or authorization required for cybersecurity testing
- Voluntarily assume all usage risks and legal consequences

### 6. Response to misuse

The project team reserves the right to:

- Close the repository, stop distribution, or update risk controls
- Report, record, and preserve evidence of malicious misuse
- Refuse technical answers, guidance, updates, or assistance for illegal use

### 7. Final statement

Illegal misuse is strictly prohibited. Individuals engaging in unlawful use bear full legal responsibility, independently of the development, maintenance, and contributor teams.

</details>

## Acknowledgements

<details>
<summary>Knowledge bases, reference projects, MCP servers, and Skill sources</summary>

### Knowledge base sources

- [PayloadsAllTheThings](https://github.com/swisskyrepo/PayloadsAllTheThings) (MIT)
- [NoorQureshi/security-skills](https://github.com/NoorQureshi/security-skills) (MIT)

### Reference projects

- [carlospolop/HackTricks](https://github.com/carlospolop/HackTricks) (CC BY-NC-SA)
- [OWASP WSTG](https://github.com/OWASP/wstg) (CC BY-SA-4.0)
- [GTFOBins](https://github.com/GTFOBins/GTFOBins.github.io) (GPL-3.0)
- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills) (MIT)

### MCP servers used by the project

Project configurations and examples reference the following MCP servers. Your local configuration determines which are enabled.

| MCP | License | Purpose |
| --- | --- | --- |
| [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp) | Custom | Reverse engineering: decompilation, cross-references, function and type lookup (headless idalib) |
| [yakit](https://github.com/yaklang/yakit) | Custom | Web testing: encoding/decoding, HTTP traffic, fuzzing, fingerprinting, ports, and deserialization |
| [playwright-mcp](https://github.com/microsoft/playwright-mcp) | Apache-2.0 | Browser automation: JS-rendered DOM, clicks, forms, and screenshots |
| pentester-mcp | Custom | Penetration testing tools: nmap, nuclei, sqlmap, ffuf, impacket, and hydra |
| burp-mcp | Custom | Web vulnerability scanning and fuzzing (official PortSwigger extension) |
| shodan-mcp | MIT | Asset intelligence (outbound requests to a third-party service) |
| virustotal-mcp | MIT | File and domain intelligence (official VirusTotal-hosted endpoint) |

### Skill sources

- [obra/superpowers](https://github.com/obra/superpowers)
- [anthropics/skills](https://github.com/anthropics/skills)
- [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills)

</details>

### Community contributors

- [@BProbie](https://github.com/BProbie): documentation formatting fixes
- [@k1ne](https://github.com/k1ne7): cross-platform compatibility
- [@neko](https://github.com/neko-11451): login page visual improvements

## Star History

<a href="https://www.star-history.com/?repos=fliaz%2Fspiritscout&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=fliaz/spiritscout&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=fliaz/spiritscout&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=fliaz/spiritscout&type=date&legend=top-left" />
 </picture>
</a>
