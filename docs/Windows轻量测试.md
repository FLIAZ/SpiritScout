# Windows 轻量测试：Ollama / llama.cpp

这是测试副本的部署入口。使用 **Docker Desktop 的 Linux 容器运行灵枢**，
Windows 宿主运行已有的 Ollama 或 llama.cpp。模型权重、IDA、许可证和个人会话不在仓库里。
首次构建镜像需要联网；离线使用前先构建、下载模型并完成一次实际演练。

## 准备

需要 Git、Python 3.11+、Docker Desktop（Linux containers，Compose 2.20+）及一个
支持工具调用的本地模型。以下命令在 PowerShell 的仓库根目录运行。

```powershell
git clone https://github.com/FLIAZ/spiritscout-light-test.git
cd spiritscout-light-test
py -3 deploy.py doctor
```

私有仓库需要先接受协作者邀请，并使用自己的 GitHub 凭据。Docker 容器访问 Windows
服务使用 `host.docker.internal`，容器里的 `127.0.0.1` 指的是容器本身。
参见 [Docker Desktop 网络说明](https://docs.docker.com/desktop/features/networking/)。

## 选择一种模型服务

### A. Windows Ollama

确认 `ollama list` 中已有模型。以 `qwen3.5:9b` 为例，缺少时先自行下载：

```powershell
ollama pull qwen3.5:9b
```

如果容器连不到只监听回环的 Ollama，先从系统托盘退出已有 Ollama，再在单独的
PowerShell 窗口启动：

```powershell
$env:OLLAMA_HOST="0.0.0.0:11434"
ollama serve
```

仅允许本机 Docker 所需的连接；不要将推理端口发布到公网。使用 Ollama 桌面启动方式时，
环境变量需配置给实际服务进程并重启，不能只在另一个终端设置后就认为已生效。

在仓库窗口初始化：

```powershell
py -3 scripts/setup_local_light.py --docker --backend ollama --model qwen3.5:9b --context 16384
```

不会新建模型别名或改写原模型，参数通过请求发送。模型名可替换成自己的已有模型；
默认参数面向 Qwen3.5 9B，其他模型需核对工具调用、思考开关和上下文支持。

### B. Windows llama.cpp

从 [llama.cpp 官方发布页](https://github.com/ggml-org/llama.cpp/releases) 准备适合显卡的
`llama-server.exe` 及其运行库、兼容的 GGUF。示例路径请替换成自己的真实路径：

```powershell
& 'C:\llama.cpp\llama-server.exe' -m 'D:\models\Qwen3.5-9B-Q4_K_M.gguf' --alias qwen35-9b --host 0.0.0.0 --port 8081 -c 16384 -np 1 -ngl 99 -b 512 -ub 128 -fa on -ctk q8_0 -ctv q8_0 --cache-ram 1024 --jinja --reasoning off --top-k 20 --top-p 0.9 --min-p 0 --repeat-penalty 1.05
```

参数名称按 llama.cpp b12186 核对，其他版本先检查 `llama-server.exe --help`。
这是 8GB 显存的起始参数，需以服务器启动日志和实际请求为准；若显存不足，先降低
GPU 层数或上下文，不能同时运行另一个占显存的模型。`-np 1` 只开一个推理槽；
灵枢配置的 `context_window` 必须匹配服务每个槽的实际容量。

视觉模型还需要匹配的视觉投影器；在上述启动命令后补：

```powershell
--mmproj 'D:\models\mmproj-Qwen3.5-9B.gguf'
```

只加载文本 GGUF 不代表服务能看图。8GB 显存不足时可尝试 `--no-mmproj-offload`
把视觉编码器放到 CPU，代价是看图更慢。不要混用其他架构的投影器；Ollama 缓存中的
GGUF 可能采用合并布局，也不能只改扩展名就认定与 llama.cpp 的多模态加载方式兼容。
参数依据：[llama-server 官方说明](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。

在仓库窗口初始化：

```powershell
py -3 scripts/setup_local_light.py --docker --backend llama_cpp --model qwen35-9b --context 16384
```

`--model` 对应服务器 `--alias`；也可使用 `/v1/models` 返回的实际 ID。
服务自定义地址用 `--base-url http://host.docker.internal:8081/v1`。
此后端走 OpenAI Chat Completions，但显式标记为本地 llama.cpp，保留轻量执行器和黑板。
无工具回复进入恢复请求时，纯 llama.cpp 候选使用原生 `tool_choice=required`，
让模型选择实际工具、询问缺口或结束；不指定解题动作、不增加固定回合上限。
普通请求仍用原有选择方式，Ollama 与混合候选不发送这一约束。

## 增大上下文

上面的 16K 是初次连通测试值。完成工具与视觉检查后，可按机器余量逐档测试：
32K = `32768`、64K = `65536`、96K = `98304`、128K = `131072`。
先结束运行中的会话，备份配置，再同步修改 llama.cpp 的 `-c` 与
`.spiritscout-light-config/models.yaml` 中对应模型的 `context_window`，重启两项服务。
首次生成配置时可直接给初始化脚本传同样的 `--context`；已有配置不要重新生成覆盖。
Ollama 仍通过请求里的上下文参数配置，不能套用 llama.cpp 的命令行开关。

`--cache-ram 1024` 将 llama.cpp 的主存提示缓存限制到 1 GiB，不是进程总内存上限，
也不改变 Q8 GPU KV 缓存精度。增大窗口后要核对服务报告的单槽容量，观察显存、
系统可用内存、交换活动，以及长会话中途消息的确认延迟。

本机 Linux / 8GB 显存实测中，128K 能运行，但显存最低只剩 94 MiB，不能直接作为
Windows 队友的通用默认值。更大窗口改善了部分历史保留，仍出现分页回读重复和报告缺项。
容量选择及真实结果见 [本地上下文扩容验证](本地上下文扩容验证.md)；配置能加载和
答案正确都不能代替完整长会话验收。

## 启动灵枢

```powershell
py -3 deploy.py init
docker compose -f docker-compose.light.yml up -d --build --wait
```

打开 **http://127.0.0.1:8001**。首次初始化生成的用户名和随机密码在本机 `.env` 中查看。
选择 **CTF 或 AWDP** 才会启用轻量单执行者；SRC、智能体模式及混入云端候选的编队不启用。

需要 pwntools、z3 等可选工具箱时，在 `.env` 中设置 `INSTALL_TOOLKIT=1` 后重新构建。
这个选项安装 Python 工具箱，不会自动提供 jadx、Java 或 GDB / pwndbg；这些依赖需要
安装在实际执行分析的环境中。轻量配置默认没有 MCP，IDA 等服务需单独配置。
完整候选工具范围、CLI / 库 / 持久调试的区别及待实现项目见
[本地模型外部工具方案](本地模型外部工具方案.md)；出现在方案里不代表已经接入或验收。

```powershell
docker compose -f docker-compose.light.yml logs -f --tail 100
docker compose -f docker-compose.light.yml restart
docker compose -f docker-compose.light.yml down
```

配置在 `.spiritscout-light-config/`，会话、黑板和产物在 Docker 的 `light-data` 数据卷。
配置文件采用 JSON 格式的 YAML，可在编辑器中修改；初始化脚本遇到已有配置会拒绝覆盖。
切换后端先结束会话并停止控制台，再备份和调整配置。`down` 保留数据卷，日常停止不要加 `-v`。
端口冲突可在 `.env` 增加 `SPIRITSCOUT_LIGHT_PORT=8101` 后重新 `up`。

## 验证模型服务可达

Ollama：

```powershell
docker compose -f docker-compose.light.yml exec spiritscout python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=10).status)"
```

llama.cpp：

```powershell
docker compose -f docker-compose.light.yml exec spiritscout python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:8081/v1/models', timeout=10).status)"
```

返回 200 只代表可达；还需在控制台执行一次实际工具任务。连接拒绝时核对服务监听地址、
端口和 Windows 防火墙；不要把“Windows 浏览器能打开”当作“容器也能访问”。

## 队友测试记录

请使用自己的已授权材料，记录模型名、量化、上下文、显存、后端版本和以下结果：

1. 基础工具：读取附件、运行一次 Python / shell，核对真实结果与产物。
2. 看图：上传包含文字和图形的图片，观察是否调用 `view_image`，并核对图像内容。
3. 长会话：工具执行期间追加一个新要求，检查原话是否收到、后续行为是否改变。
4. 黑板与探索链路：观察意图、尝试、转向和证据更新；刷新页面后应能保留。
5. 结束与恢复：目标完成后是否正常结束；继续会话是否重复已完成工作。

当前已观察到的小模型风险包括长会话重复、终态表达不稳定及部分恢复路径重读材料。
这个副本用于比较和反馈，尚不能宣称已经优于其他 Agent 或解决全部长会话问题。
Windows Docker Desktop 的完整端到端行为需队友在各自机器实测；Linux 本地或协议测试
不能替代 Windows 实机验收。

本批实际结果与未解决项见 [本地双后端验证记录](本地双后端验证记录.md)。

## Linux 原生连接 llama.cpp

已有 Python 环境和 bubblewrap 时，无需 Docker：

```bash
.venv/bin/python scripts/setup_local_light.py --backend llama_cpp --model qwen35-9b --context 16384
./launch.sh start --offline
```

默认服务地址 `http://127.0.0.1:8081/v1`，控制台端口 8001。Ollama 与 llama.cpp
统一使用 `--offline`，共用 `.spiritscout-ollama/` 下的配置与会话；目录沿用旧名。
已有轻量配置时，初始化脚本会保留文件并提示修改同目录的 `models.yaml`；切换后端
同时调整模型指派，保留 `system.yaml` 的工作区路径，避免会话分裂。
使用 `./launch.sh restart --offline`、`./launch.sh stop --offline` 管理轻量控制台；
这些命令不启动或停止模型服务。
