"""源码部署的可见 Chrome：自动启动专用配置档案，独立于 MCP 连接存活。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from spiritscout.core.exceptions import ConfigurationError
from spiritscout.platform_support import chrome_executable, process_options
from spiritscout.tools.run_lock import ExecutionRunLock


def desktop_environment(env: dict[str, str]) -> dict[str, str]:
    """终端服务未继承显示变量时，仅从当前用户桌面进程恢复显示连接。"""
    result = {**os.environ, **env}
    if sys.platform != "linux" or result.get("DISPLAY") or result.get("WAYLAND_DISPLAY"):
        return result
    keys = {"DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY",
            "DBUS_SESSION_BUS_ADDRESS"}
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            if process.joinpath("comm").read_text().strip() not in {
                    "plasmashell", "gnome-shell", "xfce4-session", "cinnamon", "sway", "weston"}:
                continue
            values = dict(item.split("=", 1) for item in
                          process.joinpath("environ").read_text().split("\0") if "=" in item)
            if values.get("DISPLAY") or values.get("WAYLAND_DISPLAY"):
                result.update({key: values[key] for key in keys if key in values})
                break
        except (OSError, ValueError):
            continue
    return result


def _ready(endpoint: str) -> bool:
    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            response = client.get(endpoint.rstrip("/") + "/json/version")
            return response.is_success and bool(response.json().get("webSocketDebuggerUrl"))
    except (httpx.HTTPError, ValueError):
        return False


def ensure_browser(endpoint: str, env: dict[str, str]) -> None:
    """本机 CDP 缺席时启动有界面的 Chrome；远程地址保留外部浏览器契约。"""
    if _ready(endpoint):
        return
    parsed = urlsplit(endpoint)
    if (parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.scheme != "http" or parsed.path not in {"", "/"}):
        raise ValueError("远程 Chrome CDP 不可达，请启动部署主机的可见浏览器或 src-browser 服务")
    desktop = desktop_environment(env)
    if sys.platform == "linux" and not (desktop.get("DISPLAY") or desktop.get("WAYLAND_DISPLAY")):
        raise ValueError("没有可用桌面，无法弹出 Chrome；Docker 请启用 src-browser 人工操作页")
    executable = chrome_executable(desktop)
    if not executable:
        raise ValueError("未安装 Chrome/Chromium，请安装浏览器或配置 SPIRITSCOUT_CHROME_EXECUTABLE")
    profile = Path(env.get("SPIRITSCOUT_CHROME_PROFILE_DIR") or
                   Path.cwd() / ".spiritscout" / "browser-profile").expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 同一配置档案只允许一个启动者；其他会话等待首个进程开放 CDP。
    lock = ExecutionRunLock(profile)
    deadline = time.monotonic() + 12
    while True:
        try:
            lock.acquire()
            break
        except ConfigurationError:
            if _ready(endpoint):
                return
            if time.monotonic() >= deadline:
                raise ValueError("Chrome 正在由另一会话启动，请稍后重试") from None
            time.sleep(0.1)
    try:
        if _ready(endpoint):
            return
        argv = [executable, f"--remote-debugging-port={parsed.port or 80}",
                "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check", "--start-minimized", "about:blank"]
        if desktop.get("WAYLAND_DISPLAY"):
            argv.insert(1, "--ozone-platform=wayland")
        # 断开标准管道和进程组：关闭会话/MCP 不关闭浏览器或清除登录态。
        process = subprocess.Popen(argv, env=desktop, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   **process_options())
        while time.monotonic() < deadline:
            if _ready(endpoint):
                return
            if process.poll() is not None:
                raise ValueError(f"可见 Chrome 启动失败（退出码 {process.returncode}），请检查桌面连接")
            time.sleep(0.1)
        raise ValueError("Chrome 已启动但 CDP 尚未就绪，请检查端口和浏览器配置")
    finally:
        lock.close()
