"""黑板 8 层定义与写权限分级（4.8.1 / 4.8.3）。

防止越权覆盖：每类子 Agent 只允许写入与其职责匹配的黑板层。
"""

from __future__ import annotations

from spiritscout.core.models import AgentType, BlackboardLayer

# 写入方/消费方对照设计书 4.8.1 表
LAYER_WRITE_PERMISSIONS: dict[AgentType, set[BlackboardLayer]] = {
    AgentType.MASTER: set(BlackboardLayer),  # 主 Agent 全权（焦点管理/终审）
    AgentType.BUSINESS_CONTEXT: {
        BlackboardLayer.ASSET,
        BlackboardLayer.TOPOLOGY,
        BlackboardLayer.TASK_STATE,
    },
    AgentType.RECON: {
        BlackboardLayer.ASSET,
        BlackboardLayer.RECON_CLUE,
        BlackboardLayer.TASK_STATE,
    },
    AgentType.PORT_SCAN: {
        BlackboardLayer.ASSET,
        BlackboardLayer.TASK_STATE,
    },
    AgentType.WEB_VULN: {
        BlackboardLayer.ASSET,
        BlackboardLayer.VULN,
        BlackboardLayer.RECON_CLUE,
        BlackboardLayer.TASK_STATE,
    },
    AgentType.HOST_VULN: {
        BlackboardLayer.VULN,
        BlackboardLayer.TASK_STATE,
    },
    AgentType.POST_EXPLOIT: {
        BlackboardLayer.CREDENTIAL,
        BlackboardLayer.TOPOLOGY,
        BlackboardLayer.VULN,
        BlackboardLayer.TASK_STATE,
    },
    AgentType.CTF_WEB: {BlackboardLayer.VULN, BlackboardLayer.TOPOLOGY,
                          BlackboardLayer.TASK_STATE},
    AgentType.CTF_PWN: {BlackboardLayer.VULN, BlackboardLayer.TOPOLOGY,
                          BlackboardLayer.TASK_STATE},
    AgentType.CTF_REVERSE: {BlackboardLayer.VULN, BlackboardLayer.TOPOLOGY,
                          BlackboardLayer.TASK_STATE},
    AgentType.CTF_CRYPTO: {BlackboardLayer.VULN, BlackboardLayer.TOPOLOGY,
                          BlackboardLayer.TASK_STATE},
    AgentType.CTF_MISC: {BlackboardLayer.VULN, BlackboardLayer.TOPOLOGY,
                          BlackboardLayer.TASK_STATE},
    AgentType.REPORT: set(),  # 报告 Agent 只读（4.6）
}


def can_write(agent_type: AgentType, layer: BlackboardLayer) -> bool:
    return layer in LAYER_WRITE_PERMISSIONS.get(agent_type, set())
