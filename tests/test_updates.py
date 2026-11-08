"""更新边界、缓存、安装识别、真实 Git/进程切换；不调用模型或更新本机仓库。"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout import __version__
from spiritscout.updates import bootstrap, source, worker
from spiritscout.updates.controller import Updater, read_json, version, write_json
from spiritscout.web.updates import mount_updates


def commit(root, text):
    (root / "code.txt").write_text(text)
    source.git(root, "add", ".")
    source.git(
        root,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        text,
    )
    return source.git(root, "rev-parse", "HEAD")


def repository(root):
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text(".spiritscout-updates/\nlocal/\n")
    (root / "pyproject.toml").write_text('[project]\nname="spiritscout"\n')
    (root / "src/spiritscout/updates").mkdir(parents=True)
    (root / "src/spiritscout/updates/source.py").write_text("")
    source.git(root, "remote", "add", "origin", source.GIT_URL)
    return commit(root, "old")


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.old = repository(self.root)

    def test_install_source_dirty_and_container_priority(self):
        module = str(self.root / "src/spiritscout/updates/source.py")
        with (
            patch.object(source, "__file__", module),
            patch.object(source, "containerized", return_value=False),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertTrue(source.installation()["supported"])
            (self.root / "untracked").write_text("collaborator work")
            self.assertFalse(source.installation()["supported"])
        with (
            patch.object(source, "containerized", return_value=True),
            patch.object(sys, "frozen", True, create=True),
        ):
            self.assertEqual(source.installation()["kind"], "container")

    def test_package_installer_and_frozen_are_distinct(self):
        with (
            patch.object(source, "containerized", return_value=False),
            patch.object(
                source, "__file__", "/tmp/site-packages/spiritscout/updates/source.py"
            ),
            patch.object(source.shutil, "which", return_value=None),
            patch.object(
                source.importlib.metadata,
                "distribution",
                return_value=SimpleNamespace(read_text=lambda _: "uv\n"),
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(source.installation()["installer"], "uv")
            with patch.object(sys, "frozen", True, create=True):
                self.assertEqual(source.installation()["kind"], "binary")
            with patch.dict(os.environ, {"APPIMAGE": "/tmp/app.AppImage"}):
                self.assertEqual(source.installation()["kind"], "appimage")

    def test_fast_forward_protected_paths_ignored_collisions_and_divergence(self):
        target = commit(self.root, "new")
        source.git(self.root, "reset", "--keep", self.old)
        source.source_preflight(self.root, self.old, target, [str(self.root / "local")])
        with self.assertRaisesRegex(ValueError, "配置/数据"):
            source.source_preflight(
                self.root, self.old, target, [str(self.root / "code.txt")]
            )
        source.git(self.root, "merge", "--ff-only", target)
        (self.root / "local").mkdir()
        (self.root / "local/config").write_text("upstream")
        source.git(self.root, "add", "-f", "local/config")
        source.git(
            self.root,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "collision",
        )
        collision = source.git(self.root, "rev-parse", "HEAD")
        source.git(self.root, "reset", "--keep", target)
        (self.root / "local").mkdir(exist_ok=True)
        (self.root / "local/config").write_text("private")
        with self.assertRaisesRegex(ValueError, "忽略文件"):
            source.source_preflight(self.root, target, collision, [])
        self.assertEqual((self.root / "local/config").read_text(), "private")
        branch = commit(self.root, "local-only")
        with self.assertRaises(ValueError):
            source.source_preflight(self.root, branch, collision, [])

    def test_asset_requires_platform_arch_and_digest(self):
        asset = {
            "name": "spiritscout-linux-arm64",
            "digest": "sha256:" + "a" * 64,
            "browser_download_url": "https://github.com/FLIAZ/SpiritScout/releases/download/v9.0.0/spiritscout-linux-arm64",
        }
        self.assertEqual(
            source.binary_asset({"assets": [asset]}, "linux", "aarch64"), asset
        )
        self.assertIsNone(source.binary_asset({"assets": [asset]}, "linux", "x86_64"))
        self.assertIsNone(
            source.binary_asset(
                {"assets": [dict(asset, digest="")]}, "linux", "aarch64"
            )
        )
        self.assertIsNone(
            source.binary_asset(
                {
                    "assets": [
                        dict(asset, browser_download_url="https://evil.invalid/file")
                    ]
                },
                "linux",
                "aarch64",
            )
        )

    def test_bootstrap_rejects_outside_runtime_and_does_not_redirect_twice(self):
        state = self.root / ".spiritscout-updates"
        with patch.dict(os.environ, {"SPIRITSCOUT_UPDATE_STATE": str(state)}):
            write_json(state / "active.json", {"python": "/bin/python"})
            with self.assertRaises(SystemExit):
                bootstrap.redirect()
            python = state / "envs/fixture/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("")
            write_json(state / "active.json", {"python": str(python)})
            with (
                patch.object(sys, "executable", str(python)),
                patch.object(bootstrap.os, "execve") as execve,
            ):
                bootstrap.redirect()
                execve.assert_not_called()


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 默认夹具模拟直接启动，不继承 CI Runner 的 systemd/Supervisor 标记。
        self.enterContext(patch.dict(os.environ, {
            "INVOCATION_ID": "", "SUPERVISOR_ENABLED": "",
        }))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = SimpleNamespace(list=lambda: [], _update_maintenance=False)
        self.cfg = SimpleNamespace(
            workspace=SimpleNamespace(root=str(self.root / "workspace")),
            models_config_path=str(self.root / "models.yaml"),
            agent_models_path=str(self.root / "agents.yaml"),
        )
        with patch(
            "spiritscout.updates.controller.state_root",
            return_value=self.root / "updates",
        ):
            self.updater = Updater(self.cfg, self.manager)
        self.updater.configure_launch(
            [], "127.0.0.1", 8000, str(self.root / "system.yaml")
        )
        self.tag = "v" + ".".join(
            map(str, (*version(__version__)[:2], version(__version__)[2] + 1))
        )
        self.release = {
            "tag": self.tag,
            "commit": "a" * 40,
            "changelog": "# release",
            "assets": [],
        }
        self.updater.cache = {"release": self.release, "checked_at": time.time()}

    async def test_network_failure_keeps_last_release_and_disables_apply(self):
        self.updater.cache["checked_at"] = 0
        with patch.object(
            self.updater, "_get", side_effect=httpx.ConnectError("offline")
        ):
            await self.updater.check()
        self.assertEqual(self.updater.cache["release"], self.release)
        with patch(
            "spiritscout.updates.controller.installation",
            return_value={"kind": "package", "label": "pip", "supported": True},
        ):
            result = await self.updater.snapshot()
        self.assertTrue(result["available"])
        self.assertFalse(result["can_update"])
        self.assertTrue(result["error"])

    async def test_latest_release_resolves_tag_and_changelog_and_etag(self):
        self.updater.cache = {}
        release = {"tag_name": self.tag, "assets": [], "body": "fallback"}
        replies = [
            (200, json.dumps(release).encode(), {"etag": '"tag"'}),
            (
                200,
                json.dumps({"object": {"type": "tag", "sha": "b" * 40}}).encode(),
                {},
            ),
            (
                200,
                json.dumps({"object": {"type": "commit", "sha": "c" * 40}}).encode(),
                {},
            ),
            (200, b"# exact changelog", {}),
        ]
        with patch.object(
            self.updater, "_get", new=AsyncMock(side_effect=replies)
        ) as get:
            await self.updater.check()
        self.assertIn("c" * 40, get.call_args_list[-1].args[0])
        self.assertEqual(
            self.updater.cache["release"]["changelog"], "# exact changelog"
        )
        self.updater.cache["checked_at"] = 0
        with patch.object(
            self.updater, "_get", new=AsyncMock(return_value=(304, None, {}))
        ) as get:
            await self.updater.check()
        self.assertEqual(get.call_args.kwargs["headers"]["If-None-Match"], '"tag"')

    async def test_apply_blocks_active_and_never_accepts_arbitrary_commands(self):
        self.manager.list = lambda: [SimpleNamespace(status="running")]
        with (
            patch(
                "spiritscout.updates.controller.installation",
                return_value={"kind": "package", "label": "pip", "supported": True},
            ),
            patch("spiritscout.updates.controller.subprocess.Popen") as popen,
        ):
            with self.assertRaisesRegex(ValueError, "会话"):
                await self.updater.apply(self.tag)
            popen.assert_not_called()
        self.assertFalse(self.updater.busy())

    async def test_managed_service_rejects_in_app_update(self):
        for key in ("INVOCATION_ID", "SUPERVISOR_ENABLED"):
            with (
                self.subTest(manager=key),
                patch.dict(os.environ, {key: "fixture"}),
                patch("spiritscout.updates.controller.installation",
                      return_value={"kind": "package", "label": "pip", "supported": True}),
                patch("spiritscout.updates.controller.subprocess.Popen") as popen,
            ):
                self.assertFalse((await self.updater.snapshot())["can_update"])
                with self.assertRaisesRegex(ValueError, "进程管理器"):
                    await self.updater.apply(self.tag)
                popen.assert_not_called()

    async def test_apply_writes_private_plan_and_does_not_expose_token(self):
        with (
            patch(
                "spiritscout.updates.controller.installation",
                return_value={"kind": "package", "label": "pip", "supported": True},
            ),
            patch("spiritscout.updates.controller.enabled_extras", return_value=[]),
            patch(
                "spiritscout.updates.controller.subprocess.Popen",
                return_value=SimpleNamespace(pid=123),
            ),
        ):
            result = await self.updater.apply(self.tag)
            state = await self.updater.snapshot()
        plan = read_json(self.updater.root / "jobs" / result["id"] / "plan.json")
        self.assertEqual(plan["release"]["commit"], "a" * 40)
        self.assertNotIn("token", state["job"])
        self.assertEqual(
            (self.updater.root / "jobs" / result["id"] / "plan.json").stat().st_mode
            & 0o777,
            0o600,
        )
        self.assertTrue(self.manager._update_maintenance)
        self.assertIsNone(self.updater.health("wrong"))
        self.assertTrue(self.updater.health(plan["token"])["idle"])

    async def test_existing_claim_does_not_overwrite_other_update(self):
        self.updater.root.mkdir()
        (self.updater.root / "claim").write_text("other")
        write_json(
            self.updater.root / "job.json", {"id": "other", "phase": "completed"}
        )
        with patch(
            "spiritscout.updates.controller.installation",
            return_value={"kind": "package", "label": "pip", "supported": True},
        ):
            with self.assertRaisesRegex(ValueError, "更新锁"):
                await self.updater.apply(self.tag)
        self.assertEqual(self.updater.job()["id"], "other")


class RouteTests(unittest.TestCase):
    def test_api_schema_token_and_no_store(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("spiritscout.updates.controller.state_root", return_value=Path(temp)),
        ):
            app = FastAPI()
            updater = mount_updates(
                app, SimpleNamespace(), SimpleNamespace(list=lambda: [])
            )
            with TestClient(app) as client:
                response = client.get("/api/updates")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(client.get("/api/updates/health").status_code, 404)
                self.assertEqual(
                    client.post(
                        "/api/updates/apply",
                        json={"tag": "v9.0.0", "command": "rm -rf /"},
                    ).status_code,
                    422,
                )
                write_json(
                    updater.root / "job.json", {"id": "fixture", "token": "private"}
                )
                value = client.get(
                    "/api/updates/health", headers={"X-SpiritScout-Update": "private"}
                ).json()
                self.assertEqual(value["pid"], os.getpid())


class SecurityIntegrationTests(unittest.TestCase):
    def test_updates_require_auth_origin_and_maintenance_blocks_writes(self):
        from spiritscout.config.settings import SystemConfig, WorkspaceConfig
        from spiritscout.web.app import create_app

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ,
                {
                    "SPIRITSCOUT_SERVER_PASSWORD": "fixture-secret",
                    "SPIRITSCOUT_ADMIN_USERNAME": "",
                    "SPIRITSCOUT_ADMIN_PASSWORD": "",
                },
            ),
            patch(
                "spiritscout.updates.controller.state_root",
                return_value=Path(temp) / "updates",
            ),
        ):
            app = create_app(
                SystemConfig(
                    workspace=WorkspaceConfig(root=str(Path(temp) / "workspace"))
                ),
                [],
            )
            with TestClient(app, base_url="http://127.0.0.1") as client:
                self.assertEqual(client.get("/api/updates").status_code, 401)
                self.assertEqual(client.get("/api/updates/health").status_code, 404)
                auth = ("spiritscout", "fixture-secret")
                self.assertEqual(
                    client.post(
                        "/api/updates/check",
                        auth=auth,
                        headers={"Origin": "https://evil.invalid"},
                    ).status_code,
                    403,
                )
                app.state.updater.maintenance = True
                self.assertEqual(
                    client.post("/api/sessions", auth=auth, json={}).status_code, 503
                )
                self.assertEqual(
                    client.get("/api/sessions", auth=auth).status_code, 200
                )
                app.state.updater.maintenance = False


# 真实本机 HTTP 服务只用于更新进程握手；没有模型、外部网络或用户服务。
SERVER = """import http.server,json,os,sys
portfile,token,version,jobid=sys.argv[1:]
class H(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  if self.headers.get('X-SpiritScout-Update') != token:self.send_error(404);return
  self.send_response(200);self.end_headers();self.wfile.write(json.dumps(dict(version=version,pid=os.getpid(),id=jobid,idle=True)).encode())
 def log_message(self,*args):pass
server=http.server.HTTPServer(('127.0.0.1',0),H)
open(portfile,'w').write(str(server.server_port))
server.serve_forever()
"""


class WorkerIntegrationTests(unittest.TestCase):
    def server(self, folder, token, tag, jobid):
        portfile = folder / ("port-" + str(time.time_ns()))
        process = subprocess.Popen(
            [sys.executable, "-c", SERVER, str(portfile), token, tag, jobid],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.stop, process)
        for _ in range(100):
            if portfile.exists():
                return process, int(portfile.read_text())
            time.sleep(0.02)
        self.fail("fixture server did not start")

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)

    def test_real_fast_forward_backup_restart_and_health(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            old = repository(root)
            target = commit(root, "new")
            source.git(root, "reset", "--keep", old)
            update = root / ".spiritscout-updates"
            folder = update / "jobs/fixture"
            folder.mkdir(parents=True)
            workspace = base / "workspace"
            workspace.mkdir()
            (workspace / "evidence.txt").write_text("keep me")
            token, tag, jobid = "private-token", "9.0.0", "fixture"
            original, port = self.server(base, token, __version__, jobid)
            plan = {
                "id": jobid,
                "root": str(update),
                "workspace": str(workspace),
                "protected": [],
                "installation": {"kind": "source", "root": str(root), "revision": old},
                "release": {"commit": target, "tag": tag},
                "python": sys.executable,
                "token": token,
                "parent_pid": original.pid,
                "launch": {
                    "args": [],
                    "cwd": str(base),
                    "host": "127.0.0.1",
                    "port": port,
                },
            }
            write_json(folder / "plan.json", plan)
            write_json(update / "job.json", {"id": jobid, "token": token})
            started = []

            def start(plan, executable, output):
                new, newport = self.server(base, token, tag, jobid)
                started.append(new)
                plan["launch"]["port"] = newport
                return new

            with (
                patch.object(worker, "prepare_python", return_value=sys.executable),
                patch.object(worker, "command", return_value=""),
                patch.object(worker, "spawn_service", side_effect=start),
            ):
                self.assertEqual(worker.run(folder / "plan.json"), 0)
            original.wait(timeout=3)
            self.assertEqual(source.git(root, "rev-parse", "HEAD"), target)
            self.assertEqual(
                (folder / "backup/workspace/evidence.txt").read_text(), "keep me"
            )
            self.assertEqual((workspace / "evidence.txt").read_text(), "keep me")
            self.assertEqual(read_json(update / "job.json")["phase"], "completed")
            self.assertTrue(started[0].poll() is None)

    def test_switch_failure_restores_code_but_started_version_requires_recovery(self):
        for started_version in (False, True):
            with (
                self.subTest(started_version=started_version),
                tempfile.TemporaryDirectory() as temp,
            ):
                base = Path(temp)
                root = base / "repo"
                old = repository(root)
                target = commit(root, "new")
                source.git(root, "reset", "--keep", old)
                update = root / ".spiritscout-updates"
                folder = update / "jobs/fixture"
                folder.mkdir(parents=True)
                workspace = base / "workspace"
                workspace.mkdir()
                (workspace / "evidence").write_text("private evidence")
                original, port = self.server(base, "token", __version__, "fixture")
                plan = {
                    "id": "fixture",
                    "root": str(update),
                    "workspace": str(workspace),
                    "protected": [],
                    "installation": {
                        "kind": "source",
                        "root": str(root),
                        "revision": old,
                    },
                    "release": {"commit": target, "tag": "9.0.0"},
                    "python": sys.executable,
                    "token": "token",
                    "parent_pid": original.pid,
                    "launch": {
                        "args": [],
                        "cwd": str(base),
                        "host": "127.0.0.1",
                        "port": port,
                    },
                }
                write_json(folder / "plan.json", plan)
                write_json(update / "job.json", {"id": "fixture", "token": "token"})
                created = []

                def start(plan, executable, output):
                    process, port = self.server(
                        base,
                        "token",
                        "9.0.0" if started_version else __version__,
                        "fixture",
                    )
                    created.append(process)
                    plan["launch"]["port"] = port
                    return process

                with (
                    patch.object(worker, "prepare_python", return_value=sys.executable),
                    patch.object(
                        worker,
                        "command",
                        side_effect=None
                        if started_version
                        else ValueError("installer failed"),
                    ),
                    patch.object(worker, "spawn_service", side_effect=start),
                    patch.object(worker, "healthy", return_value=False),
                ):
                    self.assertEqual(worker.run(folder / "plan.json"), 1)
                original.wait(timeout=3)
                self.assertEqual(
                    source.git(root, "rev-parse", "HEAD"),
                    target if started_version else old,
                )
                self.assertEqual(
                    read_json(update / "job.json")["phase"],
                    "recovery_required" if started_version else "error",
                )
                self.assertEqual(
                    (folder / "backup/workspace/evidence").read_text(),
                    "private evidence",
                )
                self.assertEqual(created[0].poll() is None, not started_version)

    def test_preparation_failure_keeps_original_service_and_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "jobs/fixture"
            folder.mkdir(parents=True)
            original, port = self.server(root, "token", __version__, "fixture")
            plan = {
                "id": "fixture",
                "root": str(root),
                "workspace": str(root / "workspace"),
                "installation": {"kind": "package"},
            }
            write_json(folder / "plan.json", plan)
            write_json(root / "job.json", {"id": "fixture"})
            with (
                patch.object(
                    worker, "prepare_python", side_effect=ValueError("download failed")
                ),
                patch.object(worker, "stop_parent") as stop,
            ):
                self.assertEqual(worker.run(folder / "plan.json"), 1)
            stop.assert_not_called()
            self.assertIsNone(original.poll())
            self.assertEqual(read_json(root / "job.json")["phase"], "error")


if __name__ == "__main__":
    unittest.main()
