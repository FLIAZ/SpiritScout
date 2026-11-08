"""宿主协议及 Compose 更新顺序；Docker 命令替身不代表真实容器部署验证。"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from spiritscout.updates.source import git
from spiritscout.web.updates import mount_updates

_SPEC = importlib.util.spec_from_file_location(
    "update_container",
    Path(__file__).resolve().parents[1] / "scripts/update-container.py",
)
host = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(host)


class BridgeTests(unittest.TestCase):
    def test_bridge_identity_approval_and_terminal_state(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("spiritscout.updates.controller.containerized", return_value=True),
            patch("spiritscout.web.updates.containerized", return_value=True),
            patch(
                "spiritscout.updates.controller.installation",
                return_value={
                    "kind": "container",
                    "label": "Docker",
                    "supported": False,
                    "reason": "需要宿主助手",
                },
            ),
        ):
            root = Path(temp)
            cfg = SimpleNamespace(
                workspace=SimpleNamespace(root=str(root)),
                models_config_path="/data/models.yaml",
                agent_models_path="/data/agents.yaml",
            )
            manager = SimpleNamespace(list=lambda: [])
            app = FastAPI()
            updater = mount_updates(app, cfg, manager)
            updater.cache = {
                "checked_at": time.time(),
                "release": {"tag": "v99.0.0", "commit": "a" * 40, "assets": []},
            }
            with TestClient(app) as client:
                self.assertFalse(client.get("/api/updates").json()["can_update"])
                bid = "b" * 32
                self.assertEqual(
                    client.post(
                        "/api/updates/host/heartbeat",
                        json={"bridge_id": bid, "ready": True},
                    ).status_code,
                    200,
                )
                self.assertTrue(client.get("/api/updates").json()["can_update"])
                result = client.post("/api/updates/apply", json={"tag": "v99.0.0"})
                self.assertEqual(result.status_code, 200, result.text)
                jobid = result.json()["id"]
                self.assertEqual(client.get("/api/updates/host/job").status_code, 409)
                headers = {"X-SpiritScout-Bridge": bid}
                job = client.get("/api/updates/host/job", headers=headers).json()
                self.assertEqual(job["id"], jobid)
                self.assertTrue(job["token"])
                self.assertNotIn("token", client.get("/api/updates").json()["job"])
                self.assertEqual(
                    client.post(
                        "/api/updates/host/heartbeat",
                        json={"bridge_id": "c" * 32, "ready": True},
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.post(
                        "/api/updates/host/report",
                        headers=headers,
                        json={"id": "d" * 32, "phase": "completed", "message": "done"},
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.post(
                        "/api/updates/host/report",
                        headers=headers,
                        json={"id": jobid, "phase": "completed", "message": "done"},
                    ).status_code,
                    200,
                )
                self.assertFalse((updater.root / "claim").exists())
                self.assertEqual(
                    client.post(
                        "/api/updates/host/report",
                        headers=headers,
                        json={"id": jobid, "phase": "preparing", "message": "replay"},
                    ).status_code,
                    409,
                )


class HostFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        git(self.root, "remote", "add", "origin", host.GIT_URL)
        (self.root / "code").write_text("old")
        self.commit("old")
        self.old = git(self.root, "rev-parse", "HEAD")
        (self.root / "code").write_text("new")
        self.commit("new")
        self.target = git(self.root, "rev-parse", "HEAD")
        git(self.root, "reset", "--keep", self.old)
        args = SimpleNamespace(
            project_dir=str(self.root),
            url="http://127.0.0.1:8000",
            service="spiritscout",
            compose_file=None,
        )
        self.helper = host.HostUpdater(args)
        self.job = {
            "id": "a" * 32,
            "target": "v99.0.0",
            "release": {"commit": self.target},
            "workspace": "/data",
            "token": "private",
        }
        self.info = {
            "Image": "sha256:old",
            "Mounts": [{"Destination": "/data", "Source": "/volumes/data"}],
        }

    def commit(self, text):
        git(self.root, "add", ".")
        git(
            self.root,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            text,
        )

    def flow(self, fail=None):
        commands, reports = [], []

        def command(args, **kwargs):
            commands.append(args)
            if fail and fail(args):
                raise subprocess.CalledProcessError(1, args)
            return ""

        def local_git(root, *args):
            return "" if args[0] == "fetch" else git(root, *args)

        with (
            patch.object(
                self.helper, "inspect", return_value=("fixture-container", self.info)
            ),
            patch.object(self.helper, "command", side_effect=command),
            patch.object(self.helper, "api", return_value={"idle": True}),
            patch.object(
                self.helper, "report", side_effect=lambda j, p, m: reports.append(p)
            ),
            patch.object(self.helper, "wait_health"),
            patch.object(host, "git", side_effect=local_git),
        ):
            result = self.helper.perform(self.job)
        return result, commands, reports

    def test_build_then_stop_backup_recreate(self):
        result, calls, reports = self.flow()
        self.assertTrue(result)
        flat = [" ".join(v) for v in calls]
        self.assertIn(" build ", flat[0])
        self.assertIn(" stop ", flat[1])
        self.assertTrue(
            flat[2].startswith("docker run --rm --network none --volumes-from")
        )
        self.assertIn("sha256:old", calls[2])
        self.assertIn(" up ", flat[3])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.target)
        self.assertEqual(reports[-1], "completed")

    def test_build_failure_restores_source_keeps_original_container(self):
        result, calls, reports = self.flow(lambda args: "build" in args)
        self.assertFalse(result)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.old)
        self.assertFalse(any("stop" in args for args in calls))
        self.assertEqual(reports[-1], "error")

    def test_recreate_failure_never_reopens_old_program_on_possible_new_data(self):
        result, calls, reports = self.flow(lambda args: "up" in args)
        self.assertFalse(result)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), self.target)
        self.assertFalse(any(args[:2] == ["docker", "start"] for args in calls))
        self.assertEqual(reports[-1], "recovery_required")

    def test_live_source_mount_is_refused(self):
        self.info["Mounts"] = [{"Source": str(self.root), "Destination": "/app"}]
        with patch.object(
            self.helper,
            "command",
            side_effect=[
                __import__("json").dumps(
                    {
                        "services": {
                            "spiritscout": {"build": {"context": str(self.root)}}
                        }
                    }
                ),
                "fixture",
                __import__("json").dumps([self.info]),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "实时源码"):
                self.helper.inspect()


if __name__ == "__main__":
    unittest.main()
