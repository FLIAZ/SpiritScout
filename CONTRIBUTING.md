# 贡献指南

## 环境搭建

```bash
git clone https://github.com/FLIAZ/SpiritScout.git
cd SpiritScout
python3 -m venv .venv && .venv/bin/pip install -e '.[toolkit]'
./launch.sh
```

沙箱依赖 bubblewrap（`dnf install bubblewrap` 或发行版等价命令）。

模型 API Key 在控制台「模型」页配置（加密存本机 vault，不落明文）。

## 分支流程

- `main` 始终保持可运行——每个 push 都会触发 CI
- 功能开发从 main 拉短分支：`git checkout -b feat/简短描述`
- 提交 PR 到 main，CI 通过后合并
- 提交信息用一行说明改动

## 代码规范

- Python：ruff 全绿（`ruff check src/ tests/`）
- 前端 JS：`node --check` 通过（无构建步骤，原生 HTML/CSS/JS）
- 每次改完跑：`.venv/bin/python -m unittest discover tests/`

## 项目结构

版本与发布规则见下方「版本管理」。

```
src/spiritscout/
├── agents/          # 主 Agent + 各专项智能体
│   ├── master/      # DAG 元决策状态机
│   ├── hostops/     # 听风/贯甲/深耕（SRC 主机专项）
│   └── profiles.py  # 角色档案（名字/职责/工具权限）
├── ctf/             # CTF 五方向智能体 + Flag 管理 + WriteUp
├── gateway/         # 多模型网关（LiteLLM 路由/主备/流式）
├── knowledge/       # 知识库（SQLite FTS）+ 经验库 + 安全技能
├── policy/          # 授权事实、分模式审批、提问
├── tools/           # 内置工具 + MCP 客户端 + bwrap 沙箱
├── modes/           # 模式装配器（SRC/CTF/渗透赛）
├── runtime.py       # 三模式入口 + 基建组装
└── web/             # FastAPI 控制台 + 前端（static/）
```

## 设计原则

- **模型决策**：模型负责测试判断与取舍；后端负责工具、状态、审批和审计。
- **不写死解题模板**：知识库存参考事实（平台特性/载荷/坑），不存步骤序列
- **智能体自治**：spawn 派生、路线规划、Flag 提交均由模型自主决策
- **授权边界**：由模型结合用户原话、项目规则和后续答复维护；后端不增加固定禁打列表。
- **按模式审批**：SRC 危险动作由 ApprovalBroker 请求批准；CTF 与智能体模式
  的工具操作自动审批并留审计。智能体模式不向用户提问。

## 版本管理

使用 `主版本.次版本.修订号`（如 `0.12.0`）：

- **修订号 +1**：兼容性修复、界面细节、性能优化、文档修订，例如 `0.12.0 → 0.12.1`。
- **次版本 +1**：新增能力或成组的功能/架构演进，修订号归零，例如 `0.12.1 → 0.13.0`。
- **主版本 +1**：稳定版发生不兼容的接口或数据格式变更，次版本和修订号归零。
  当前仍是 `0.x` 开发阶段，不因历史改动多就宣称 `1.0.0`；这一阶段的不兼容变更
  升次版本并说明迁移要求，建立稳定接口承诺后再进入 `1.0.0`。

每次独立交付的仓库变更更新一次版本与日志，同一任务内的多次编辑、测试和修复
不逐次加号。纯分析、问答或只运行测试不升版本；用户明确指定版本时优先遵从。

唯一版本值在 `src/spiritscout/__init__.py` 的 `__version__`。`pyproject.toml`
通过 setuptools 动态读取该值，CLI、Web OpenAPI/侧栏和 MCP 客户端共用该来源。
不要在其它代码里复制版本字面量。构建产物应重新构建/安装以更新其包元数据。

更新时同步在 `CHANGELOG.md` 顶部加入 `## [版本号] - YYYY-MM-DD` 和本次内容；
若版本已在本次任务中更新，则把同一任务的后续改动补入该条目。
回溯编号只整理可核实的功能阶段，并明确标注；不要改写旧提交或补造历史发布标签。
Git 标签只在实际决定发布并提交后创建，不在未提交工作区自动打标签。
