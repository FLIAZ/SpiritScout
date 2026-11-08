"""只读安装识别与 Git 快进检查；不接受网页传入仓库地址或命令。"""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

REPOSITORY = "FLIAZ/SpiritScout"
GIT_URL = f"https://github.com/{REPOSITORY}.git"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ValueError("Git 检查失败，请在本机检查仓库或网络")
    return result.stdout if "-z" in args else result.stdout.rstrip("\n")


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return (
                bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                and code.value == 259
            )
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def official_remote(value: str) -> bool:
    value = value.strip().removesuffix(".git").rstrip("/").lower()
    return value in {
        f"https://github.com/{REPOSITORY.lower()}",
        f"git@github.com:{REPOSITORY.lower()}",
        f"ssh://git@github.com/{REPOSITORY.lower()}",
    }


def containerized() -> bool:
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        return bool(
            re.search(r"docker|kubepods|containerd", Path("/proc/1/cgroup").read_text())
        )
    except OSError:
        return False


def installation() -> dict:
    # 冻结/容器必须优先于旁边是否存在 .git，防止修改挂载的宿主源码。
    if containerized():
        return {
            "kind": "container",
            "label": "Docker / 容器",
            "supported": False,
            "reason": "容器镜像需由宿主部署工具更新；本控制台不申请 Docker socket 权限。",
        }
    if os.environ.get("APPIMAGE") or os.environ.get("SPIRITSCOUT_PACKAGE_FORMAT") == "appimage":
        return {
            "kind": "appimage",
            "label": "AppImage",
            "supported": False,
            "reason": "AppImage 应用内替换尚未接入，请从原发布渠道更新。",
        }
    package_format = os.environ.get("SPIRITSCOUT_PACKAGE_FORMAT")
    if package_format in {"deb", "rpm", "arch"}:
        label = {"deb": "deb / apt", "rpm": "rpm", "arch": "Arch / pacman"}[
            package_format
        ]
        return {
            "kind": "system_package",
            "label": label,
            "supported": False,
            "reason": "由系统包管理器安装，请使用原包管理器更新。",
        }
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        onefile = Path(getattr(sys, "_MEIPASS", "")).name.startswith("_MEI")
        return {
            "kind": "binary",
            "label": "独立程序（ELF / EXE）",
            "supported": onefile,
            "reason": ""
            if onefile
            else "当前编译包不是已支持的 PyInstaller 单文件格式，请通过原发布渠道更新。",
            "root": str(Path(sys.executable).resolve().parent),
        }
    module = Path(__file__).resolve()
    root = module.parents[3]
    if (root / "src/spiritscout/updates/source.py").resolve() == module and (
        root / "pyproject.toml"
    ).is_file():
        result = {
            "kind": "source",
            "label": "Git 源码",
            "root": str(root),
            "supported": True,
        }
        try:
            if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
                raise ValueError("不是独立 Git 仓库")
            if not official_remote(git(root, "remote", "get-url", "origin")):
                raise ValueError("origin 不是官方仓库，避免覆盖分叉项目，请手动更新")
            result["revision"] = git(root, "rev-parse", "HEAD")
            if git(root, "status", "--porcelain", "--untracked-files=normal"):
                raise ValueError(
                    "源码目录有本地改动或未跟踪文件，请先自行提交或整理后更新"
                )
            if not git(root, "symbolic-ref", "--quiet", "--short", "HEAD"):
                raise ValueError("仓库处于分离 HEAD 状态，请先切回分支")
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            result.update(supported=False, reason=str(exc))
        return result
    installer = "pip"
    for name, args, label in (
        ("dpkg-query", ["-S"], "deb / apt"),
        ("rpm", ["-qf"], "rpm"),
        ("pacman", ["-Qo"], "Arch / pacman"),
    ):
        if executable := shutil.which(name):
            try:
                found = subprocess.run(
                    [executable, *args, str(module)], capture_output=True, timeout=5
                )
                if found.returncode == 0:
                    return {
                        "kind": "system_package",
                        "label": label,
                        "supported": False,
                        "reason": "由系统包管理器安装，请使用原包管理器更新，避免破坏文件归属。",
                    }
            except (OSError, subprocess.TimeoutExpired):
                pass
    try:
        installer = (
            importlib.metadata.distribution("spiritscout")
            .read_text("INSTALLER")
            .strip()
            or "pip"
        )
    except (importlib.metadata.PackageNotFoundError, AttributeError):
        pass
    return {
        "kind": "package",
        "label": f"Python 包（{installer}）",
        "supported": True,
        "installer": installer,
    }


