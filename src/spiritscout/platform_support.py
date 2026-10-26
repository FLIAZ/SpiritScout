"""原生平台的进程与 Node.js 入口；不借助 shell 解释用户参数。"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


def process_options() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(process: Any) -> None:
    """Unix 清理独立进程组；Windows 使用系统任务树终止，失败再终止父进程。"""
    if sys.platform == "win32":
        if process.returncode is not None:
            return
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with contextlib.suppress(ProcessLookupError):
        process.kill()


def node_cli_command(name: str) -> list[str]:
    """Windows 的 npm/npx 是 .cmd；直接执行其 JS 入口，避免 cmd.exe 引号注入。"""
    executable = shutil.which(name)
    if not executable:
        raise ValueError(f"找不到 {name}，请安装 Node.js 22+")
    if sys.platform == "win32" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        cli = Path(executable).parent / "node_modules" / "npm" / "bin" / f"{name}-cli.js"
        node = shutil.which("node")
        if node and cli.is_file():
            return [node, str(cli)]
        raise ValueError(f"找不到 {name} 的 Node.js CLI，请修复 Node.js 安装或使用 Docker")
    return [executable]


def chrome_executable(env: dict[str, str]) -> str:
    """显式路径优先，再找 PATH 和 Windows/macOS 的标准安装位置。"""
    if env.get("SPIRITSCOUT_CHROME_EXECUTABLE"):
        return str(Path(env["SPIRITSCOUT_CHROME_EXECUTABLE"]).expanduser())
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if found := shutil.which(name):
            return found
    candidates: list[Path] = []
    if sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            candidates += [root / "Google Chrome.app/Contents/MacOS/Google Chrome",
                           root / "Chromium.app/Contents/MacOS/Chromium"]
    elif sys.platform == "win32":
        for key, fallback in (("PROGRAMFILES", "C:/Program Files"),
                              ("PROGRAMFILES(X86)", "C:/Program Files (x86)"),
                              ("LOCALAPPDATA", str(Path.home() / "AppData/Local"))):
            candidates.append(Path(env.get(key) or fallback) / "Google/Chrome/Application/chrome.exe")
    return next((str(path) for path in candidates if path.is_file()), "")
