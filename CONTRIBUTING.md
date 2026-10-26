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
