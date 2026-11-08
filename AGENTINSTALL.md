# AGENTINSTALL.md — 给 Agent 的安装指南

用户要求安装、部署或修复安装时，按本文执行。先确认安装方式，再检查环境，最后安装和验证。用户只要求修改文档时，不执行安装。

命令以当前仓库的 [部署入口](deploy.py)、[依赖声明](pyproject.toml) 和配置文件为准。详细配置见 [部署与配置](docs/部署与配置.md)，开发约定见 [AGENTS.md](AGENTS.md)。

## 1. 先问清楚用户要什么

用户已经说清楚的内容直接采用，只问缺失项。可以一次问：

> 1. 你想用源码、Docker 还是 Linux 安装包？Windows 优先推荐 WSL2 内源码部署，Linux 优先推荐源码部署；不确定的话，我先检查机器。
> 2. 你要常规多 Agent 功能，还是只用 Ollama / llama.cpp 做本地 CTF、AWDP？本地模型服务是否已经装好？
> 3. 这是新安装还是保留旧数据升级？只在本机用，还是需要其他电脑访问？是否需要 SRC 可操作浏览器或额外安全工具？

操作系统、CPU 架构、已装软件和端口占用由 Agent 检查，不让用户重复查询。云端模型需要知道供应商、接口地址和模型 ID；密钥让用户在本机控制台填写，不要求贴到聊天里。

| 用户需求 | 选择 |
| --- | --- |
| Windows 上使用完整工具执行功能 | 优先建议在 WSL2 内源码安装，按第 4 节操作；依赖和虚拟环境均安装在 WSL 内 |
| Linux 上运行或开发 | 优先建议源码安装，按第 4 节操作；需要 bubblewrap |
| 已有 Docker 环境，或希望统一容器依赖、运行多个独立实例 | 可选 Docker，按第 5 节安装 |
| Windows/macOS 只运行控制台、管理模型和 MCP | 可以原生运行源码；需要 shell/Python 沙箱时，Windows 优先选 WSL2，macOS 可选 Docker |
| 已有匹配系统和架构的 Linux 安装包 | 按第 6 节安装；外部工具仍需单独准备 |
| 使用本地模型的轻量 CTF / AWDP | 优先建议 Linux/WSL2 源码，也可选 Docker；再按第 7 节配置本地模型 |

推荐源码是为了贴近项目日常开发和排查方式，不代表所有 WSL 发行版均已验证。记录实际发行版、Python/Node 版本和沙箱检查结果，不把开发机已有依赖当成新机器默认具备。

用户尚未选定时，给出建议后等用户选择；已经选定就直接采用，不重复询问。环境不支持所选方式时，说明具体缺项，再确认是否换方式。不要在失败后自行改成另一套部署。

## 2. 检查环境

先做只读检查。只检查所选方式需要的项目，不要求把所有工具装齐。

### 系统、目录和资源

Linux / macOS：

```bash
pwd
uname -srm
df -h .
git status --short
python3 --version
```

Linux 另外查看 `/etc/os-release` 和 `free -h`；macOS 使用 `sw_vers`。非 Git 下载目录跳过 `git status`。

Windows PowerShell：

```powershell
Get-Location
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, OSArchitecture, TotalVisibleMemorySize, FreePhysicalMemory
Get-PSDrive -PSProvider FileSystem
```

选择 WSL2 时，在 PowerShell 执行 `wsl --status` 和 `wsl --list --verbose`，然后进入实际安装的发行版执行上面的 Linux 检查；Windows 宿主不必另装 Python。只有选择 Windows 原生源码或用宿主 Python 运行部署脚本时，才检查 `py -3 --version`。Git 和 Python 必须在所选运行环境中检查。

记录系统、CPU 架构、可用内存、磁盘空间、安装目录和当前是否在容器/WSL 中。本地模型还要查实际 GPU、显存、已下载模型和推理服务占用；不能只根据模型名判断机器能否运行。镜像、模型权重和数据都需要磁盘空间，不在下载失败后反复重试同一命令。

### 按安装方式检查

| 安装方式或功能 | 检查内容 | 通过条件 |
| --- | --- | --- |
| WSL2 源码 | PowerShell 中的 `wsl --list --verbose`，所选发行版内的系统、路径和依赖 | 发行版 `VERSION` 为 `2`；项目、虚拟环境和数据在 Linux 文件系统中 |
| 源码 | Python 版本、解释器位置、`python3 -m venv --help`；仅 Windows 原生换成 `py -3` | Python 3.11+，可以创建当前平台的虚拟环境 |
| Linux 沙箱 | `command -v bwrap`、`bwrap --version` | 已安装 bubblewrap；安装完成后还要实际验证命名空间和工具执行 |
| Docker | `docker version`、`docker info --format '{{.OSType}}'`、`docker compose version --short` | 守护进程可访问，容器类型为 `linux`，Compose 2.20+ |
| 原生浏览器 | `node --version`、`npm --version`、Chrome/Chromium 可执行文件 | Node.js 22+，浏览器存在；Docker 已包含这些依赖 |
| 原生 CyberChef | Node.js、npm、bubblewrap | Node.js 24–26；浏览器所需的 Node.js 22 不够运行此组件 |
| Linux 安装包 | `uname -m`、`getconf GNU_LIBC_VERSION` | 架构匹配、glibc 2.31+；不适用于 musl/Alpine |
| 本地模型 | Ollama 的 `/api/tags` 或 llama.cpp 的 `/v1/models` | 从灵枢实际运行环境可访问，返回所选模型 |

