"""灵枢 Web 控制台：FastAPI + SSE + 无构建单页前端。

- 会话隔离：SessionManager 为每个会话建独立工作区与配置副本；
- 实时进度：SSE 跟随会话审计 JSONL（断线 Last-Event-ID 续传）；
- 授权声明：创建会话经 launch.prepare_launch 装配与确认，运行期边界交给模型上下文。
"""

from spiritscout.web.app import create_app
from spiritscout.web.sessions import Session, SessionManager

__all__ = ["Session", "SessionManager", "create_app"]
