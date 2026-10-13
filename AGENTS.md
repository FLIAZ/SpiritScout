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
- **不要后端写死行为规则**：意图分类、模式选择、派生时机均由模型从提示词决策
- **回合顶已拆除**：不要重新加入固定回合数限制或预算熔断
- **授权边界**：禁打列表是唯一硬边界；白名单转动态扩围由模型提问用户确认
- **危险操作必须用户审批**：删除文件、反弹 shell、上传 WebShell、容器逃逸、
  爆破等动作触发时由 ApprovalBroker 即时请示用户——不要绕过或恢复自动放行
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
```

行为质量以真实任务浏览器实测为准——mock 不等于真实体验。