Docker 安装缺少前置软件时，Windows/macOS 安装并启动 Docker Desktop，Linux 安装对应发行版的 Docker Engine 与 Compose 插件。按实际系统选择安装方法，不在 Fedora 执行 `apt`，也不在 Windows PowerShell 执行 Bash 命令。

仓库文档记录的 Docker 实测范围为 Windows 与 amd64 Linux；macOS/ARM64 有兼容路径，但不能据此承诺完整可用。选用这些环境时，明确记录本机实际验证结果。

### 端口、网络和已有数据

| 入口 | 默认宿主机端口 |
| --- | --- |
| 常规 Docker 控制台 | 10010 |
| Docker SRC 浏览器操作页 | 10011 |
| 常规源码 / Linux 安装包 | 8000 |
| 本地模型轻量控制台 | 8001 |

Linux 用 `ss -ltnp`，macOS 用 `lsof -nP -iTCP -sTCP:LISTEN`，PowerShell 用 `Get-NetTCPConnection -State Listen` 检查占用。端口被其他服务使用时换端口，不直接结束未知进程。

检查能否下载仓库、容器镜像和所选依赖；离线安装则确认这些内容已准备好。下载失败要区分 DNS、代理、证书、权限和磁盘问题。不要关闭 TLS 校验或不断更换来源碰运气。

检查是否已有 `.env`、用户修改的配置、`.spiritscout/`、轻量工作区或同名 Docker 卷。仓库自带 `configs/` 不代表已经安装过。只汇报是否存在和路径，不输出密钥或整份配置。已有安装转到第 10 节处理。

## 3. 执行时遵守这些规则

- 在仓库根目录运行源码和 Compose 命令；有空格的路径加引号。已有仓库直接使用，不再嵌套克隆一份。
- 保留用户配置和未提交改动。创建配置前先检查文件是否存在；修改已有配置前保存副本。
- 不复制别人的 `.env`、模型清单、Cookie、数据库或 `.venv`。Windows、WSL、Linux 各用自己的虚拟环境。
- 不使用 `sudo pip` 或 `--break-system-packages`。依赖安装到项目 `.venv`；不用全局升级 LiteLLM/MCP 解决报错。
- 普通运行不需要前端构建，也不需要 Redis、NATS、Chroma、IDA 或所有安全工具。`ctfy-docker/`、`tsecbench-docker/` 不是安装依赖。
- 不为通过检查关闭认证、绕过沙箱或全局关闭 SELinux/AppArmor。不把配置文件中的工具名称当成工具已安装。
- 执行失败时记录命令、退出码和脱敏后的错误，修复具体原因后重跑该步骤。不要删除数据后从头重装。

Docker 和源码安装都需要仓库。使用 WSL2 时先完成第 4 节的环境准备，在 WSL 的 Linux 目录中执行克隆，不要先在 Windows 创建 `.venv`。没有仓库时执行：

```bash
git clone https://github.com/FLIAZ/SpiritScout.git
cd SpiritScout
```

## 4. 源码安装（推荐）

### Windows 先准备 WSL2

以下 WSL 管理命令在 Windows PowerShell 中执行：

```powershell
wsl --status
wsl --list --verbose
```

已有可用的 WSL2 发行版就复用。没有发行版时，先用 `wsl --list --online` 确认名称，再在管理员 PowerShell 安装，例如：

```powershell
wsl --install -d Ubuntu-24.04
```

