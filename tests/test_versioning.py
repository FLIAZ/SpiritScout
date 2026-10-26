"""版本单一来源、对外展示与回溯日志的一致性。"""

from __future__ import annotations

import contextlib
import io
import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from spiritscout import __version__
from spiritscout.__main__ import build_parser
from spiritscout.config.settings import SystemConfig, WorkspaceConfig
from spiritscout.tools.mcp_client import _CLIENT_INFO
from spiritscout.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]


class VersioningTests(unittest.TestCase):
    def test_package_metadata_reads_runtime_version(self) -> None:
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertRegex(__version__, r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
        self.assertIn("version", config["project"]["dynamic"])
        self.assertNotIn("version", config["project"])
        self.assertEqual(config["tool"]["setuptools"]["dynamic"]["version"],
                         {"attr": "spiritscout.__version__"})

    def test_cli_and_mcp_share_runtime_version(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as error:
            build_parser().parse_args(["--version"])
        self.assertEqual(error.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"spiritscout {__version__}")
        self.assertEqual(_CLIENT_INFO.version, __version__)

    def test_console_openapi_uses_runtime_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(SystemConfig(workspace=WorkspaceConfig(root=temporary)), [])
            self.assertEqual(app.openapi()["info"]["version"], __version__)
            html = (ROOT / "src/spiritscout/web/static/index.html").read_text()
            self.assertIn('id="ver"', html)
            console = (ROOT / "src/spiritscout/web/static/js/console.js").read_text()
            self.assertIn('fetch("/openapi.json")', console)

    def test_changelog_has_ordered_versions_and_explains_backfill(self) -> None:
        log = (ROOT / "CHANGELOG.md").read_text()
        versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", log, re.MULTILINE)
        self.assertTrue(versions)
        self.assertEqual(versions[0], __version__)
        self.assertEqual(len(versions), len(set(versions)))
        parsed = [tuple(map(int, version.split("."))) for version in versions]
        self.assertEqual(parsed, sorted(parsed, reverse=True))
        self.assertIn("回溯整理", log)
        self.assertIn("并非当时已发布", log)
