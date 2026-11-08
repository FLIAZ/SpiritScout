# 贡献指南

欢迎提交问题报告、文档修订和代码改动。先确认问题属于当前版本，再从最新 `main`
创建分支；把改动控制在可独立审核的范围内。

## 准备开发环境

安装和数据迁移统一参考 [部署与配置](docs/部署与配置.md)，不要用开发工作区覆盖运行中的配置。
只修改文档或前端不需要配置真实模型，也不需要下载额外操作系统或启动付费测试。

**uv（推荐，三平台一致）**

项目依赖 LiteLLM 1.102.0，Python 范围为 3.11–3.14；uv 会自动选择兼容解释器。
安装 [uv](https://docs.astral.sh/uv/) 后，在项目根执行：

```bash
uv sync --extra dev                      # 运行依赖 + ruff/pytest
uv sync --extra dev --extra toolkit      # 同时需要 pwntools、z3、scapy 等工具库
uv run python -m spiritscout --help
```

多个 extras 需在同一条 `uv sync` 命令中组合；单独执行后一次会按当前参数同步环境。

`uv sync` 按 `uv.lock` 创建项目根 `.venv`，`launch.sh`、`launch.ps1` 和下列检查命令
可直接使用该环境。修改依赖后执行 `uv lock` 并一并提交锁文件。

**不使用 uv 时仍可使用 pip**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

**Windows 原生 PowerShell（不含 Linux 工具沙箱）**

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

各系统使用自己的虚拟环境。Windows 完整工具执行优先在 WSL2 内源码部署，Linux
优先源码部署；沙箱需要 bubblewrap 和命名空间权限。Docker 保留为可选方式。
WSL2 用户在 Linux 文件系统中创建仓库和虚拟环境，具体检查见 [Agent 安装指南](AGENTINSTALL.md)。
测试密钥使用明确标记的夹具值，真实密钥通过控制台配置。

## 分支与 PR

1. 从最新 `main` 创建短分支，例如 `fix/streaming-compatibility` 或 `docs/quickstart`。
2. 新行为和重要缺陷补充有价值的测试，按影响范围验证；文档修订只核对链接、命令与代码事实，不强制测试先行或全量回归。
3. 同期改动合并到当前 CHANGELOG 条目；调整版本号前先询问用户，明确获准后再按下文规则递增。
4. 经用户明确同意后再推送远端、提交 PR 到 `main` 或执行合并；PR 说明问题、最终行为、验证结果和限制，等待 CI 通过后审核合并。

提交信息按实际修改的文件或模块简短说明改了什么，标题用一句人话；需要正文时按
`文件或模块：改动` 列出即可。不逐行复述 diff，不写宣传语、空泛的“全面优化”，
不添加 AI 生成说明或代理署名。PR 不夹带无关重构、个人配置、日志或构建缓存；
`.env`、真实模型清单、Cookie、数据库与密钥文件不得提交。

### 本机项目与远端推送

根目录的 `ctfy-docker/` 与 `tsecbench-docker/` 是维护者本机的验证项目，
不暂存、不提交、不推送；不要强制添加或删除本地目录。协作者无需下载、克隆或启动它们，
开发和测试不能将它们作为必备依赖。这个排除只针对两个本机目录，SpiritScout 自身的
Dockerfile、Compose 和通用部署代码仍正常维护、提交。

未经用户明确同意，不执行远端推送、标签推送、PR 合并或发布；修改完成、测试通过或
完成本地 commit 不代表获得推送授权。用户已经明确授权本次目标和范围时无需重复询问。

## 检查与验证

在项目根目录执行，Windows 将 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`：

```bash
.venv/bin/python -m ruff check src/ tests/ deploy.py deploy
.venv/bin/python -m unittest discover -s tests -v
```

前端没有构建步骤，使用 Node.js 22+ 检查语法并运行回归：

```bash
for file in src/spiritscout/web/static/js/*.js; do node --check "$file" || exit; done
for file in tests/test_*.js; do node "$file" || exit; done
```

Windows PowerShell 的等价命令：

```powershell
Get-ChildItem src/spiritscout/web/static/js/*.js | ForEach-Object {
    node --check $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "JavaScript 语法检查失败" }
}
Get-ChildItem tests/test_*.js | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "前端测试失败" }
}
```

- 按影响范围选择验证：文档修改核对内容与链接，局部代码运行相关测试及涉及文件的静态检查；新增行为和重要缺陷补充有价值的覆盖，不强制测试先行。
- 不把全量检查作为每次修改或交付的门槛。仅在用户要求或跨模块、基础设施、依赖变更确有广泛影响时，等整批修改稳定后集中运行；修复后优先定向重跑，避免反复全量回归。
- 环境依赖导致的跳过要列出对应测试和原因，不能把跳过记为成功或关闭失败用例。
- Mock 可验证局部逻辑，不能证明真实协议或浏览器行为。涉及相关请求链路时，针对受影响的本机 HTTP/SSE、SDK、浏览器或沙箱路径实测；环境不可用时如实说明未覆盖范围，不要求无关修改也运行这些检查。
- 真实模型和付费端点测试需有用户授权，记录测试目的与用量；默认使用本机夹具。

### 前端本地化

前端界面文案使用 `I18n.t("中文源文案")` 或带插值的 `I18n.t` 模板；HTML 模板使用
`I18n.html` 标签，并在 `src/spiritscout/web/static/js/i18n.en.js` 中维护英文译文。
译文的 `{0}`、`{1}` 对应模板插值，必须保留。动态内容仍须按原位置转义；不要把会话、
项目名称、工具输出或证据传入翻译函数。新增静态页面文案也应同步补充词表。

### 依赖兼容

LiteLLM 固定为 `1.102.0`，MCP SDK 使用 `>=1.28.1,<2`。较新的 LiteLLM MCP 支持
已转向 2.x，单独升级可能使普通请求通过、流式工具调用在本地导入阶段失败。
依赖变更需要同时验证普通请求、流式输出、工具参数和 MCP 握手，不只点击一次连通性测试。

修改依赖后重新安装或构建镜像，确认实际运行版本；不要用运行中容器里的临时补丁代替仓库约束。

### 打包可执行文件

`scripts/build-exe.ps1` 与 `scripts/build-elf.sh` 使用 PyInstaller 生成单文件控制台，
配置在 `scripts/spiritscout.spec`。构建使用 Python 3.11–3.14；有 uv 时脚本自动获取
指定版本并创建隔离的 `.venv-build`，没有 uv 时退回系统兼容 Python 与 pip。

```powershell
.\scripts\build-exe.ps1                 # dist\SpiritScout.exe
.\scripts\build-exe.ps1 -PythonVersion 3.12
```

```bash
./scripts/build-elf.sh                  # dist/spiritscout
BUILD_PYTHON=3.12 ./scripts/build-elf.sh
```

构建脚本最后会执行 `dist/... --version`。修改打包资源、依赖或启动路径后，至少运行一次
对应平台的构建，并在空目录中启动产物确认首次配置生成和 `/login` 可访问。
`.venv-build/`、`build/`、`dist/` 属本地构建产物，不提交。打包版的能力边界见
[部署与配置](docs/部署与配置.md#免安装可执行文件)。

## 设计与安全约定

| 事项 | 约定 |
| --- | --- |
| 模型与后端 | 模型负责规划、派生与取舍；后端负责工具、状态、按模式审批和审计 |
| 任务模式 | 由用户在前端选择，后端不改判；完整规则交模型理解，不按赛制标签套固定步骤 |
| 自主执行 | 不增加固定回合顶、预算熔断、关键词解题模板或自动 Flag 正则判定 |
| 测试范围 | 模型结合用户原话、规则和后续答复维护；不新增固定禁打列表 |
| SRC 审批 | 删除文件、反弹 shell、WebShell、容器逃逸、爆破等危险动作由 ApprovalBroker 即时请示 |
| CTF / 智能体 | 工具操作预授权并留审计；智能体不向用户提问，普通 CTF 可询问缺失信息或范围 |
| 知识与证据 | 知识库存参考事实；发现保留来源与实际验证结果，不把推断写成已确认成果 |

涉及安全机制先阅读 [安全设计](docs/04-安全设计与防绕过防攻击.md)。
项目自身漏洞按 [安全策略](SECURITY.md) 私密报告，不把敏感复现细节放进公开 Issue。

## 文档维护

- `README.md` 与英文版 `README_EN.md` 面向首次使用者，优先说明入口、部署和真实能力；两者同步维护顶部语言切换、功能、命令、使用边界及展示版本，实现细节放到专题文档。
- 安装、数据路径、配置或安全边界变化时，同步更新 `docs/部署与配置.md` 和对应专题。
- 命令、示例和链接应可复现；历史测试规模、跑分、回溯版本要标明日期，不当作当前结果。
- README、文档索引中的展示版本与源码版本同步，保留第三方来源、许可证说明与贡献者致谢。

## 版本管理

使用 `主版本.次版本.修订号`；唯一代码版本值为
`src/spiritscout/__init__.py` 的 `__version__`：

| 改动 | 递增方式 | 示例 |
| --- | --- | --- |
| 兼容修复、性能或界面细节、文档修订 | 修订号 +1 | `0.20.0 → 0.20.1` |
| 新能力或成组的功能／架构演进 | 次版本 +1，修订号归零 | `0.20.2 → 0.21.0` |
| 稳定版发生不兼容接口或数据格式变更 | 主版本 +1，其余归零 | `1.4.2 → 2.0.0` |

当前 `0.x` 阶段的不兼容变更升次版本并说明迁移要求，不因历史改动数量宣称稳定版。
调整版本号前先询问用户，未经明确同意不递增、回退或另定版本。用户已明确指定本批版本
或授权调整时直接按要求执行，无需重复询问。收到新消息、完成功能或准备 commit 不构成升版授权。
同期功能、后续优化、修复、文档和测试共用同一版本，统一补充当前 CHANGELOG 条目，
不按消息、文件、功能点或 commit 单独升版；是否开始下一版本由用户决定。
当前批次为 `0.21.0`。小修依次使用 `0.21.1`、`0.21.2`；下一批较大的功能更新使用
`0.22.0`。次版本号可以大于 9，例如 `0.16.0` 本身就是合法版本号，不回退编号。
上表只用于用户同意升版后的编号选择。
纯分析、问答或只运行测试不升版本，用户指定版本优先。

`pyproject.toml` 动态读取源码版本，CLI、Web 和 MCP 共用它；其它代码不要复制版本字面量。
构建产物需要重新安装／构建以更新包元数据。同步在 CHANGELOG 顶部加入
`## [版本号] - YYYY-MM-DD` 及本次内容；展示文档也要一起更新。

回溯编号只整理可核实的历史阶段，并明确说明其不是当时的发布或标签。
不改写旧提交，不补造发布；创建或推送 Git 标签须获得用户明确授权。
