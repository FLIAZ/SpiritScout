"""轻量启动面板及真实监听就绪提示，不依赖终端 UI 库。"""
from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Any, TextIO

from spiritscout import __version__

_WORDMARK = (
    '   ____ ___   ____ ___   ____ ______ ____ ____ ____   __  __ ______',
    '  / __// _ \\ /  _// _ \\ /  _//_  __// __// __// __ \\ / / / //_  __/',
    ' _\\ \\ / ___/_/ / / , _/_/ /   / /  _\\ \\ / /__/ /_/ // /_/ /  / /',
    '/___//_/   /___//_/|_//___/  /_/  /___/ \\___/\\____/ \\____/  /_/',
)


class StartupDisplay:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.started_at = time.perf_counter()
        self.terminal = self.stream.isatty()
        self.color = (self.terminal and "NO_COLOR" not in os.environ
                      and os.environ.get("TERM") != "dumb"
                      and (os.name != "nt" or "WT_SESSION" in os.environ
                           or "ANSICON" in os.environ))
        self._brand_color = ("38;2;129;183;173"
                             if os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}
                             else "36")
        self._status_line = False

    def _paint(self, text: str, code: str = "36") -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _write(self, text: str) -> None:
        print(text, file=self.stream, flush=True)

    def start(self) -> None:
        self._write("")
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
        if (self.terminal and width >= max(map(len, _WORDMARK)) + 2
                and os.environ.get("TERM") != "dumb"):
            for line in _WORDMARK:
                self._write("  " + self._paint(line, self._brand_color))
            self._write("")
        else:
            self._write("  " + self._paint("SPIRITSCOUT", f"1;{self._brand_color}"))
        self._write("  灵枢 · 多智能体安全测试控制台")
        self._write("  " + self._paint(f"v{__version__}", "2"))
        self._write("")

    def _clear_status(self) -> None:
        if self._status_line:
            self.stream.write("\r\033[2K")
            self.stream.flush()
            self._status_line = False

    def phase(self, label: str) -> None:
        self._clear_status()
        text = f"  {self._paint('启动', '2')}  {label}…"
        if self.color:
            self.stream.write(text)
            self.stream.flush()
            self._status_line = True
        else:
            self._write(text)

    def ready(self, host: str, port: int, *, auth_enabled: bool, warming: bool = False) -> None:
        self._clear_status()
        elapsed = time.perf_counter() - self.started_at
        self._write(f"  {self._paint('就绪', '1;32')}  HTTP 服务已监听  "
                    + self._paint(f"{elapsed:.2f}s", "2"))
        local_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        address = f"[{local_host}]" if ":" in local_host else local_host
        self._write(f"  {self._paint('访问', '2')}  "
                    + self._paint(f"http://{address}:{port}", self._brand_color))
        if auth_enabled:
            access = "管理员登录已启用"
        elif host in {"127.0.0.1", "localhost", "::1"}:
            access = "仅本机访问 · 未配置管理员"
        else:
            access = "未配置管理员 · 当前绑定允许远程访问"
        self._write(f"  {self._paint('安全', '2')}  {access}")
        if warming:
            self._write(f"  {self._paint('后台', '2')}  "
                        "模型 SDK 正在预热，首次任务会等待预热完成")
        self._write("")
        self._write("  " + self._paint("Ctrl+C 安全停止控制台", "2"))
        self._write("")

    def failed(self, message: str) -> None:
        self._clear_status()
        print(f"  {self._paint('[错误]', '1;31')} {message}",
              file=sys.stderr, flush=True)

    def stopped(self) -> None:
        self._clear_status()
        self._write("  " + self._paint("控制台已停止", "2"))


def serve(app: Any, *, display: StartupDisplay, **kwargs: Any) -> None:
    import asyncio

    import uvicorn
    from uvicorn.server import STARTUP_FAILURE

    app.state._http_started = asyncio.Event()

    class ConsoleServer(uvicorn.Server):
        async def startup(self, sockets: Any = None) -> None:
            await super().startup(sockets=sockets)
            if self.started:
                port = self.servers[0].sockets[0].getsockname()[1]
                display.ready(self.config.host, port,
                              auth_enabled=app.state.auth_enabled,
                              warming=getattr(app.state, "model_warmup", None) is not None)
                app.state._http_started.set()

    server = ConsoleServer(uvicorn.Config(app, **kwargs))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    except (OSError, SystemExit) as exc:
        if not isinstance(exc, SystemExit) or exc.code:
            display.failed("HTTP 服务启动失败，请查看上方错误和端口占用情况")
        raise
    if not server.started:
        display.failed("HTTP 服务未完成启动")
        raise SystemExit(STARTUP_FAILURE)
    display.stopped()
