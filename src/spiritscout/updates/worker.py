"""独立更新进程；只执行控制面保存的计划，日志不经网页返回。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from spiritscout.updates.controller import read_json, release_claim, write_json
from spiritscout.updates.source import (
    GIT_URL,
    git,
    installer_command,
    process_alive,
    source_preflight,
)


def command(args, *, cwd=None, env=None, timeout=1200, capture=False):
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=True,
        timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )
    return result.stdout or ""


def phase(plan, state, message):
    path = Path(plan["root"]) / "job.json"
    value = read_json(path)
    if value.get("id") != plan["id"]:
        raise ValueError("更新计划已被替换，停止执行")
    value.update(
        phase=state, message=message, worker_pid=os.getpid(), updated_at=time.time()
    )
    write_json(path, value)


def worker_env(plan):
    env = dict(
        os.environ,
        SPIRITSCOUT_UPDATE_STATE=plan["root"],
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        env.pop(key, None)
    return env


def prepare_python(plan, folder):
    stage = folder / "source"
    stage.mkdir()
    command(["git", "init", "--quiet", str(stage)])
    git(stage, "fetch", "--depth=1", "--no-tags", GIT_URL, plan["release"]["commit"])
    git(stage, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    if git(stage, "rev-parse", "HEAD") != plan["release"]["commit"]:
        raise ValueError("下载提交与发布版本不一致")
    if plan["installation"]["kind"] == "source":
        root = Path(plan["installation"]["root"])
        git(root, "fetch", "--no-tags", GIT_URL, plan["release"]["commit"])
        source_preflight(
            root,
            plan["installation"]["revision"],
            plan["release"]["commit"],
            plan["protected"],
        )
    env_path = Path(plan["root"]) / "envs" / plan["id"]
    env = worker_env(plan)
    uv = plan.get("uv")
    if uv:
        command(
            [
                uv,
                "venv",
                "--python",
                plan["python"],
                "--no-python-downloads",
                str(env_path),
            ],
            env=env,
        )
    else:
        command([plan["python"], "-m", "venv", str(env_path)], env=env)
    python = str(
        env_path / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    extra = "[" + ",".join(plan["extras"]) + "]" if plan["extras"] else ""
    if uv and (stage / "uv.lock").is_file():
        lock_env = dict(env, UV_PROJECT_ENVIRONMENT=str(env_path))
        args = [
            uv,
            "sync",
            "--frozen",
            "--no-dev",
            "--project",
            str(stage),
            "--python",
            plan["python"],
        ]
        for name in plan["extras"]:
            args += ["--extra", name]
        command(args, env=lock_env)
        if plan["installation"]["kind"] == "package":
            # 包安装不依赖随后可能清理的源码目录。
            command(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    python,
                    "--no-deps",
                    "--reinstall-package",
                    "spiritscout",
                    str(stage),
                ],
                env=env,
            )
    elif plan["installation"]["kind"] == "source":
        command(installer_command(python, str(stage) + extra, uv=uv), env=env)
    else:
        args = (
            [uv, "pip", "install", "--python", python]
            if uv
            else [python, "-m", "pip", "install", "--disable-pip-version-check"]
        )
        command([*args, str(stage) + extra], env=env)
    actual = command(
        [python, "-c", "from spiritscout import __version__; print(__version__)"],
        cwd=folder,
        env=env,
        capture=True,
    ).strip()
    if actual != plan["release"]["tag"].removeprefix("v"):
        raise ValueError("候选环境版本与发布标签不一致")
    # 静态验证核心依赖可导入，避免修改 LiteLLM/MCP 的版本约束。
    command(
        [
            python,
            "-c",
            "import fastapi, uvicorn, litellm, mcp; from spiritscout.web import create_app",
        ],
        cwd=folder,
        env=env,
    )
    return python


def prepare_binary(plan, folder):
    asset = plan["asset"]
    digest = asset["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("发布附件缺少有效 SHA-256")
    path = folder / ("candidate.exe" if sys.platform == "win32" else "candidate")
    checksum, size = hashlib.sha256(), 0
    request = urllib.request.Request(
        asset["browser_download_url"], headers={"User-Agent": "SpiritScout-updater"}
    )
    with (
        urllib.request.urlopen(request, timeout=30) as response,
        path.open("xb") as output,
    ):
        final = urlparse(response.url)
        if final.scheme != "https" or not (
            final.hostname == "github.com"
            or (final.hostname or "").endswith(".githubusercontent.com")
        ):
            raise ValueError("发布附件重定向到未知来源")
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 512 * 1024 * 1024:
                raise ValueError("发布附件超过 512 MiB")
            checksum.update(chunk)
            output.write(chunk)
    if checksum.hexdigest() != digest.split(":")[1] or size != asset["size"]:
        raise ValueError("发布附件大小或校验值不符")
    path.chmod(0o700)
    actual = command([str(path), "--version"], cwd=folder, capture=True, timeout=60)
    if actual.strip().split()[-1].removeprefix("v") != plan["release"][
        "tag"
    ].removeprefix("v"):
        raise ValueError("独立程序版本与发布不符")
    return str(path)


def health_url(plan):
    host = plan["launch"]["host"]
    host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{plan['launch']['port']}/api/updates/health"


def stop_parent(plan):
    pid = plan["parent_pid"]
    # 用本轮令牌确认端口上的服务仍是原进程，避免 PID 重用时误杀其他程序。
    request = urllib.request.Request(
        health_url(plan),
        headers={"X-SpiritScout-Update": plan["token"]},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        value = json.load(response)
    if (
        value.get("pid") != pid
        or value.get("id") != plan["id"]
        or not value.get("idle")
    ):
        raise ValueError("原服务身份或空闲状态已变化，停止更新")
    os.kill(pid, signal.SIGTERM)
    for _ in range(300):
        if not process_alive(pid):
            return
        # Linux 的孤儿/僵尸不再持有端口，不能等待其父 shell 回收。
        try:
            if sys.platform.startswith("linux") and Path(
                f"/proc/{pid}/stat"
            ).read_text().split(") ", 1)[1].startswith("Z"):
                return
        except FileNotFoundError:
            return
        time.sleep(0.1)
    raise ValueError("服务未能正常退出；没有强制终止，请手动处理")


def backup(plan, folder):
    target = folder / "backup"
    target.mkdir()
    workspace = Path(plan["workspace"])
    if workspace.exists():
        shutil.copytree(workspace, target / "workspace", symlinks=True)
    for index, item in enumerate(plan["protected"]):
        path = Path(item)
        if path.is_file() and not path.is_symlink():
            shutil.copy2(path, target / f"config-{index}")
    write_json(
        target / "manifest.json",
        {
            "workspace": plan["workspace"],
            "paths": plan["protected"],
            "previous": plan["installation"],
            "python": plan["python"],
        },
    )


def spawn_service(plan, executable, output):
    frozen = plan["installation"]["kind"] == "binary"
    args = [
        executable,
        *([] if frozen else ["-m", "spiritscout"]),
        *plan["launch"]["args"],
    ]
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    return subprocess.Popen(
        args,
        cwd=plan["launch"]["cwd"],
        env=worker_env(plan),
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=output,
        **options,
    )


def healthy(plan, process, *, timeout=120):
    url = health_url(plan)
    request = urllib.request.Request(
        url, headers={"X-SpiritScout-Update": plan["token"]}
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                value = json.load(response)
            if (
                value.get("id") == plan["id"]
                and (
                    plan["installation"]["kind"] == "binary"
                    or value.get("pid") == process.pid
                )
                and value.get("version") == plan["release"]["tag"].removeprefix("v")
            ):
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def run(plan_path: Path) -> int:
    plan = read_json(plan_path)
    folder = plan_path.parent
    stopped = switched = spawned = False
    old_active = read_json(Path(plan["root"]) / "active.json")
    try:
        phase(plan, "preparing", "正在下载并验证候选版本，当前服务保持运行")
        workspace, update_root = (
            Path(plan["workspace"]).resolve(),
            Path(plan["root"]).resolve(),
        )
        if workspace == update_root or workspace in update_root.parents:
            raise ValueError("工作区包含更新目录，无法建立独立备份，请手动更新")
        executable = (
            prepare_binary(plan, folder)
            if plan["installation"]["kind"] == "binary"
            else prepare_python(plan, folder)
        )
        phase(plan, "backup", "候选环境就绪，正在停止空闲服务并备份数据")
        # 前端进入维护态后不接受新任务，父进程再确认所有异步作业已结束。
        stop_parent(plan)
        stopped = True
        backup(plan, folder)
        phase(plan, "switching", "正在切换版本，配置和工作区保持原路径")
        if plan["installation"]["kind"] == "source":
            root = Path(plan["installation"]["root"])
            source_preflight(
                root,
                plan["installation"]["revision"],
                plan["release"]["commit"],
                plan["protected"],
            )
            git(root, "merge", "--ff-only", "--no-edit", plan["release"]["commit"])
            switched = True
            args = installer_command(executable, str(root), uv=plan.get("uv"))
            command([*args, "--no-deps"], env=worker_env(plan))
        elif plan["installation"]["kind"] == "binary":
            destination = Path(plan["python"])
            shutil.copy2(destination, folder / "previous-binary")
            temporary = destination.with_name(
                destination.name + ".update-" + plan["id"]
            )
            shutil.copy2(executable, temporary)
            os.replace(temporary, destination)
            executable = str(destination)
            switched = True
        if plan["installation"]["kind"] != "binary":
            write_json(
                Path(plan["root"]) / "active.json",
                {"python": executable, "version": plan["release"]["tag"]},
            )
        phase(plan, "restarting", "正在启动新版本并检查服务")
        with open(folder / "service.log", "ab") as output:
            process = spawn_service(plan, executable, output)
        spawned = True
        if not healthy(plan, process):
            # 新版可能已经执行数据迁移，不盲目用旧代码打开新版数据库。
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            phase(
                plan,
                "recovery_required",
                "新版启动检查失败，已保留备份；请查看更新目录中的日志和恢复说明",
            )
            return 1
        phase(plan, "completed", "更新完成，新版本已启动")
        return 0
    except Exception as exc:
        print(f"更新失败：{type(exc).__name__}: {exc}", flush=True)
        if stopped and not spawned:
            try:
                if switched and plan["installation"]["kind"] == "source":
                    root = Path(plan["installation"]["root"])
                    # 只有仍是本次目标且无其他修改才允许回退，绝不使用 --hard。
                    if git(root, "rev-parse", "HEAD") != plan["release"][
                        "commit"
                    ] or git(root, "status", "--porcelain", "--untracked-files=normal"):
                        raise ValueError("源码已由其他进程修改，不能自动回退")
                    git(root, "reset", "--keep", plan["installation"]["revision"])
                if switched and plan["installation"]["kind"] == "binary":
                    shutil.copy2(folder / "previous-binary", plan["python"])
                active = Path(plan["root"]) / "active.json"
                if old_active:
                    write_json(active, old_active)
                else:
                    active.unlink(missing_ok=True)
                phase(plan, "error", "更新失败，已恢复原程序和环境；详情见本机更新日志")
                with open(folder / "service.log", "ab") as output:
                    spawn_service(plan, plan["python"], output)
            except Exception as recovery:
                print(f"恢复失败：{recovery}", flush=True)
                phase(
                    plan,
                    "recovery_required",
                    "更新未完成，请根据备份和本机日志手动恢复",
                )
        else:
            phase(
                plan,
                "error" if not spawned else "recovery_required",
                "更新准备失败，当前版本保持不变；详情见本机更新日志"
                if not stopped
                else "更新未完成，请检查本机日志和备份",
            )
        return 1
    finally:
        release_claim(Path(plan["root"]), plan["id"])


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1])))
