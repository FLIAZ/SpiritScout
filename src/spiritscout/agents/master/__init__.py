"""主 Agent（元规划决策层，设计书 4.1）。

职责：全局目标拆解（ATT&CK 战术 DAG）、多轮迭代调度、策略更新、
漏洞终审、模式感知任务图谱裁剪。决策载体为 LangGraph 状态机。
"""

from spiritscout.agents.master.agent import MasterAgent, MasterContext
from spiritscout.agents.master.dag import TaskDAG

__all__ = ["MasterAgent", "MasterContext", "TaskDAG"]