按提示重启 Windows、创建 Linux 用户，再用 `wsl --list --verbose` 确认该发行版的 `VERSION` 为 `2`。示例名称须以实际列表为准；Ubuntu 示例不表示项目已完成该系统上的全新安装验收。系统要求和安装故障参考 [微软 WSL 安装说明](https://learn.microsoft.com/en-us/windows/wsl/install)。

已有发行版为 WSL1 时，先备份该发行版的重要数据，再转换；以下名称也要替换为实际名称：

```powershell
wsl --set-version Ubuntu-24.04 2
wsl --distribution Ubuntu-24.04
```

进入发行版后，后续 Git、Python、pip、系统包和 `launch.sh` 命令都在这个 Linux 终端运行。先选 Linux 文件系统中的安装目录：

```bash
mkdir -p ~/projects
cd ~/projects
pwd
df -T .
```

先按下一节补齐 Git 等系统依赖，再在此目录克隆项目并进入 `SpiritScout`。项目、`.venv`、配置和 SQLite 数据都放在 WSL 的 `/home/...` 下，避免 `/mnt/c`、`/mnt/d` 等 Windows 挂载盘。已有仓库在 Windows 盘时保留原件，按第 10 节保护配置和数据后迁到 Linux 目录，并重新创建 Linux 虚拟环境。[微软文件存储建议](https://learn.microsoft.com/en-us/windows/wsl/filesystems)

继续下面的 Linux 源码步骤。WSL2 仍须验证 bubblewrap 命名空间和实际 shell/Python 调用；不要只检查 `bwrap --version` 就宣布沙箱可用，也不要因为源码安装而额外要求安装 Docker。

### Linux / WSL2 安装与启动

准备 Python 3.11+ 和 bubblewrap。安装脚本默认用 `/usr/bin/python3` 创建虚拟环境；沙箱会遮蔽用户主目录和 `/usr/local`，优先使用发行版的系统 Python。

如果 Linux/WSL2 缺少系统依赖，先按发行版安装，二选一：

```bash
# Debian / Ubuntu；系统 Python 仍须满足 3.11+
sudo apt update
sudo apt install git python3 python3-venv bubblewrap
```

```bash
# Fedora
sudo dnf install git python3 python3-pip bubblewrap
```

基础依赖齐备后，按第 3 节获取仓库，进入项目根目录执行：

```bash
./install.sh
./launch.sh
```

`install.sh` 检查 Python，创建或复用 `.venv`，安装并检查核心依赖，只创建缺失的 `configs/system.yaml` 和空模型清单。已有配置、`.env` 和运行数据保持不变。安装失败按错误提示处理后重跑；不要复制模型示例冒充真实模型。

新建环境需要指定 Python 时，用 `SPIRITSCOUT_PYTHON=/usr/bin/python3.12 ./install.sh`，路径替换为实际解释器；已有 `.venv` 仍会复用。Windows 或损坏的 `.venv` 会被拒绝，先移到备份目录再重试。

需要管理员登录时，启动前执行 `python3 deploy.py init`；该命令只初始化 `.env`，不启动 Docker。

默认访问 `http://127.0.0.1:8000`，WSL2 用户可用 Windows 浏览器打开。前台用 `Ctrl+C` 停止。换端口用 `./launch.sh --port 8100`；不要使用仅供 Compose 的 `SPIRITSCOUT_PORT` 修改源码端口。

### WSL2 连接 Windows 上的模型或浏览器

模型服务与灵枢位于同一 WSL 发行版时使用其本地地址。模型或 SRC CDP 浏览器在 Windows 上时，先确认 WSL 网络模式：

- 默认 NAT 模式下，在 WSL 执行 `ip route show default`，用 `default via` 后的宿主地址连接 Windows 服务，并检查服务监听地址与防火墙。
- 已启用 mirrored 模式时，可以通过 `127.0.0.1` 连接 Windows 服务；仍需实际检查端口可达。不要为安装灵枢自动修改用户的全局网络模式。
- 从 WSL 请求实际地址的 Ollama `/api/tags`、llama.cpp `/v1/models` 或 Chrome `/json/version`。Windows 浏览器能打开不代表 WSL 可达；`host.docker.internal` 也不是普通 WSL 的默认宿主地址。

将已验证的地址写入模型配置或 `SPIRITSCOUT_CHROME_CDP_URL`。原生轻量初始化时用 `setup_local_light.py --base-url` 指定 Windows 上的模型服务地址；不使用 Docker 的 `--docker` 参数。网络模式与访问规则见 [微软 WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)。

### macOS 原生控制台

macOS 准备好 Python 3.11+ 后，同样运行 `./install.sh`、`./launch.sh`，跳过 Linux 的 apt/dnf 命令；安装脚本默认使用 PATH 中的 `python3`。原生控制台不能提供本项目的 Linux shell/Python 沙箱，需要完整工具执行时可选 Docker，并记录尚未验证的兼容项。

### Windows PowerShell

确认 `py -3` 是 Python 3.11+，且现有 `.venv` 不是从 Linux/WSL 复制来的。复用已检查的 Windows 虚拟环境时跳过第一行：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pip check
if (-not (Test-Path configs/system.yaml)) { Copy-Item configs/system.example.yaml configs/system.yaml }
if (-not (Test-Path configs/models.yaml)) { Set-Content -Path configs/models.yaml -Value "providers: []`nmodels: []" -Encoding utf8 }
.\launch.ps1
```

默认访问 `http://127.0.0.1:8000`。换端口用 `.\launch.ps1 --port 8100`。需要登录时，先用 `py -3 deploy.py init` 初始化凭据。

若 PowerShell 禁止运行 `.ps1`，直接从仓库根目录运行解释器，不修改全局执行策略：

```powershell
.venv\Scripts\python.exe -X utf8 -m spiritscout --config configs/system.yaml
```

Windows 原生安装没有 Linux 工具沙箱。需要 shell/Python 工具时优先在 WSL2 内源码部署，也可选择 Docker；不要让 Windows 和 WSL 共用 `.venv` 或同时写同一工作区。

### 按需安装工具

| 功能 | 操作 |
| --- | --- |
| Python CTF 工具箱 | Linux 源码运行 `.venv/bin/python -m pip install -e '.[toolkit]'` |
| AWDP 本地源码扫描 | Linux 源码运行 `.venv/bin/python -m pip install -e '.[audit]'`；轻量 Docker 默认启用该构建项 |
| AWDP 离线编译与运行 | 先用 `scripts/verify_awdp_environment.py` 在真实断网沙箱检查；系统包、便携工具和依赖缓存见 [AWDP 离线编程环境](docs/AWDP离线编程环境.md) |
| 开发与测试 | 只有开发需求才安装 `.[dev]` |
| 浏览器 | 安装 Node.js 22+、npm、Chrome/Chromium；必要时设置 `SPIRITSCOUT_CHROME_EXECUTABLE`。在 MCP 配置中启用所需的 `chrome` 服务 |
| CyberChef | 准备 Node.js 24–26、npm、bubblewrap，再运行 `.venv/bin/python scripts/setup_cyberchef.py`；独立 Node 路径用 `--node` 指定 |
| IDA、Yakit、其他 MCP | 检查执行文件、许可证和地址；只配置用户需要且已准备好的服务 |

当前 `configs/system.yaml` 可能包含 `.venv/bin/idalib-mcp`、`.tools/yak/yak` 等外部程序。它们不在核心 pip 依赖中，也不保证存在于镜像内。缺少且用户不需要时将对应 MCP 的 `enabled` 设为 `false`；用户需要时补齐实际依赖后验证。不要整份覆盖用户配置。

安全工具要安装在实际执行环境中。宿主机命令可用不代表容器或沙箱可用；`~/.local/bin`、`/usr/local` 中的工具也不自动进入本项目沙箱。

## 5. Docker 安装（可选）

适用于已选择 Docker 的常规部署，不是 Linux/WSL2 源码安装的前置条件。本地模型轻量 Docker 使用第 7 节的独立 Compose 文件。

下列命令使用 `python3`；Windows PowerShell 将它替换为 `py -3`。部署脚本只用 Python 标准库，宿主机不需要先安装 SpiritScout 的 Python 依赖。

```bash
python3 deploy.py doctor
python3 deploy.py init
```

`doctor` 成功只证明 Docker、Linux 容器模式和 Compose 版本符合要求。`init` 首次创建 `.env` 并生成随机管理员密码；已有 `.env` 原样保留。

检查 `.env` 的管理员用户名和密码是否已设置，密码不能继续使用 `please-change-me`。如果旧文件仍是模板值，修改对应项；重复执行 `init` 不会替换它。不要把 `.env` 内容打印到聊天或日志中。

没有 Python 时，可以手动复制 `.env.example` 为 `.env`，但仅在 `.env` 不存在时复制，随后设置自己的强密码。再执行 `docker compose up -d --build --wait`。

按用户所需设置可选项：

| `.env` 项 | 用途 |
| --- | --- |
| `SPIRITSCOUT_PORT=10020` | 修改宿主机控制台端口，容器内仍是 10010 |
| `SPIRITSCOUT_BROWSER_PORT=10021` | 修改 SRC 浏览器操作页端口 |
| `INSTALL_TOOLKIT=1` | 构建时增加 Python CTF 工具箱；不是所有外部安全工具 |
| `PIP_INDEX_URL` | 仅在 pip 来源访问失败且已有可用来源时修改；不解决 Docker 拉镜像或 apt 的网络问题 |

启动并查看状态：

```bash
python3 deploy.py up
python3 deploy.py status
```

首次构建需要下载依赖，退出码必须为 0，主服务应为 `healthy`。默认打开 `http://127.0.0.1:10010`，凭据由用户在本机 `.env` 查看。启动失败用 `docker compose logs --tail 100 spiritscout` 查看原因。

只有用户需要 SRC 人工操作浏览器时，改用：

```bash
python3 deploy.py up --browser
```

浏览器操作页默认 `http://127.0.0.1:10011`，使用同一组管理员凭据。CTF/智能体的无头浏览器不需要此选项。

注意：

- 默认源码来自镜像，配置和数据来自命名卷。改代码、依赖或构建参数后运行 `up` 重建；`restart` 不会更新镜像，也不会重新注入修改后的 Compose 环境变量。
- 默认部署不会读取宿主 `configs/models.yaml`。在运行中的控制台配置模型；修改卷内主配置时参考 [配置与迁移](docs/部署与配置.md#升级与数据迁移)。
- `.env` 不是容器全部环境变量的自动映射；只有 Compose 中声明的项才会注入。不要只往 `.env` 添加任意变量就认为容器已使用它。
- 同时运行多套部署时，端口和 Compose project name 都要不同。`--project-name` 必须用于该部署后续的启动、状态、日志、迁移和停止命令。
- 只在用户需要源码开发挂载时使用 `docker-compose.dev.yml`，其数据路径与默认部署不同。日常停止用 `python3 deploy.py down`，不加 `-v`。

## 6. Linux 安装包

仅在已有可信下载地址或用户提供安装包时使用。先确认实际存在的产物，不猜 Release 文件名或假定已发布。构建入口见 [Linux 打包与发行](docs/Linux打包与发行.md)；构建包和安装包是两件事。

检查 `uname -m`、glibc 版本及产物架构；`amd64` 对应 `x86_64`，`arm64` 对应 `aarch64`。用随产物提供的 SHA256 清单校验所选文件。校验和不能代替来源可信性。

选择对应格式执行，将“实际文件”替换为下载文件名：

| 格式 | 安装 / 启动命令 |
| --- | --- |
| Debian / Ubuntu | `sudo apt install ./实际文件.deb`，随后运行 `spiritscout` |
| Fedora / RHEL | `sudo dnf install ./实际文件.rpm`，随后运行 `spiritscout` |
| Arch | `sudo pacman -U ./实际文件.pkg.tar.zst`，随后运行 `spiritscout` |
| AppImage | `chmod +x ./实际文件.AppImage`，随后运行 `./实际文件.AppImage` |
| 独立 ELF | 添加执行权限后直接运行；放在可写目录，或显式设置 `SPIRITSCOUT_DATA_HOME` |

默认控制台为 `http://127.0.0.1:8000`。系统包和 AppImage 的数据目录是 `${XDG_DATA_HOME:-~/.local/share}/spiritscout/`；裸 ELF 默认使用程序所在目录。`SPIRITSCOUT_DATA_HOME` 可指定另一数据目录。程序不会自动安装后台服务。

包内含控制台的 Python 运行时和核心依赖，但完整沙箱还需要系统 Python 3.11+、bubblewrap；浏览器还需 Node.js、Chrome/MCP。不要将“可打开控制台”报告为“所有工具可用”。包由原包管理器升级，卸载不删除用户数据。

## 7. 本地模型轻量安装

轻量配置面向 CTF/AWDP，不是常规部署的通用加速开关。先确认后端、实际模型 ID、服务地址、上下文容量、内存/显存和模型是否支持工具调用。

先准备并启动 Ollama 或 llama.cpp，核对 `/api/tags` 或 `/v1/models`。下面的初始化脚本不安装推理服务、不下载权重，也不验证模型已经能回答。示例中的模型必须替换为用户实际已有的模型。

### Linux / WSL2 源码 + 本地模型服务

先完成第 4 节依赖和沙箱准备。只执行选定的一组；服务运行在 Windows 或其他机器时，给初始化命令补上已验证的 `--base-url`：

```bash
.venv/bin/python scripts/setup_local_light.py --backend ollama --model qwen3.5:9b --context 16384
./launch.sh start --offline
```

```bash
.venv/bin/python scripts/setup_local_light.py --backend llama_cpp --model qwen35-9b --context 16384
./launch.sh start --offline
```

默认模型地址分别为 `127.0.0.1:11434` 和 `127.0.0.1:8081/v1`。两种后端都使用 `--offline`，配置与会话共用 `.spiritscout-ollama/`，控制台默认端口 8001；目录沿用旧名，不代表只能使用 Ollama。切换后端只调整同目录的 `models.yaml` 与模型指派，保留 `system.yaml` 的工作区路径。初始化脚本不覆盖已有配置，也不按后端创建第二套会话目录。

直接运行 `./launch.sh start --offline` 且缺少配置时，会调用旧的 `setup_ollama_light.py`，要求已有 `qwen3.5:9b` 并创建专用别名。自选模型时使用上面的显式初始化命令。`--offline` 不会自动下载模型、安装工具或切断网络。

### Docker + 宿主机模型服务

先完成第 2 节 Docker 检查和仓库准备。以下以 PowerShell 为例，Linux 将 `py -3` 换成 `python3`。只选一个后端：

```powershell
# Ollama
py -3 scripts/setup_local_light.py --docker --backend ollama --model qwen3.5:9b --context 16384
```

```powershell
# llama.cpp；模型 ID 必须与服务的 --alias 或 /v1/models 一致
py -3 scripts/setup_local_light.py --docker --backend llama_cpp --model qwen35-9b --context 16384
```

默认分别连接 `http://host.docker.internal:11434` 和 `http://host.docker.internal:8081/v1`。不同地址使用 `--base-url`；Ollama 用原生 API 根地址，llama.cpp 使用 OpenAI Chat Completions 接口。`--context` 必须与实际容量匹配，llama.cpp 按每个推理槽计算。

```powershell
py -3 deploy.py init
docker compose -f docker-compose.light.yml up -d --build --wait
docker compose -f docker-compose.light.yml ps
```

默认访问 `http://127.0.0.1:8001`。以后该部署的 `logs`、`restart`、`down`、`exec` 都必须带 `-f docker-compose.light.yml`；`deploy.py up` 管理的是常规部署，不能用来启动轻量部署。

从容器验证所选模型服务，例如 Ollama：

```powershell
docker compose -f docker-compose.light.yml exec spiritscout python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=10).status)"
```

llama.cpp 将 URL 换成 `http://host.docker.internal:8081/v1/models`。容器中的 `127.0.0.1` 是容器自己。连接拒绝时检查宿主服务监听地址、防火墙和实际端口；只在另一个终端设置环境变量，不会修改已经运行的模型进程。需要调整监听时限制可访问来源，不把无认证的模型端口开放到公网。

配置位于 `.spiritscout-light-config/`，数据位于 `light-data` 卷。已有配置时初始化脚本拒绝覆盖，这是保护行为。先核对并备份原配置，再修改相应字段，不删除整个目录重试。更换网页端口用 `.env` 的 `SPIRITSCOUT_LIGHT_PORT`，随后重新执行 `up`。

进入控制台后选择 CTF 或 AWDP；SRC、智能体模式及混入云端候选的配置不会启用本地轻量执行。离线使用前应在断公网条件下验证模型、工具及附件处理。更多参数见 [Ollama 轻量配置](docs/Ollama轻量配置.md) 和 [Windows 轻量测试](docs/Windows轻量测试.md)；后者的测试副本仓库地址不替代本文的主仓库地址。

## 8. 配置模型并验证

按顺序验证，前一步失败就先处理。记录实际结果，不把未运行的检查记为通过。

1. **进程和网页**：Docker 查看所选 Compose 的 `ps`，主服务应为 `healthy`；原生查看启动输出。访问实际端口的 `/login` 和首页。`/login` 能打开只证明 HTTP 服务工作。
2. **管理员登录**：有凭据时实际登录。绑定非回环地址必须配置管理员；远程访问优先使用 SSH 转发，或按用户需求配置支持 WebSocket 的 HTTPS 反向代理。`0.0.0.0` 是监听地址，不是给用户访问的地址。
3. **配置保存**：常规部署在「模型」页添加供应商、协议、地址和准确模型 ID，由用户填写 API Key；在「编队」页指派模型。轻量部署核对生成的本地后端配置，不填假的云端 Key。模型列表接口不可用时可手动添加准确 ID。
4. **依赖与沙箱**：源码使用项目虚拟环境的 Python 运行 `-m pip check`；Docker 在对应服务内运行 `python -m pip check`。Linux 工具环境先运行下面的命名空间检查，再验证项目实际的 shell/Python 工具。Windows/macOS 原生应如实报告沙箱不支持。
5. **浏览器和 MCP**：仅验证用户启用的服务。登录后查看 `/api/mcp/health` 和启动日志；initialize / tools/list 成功后，还要实际打开一个用户允许的测试页。工具目录可用不等于浏览器或登录态可用。
6. **真实模型与工具**：用户已同意调用所选模型时，做一次连通性测试，再用不访问外部目标的小任务验证流式输出和工具调用，例如让工具计算 `1 + 1` 并读取结果。云端测试会产生用量，未获授权时完成其他检查并标明此项未做。只回复 `ok` 不能证明任务链路正常。

命名空间检查在 Linux 原生运行：

```bash
bwrap --ro-bind / / --dev /dev --unshare-pid --proc /proc --die-with-parent /bin/true
```

Docker 常规部署运行：

```bash
docker compose exec spiritscout bwrap --ro-bind / / --dev /dev --unshare-pid --proc /proc --die-with-parent /bin/true
```

轻量 Docker 加 `-f docker-compose.light.yml`。退出码 0 只证明这个最小命名空间命令成功；它没有验证项目的路径遮蔽、Python 解释器和所有工具依赖。源码/Docker 可直接调用 `spiritscout.tools.sandbox.sandbox_exec` 在临时工作区测试 shell/Python，无需调用模型；同时检查 `ok`、`returncode == 0` 和预期输出，不能只检查 `ok`。

## 9. 常见报错怎么处理

先读完整错误，确认是宿主、容器、Python 依赖、配置还是模型服务报错。只修对应层。日志和 `docker compose config` 可能包含凭据，对外汇报前脱敏。

| 报错或现象 | 检查和处理 |
| --- | --- |
| `找不到 Docker` / `Docker 守护进程不可用` | 检查 Docker 是否安装、是否启动、当前 context 和用户权限。Linux socket 权限问题按已有管理策略处理，不执行 `chmod 666 /var/run/docker.sock` |
| `需要 Linux 容器模式` / `需要 Docker Compose 2.20+` | Docker Desktop 切换 Linux containers；安装或升级 Compose v2 插件，用 `docker compose`，再运行 `doctor` |
| `port is already allocated` / `Address already in use` | 查占用者；常规 Docker 改 `SPIRITSCOUT_PORT`，轻量 Docker 改 `SPIRITSCOUT_LIGHT_PORT`，原生用 `--port`。保留容器内部端口，重新 `up` |
| `unhealthy` / `--wait` 失败 | 查看主服务日志和容器健康检查输出，先修其中的启动错误；不要直接延长超时或删除健康检查 |
| 拉镜像、apt、pip 超时或证书错误 | 确认失败的是哪个下载服务，再修该环境的代理、DNS、可信 CA 或来源。宿主浏览器能联网不等于 Docker 构建能联网；不要用 `--trusted-host` 或关闭证书校验掩盖问题 |
| `No space left on device` | 查宿主和 Docker 数据目录空间。先识别可清理的构建缓存或无用文件；不要运行包含数据卷的全局清理命令 |
| Python 版本不足 / `ensurepip is not available` | 安装 Python 3.11+ 和对应 venv 组件，再运行 `./install.sh`。新环境可用 `SPIRITSCOUT_PYTHON` 指定解释器；已有旧版或跨平台 `.venv` 先备份再重建 |
| `externally-managed-environment` / `No module named spiritscout` | 确认使用项目 `.venv` 的 Python，执行该解释器的 `-m pip install -e .`；不要绕过系统包保护 |
| `install.sh` / `launch.sh` 提示 `Permission denied` | 可分别用 `bash install.sh`、`bash launch.sh`；若目录所在磁盘禁止执行程序，将项目移到可执行的 Linux 目录 |
| PowerShell 禁止运行脚本 | Windows 原生用第 4 节的直接 Python 命令，不全局放宽脚本策略 |
| WSL 中 `python3`、`pip` 指向 Windows / 路径在 `/mnt/c` | 确认命令运行在选定的 WSL2 发行版，用 Linux 系统 Python 在 `/home/...` 中创建 `.venv`；保留 Windows 原件，不复用其虚拟环境 |
| WSL 连接 Windows 模型或 CDP 时 `Connection refused` / 超时 | 按第 4 节检查 NAT/mirrored 模式、实际宿主地址、服务监听与防火墙，再从 WSL 重试接口；不直接套用 Docker 地址或修改全局网络模式 |
| `配置文件不存在` / `YAML 解析失败` / `配置引用的环境变量未设置` | 检查工作目录、`--config`、`models_config_path`、缩进和被引用变量。只在文件缺失时创建模板或空模型清单；不要覆盖已有文件 |
| `必须配置管理员凭据` / 密码仍是 `please-change-me` | 设置 `.env` 中的管理员用户名和新密码。`init` 不修改已有 `.env`；原生重启，Docker 重新 `up` 使变量生效 |
| 修改密码后仍不能登录 | 检查进程环境变量是否覆盖 `.env`、是否设置 `SPIRITSCOUT_ADMIN_SYNC=0`、是否打开了另一端口或工作区。Docker 仅 `restart` 不重载 `.env`。确认后重启/重建容器，不删除 `admin.db`、`vault.key` |
| `cannot import name 'CacheableResult' from 'mcp.types'` | 按当前 `pyproject.toml` 重新安装依赖或重建镜像。目前约束为 LiteLLM `1.102.0`、MCP `>=1.28.1,<2`。不是密钥问题，不单独升级其中一个包 |
| 模型测试通过，任务立即 `APIConnectionError` | 查看未截断的完整错误，先排除依赖导入错误，再检查流式工具协议、模型 ID 和端点。不要把所有异常都当成网络故障 |
| 模型接口 401/403、404、429 | 分别核对该供应商的凭据/权限、协议路径/模型 ID、限流/额度。无 `/models` 接口可手动登记；不要凭模型名字随意切换协议或伪造 Key |
| `沙箱不可用：需要 Linux 与 bubblewrap` | Linux/WSL2 安装 bubblewrap；Windows 原生优先改用已确认的 WSL2 源码部署，macOS 可选 Docker。禁止退回宿主直接执行工具 |
| `bwrap: ... Operation not permitted` / 无法创建命名空间 | 在实际运行环境重跑第 8 节检查。WSL 先确认发行版为 WSL2，再查内核与发行版安全策略；Docker 核对项目 Compose 已声明的权限。受限环境仍失败时说明限制，不改成 `--privileged` 或全局关闭安全策略 |
| 控制台正常，但沙箱找不到 Python 或工具 | 核对 `.venv` 的解释器链接及工具安装位置。家目录和 `/usr/local` 被遮蔽；改用可访问的系统 Python 建立环境，工具装入执行环境可见的位置 |
| MCP `No such file or directory` / `找不到 npx` | 检查运行环境内的命令、Node.js/npm 和路径；IDA/Yakit 需另行准备。只关闭用户不需要的那项 MCP，不把所有 MCP 失败当成核心安装失败 |
| Chrome/MCP 超时或 CDP 连接失败 | 先确认浏览器和固定 MCP 版本已准备好。SRC 从实际运行环境访问配置的 `/json/version`，检查 CDP 地址、监听和转发；CTF/智能体不需要 SRC CDP |
| 轻量 Docker 提示配置目录不存在 / 要先生成 `.env` | 先运行带 `--docker` 的 `setup_local_light.py` 和 `deploy.py init`，再启动 `docker-compose.light.yml` |
| `保留已有配置，请使用新目录或手工修改` | 检查现有轻量配置是否可直接复用；切换后端时先停止任务、备份，再改配置。不要删除配置消除报错 |
| 本地模型连接拒绝 / model not found | 检查服务是否运行、运行环境是否可达、模型是否已下载及 ID 是否一致；容器访问宿主使用可达宿主地址，不用容器回环地址 |
| 本地模型显存不足 / 上下文容量错误 | 查其他推理进程和服务日志；按实际容量降低上下文或模型负载，同步修改服务与灵枢模型配置。不要只把 `context_window` 改大 |
| AppImage 报 FUSE 缺失 | 可用 `./实际文件.AppImage --appimage-extract-and-run`；架构或 glibc 不匹配需换匹配产物，不能靠提取解决 |
| `Exec format error` / `GLIBC_... not found` | 核对安装包架构和系统 glibc，选择匹配包、源码或 Docker；不要替换系统 libc。未签名包被拒绝时不要关闭全局签名校验 |
| 升级后模型/会话变空 / `目标数据卷非空，拒绝覆盖` | 查 Compose project name、数据卷、源码/安装包数据目录是否换了。按第 10 节迁移；目标有数据则先分清归属，不强行覆盖 |

## 10. 已有安装、迁移和交付

升级前记录实际运行方式、配置路径、工作区、端口和 Compose project name。先完成或停止运行中的任务，再停服务并备份；数据库与 `vault.key` 等密钥文件一起保留。不要复制正在写入的 SQLite 数据库，也不要让两个实例同时写同一工作区。

| 方式 | 默认配置 / 数据位置 |
| --- | --- |
| 常规 Linux/WSL2 源码 | Linux 文件系统中的仓库 `configs/` / `.spiritscout/` |
| 常规 Docker | `spiritscout-config` / `spiritscout-data` 命名卷；可选 `browser-profile` 卷 |
| Docker 开发覆盖 | 宿主仓库 `configs/` / `.spiritscout/` |
| 轻量 Docker | 宿主 `.spiritscout-light-config/` / `light-data` 卷 |
| Linux/WSL2 源码轻量（Ollama / llama.cpp） | 共用 `.spiritscout-ollama/` |
| Linux 系统包 / AppImage | `${XDG_DATA_HOME:-~/.local/share}/spiritscout/` 下的配置和工作区 |
| 裸 ELF | 程序所在目录，或 `SPIRITSCOUT_DATA_HOME` 指定目录 |

以上是默认值，最终以实际配置为准。Compose 卷通常带项目名前缀；更换目录或 project name 可能选择另一组卷，不表示旧数据已丢失。

只有旧版宿主目录挂载要迁移到常规 Docker 命名卷时，使用下面的流程；不是每次升级都迁移：

```bash
docker compose --profile src-browser down
docker compose build spiritscout
python3 deploy.py migrate
python3 deploy.py up --no-build
```

Windows 把 `python3` 换成 `py -3`；所有命令必须使用同一 project name。另行停止仍在使用原目录的原生实例或其他项目容器。迁移目标数据卷必须为空，保留源目录备份。普通重建会保留配置卷；有新增配置项时逐项合并，不覆盖模型和编队配置。

交付时用直接的文字列出：

- 安装方式、安装目录、配置/数据位置、访问地址和实际运行版本；版本从运行程序或源码 `__version__` 获取。
- 如何启动、停止、查看日志；使用的 Compose 文件和 project name。
- 管理员凭据在本机哪里查看，不附密码和 API Key。
- 已验证的网页、登录、模型、shell/Python、浏览器/MCP；未验证项及具体原因。
- 仍需用户填写或完成的事项。只有网页能打开时，写“控制台已启动，模型/工具尚未验证”，不要写“安装完成，全部可用”。

说明错误时写“Docker 没有启动，启动后重新执行 `doctor`”或“容器连不到宿主模型，需要修正监听地址”。避免“环境异常，请优化配置”“适当调整参数”等无法执行的说法。
