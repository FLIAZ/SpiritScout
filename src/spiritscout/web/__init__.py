"""灵枢 Web 控制台：FastAPI + SSE + 无构建单页前端。

- 会话隔离：SessionManager 为每个会话建独立工作区与配置副本；
- 实时进度：SSE 跟随会话审计 JSONL（断线 Last-Event-ID 续传）；
- 合规不破：创建会话经 launch.prepare_launch 装配 + authorized/范围门（G1）。
"""

from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session, SessionManager

__all__ = ["Session", "SessionManager", "create_app"]
