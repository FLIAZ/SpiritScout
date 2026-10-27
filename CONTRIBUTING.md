# 贡献指南

欢迎提交问题报告、文档修订和代码改动。先确认问题属于当前版本，再从最新 `main`
创建分支；把改动控制在可独立审核的范围内。

## 准备开发环境

安装和数据迁移统一参考 [部署与配置](docs/部署与配置.md)，不要用开发工作区覆盖运行中的配置。
只修改文档或前端不需要配置真实模型，也不需要下载额外操作系统或启动付费测试。

**Linux / macOS 的 Python 环境**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

各系统使用自己的虚拟环境。完整工具执行优先使用 Docker；Linux 原生沙箱需要
bubblewrap 和命名空间权限。测试密钥使用明确标记的夹具值，真实密钥通过控制台配置。

## 分支与 PR

1. 从最新 `main` 创建短分支，例如 `fix/streaming-compatibility` 或 `docs/quickstart`。
2. 新行为先补充能暴露问题的测试，完成修复后再运行回归；文档修订核对链接、命令与代码事实。
3. 更新版本和 CHANGELOG，同一项交付只递增一次；按下文规则选择修订号或次版本。
4. 提交 PR 到 `main`，说明问题、最终行为、验证结果和限制，等待 CI 通过后审核合并。

提交信息用一行人话描述结果。PR 不夹带无关重构、个人配置、日志或构建缓存；
`.env`、真实模型清单、Cookie、数据库与密钥文件不得提交。

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

- 新模块至少通过最小可用性测试后再扩展；交付前完成后端、Ruff 和前端全量检查。
- 环境依赖导致的跳过要列出对应测试和原因，不能把跳过记为成功或关闭失败用例。
- Mock 可验证局部逻辑，不能替代真实协议。模型网关要覆盖本机 HTTP/SSE 的真实 SDK
  请求；浏览器、沙箱和部署改动要说明实际运行环境及没有验证的平台。
- 真实模型和付费端点测试需有用户授权，记录测试目的与用量；默认使用本机夹具。

### 依赖兼容

LiteLLM 固定为 `1.102.0`，MCP SDK 使用 `>=1.28.1,<2`。较新的 LiteLLM MCP 支持
已转向 2.x，单独升级可能使普通请求通过、流式工具调用在本地导入阶段失败。
依赖变更需要同时验证普通请求、流式输出、工具参数和 MCP 握手，不只点击一次连通性测试。

修改依赖后重新安装或构建镜像，确认实际运行版本；不要用运行中容器里的临时补丁代替仓库约束。

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
| 兼容修复、性能或界面细节、文档修订 | 修订号 +1 | `0.12.6 → 0.12.7` |
| 新能力或成组的功能／架构演进 | 次版本 +1，修订号归零 | `0.12.7 → 0.13.0` |
| 稳定版发生不兼容接口或数据格式变更 | 主版本 +1，其余归零 | `1.4.2 → 2.0.0` |

当前 `0.x` 阶段的不兼容变更升次版本并说明迁移要求，不因历史改动数量宣称稳定版。
每次独立交付更新一次版本；同一任务的后续编辑、验证和修复不重复加号。
纯分析、问答或只运行测试不升版本，用户指定版本优先。

`pyproject.toml` 动态读取源码版本，CLI、Web 和 MCP 共用它；其它代码不要复制版本字面量。
构建产物需要重新安装／构建以更新包元数据。同步在 CHANGELOG 顶部加入
`## [版本号] - YYYY-MM-DD` 及本次内容；展示文档也要一起更新。

回溯编号只整理可核实的历史阶段，并明确说明其不是当时的发布或标签。
不改写旧提交，不补造发布；只有实际决定发布并提交后才创建 Git 标签。
