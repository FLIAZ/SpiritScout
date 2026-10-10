"""黑板架构（设计书 4.8）：8 层结构化共享状态 + 汇聚引擎。

- 黑板 = 全局共享知识库的逻辑分层视图（SQLite 强一致 + 版本号乐观锁）；
- 环形数据总线 = 黑板事件通道（写入校验通过后才发布事件，6.9）；
- 汇聚引擎 = 黑板写入的去重/冲突解决/版本控制网关；
- 写权限分级（4.8.3）：按子 Agent 类型限制黑板层写入范围。
"""

from spiritscout.blackboard.convergence import ConvergenceEngine
from spiritscout.blackboard.layers import LAYER_WRITE_PERMISSIONS, can_write
from spiritscout.blackboard.store import Blackboard

__all__ = ["LAYER_WRITE_PERMISSIONS", "Blackboard", "ConvergenceEngine", "can_write"]
