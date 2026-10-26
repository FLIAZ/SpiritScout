"""Windows/Linux/macOS 通用 Docker 部署入口，只依赖 Python 标准库。"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import BinaryIO

ROOT = Path(__file__).resolve().parent


def init_environment(root: Path) -> bool:
    """只在首次部署创建凭据，绝不覆盖已有 .env。"""
    target = root / ".env"
    if target.exists():
        return False
    content = (root / ".env.example").read_text(encoding="utf-8-sig")
    content = content.replace("SPIRITSCOUT_ADMIN_PASSWORD=please-change-me",
                              "SPIRITSCOUT_ADMIN_PASSWORD=" + secrets.token_urlsafe(32))
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)
    return True


def compose_command(project: str = "", browser: bool = False) -> list[str]:
    command = ["docker", "compose", "--project-directory", str(ROOT)]
    if project:
        command += ["--project-name", project]
    if browser:
        command += ["--profile", "src-browser"]
    return command


def check_docker() -> None:
    if not shutil.which("docker"):
        raise RuntimeError("找不到 Docker；Windows/macOS 请安装 Docker Desktop，Linux 请安装 Docker Engine 与 Compose 插件")
    result = subprocess.run(["docker", "info", "--format", "{{.OSType}}"],
                            capture_output=True, text=True, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError("Docker 守护进程不可用，请启动 Docker 并确认当前用户有访问权限")
    if result.stdout.strip() != "linux":
        raise RuntimeError("需要 Linux 容器模式，请将 Docker Desktop 切换为 Linux containers")
    result = subprocess.run(["docker", "compose", "version", "--short"],
                            capture_output=True, text=True, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError("需要 Docker Compose v2 插件（docker compose）")
    version = result.stdout.strip().lstrip("v").split(".")
    if len(version) < 2 or tuple(int(part) for part in version[:2]) < (2, 20):
        raise RuntimeError("需要 Docker Compose 2.20+，请升级 Docker Desktop 或 Compose 插件")


def restore_stream(stream: BinaryIO, data: Path, configs: Path) -> None:
    """将停服后的备份导入空数据卷；拒绝路径逃逸、链接和覆盖现有工作区。"""
    if data.exists() and any(data.iterdir()):
        raise RuntimeError("目标数据卷非空，拒绝覆盖；请使用新的 Compose project name")
    roots = {"data": data.resolve(), "configs": configs.resolve()}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            parts = member.name.split("/")
            if parts[0] not in roots or any(part in {"..", ""} or ":" in part or "\\" in part for part in parts):
                raise RuntimeError("迁移归档包含无效路径")
            root = roots[parts[0]]
            target = root.joinpath(*parts[1:]).resolve()
            if not target.is_relative_to(root) or not (member.isdir() or member.isfile()):
                raise RuntimeError("迁移归档包含不安全路径或链接")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                target.chmod(0o600)


def migrate(command: list[str]) -> int:
    source = ROOT / ".spiritscout"
    if not source.is_dir():
        raise RuntimeError("项目内没有旧版 .spiritscout 目录，无需迁移")
    result = subprocess.run(command + ["ps", "--status", "running", "-q", "spiritscout"],
                            capture_output=True, text=True, check=True)
    if result.stdout.strip():
        raise RuntimeError("请先停止旧服务（docker compose down），停止原生实例后再迁移，避免复制正在写入的 SQLite 数据库")
    args = command + ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
                      "spiritscout", "/app/deploy.py", "_restore"]
    with subprocess.Popen(args, stdin=subprocess.PIPE) as process:
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                def regular_only(member):
                    if not (member.isdir() or member.isfile()):
                        raise RuntimeError("旧数据包含链接，请先将链接替换为实际文件再迁移")
                    return member
                archive.add(source, arcname="data", filter=regular_only)
                archive.add(ROOT / "configs", arcname="configs", filter=regular_only)
            process.stdin.close()
            return process.wait()
        except BaseException:
            process.stdin.close()
            process.terminate()
            process.wait()
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="up",
                        choices=["init", "doctor", "up", "down", "logs", "status", "migrate", "_restore"])
    parser.add_argument("--browser", action="store_true", help="同时启动 SRC 人工接管浏览器")
    parser.add_argument("--no-build", action="store_true", help="使用已构建的镜像")
    parser.add_argument("--project-name", default="", help="隔离另一套部署的容器、卷和网络")
    args = parser.parse_args(argv)
    try:
        if args.action == "_restore":
            restore_stream(sys.stdin.buffer, Path("/app/.spiritscout"), Path("/app/configs"))
            print("旧版数据与配置已导入；原目录未改动")
            return 0
        if args.action in {"init", "up"}:
            if init_environment(ROOT):
                print("已创建 .env 并生成随机管理员密码；请在项目 .env 中查看凭据")
            if args.action == "init":
                return 0
        check_docker()
        if args.action == "doctor":
            print("Docker Linux 容器模式与 Compose 版本检查通过")
            return 0
        command = compose_command(args.project_name, args.browser)
        if args.action == "migrate":
            return migrate(command)
        actions = {"up": ["up", "-d", "--wait", *([] if args.no_build else ["--build"])],
                   "down": ["--profile", "src-browser", "down"],
                   "logs": ["logs", "-f", "--tail", "100"], "status": ["ps"]}
        return subprocess.run(command + actions[args.action], cwd=ROOT, check=False).returncode
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[部署失败] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
