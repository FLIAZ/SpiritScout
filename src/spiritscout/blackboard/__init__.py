"""共享黑板：分层状态存储、写权限和结果汇总。"""

from spiritscout.blackboard.convergence import ConvergenceEngine
from spiritscout.blackboard.layers import LAYER_WRITE_PERMISSIONS, can_write
from spiritscout.blackboard.store import Blackboard

__all__ = ["LAYER_WRITE_PERMISSIONS", "Blackboard", "ConvergenceEngine", "can_write"]
