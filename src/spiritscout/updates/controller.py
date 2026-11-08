"""控制台更新控制面：固定官方源、缓存检查、安装能力和更新进程状态。"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

from spiritscout import __version__
from spiritscout.updates.bootstrap import state_root
from spiritscout.updates.source import (
    REPOSITORY,
    binary_asset,
    containerized,
    enabled_extras,
    installation,
    process_alive,
)

ACTIVE_PHASES = {"preparing", "backup", "switching", "restarting"}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with open(
        temp, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)
    ) as file:
        json.dump(value, file, ensure_ascii=False)
    os.replace(temp, path)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("状态文件必须是对象")
        return value
    except FileNotFoundError:
        return {}


def version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else None


def release_claim(root: Path, job_id: str) -> None:
    path = root / "claim"
    try:
        if path.read_text() == job_id:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass


class Updater:
    def __init__(self, cfg, manager):
        self.cfg, self.manager = cfg, manager
        self.root = state_root()
        if containerized():
            # 镜像重建后仍能取得更新回执；容器的工作区本身必须持久化挂载。
            self.root = Path(cfg.workspace.root).resolve() / ".spiritscout-updates"
        self.launch: dict | None = None
        self.lock = asyncio.Lock()
        self.writes = 0
        self.maintenance = False
        self.task: asyncio.Task | None = None
        self.cache = {}
        try:
            self.cache = read_json(self.root / "release.json")
            if not isinstance(self.cache.get("checked_at", 0), (int, float)):
                self.cache = {}
            cached_release = self.cache.get("release")
            if cached_release and (
                not isinstance(cached_release, dict)
                or version(cached_release.get("tag", "")) is None
            ):
                self.cache = {}
        except (OSError, ValueError):
            pass

    def job(self):
        try:
            return read_json(self.root / "job.json")
        except (OSError, ValueError):
            return {"phase": "error", "message": "更新状态不可读，请检查本机更新目录"}

    def busy(self):
        return self.maintenance or self.job().get("phase") in ACTIVE_PHASES | {
            "recovery_required"
        }

    def configure_launch(self, args: list[str], host: str, port: int, config: str):
        self.launch = {
            "args": args,
            "cwd": str(Path.cwd()),
            "host": host,
            "port": port,
            "config": config,
        }

    def bridge(self):
        try:
            value = read_json(self.root / "bridge.json")
            return value if time.time() - value.get("seen_at", 0) < 120 else {}
        except (OSError, ValueError):
            return {}

    async def start(self):
        # 服务重启或更新进程异常退出后不重放更新操作。
        job = self.job()
        if job.get("phase") in ACTIVE_PHASES and job.get("adapter") != "container":
            worker = job.get("worker_pid")
            try:
                if not process_alive(int(worker)):
                    raise ProcessLookupError()
            except (OSError, ValueError, TypeError):
                job.update(
                    phase="recovery_required",
                    message="更新进程已中断，请查看备份和日志后手动恢复",
                )
                write_json(self.root / "job.json", job)
        self.task = asyncio.create_task(self._checks(), name="release-check")

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _checks(self):
        await asyncio.sleep(3)
        while True:
            job = self.job()
            process = getattr(self, "worker", None)
            if (
                process
                and process.poll() is not None
                and job.get("id") == getattr(self, "worker_job", None)
                and job.get("phase") in ACTIVE_PHASES
            ):
                job.update(
                    phase="error"
                    if job.get("phase") == "preparing"
                    else "recovery_required",
                    message="独立更新进程异常退出，请查看本机更新日志",
                )
                write_json(self.root / "job.json", job)
                release_claim(self.root, job["id"])
            if (
                job.get("adapter") == "container"
                and job.get("phase") in ACTIVE_PHASES
                and not self.bridge()
            ):
                job.update(
                    phase="recovery_required",
                    message="宿主更新器失去连接，请在宿主检查更新日志",
                )
                write_json(self.root / "job.json", job)
            if self.job().get("phase") not in ACTIVE_PHASES:
                self.maintenance = False
                self.manager._update_maintenance = self.busy()
            await self.check()
            await asyncio.sleep(2)

    async def _get(self, url, *, headers=None, missing=False):
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            async with client.stream(
                "GET",
                url,
                headers={"User-Agent": "SpiritScout-update-check", **(headers or {})},
            ) as response:
                if response.status_code == 304 or (
                    missing and response.status_code == 404
                ):
                    return response.status_code, None, response.headers
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise ValueError("更新信息超过大小限制")
                return response.status_code, bytes(body), response.headers

    async def check(self, *, force=False):
        async with self.lock:
            now = time.time()
            checked = self.cache.get("checked_at", 0)
            wait = 60 if force else 900 if self.cache.get("error") else 21600
            if now - checked < wait:
                return
            try:
                headers = {"Accept": "application/vnd.github+json"}
                if self.cache.get("etag"):
                    headers["If-None-Match"] = self.cache["etag"]
                status, body, response_headers = await self._get(
                    f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
                    headers=headers,
                    missing=True,
                )
                if status == 304:
                    self.cache.update(checked_at=now, error="")
                elif status == 404:
                    self.cache = {"checked_at": now, "error": "", "release": None}
                else:
                    release = json.loads(body)
                    tag = release["tag_name"]
                    if (
                        release.get("draft")
                        or release.get("prerelease")
                        or version(tag) is None
                    ):
                        raise ValueError("发布版本格式不符合稳定版约定")
                    _, data, _ = await self._get(
                        f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{quote(tag, safe='')}"
                    )
                    obj = json.loads(data)["object"]
                    for _ in range(5):
                        if obj["type"] == "commit":
                            break
                        if obj["type"] != "tag" or not re.fullmatch(
                            r"[0-9a-f]{40}", obj["sha"]
                        ):
                            raise ValueError("发布标签无效")
                        _, data, _ = await self._get(
                            f"https://api.github.com/repos/{REPOSITORY}/git/tags/{obj['sha']}"
                        )
                        obj = json.loads(data)["object"]
                    if obj["type"] != "commit" or not re.fullmatch(
                        r"[0-9a-f]{40}", obj["sha"]
                    ):
                        raise ValueError("不能确定发布提交")
                    commit = obj["sha"]
                    _, changelog, _ = await self._get(
                        f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/CHANGELOG.md",
                        missing=True,
                    )
                    self.cache = {
                        "checked_at": now,
                        "etag": response_headers.get("etag", ""),
                        "error": "",
                        "release": {
                            "tag": tag,
                            "commit": commit,
                            "published_at": release.get("published_at", ""),
                            "url": f"https://github.com/{REPOSITORY}/releases/tag/{quote(tag, safe='')}",
                            "assets": release.get("assets", []),
                            "changelog": changelog.decode("utf-8")
                            if changelog
                            else release.get("body", ""),
                            "changelog_source": "CHANGELOG.md"
                            if changelog
                            else "Release 发布说明",
                        },
                    }
            except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
                # 失败保留上次成功结果，不能将网络失败误报为“已经最新”。
                self.cache.update(
                    checked_at=now, error="暂时无法检查 GitHub 更新，稍后会自动重试"
                )
            try:
                await asyncio.to_thread(
                    write_json, self.root / "release.json", self.cache
                )
            except OSError:
                self.cache["error"] = "更新缓存无法写入，请检查目录权限"

    async def snapshot(self, *, details=False):
        installed = await asyncio.to_thread(installation)
        bridge = self.bridge()
        if installed["kind"] == "container" and bridge:
            installed.update(
                supported=bridge.get("ready", False), reason=bridge.get("reason", "")
            )
        release = self.cache.get("release")
        available = bool(release and version(release["tag"]) > version(__version__))
        reason = installed.get("reason", "")
        if installed["kind"] == "binary" and not os.access(installed["root"], os.W_OK):
            reason = reason or "程序目录不可写，请使用原安装渠道更新"
        if installed["kind"] == "binary" and release and not binary_asset(release):
            reason = "该版本尚未发布匹配当前系统和架构、带 SHA-256 校验值的独立程序"
        if sys.platform not in {"linux", "win32"}:
            reason = reason or "当前系统暂不支持应用内更新"
        if not self.launch and installed["kind"] != "container":
            reason = reason or "当前由外部 ASGI 服务启动，请通过部署工具更新和重启"
        if installed["kind"] != "container" and (
            os.environ.get("INVOCATION_ID") or os.environ.get("SUPERVISOR_ENABLED")
        ):
            reason = reason or "当前服务由进程管理器托管，请由原部署工具更新和重启"
        if not os.access(
            self.root if self.root.exists() else self.root.parent, os.W_OK
        ):
            reason = reason or "安装目录不可写，请由原安装工具或管理员更新"
        if self.cache.get("error"):
            reason = reason or "无法确认最新发布，请恢复网络后重新检查"
        active = sum(
            s.status not in {"completed", "failed", "stopped", "interrupted"}
            or (getattr(s, "task", None) is not None and not s.task.done())
            for s in self.manager.list()
        )
        if active:
            reason = reason or f"还有 {active} 个会话未结束，请结束后更新"
        installed.pop("root", None)
        return {
            "current": __version__,
            "installation": installed,
            "available": available,
            "release": {
                k: v
                for k, v in release.items()
                if k not in {"assets", "changelog"} or (details and k == "changelog")
            }
            if release
            else None,
            "checked_at": self.cache.get("checked_at"),
            "error": self.cache.get("error", ""),
            "can_update": bool(
                available and installed["supported"] and not reason and not self.busy()
            ),
            "reason": reason,
            "job": {
                k: v
                for k, v in self.job().items()
                if k not in {"token", "release", "workspace", "protected", "bridge_id"}
            },
        }

    async def apply(self, tag: str):
        async with self.lock:
            if self.busy() or self.writes:
                raise ValueError("正在更新或有写入操作尚未结束，请稍后重试")
            snapshot = await self.snapshot()
            release = self.cache.get("release")
            if not release or release["tag"] != tag or not snapshot["can_update"]:
                raise ValueError(snapshot["reason"] or "发布信息已变化，请重新检查更新")
            installed = await asyncio.to_thread(installation)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            claim = self.root / "claim"
            job_id = uuid.uuid4().hex
            try:
                with open(claim, "x", opener=lambda p, f: os.open(p, f, 0o600)) as file:
                    file.write(job_id)
            except FileExistsError as exc:
                raise ValueError(
                    "已有更新锁；若上次异常中断，请检查本机更新日志后恢复"
                ) from exc
            self.maintenance = True
            self.manager._update_maintenance = True
            try:
                folder = self.root / "jobs" / job_id
                folder.mkdir(parents=True, mode=0o700)
                token = secrets.token_hex(32)
                if installed["kind"] == "container":
                    write_json(
                        self.root / "job.json",
                        {
                            "id": job_id,
                            "phase": "preparing",
                            "message": "等待宿主更新器执行",
                            "adapter": "container",
                            "bridge_id": self.bridge()["bridge_id"],
                            "token": token,
                            "target": release["tag"],
                            "release": release,
                            "workspace": str(Path(self.cfg.workspace.root).resolve()),
                            "protected": [
                                str(Path(p).resolve())
                                for p in [
                                    self.cfg.models_config_path,
                                    self.cfg.agent_models_path,
                                    self.launch["config"],
                                ]
                            ]
                            if self.launch
                            else [],
                            "created_at": time.time(),
                        },
                    )
                    return {"ok": True, "id": job_id}
                plan = {
                    "id": job_id,
                    "root": str(self.root),
                    "installation": installed,
                    "release": release,
                    "launch": self.launch,
                    "parent_pid": os.getpid(),
                    "python": sys.executable,
                    "token": token,
                    "workspace": str(Path(self.cfg.workspace.root).resolve()),
                    "protected": [
                        str(Path(p).resolve())
                        for p in [
                            self.cfg.models_config_path,
                            self.cfg.workspace.root,
                            self.cfg.agent_models_path,
                            self.launch["config"],
                            str(Path.cwd() / ".env"),
                        ]
                    ],
                    "extras": await asyncio.to_thread(
                        enabled_extras,
                        Path(installed["root"])
                        if installed["kind"] == "source"
                        else None,
                    ),
                    "uv": shutil.which("uv"),
                }
                if installed["kind"] == "binary":
                    plan["asset"] = binary_asset(release)
                write_json(folder / "plan.json", plan)
                job = {
                    "id": job_id,
                    "phase": "preparing",
                    "message": "正在下载并准备更新环境",
                    "token": token,
                    "target": release["tag"],
                    "created_at": time.time(),
                }
                write_json(self.root / "job.json", job)
                if installed["kind"] == "binary":
                    worker = folder / Path(sys.executable).name
                    await asyncio.to_thread(shutil.copy2, sys.executable, worker)
                    command = [
                        str(worker),
                        "--internal-update-worker",
                        str(folder / "plan.json"),
                    ]
                else:
                    command = [
                        sys.executable,
                        "-m",
                        "spiritscout.updates.worker",
                        str(folder / "plan.json"),
                    ]
                options = (
                    {
                        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                        | subprocess.DETACHED_PROCESS
                    }
                    if sys.platform == "win32"
                    else {"start_new_session": True}
                )
                with open(
                    folder / "worker.log",
                    "ab",
                    opener=lambda p, f: os.open(p, f, 0o600),
                ) as output:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=output,
                        **options,
                    )
                # worker 自己先写 PID 再执行；不在父进程覆盖它的后续阶段。
                self.worker = process
                self.worker_job = job_id
                return {"ok": True, "id": job_id}
            except BaseException:
                self.maintenance = False
                self.manager._update_maintenance = False
                job = self.job()
                job.update(
                    phase="error", message="更新进程无法启动，请检查目录权限和日志"
                )
                write_json(self.root / "job.json", job)
                release_claim(self.root, job_id)
                raise

    def health(self, token: str):
        job = self.job()
        if not token or not hmac.compare_digest(token, str(job.get("token", ""))):
            return None
        idle = not self.writes and not any(
            s.status not in {"completed", "failed", "stopped", "interrupted"}
            or (getattr(s, "task", None) is not None and not s.task.done())
            for s in self.manager.list()
        )
        return {
            "version": __version__,
            "pid": os.getpid(),
            "id": job.get("id"),
            "idle": idle,
        }
