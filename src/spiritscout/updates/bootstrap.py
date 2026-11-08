"""在导入服务依赖之前，转入网页更新准备的独立 Python 环境。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def state_root() -> Path:
    explicit = os.environ.get("SPIRITSCOUT_UPDATE_STATE")
    if explicit:
        return Path(explicit).resolve()
    if getattr(sys, "frozen", False):
        if value := os.environ.get("SPIRITSCOUT_DATA_HOME"):
            return Path(value).expanduser().resolve() / ".spiritscout-updates"
        return Path(sys.executable).resolve().parent / ".spiritscout-updates"
    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file() and (root / "src/spiritscout").is_dir():
        return root / ".spiritscout-updates"
    # 普通包/uv tool 可以装在只读解释器或用户 site；状态不写入系统 Python。
    base = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        if sys.platform == "win32"
        else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    )
    identity = hashlib.sha256(os.path.abspath(sys.prefix).encode()).hexdigest()[:16]
    return base / "spiritscout/updates" / identity


def redirect() -> None:
    if getattr(sys, "frozen", False):
        return
    root = state_root()
    path = root / "active.json"
    if not path.exists():
        return
    try:
        value = json.loads(path.read_text())
        python = Path(os.path.abspath(value["python"]))
        # 只执行本更新目录下的环境，不接受任意路径/启动参数。
        python.absolute().relative_to((root / "envs").absolute())
        if not python.is_file() or python.name not in {"python", "python.exe"}:
            raise ValueError("更新环境不存在")
        if os.path.abspath(sys.executable) == str(python.absolute()):
            return
        env = dict(os.environ, SPIRITSCOUT_UPDATE_STATE=str(root))
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        os.execve(str(python), [str(python), "-m", "spiritscout", *sys.argv[1:]], env)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # 禁止静默启动旧依赖和新代码的混合环境。
        raise SystemExit(f"更新环境不可用，请检查 {path}：{exc}") from exc