def source_preflight(root: Path, old: str, target: str, protected: list[str]) -> None:
    if git(root, "rev-parse", "HEAD") != old or git(
        root, "status", "--porcelain", "--untracked-files=normal"
    ):
        raise ValueError("更新期间源码发生变化，已停止，未覆盖本地改动")
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise ValueError("目标提交无效")
    git(root, "merge-base", "--is-ancestor", old, target)
    changed = git(root, "diff", "--name-only", "-z", old, target).split("\0")
    for name in changed:
        if not name:
            continue
        candidate = (root / name).resolve()
        if name == ".spiritscout-updates" or name.startswith(".spiritscout-updates/"):
            raise ValueError("目标版本占用了本机更新目录")
        for value in protected:
            path = Path(value).resolve()
            if (
                candidate == path
                or path in candidate.parents
                or candidate in path.parents
            ):
                raise ValueError(f"更新会修改使用中的配置/数据路径：{name}，请手动迁移")
    # Git 可能覆盖被忽略的未跟踪文件，因此单独检查碰撞（也检查目录/文件互换）。
    ignored = git(
        root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    ).split("\0")
    target_files = set(
        git(root, "ls-tree", "-r", "--name-only", "-z", target).split("\0")
    )
    prefixes = {
        str(parent)
        for name in target_files
        if name
        for parent in Path(name).parents
        if str(parent) != "."
    }
    for name in ignored:
        if name and (
            name in target_files
            or name in prefixes
            or any(str(p) in target_files for p in Path(name).parents)
        ):
            raise ValueError(f"目标版本会覆盖本地忽略文件：{name}")


def binary_asset(
    release: dict, platform: str = sys.platform, machine: str | None = None
) -> dict | None:
    import platform as platform_module

    machine = (machine or platform_module.machine()).lower()
    arch = (
        "arm64"
        if machine in {"arm64", "aarch64"}
        else "x86_64"
        if machine in {"x86_64", "amd64"}
        else machine
    )
    system = (
        "windows"
        if platform == "win32"
        else "linux"
        if platform.startswith("linux")
        else "macos"
    )
    # 发布约定：平台和架构必须明确，不能猜测一个没有架构信息的二进制。
    expected = f"spiritscout-{system}-{arch}" + (".exe" if platform == "win32" else "")
    assets = release.get("assets", [])
    matches = [a for a in assets if a.get("name", "").lower() == expected]
    if len(matches) != 1 or not str(matches[0].get("digest", "")).startswith("sha256:"):
        return None
    value = matches[0]
    url = urlparse(value.get("browser_download_url", ""))
    if (
        url.scheme != "https"
        or url.hostname != "github.com"
        or not url.path.startswith(f"/{REPOSITORY}/releases/download/")
    ):
        return None
    return value


def installer_command(python: str, package: str, *, uv: str | None = None) -> list[str]:
    if uv:
        return [uv, "pip", "install", "--python", python, "--editable", package]
    return [
        python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--editable",
        package,
    ]


def enabled_extras(root: Path | None) -> list[str]:
    """保留已完整安装的可选功能组；不安装原本未启用的大型工具链。"""
    if root is None:
        try:
            dist = importlib.metadata.distribution("spiritscout")
            extras = dist.metadata.get_all("Provides-Extra") or []
            requires = dist.requires or []
        except importlib.metadata.PackageNotFoundError:
            return []
    else:
        import tomllib

        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        groups = project.get("optional-dependencies", {})
        extras = list(groups)
        requires = []
        for key, values in groups.items():
            for requirement in values:
                name, _, marker = requirement.partition(";")
                requires.append(
                    f'{name}; ({marker}) and extra == "{key}"'
                    if marker
                    else f'{name}; extra == "{key}"'
                )
    from packaging.requirements import Requirement

    selected = []
    for extra in extras:
        rows = [Requirement(r) for r in requires]
        rows = [r for r in rows if r.marker and r.marker.evaluate({"extra": extra})]
        if not rows:
            continue
        try:
            if all(importlib.metadata.version(r.name) in r.specifier for r in rows):
                selected.append(extra)
        except importlib.metadata.PackageNotFoundError:
            pass
    return selected
