#!/usr/bin/env python3
"""Docker Compose 宿主更新助手；不在容器中运行，不接受网页下发命令/路径。"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

# 助手在已有源码仓库运行，只依赖 Python 标准库和宿主 Docker/Git。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spiritscout.updates.source import (  # noqa: E402
    GIT_URL,
    git,
    official_remote,
    source_preflight,
)

BACKUP = """import json,os,shutil,sys
from pathlib import Path
os.umask(0o077)
workspace=Path(sys.argv[1]);job=sys.argv[2];paths=json.loads(sys.argv[3])
backup=workspace/'.spiritscout-updates'/'jobs'/job/'backup'
backup.mkdir(parents=True,exist_ok=False)
def ignore(path,names):return ['.spiritscout-updates'] if Path(path)==workspace else []
shutil.copytree(workspace,backup/'workspace',symlinks=True,ignore=ignore)
for n,name in enumerate(paths):
 p=Path(name)
 if p.is_file() and not p.is_symlink():shutil.copy2(p,backup/('config-'+str(n)))
(backup/'manifest.json').write_text(json.dumps(dict(workspace=str(workspace),paths=paths)))
"""


class HostUpdater:
    def __init__(self, args):
        self.args, self.root = args, Path(args.project_dir).resolve()
        self.bridge_id = uuid.uuid4().hex
        self.stop = threading.Event()
        self.current = None
        self.output = None
        self.ready, self.reason = False, "正在检查宿主部署"
        self.auth = ""
        username, password = (
            os.environ.get("SPIRITSCOUT_UPDATE_USERNAME", ""),
            os.environ.get("SPIRITSCOUT_UPDATE_PASSWORD", ""),
        )
        if username and password:
            self.auth = (
                "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
            )
        self.compose = ["docker", "compose", "--project-directory", str(self.root)]
        for path in args.compose_file or ["docker-compose.yml"]:
            self.compose += ["-f", str((self.root / path).resolve())]

    def api(self, path, value=None, *, token=None):
        headers = {
            "Content-Type": "application/json",
            "X-SpiritScout-Bridge": self.bridge_id,
        }
        if self.auth:
            headers["Authorization"] = self.auth
        if token:
            headers["X-SpiritScout-Update"] = token
        request = urllib.request.Request(
            self.args.url.rstrip("/") + path,
            data=json.dumps(value).encode() if value is not None else None,
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def command(self, args, *, capture=False, timeout=1200):
        result = subprocess.run(
            args,
            cwd=self.root,
            check=True,
            timeout=timeout,
            stdout=subprocess.PIPE if capture else self.output,
            stderr=subprocess.PIPE if capture else self.output,
            text=True,
        )
        return result.stdout.strip() if capture else ""

    def inspect(self):
        config = json.loads(
            self.command([*self.compose, "config", "--format", "json"], capture=True)
        )
        definition = config.get("services", {}).get(self.args.service, {}).get("build")
        context = (
            definition.get("context") if isinstance(definition, dict) else definition
        )
        if not context or (self.root / context).resolve() != self.root:
            raise ValueError(
                "需要以当前仓库为构建上下文的 Compose 服务；纯镜像拉取部署请通过原部署工具更新"
            )
        cid = self.command([*self.compose, "ps", "-q", self.args.service], capture=True)
        if not cid or "\n" in cid:
            raise ValueError("需要恰好一个运行中的目标服务容器")
        info = json.loads(self.command(["docker", "inspect", cid], capture=True))[0]
        for mount in info.get("Mounts", []):
            source = Path(mount.get("Source", "/nonexistent")).resolve()
            if (
                source in {self.root, self.root / "src"}
                or self.root / "src" in source.parents
            ):
                raise ValueError("开发容器挂载了实时源码，请先改用镜像部署后再更新")
        return cid, info

    def check(self):
        if not official_remote(git(self.root, "remote", "get-url", "origin")):
            raise ValueError("宿主 origin 不是官方仓库")
        if git(self.root, "status", "--porcelain", "--untracked-files=normal"):
            raise ValueError("宿主源码有本地改动，请先自行整理后更新")
        git(self.root, "symbolic-ref", "--quiet", "--short", "HEAD")
        self.inspect()

    def heartbeat(self):
        while not self.stop.is_set():
            try:
                self.api(
                    "/api/updates/host/heartbeat",
                    {
                        "bridge_id": self.bridge_id,
                        "ready": self.ready,
                        "reason": self.reason,
                    },
                )
            except (OSError, ValueError):
                pass
            self.stop.wait(5)

    def report(self, job, phase, message):
        return self.api(
            "/api/updates/host/report",
            {"id": job["id"], "phase": phase, "message": message},
        )

    def wait_health(self, job, *, target=True):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                value = self.api("/api/updates/health", token=job["token"])
                if value.get("id") == job["id"] and (
                    not target
                    or value.get("version") == job["target"].removeprefix("v")
                ):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise ValueError("容器服务健康检查超时")

    def perform(self, job):
        old = git(self.root, "rev-parse", "HEAD")
        switched = stopped = recreated = False
        cid = None
        try:
            self.check()
            cid, info = self.inspect()
            workspace = Path(job["workspace"])
            if not any(
                workspace == Path(m["Destination"])
                or Path(m["Destination"]) in workspace.parents
                for m in info.get("Mounts", [])
            ):
                raise ValueError("会话工作区没有持久化挂载，拒绝重建容器")
            target = job["release"]["commit"]
            git(self.root, "fetch", "--no-tags", GIT_URL, target)
            protected = [
                str(self.root / p)
                for p in [
                    ".env",
                    ".spiritscout",
                    "configs/system.yaml",
                    "configs/models.yaml",
                    "configs/agent_models.yaml",
                ]
            ]
            source_preflight(self.root, old, target, protected)
            self.report(
                job, "preparing", "宿主正在同步源码并构建候选镜像，当前容器保持运行"
            )
            git(self.root, "merge", "--ff-only", "--no-edit", target)
            switched = True
            self.command([*self.compose, "build", self.args.service])
            self.report(job, "backup", "镜像构建成功，正在停止空闲容器并备份持久化数据")
            health = self.api("/api/updates/health", token=job["token"])
            if not health.get("idle"):
                raise ValueError("容器仍有活动操作，停止更新")
            self.command([*self.compose, "stop", "--timeout", "30", self.args.service])
            stopped = True
            self.command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--volumes-from",
                    cid,
                    "--entrypoint",
                    "python",
                    info["Image"],
                    "-c",
                    BACKUP,
                    str(workspace),
                    job["id"],
                    json.dumps(job.get("protected", [])),
                ]
            )
            # 此时服务离线，通过持久化工作区写入下一阶段，重建后再恢复 API 通信。
            recreated = True
            self.command(
                [
                    *self.compose,
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    self.args.service,
                ]
            )
            self.wait_health(job)
            self.report(job, "completed", "镜像已更新，容器健康检查通过")
        except Exception as exc:
            print(
                f"容器更新失败：{type(exc).__name__}: {exc}",
                file=self.output or sys.stdout,
                flush=True,
            )
            if not recreated:
                if (
                    switched
                    and git(self.root, "rev-parse", "HEAD") == job["release"]["commit"]
                    and not git(
                        self.root, "status", "--porcelain", "--untracked-files=normal"
                    )
                ):
                    git(self.root, "reset", "--keep", old)
                if stopped and cid:
                    self.command(["docker", "start", cid])
                    self.wait_health(job, target=False)
                self.report(
                    job, "error", "宿主更新失败，原容器保持运行；请查看宿主更新日志"
                )
            else:
                self.report(
                    job,
                    "recovery_required",
                    "新容器验证失败，请检查宿主日志和持久化工作区备份",
                )
            return False
        return True

    def run(self):
        threading.Thread(target=self.heartbeat, daemon=True).start()
        print(
            "宿主更新助手已启动；只有在网页点击更新后才执行。Ctrl+C 退出。", flush=True
        )
        handled = set()
        try:
            while True:
                try:
                    if not self.current:
                        try:
                            self.check()
                            self.ready, self.reason = True, ""
                        except (OSError, ValueError, subprocess.SubprocessError) as exc:
                            self.ready, self.reason = False, str(exc)[:300]
                    job = self.api("/api/updates/host/job")
                    if job and job["id"] not in handled:
                        handled.add(job["id"])
                        self.current = job["id"]
                        self.ready = False
                        folder = self.root / ".spiritscout-updates"
                        folder.mkdir(mode=0o700, exist_ok=True)
                        with open(
                            folder / f"host-{job['id']}.log",
                            "a",
                            opener=lambda p, f: os.open(p, f, 0o600),
                        ) as log:
                            log.write("开始更新 " + job["target"] + "\n")
                            log.flush()
                            self.output = log
                            try:
                                self.perform(job)
                            finally:
                                self.output = None
                        self.current = None
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    print(f"等待控制台连接：{type(exc).__name__}", flush=True)
                time.sleep(3)
        except KeyboardInterrupt:
            self.stop.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--project-dir", default=str(Path(__file__).resolve().parents[1])
    )
    parser.add_argument("--service", default="spiritscout")
    parser.add_argument("--compose-file", action="append")
    args = parser.parse_args()
    from urllib.parse import urlparse

    url = urlparse(args.url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
    ):
        parser.error("控制台地址必须是 HTTP(S)，凭据通过环境变量提供")
    if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("远程控制台必须使用 HTTPS")
    HostUpdater(args).run()


if __name__ == "__main__":
    main()
