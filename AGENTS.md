# AGENTS.md — AI 编码代理说明书

本文件供 AI 编码助手（Claude Code / Cursor / Codex 等）在本仓库中工作时参考。

## 项目概述

灵枢（SpiritScout）是一个多智能体安全测试系统。主 Agent「灵枢」通过 DAG
动态规划任务，专项智能体集群并行执行，覆盖 SRC 漏洞挖掘、CTF 解题与渗透赛。

## 技术栈

- 后端：Python 3.11+ / FastAPI / SQLite / LiteLLM / LangGraph
- 前端：原生 HTML/CSS/JS（无构建步骤），多文件结构
- 沙箱：bubblewrap（bwrap）
- MCP：官方 SDK，stdio/http/sse 三传输

## 关键约束

- **不要写死解题模板**：知识库存参考事实（平台特性/载荷/坑），不存步骤序列
- **不要后端写死行为规则**：意图分类、派生时机由模型从提示词决策；新任务的模式由用户在前端选择，后端不改判。智能体模式不限定赛制，规则原文交模型理解与动态规划，兼容创新/混合赛制与旧标签
- **回合顶已拆除**：不要重新加入固定回合数限制或预算熔断
- **授权边界**：测试边界由模型结合用户原话、项目规则与后续答复在上下文中维护；后端不写硬性禁打列表，扩围由模型提问用户确认
- **按模式执行审批**：SRC 模式中，删除文件、反弹 shell、上传 WebShell、
  容器逃逸、爆破等动作由 ApprovalBroker 即时请示用户；普通 CTF 与智能体
  （competition）模式所有工具操作预授权并留审计记录，不请求批准。
  智能体模式用于无人值守竞赛，也不向用户提问；普通 CTF 仍可就任务缺失信息
  或授权范围不清向用户提问，操作审批不再阻塞执行。
- **Flag 提交由模型判定**：不要恢复后端正则提取
- **前端无构建步骤**：直接修改 static/ 下的文件，改完 node --check 校验

## 代码风格

- Python：ruff 全绿，中文注释，类型标注
- 前端：原生 ES2020+，无框架，无外部依赖
- 提交信息：一行人话摘要

## 测试

```bash
.venv/bin/python -m unittest discover tests/
.venv/bin/python -m ruff check src/ tests/
for f in src/spiritscout/web/static/js/*.js; do node --check "$f"; done
for f in tests/test_*.js; do node "$f"; done
```

行为质量以真实任务浏览器实测为准——mock 不等于真实体验。

## 任务完成标准（文档与测试）

1. **文档与注释同步**：每次任务完成后都要检查并更新文档与注释——至少评估
   `docs/` 下对应专题、`README.md`、`CHANGELOG.md` 是否仍然准确；行为、架构、
   安全边界、算法、阶段或配置发生变化时必须同步修改，过时注释一并修正。
   文档与代码冲突时以代码为准，并立即补齐文档。
2. **测试先行与全量回归**：每新增一个功能或模块，都要编写测试；至少通过一个
   最小可用性测试后才能继续下一步。任务结束前必须运行全量测试（后端 unittest、
   ruff、前端 `node --check` 与 `tests/test_*.js`）；全部通过才能结束，否则继续
   排查和修复。环境依赖导致的 skip 必须在交付说明中列出，不得用 skip 掩盖真实失败。
