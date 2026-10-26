"""可选浏览器容器与控制台共用管理员环境变量。"""
from __future__ import annotations

import base64
import hashlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch


class BrowserCredentialTests(unittest.TestCase):
    def credential(self, environment: dict[str, str]) -> str:
        entrypoint = Path(__file__).parents[1] / "deploy/browser/entrypoint.sh"
        script = entrypoint.read_text().split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(Path, "write_text") as write, patch("os.chmod"):
            exec(compile(script, str(entrypoint), "exec"), {})
        return write.call_args.args[0]

    def test_admin_credentials_override_legacy_password(self) -> None:
        actual = self.credential({"SPIRITSCOUT_ADMIN_USERNAME": " operator ",
                                  "SPIRITSCOUT_ADMIN_PASSWORD": "new-password",
                                  "SPIRITSCOUT_SERVER_PASSWORD": "old-password"})
        digest = base64.b64encode(hashlib.sha1(b"new-password").digest()).decode()
        self.assertEqual(actual, "operator:{SHA}" + digest + "\n")

    def test_legacy_password_keeps_default_username(self) -> None:
        self.assertTrue(self.credential({"SPIRITSCOUT_SERVER_PASSWORD": "old-password"})
                        .startswith("spiritscout:{SHA}"))

    def test_missing_password_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self.credential({})

    def test_invalid_htpasswd_username_is_rejected(self) -> None:
        for name in ("bad:name", "bad\nname"):
            with self.subTest(name=name), self.assertRaises(SystemExit):
                self.credential({"SPIRITSCOUT_ADMIN_USERNAME": name,
                                 "SPIRITSCOUT_ADMIN_PASSWORD": "password",
                                 "SPIRITSCOUT_SERVER_PASSWORD": "legacy"})
